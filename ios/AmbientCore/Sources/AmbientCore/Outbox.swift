import Foundation

// MARK: - Exponential backoff (spec §3.4). Deterministic for tests; jitter applied at
// call sites that pass one (avoids thundering herds when the home server restarts).

public struct BackoffConfig: Sendable, Equatable {
    public var base: TimeInterval
    public var max: TimeInterval
    public init(base: TimeInterval = 1, max: TimeInterval = 300) {
        self.base = base; self.max = max
    }

    /// delay after `attempts` consecutive failures: min(base · 2^(attempts-1), max)
    /// — first retry waits `base` (spec §3.4: 1s → 2s → 4s … 5 min cap)
    public func delay(afterAttempts attempts: Int) -> TimeInterval {
        guard attempts > 0 else { return 0 }
        let exp = min(Double(attempts - 1), 40)
        return min(base * pow(2, exp), max)
    }
}

// MARK: - Endpoint failover list (spec §3.4: ordered endpoints, remember last good)

public actor EndpointRouter {
    public private(set) var endpoints: [URL]
    private var index: Int = 0
    private var lastGood: URL?
    public var onChange: (@Sendable (URL?) -> Void)?

    public init(endpoints: [URL]) {
        precondition(!endpoints.isEmpty, "at least one endpoint is required")
        self.endpoints = endpoints
    }

    /// The endpoint to try now: last known good, else the current rotation slot.
    public func current() -> URL { lastGood ?? endpoints[index] }

    public func markSuccess(_ url: URL) {
        lastGood = url
        if let i = endpoints.firstIndex(of: url) { index = i }
        onChange?(url)
    }

    /// Rotate to the next endpoint after `url` (wraps). Returns the new current.
    @discardableResult
    public func markFailure(_ url: URL) -> URL {
        if lastGood == url { lastGood = nil }
        let i = endpoints.firstIndex(of: url) ?? index
        index = (i + 1) % endpoints.count
        let next = endpoints[index]
        onChange?(next)
        return next
    }

    public func replaceEndpoints(_ urls: [URL]) {
        endpoints = urls.isEmpty ? endpoints : urls
        index = 0
        if let lg = lastGood, !urls.contains(lg) { lastGood = nil }
        onChange?(urls.first)
    }
}

// MARK: - Outbox (spec §3.4): every finalized utterance is written locally first.
// Ordered by insertion (seq), idempotent by event id, per-entry backoff, disk cap with
// oldest-drop. Survives kill/reboot (SQLite WAL); sender drains in order.

public struct OutboxEntry: Sendable, Equatable {
    public var id: String
    public var seq: Int64
    public var event: UtteranceEvent
    public var attempts: Int
    public var nextAttemptAt: Date
    public var createdAt: Date
    public var bytes: Int
}

public struct OutboxConfig: Sendable, Equatable {
    public var maxBytes: Int          // queue cap by disk size (default 2 GB, spec)
    public var batchLimit: Int
    public var backoff: BackoffConfig
    public init(maxBytes: Int = 2 * 1024 * 1024 * 1024, batchLimit: Int = 200,
                backoff: BackoffConfig = BackoffConfig()) {
        self.maxBytes = maxBytes; self.batchLimit = batchLimit; self.backoff = backoff
    }
}

