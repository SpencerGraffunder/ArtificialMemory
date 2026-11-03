import Foundation

// MARK: - Phone-side memory storage (owner decision 2026-11-03: “storage on the phone”)
//
// A full mirrored store, NOT the thin retrieval cache from the original spec §8.3.
// The server's /v1/memories/sync ships every memory (incl. history: the server may
// prune old rows, the phone keeps its own full archive — spec §5.3's "never delete"
// spirit, enforced client-side); the phone runs its own FTS5 search + ranking, the
// same formula shape as server-side (recency half-life, importance, active-over-
// demoted boost), so the always-on loop never waits on the home server for recall.
//
// Embeddings: the local schema reserves the column; device embeddings
// (CoreML/whisper-style) drop in without a migration. Until then search degrades
// exactly like the server's degraded mode: FTS5 + LIKE + recency.

public struct SyncedMemory: Codable, Equatable, Sendable {
    public var id: String
    public var text: String
    public var type: String
    public var importance: Double
    public var confidence: Double
    public var eventStart: String?
    public var eventEnd: String?
    public var validFrom: String
    public var validTo: String?
    public var recordedAt: String
    public var status: String
    public var source: String
    public var speakerName: String?
    public var entities: [String]
    /// ISO instant, set by the sync client (when this row landed on the phone)
    public var syncedAt: String?

    public init(id: String, text: String, type: String, importance: Double,
                confidence: Double, eventStart: String?, eventEnd: String?,
                validFrom: String, validTo: String?, recordedAt: String,
                status: String, source: String, speakerName: String?,
                entities: [String], syncedAt: String? = nil) {
        self.id = id; self.text = text; self.type = type
        self.importance = importance; self.confidence = confidence
        self.eventStart = eventStart; self.eventEnd = eventEnd
        self.validFrom = validFrom; self.validTo = validTo
        self.recordedAt = recordedAt; self.status = status; self.source = source
        self.speakerName = speakerName; self.entities = entities
        self.syncedAt = syncedAt
    }
}

public struct StoredMemory: Sendable, Equatable, Identifiable {
    public var id: String
    public var text: String
    public var type: String
    public var importance: Double
    public var confidence: Double
    public var eventStart: Date?
    public var eventEnd: Date?
    public var validFrom: Date?
    public var validTo: Date?
    public var recordedAt: Date?
    public var status: String
    public var source: String
    public var speakerName: String?
    public var entities: [String]
    /// search score when produced by search() (0 for recent()/memory())
    public var score: Double

    public init(id: String, text: String, type: String, importance: Double,
                confidence: Double, eventStart: Date?, eventEnd: Date?,
                validFrom: Date?, validTo: Date?, recordedAt: Date?, status: String,
                source: String, speakerName: String?, entities: [String],
                score: Double = 0) {
        self.id = id; self.text = text; self.type = type
        self.importance = importance; self.confidence = confidence
        self.eventStart = eventStart; self.eventEnd = eventEnd
        self.validFrom = validFrom; self.validTo = validTo
        self.recordedAt = recordedAt; self.status = status; self.source = source
        self.speakerName = speakerName; self.entities = entities; self.score = score
    }
}

public struct MemorySearchConfig: Sendable, Equatable {
    public var recencyHalfLifeHours: Double = 168   // matches server (spec §5.3)
    public var importanceWeight: Double = 0.4
    public var recencyWeight: Double = 0.3
    /// active memories rank above demoted ones by this factor
    public var activeBoost: Double = 3.0
    public var limit: Int = 50
    public init() {}
}