public actor OutboxStore {
    private let db: SQLDatabase
    public var config: OutboxConfig
    /// oldest-drop alerts (spec): id and bytes of every dropped entry
    public private(set) var dropAlerts: [(id: String, bytes: Int)] = []

    public init(path: String, config: OutboxConfig = OutboxConfig()) throws {
        self.config = config
        db = try SQLDatabase(path: path)
        try db.exec("""
        CREATE TABLE IF NOT EXISTS outbox(
            id              TEXT PRIMARY KEY,
            seq             INTEGER NOT NULL,
            event_json      TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL,
            created_at      REAL NOT NULL,
            bytes           INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS outbox_seq ON outbox(seq);
        CREATE INDEX IF NOT EXISTS outbox_due ON outbox(next_attempt_at);
        """)
    }

    /// Append (or supersede by higher `revision`). False = duplicate/older revision.
    @discardableResult
    public func append(_ event: UtteranceEvent, now: Date = Date()) throws -> Bool {
        let json = String(decoding: try AmbientJSON.encoder(event), as: UTF8.self)
        let bytes = json.utf8.count
        if let row = try db.query("SELECT event_json FROM outbox WHERE id=?",
                                  [.text(event.id)]).first {
            // same id: only a real revision replaces the stored payload
            guard event.revision > Self.revisionOf(json: row.text("event_json") ?? "") else {
                return false
            }
            try db.run("UPDATE outbox SET event_json=?, bytes=?, attempts=0, "
                     + "next_attempt_at=? WHERE id=?",
                       [.text(json), .int(Int64(bytes)), .double(now.timeIntervalSince1970),
                        .text(event.id)])
            return true
        }
        let seq = try db.query("SELECT COALESCE(MAX(seq)+1,1) AS s FROM outbox").first?
            .int64("s") ?? 1
        try db.run("INSERT INTO outbox(id,seq,event_json,attempts,next_attempt_at,"
                 + "created_at,bytes) VALUES(?,?,?,?,?,?,?)",
                   [.text(event.id), .int(seq), .text(json), .int(0),
                    .double(now.timeIntervalSince1970), .double(now.timeIntervalSince1970),
                    .int(Int64(bytes))])
        return true
    }

    /// Due entries in strict FIFO order (order preserved so transcripts reassemble in
    /// sequence even after retries of earlier entries).
    public func pendingBatch(limit: Int? = nil, now: Date = Date()) throws -> [OutboxEntry] {
        let n = limit ?? config.batchLimit
        let rows = try db.query(
            "SELECT * FROM outbox WHERE next_attempt_at <= ? ORDER BY seq LIMIT ?",
            [.double(now.timeIntervalSince1970), .int(Int64(n))])
        return try rows.map(Self.entry(row:))
    }

    public func pendingCount() throws -> Int {
        Int(try db.query("SELECT COUNT(*) AS c FROM outbox").first?.int("c") ?? 0)
    }

    public func totalBytes() throws -> Int {
        Int(try db.query("SELECT COALESCE(SUM(bytes),0) AS b FROM outbox").first?
            .int("b") ?? 0)
    }

    /// Sent: remove. Unknown ids ignored (server is the idempotency authority).
    public func markSent(ids: [String]) throws {
        for id in ids { try db.run("DELETE FROM outbox WHERE id=?", [.text(id)]) }
    }

    /// Failed batch: bump attempts, schedule each entry's next attempt via backoff.
    public func scheduleRetry(ids: [String], now: Date = Date()) throws {
        for id in ids {
            guard let row = try db.query("SELECT attempts FROM outbox WHERE id=?",
                                         [.text(id)]).first else { continue }
            let attempts = Int(row.int("attempts") ?? 0) + 1
            let delay = config.backoff.delay(afterAttempts: attempts)
            try db.run("UPDATE outbox SET attempts=?, next_attempt_at=? WHERE id=?",
                       [.int(Int64(attempts)), .double(now.timeIntervalSince1970 + delay),
                        .text(id)])
        }
    }

    /// Queue cap enforcement: drop oldest until under `maxBytes` (spec: 2 GB default,
    //  with alerts). Returns number dropped.
    @discardableResult
    public func enforceCapacity(maxBytes: Int? = nil) throws -> Int {
        let cap = maxBytes ?? config.maxBytes
        var dropped = 0
        while try totalBytes() > cap {
            guard let row = try db.query(
                "SELECT id, bytes FROM outbox ORDER BY seq LIMIT 1").first else { break }
            try db.run("DELETE FROM outbox WHERE id=?", [.text(row.text("id") ?? "")])
            dropAlerts.append((id: row.text("id") ?? "", bytes: Int(row.int("bytes") ?? 0)))
            dropped += 1
        }
        return dropped
    }

    /// Everything queued, in order (tests, export, migration).
    public func allEntries() throws -> [OutboxEntry] {
        let rows = try db.query("SELECT * FROM outbox ORDER BY seq")
        return try rows.map(Self.entry(row:))
    }

    private static func entry(row: SQLRow) throws -> OutboxEntry {
        let json = row.text("event_json") ?? "{}"
        let event = try AmbientJSON.decoder(UtteranceEvent.self,
                                            from: Data(json.utf8))
        return OutboxEntry(id: row.text("id") ?? event.id, seq: row.int64("seq") ?? 0,
                           event: event, attempts: Int(row.int("attempts") ?? 0),
                           nextAttemptAt: Date(timeIntervalSince1970: row.double(
                            "next_attempt_at") ?? 0),
                           createdAt: Date(timeIntervalSince1970: row.double("created_at") ?? 0),
                           bytes: Int(row.int("bytes") ?? 0))
    }

    private static func revisionOf(json: String) -> Int {
        (try? AmbientJSON.decoder(UtteranceEvent.self, from: Data(json.utf8)))?.revision ?? 0
    }
}