public actor MemoryStore {
    private let db: SQLDatabase
    public var config = MemorySearchConfig()

    public init(path: String, config: MemorySearchConfig = MemorySearchConfig()) throws {
        self.db = try SQLDatabase(path: path)
        self.config = config
        try db.exec("""
        CREATE TABLE IF NOT EXISTS memories(
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            type TEXT NOT NULL,
            importance REAL NOT NULL,
            confidence REAL NOT NULL,
            event_start INTEGER,
            event_end INTEGER,
            valid_from INTEGER,
            valid_to INTEGER,
            recorded_at INTEGER,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            speaker_name TEXT,
            entities TEXT NOT NULL DEFAULT '[]',
            embedding BLOB,
            synced_at INTEGER NOT NULL
        );
        """)
        try db.exec("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            memory_id UNINDEXED,
            text,
            tokenize = 'porter unicode61'
        );
        """)
        try db.exec("CREATE INDEX IF NOT EXISTS memories_status ON memories(status);")
        try db.exec("CREATE INDEX IF NOT EXISTS memories_recorded ON memories(recorded_at);")
    }

    // MARK: writes

    /// The sync is FULL-state: rows vanish server-side (prune/deleted:true), so the
    /// local mirror swaps atomically. Locally-computed FTS stays consistent because
    /// the FTS table is rebuilt from the same transaction.
    public func replaceAll(with memories: [SyncedMemory], now: Date = Date()) throws {
        let syncedMs = Int64(now.timeIntervalSince1970 * 1000)
        try db.exec("BEGIN IMMEDIATE")
        do {
            try db.run("DELETE FROM memories")
            try db.run("DELETE FROM memories_fts")
            for m in memories {
                try db.run(
                    """
                    INSERT INTO memories(id, text, type, importance, confidence,
                        event_start, event_end, valid_from, valid_to, recorded_at,
                        status, source, speaker_name, entities, synced_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [.text(m.id), .text(m.text), .text(m.type), .double(m.importance),
                     .double(m.confidence), m.eventStart.map { $0.toDate().ms } ?? .null,
                     m.eventEnd.map { $0.toDate().ms } ?? .null, m.validFrom.toDate().ms,
                     m.validTo.map { $0.toDate().ms } ?? .null, m.recordedAt.toDate().ms,
                     .text(m.status), .text(m.source), m.speakerName.map { .text($0) } ?? .null,
                     .text(Self.json(m.entities)), .int(syncedMs)])
                let rid = db.lastInsertRowId
                try db.run("INSERT INTO memories_fts(rowid, memory_id, text) VALUES(?,?,?)",
                           [.int(rid), .text(m.id), .text(m.text)])
            }
            try db.exec("COMMIT")
        } catch {
            try? db.exec("ROLLBACK")
            throw error
        }
    }

    public func count() throws -> Int {
        try db.query("SELECT COUNT(*) AS n FROM memories").first?.int("n") ?? 0
    }

    public func statusCounts() throws -> [String: Int] {
        var out: [String: Int] = [:]
        for r in try db.query("SELECT status, COUNT(*) AS n FROM memories GROUP BY status") {
            if let s = r.string("status") { out[s] = r.int("n") }
        }
        return out
    }

    // MARK: reads

    public func memory(id: String) throws -> StoredMemory? {
        try db.query("SELECT * FROM memories WHERE id = ?", [.text(id)])
            .first.map { Self.map($0, score: 0) }
    }

    public func recent(limit: Int = 20, includeInactive: Bool = false) throws -> [StoredMemory] {
        let guardSQL = includeInactive ? "TRUE" : "status = 'active'"
        return try db.query(
            "SELECT *, 0.0 AS score FROM memories WHERE \(guardSQL) "
            + "ORDER BY recorded_at DESC LIMIT ?", [.int(Int64(limit))])
            .map { Self.map($0, score: 0) }
    }

    /// FTS5 (porter) with the server's degraded-mode ranking mirrored:
    /// score = (1 + importance·iw + recency·rw) · (active ? boost : 1).
    /// LIKE prefix fallback for partial words; recents when nothing matches —
    /// same fallback ladder the server's store.search uses.
    public func search(_ query: String, includeInactive: Bool = false,
                       now: Date = Date()) throws -> [StoredMemory] {
        let limit = config.limit
        let activeGuard = includeInactive ? "TRUE" : "status = 'active'"
        var hits: [(row: StoredMemory, ftsRank: Double)] = []

        let ftsQuery = Self.ftsMatchQuery(query)
        if !ftsQuery.isEmpty {
            for row in try db.query(
                """
                SELECT m.*, f.rank AS fts_rank FROM memories_fts f
                JOIN memories m ON m.rowid = f.rowid
                WHERE memories_fts MATCH ? AND \(activeGuard)
                ORDER BY f.rank LIMIT ?
                """, [.text(ftsQuery), .int(Int64(limit))]) {
                hits.append((Self.map(row, score: 0), row.double("fts_rank") ?? 0))
            }
        }
        if hits.count < limit {
            var clauses: [String] = []
            var params: [SQLValue] = []
            for w in Self.tokens(query) where w.count >= 3 {
                // tokens are alphanumerics-only → no LIKE wildcard escaping needed
                clauses.append("text LIKE ?")
                params.append(.text("%" + w + "%"))
            }
            if !clauses.isEmpty {
                for row in try db.query(
                    "SELECT *, 0.0 AS score FROM memories WHERE \(activeGuard) AND ("
                    + clauses.joined(separator: " OR ") + ") ORDER BY recorded_at DESC LIMIT ?",
                    params + [.int(Int64(limit))]) {
                    let m = Self.map(row, score: 0)
                    if !hits.contains(where: { $0.row.id == m.id }) {
                        hits.append((m, -1_000_000))   // LIKE matches rank below FTS hits
                    }
                }
            }
        }
        if hits.isEmpty {
            return try recent(limit: limit, includeInactive: includeInactive)
        }

        let maxRank = hits.map { -$0.ftsRank }.max() ?? 1
        let denom = max(maxRank, 0.0001)
        var results: [(StoredMemory, Double)] = []
        for hit in hits {
            var m = hit.row
            let rank = hit.ftsRank
            let normalized = max(0, min(1, maxRank > 0 ? (-rank) / denom : 0))
            let ageH = m.recordedAt.map {
                max(0, now.timeIntervalSince($0) / 3600)
            } ?? 87_600
            let recency = pow(0.5, ageH / max(config.recencyHalfLifeHours, 0.01))
            var s = normalized * 0.3
            s += m.importance * config.importanceWeight
            s += recency * config.recencyWeight
            if m.status == "active" { s *= config.activeBoost }
            m.score = s
            results.append((m, s))
        }
        return Array(results.sorted { lhs, rhs in
            if lhs.1 != rhs.1 { return lhs.1 > rhs.1 }
            return (lhs.0.recordedAt ?? .distantPast) > (rhs.0.recordedAt ?? .distantPast)
        }.prefix(limit)).map(\.0)
    }

    // MARK: helpers

    static func json(_ v: [String]) -> String {
        (try? String(data: JSONEncoder().encode(v), encoding: .utf8)) ?? "[]"
    }

    static func tokens(_ text: String) -> [String] {
        text.lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { $0.count >= 3 }
    }

    /// FTS5 MATCH string: quoted terms ORed (no syntax hazards from user text).
    static func ftsMatchQuery(_ query: String) -> String {
        tokens(query).map { "\"\(replacingQuotes($0))\"" }.joined(separator: " OR ")
    }

    private static func replacingQuotes(_ s: String) -> String {
        s.replacingOccurrences(of: "\"", with: "")
    }

    static func map(_ row: SQLRow, score: Double) -> StoredMemory {
        StoredMemory(
            id: row.string("id") ?? "",
            text: row.string("text") ?? "",
            type: row.string("type") ?? "other",
            importance: row.double("importance") ?? 0,
            confidence: row.double("confidence") ?? 0,
            eventStart: row.int("event_start").map { Date(ms: Int64($0)) },
            eventEnd: row.int("event_end").map { Date(ms: Int64($0)) },
            validFrom: row.int("valid_from").map { Date(ms: Int64($0)) },
            validTo: row.int("valid_to").map { Date(ms: Int64($0)) },
            recordedAt: row.int("recorded_at").map { Date(ms: Int64($0)) },
            status: row.string("status") ?? "active",
            source: row.string("source") ?? "extraction",
            speakerName: row.string("speaker_name"),
            entities: (row.string("entities").flatMap {
                (try? JSONDecoder().decode([String].self, from: Data($0.utf8)))
            }) ?? [],
            score: score)
    }
}