// MARK: - Uploader: drains the outbox to the server (batch endpoint for catch-up,
// spec §4.1). Resumes on reachability/foreground via `drainSoon()`; zero data loss
// across kill: entries leave the outbox only after the server confirms their ids.

public protocol IngestTransport: Sendable {
    func send(_ batch: [UtteranceEvent], endpoint: URL) async throws -> IngestBatchResponse
}

public struct HTTPIngestTransport: IngestTransport {
    public let token: String
    public var session: URLSession
    public init(token: String, session: URLSession = .shared) {
        self.token = token; self.session = session
    }

    public func send(_ batch: [UtteranceEvent], endpoint: URL) async throws
        -> IngestBatchResponse {
        var req = URLRequest(url: endpoint.appendingPathComponent("v1/ingest/batch"))
        req.httpMethod = "POST"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        struct Body: Encodable { let utterances: [UtteranceEvent] }
        req.httpBody = try AmbientJSON.encoder(Body(utterances: batch))
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw SQLiteError(message: "no http") }
        guard (200..<300).contains(http.statusCode) else {
            throw SQLiteError(message: "ingest HTTP \(http.statusCode)")
        }
        return try AmbientJSON.decoder(IngestBatchResponse.self, from: data)
    }
}

public struct DrainReport: Sendable, Equatable {
    public var sent: Int
    public var rescheduled: Int
    public var droppedByCapacity: Int
    public var endpoint: URL
    public var endpointRotated: Bool
}

public actor Uploader {
    private let outbox: OutboxStore
    private let transport: any IngestTransport
    private let router: EndpointRouter
    private var loopTask: Task<Void, Never>?
    public var onState: (@Sendable (String) -> Void)?   // "online"/"offline"/"error"

    public init(outbox: OutboxStore, transport: any IngestTransport, router: EndpointRouter) {
        self.outbox = outbox; self.transport = transport; self.router = router
    }

    /// One drain pass: send everything currently due; returns what happened.
    @discardableResult
    public func drainOnce(now: Date = Date()) async throws -> [DrainReport] {
        var reports: [DrainReport] = []
        let dropped = try await outbox.enforceCapacity()
        while true {
            let batch = try await outbox.pendingBatch(now: now)
            if batch.isEmpty { break }
            let endpoint = await router.current()
            do {
                let resp = try await transport.send(batch.map(\.event), endpoint: endpoint)
                // server confirms ids (accepted ∪ duplicate = durably stored)
                let confirmed = Set(resp.acceptedIds).union(resp.duplicateIds)
                try await outbox.markSent(ids: Array(confirmed))
                let unconfirmed = batch.map(\.id).filter { !confirmed.contains($0) }
                if !unconfirmed.isEmpty { try await outbox.scheduleRetry(ids: unconfirmed, now: now) }
                await router.markSuccess(endpoint)
                reports.append(DrainReport(sent: confirmed.count,
                                           rescheduled: unconfirmed.count,
                                           droppedByCapacity: dropped,
                                           endpoint: endpoint, endpointRotated: false))
                onState?("online")
                if confirmed.isEmpty && unconfirmed.isEmpty { break }  // avoid spin
            } catch {
                try await outbox.scheduleRetry(ids: batch.map(\.id), now: now)
                let next = await router.markFailure(endpoint)
                reports.append(DrainReport(sent: 0, rescheduled: batch.count,
                                           droppedByCapacity: dropped,
                                           endpoint: next, endpointRotated: true))
                onState?("offline")
                break  // endpoint unhealthy: retry later, don't rotate per-entry
            }
        }
        return reports
    }

    /// Continuous drain with idle sleep; `stop()` on background transitions.
    public func start(idleSeconds: TimeInterval = 5) {
        guard loopTask == nil else { return }
        loopTask = Task { [weak self] in
            while !Task.isCancelled {
                _ = try? await self?.drainOnce()
                try? await Task.sleep(for: .seconds(idleSeconds))
            }
        }
    }

    public func stop() { loopTask?.cancel(); loopTask = nil }
}
