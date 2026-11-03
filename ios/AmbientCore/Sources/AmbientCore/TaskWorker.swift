import Foundation

// MARK: - Extraction task worker (phone side of the durable queue, spec-informed by
// the 2026-11-03 storage decision): poll GET /v1/tasks/pending → fetch the
// server-rendered prompt → call the phone's own LLM → POST the raw JSON back.
// Results are PERSISTED before posting and drained idempotently, so an LLM response
// that arrived seconds before a crash is still delivered — the server queue guarantees
// it would otherwise run after its fallback delay. Nothing is ever lost either way.

public struct PendingTask: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var conversationId: String
    public var mode: String
    public var attempts: Int
    public var createdAt: String
    public var fallbackAfter: String
}

public struct PendingTasksPage: Codable, Sendable {
    public var tasks: [PendingTask]
    public var count: Int
    public var promptsHash: String
}

public struct TaskContextResponse: Codable, Sendable {
    public var completed: Bool
    public var reason: String?
    public var taskId: String?
    public var conversationId: String?
    public var mode: String?
    public var profile: String?          // "deep"
    public var messages: [ChatMessage]?
    public var promptsHash: String?
    public var utteranceCount: Int?
}

public struct TaskResultResponse: Codable, Sendable {
    public var ok: Bool
    public var factsCreated: Int
    public var memories: [String]
    public var clarifications: Int
}

public protocol TaskExecuting: Sendable {
    func complete(profileName: String, messages: [ChatMessage]) async throws -> String
}

extension LLMClient: TaskExecuting {}

public struct TaskWorkerConfig: Sendable, Equatable {
    public var pollSeconds: Double = 20
    /// repair round-trips (phone re-calls the model with the server's error detail)
    public var repairTries: Int = 2
    /// failures before we hand the task to the server's fallback LLM
    public var giveUpAfter: Int = 3
    public init() {}
}

/// Server queue surface the worker needs (APIClient conforms; fakes in tests).
public protocol TasksAPI: Sendable {
    func pendingTasks() async throws -> PendingTasksPage
    func taskContext(id: String) async throws -> TaskContextResponse
    func postTaskResult(id: String, rawJson: String) async throws -> TaskResultResponse
    func abandonTask(id: String, reason: String) async throws
    func memoriesSync() async throws -> [SyncedMemory]
}

public actor TaskWorker {
    public typealias Changed = @Sendable () -> Void

    private let api: any TasksAPI
    private let llm: (any TaskExecuting)?
    private let results: LocalTaskResults
    public var config = TaskWorkerConfig()
    /// LLM may be unconfigured (server fallback handles it) — then we stay idle.
    public var isEnabled: Bool = true
    public var onChanged: Changed?

    private var loopTask: Task<Void, Never>?
    private let kick = KickSignal()

    public init(api: any TasksAPI, llm: (any TaskExecuting)?, results: LocalTaskResults) {
        self.api = api; self.llm = llm; self.results = results
    }

    public func start() {
        guard loopTask == nil else { return }
        loopTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let enabled = await self.isEnabled
                let hasLLM = await self.llm != nil
                let active = enabled && hasLLM
                if active {
                    await self.drainPersistedResults()
                    await self.runPendingOnce()
                }
                await self.kick.waitOr(timeoutSeconds: self.config.pollSeconds)
            }
        }
    }

    public func stop() { loopTask?.cancel(); loopTask = nil }

    /// WS heartbeat reported pending_tasks > 0 → check now, not in 20 s.
    public func kickNow() { Task { await self.kick.signal() } }

    /// Deliver persisted-but-unposted results first (crash between LLM-success and
    /// POST must not waste the inference — the whole point of keeping the buffer).
    @discardableResult
    public func drainPersistedResults() async -> Set<String> {
        var delivered: Set<String> = []
        guard let held = try? await results.pending(), !held.isEmpty else { return delivered }
        for heldTask in held {
            do {
                let outcome = try await deliver(taskId: heldTask.taskId, raw: heldTask.raw)
                switch outcome {
                case .accepted:
                    delivered.insert(heldTask.taskId)
                    try await results.markPosted(heldTask.taskId)
                case .obsolete:
                    delivered.insert(heldTask.taskId)
                    try await results.discard(heldTask.taskId)
                case .invalid(let err):
                    // held result is stale/invalid: drop it; fresh attempts continue
                    _ = err
                    try await results.discard(heldTask.taskId)
                }
                continue
            } catch {
                try? await results.bumpAttempts(heldTask.taskId)   // network; retry later
                if heldTask.attempts + 1 >= 8 {               // network gone for good
                    try? await api.abandonTask(id: heldTask.taskId,
                                               reason: "result undeliverable")
                    try? await results.discard(heldTask.taskId)
                }
            }
        }
        return delivered
    }

    public func runPendingOnce() async {
        // ship persisted inferences BEFORE burning battery on new ones
        let delivered = await drainPersistedResults()
        guard let page = try? await api.pendingTasks() else { return }
        for task in page.tasks {
            // server may not have dequeued yet while our POST just landed
            if delivered.contains(task.id) { continue }
            if (try? await results.held(taskId: task.id)) != nil { continue }
            await process(task: task)
        }
    }

    private func process(task: PendingTask) async {
        guard let llm else { return }
        guard let ctx = try? await api.taskContext(id: task.id) else { return }
        if ctx.completed { return }
        guard let messages = ctx.messages, let profile = ctx.profile else { return }

        var convo = messages
        var tries = 0
        while tries <= config.repairTries {
            tries += 1
            let raw: String
            do {
                raw = try await llm.complete(profileName: profile, messages: convo)
            } catch {
                // model unreachable/limits → let the fallback deadline own it
                return
            }
            // persist BEFORE posting: the inference is now crash-safe
            try? await results.hold(taskId: task.id, raw: raw)
            do {
                let outcome = try await deliver(taskId: task.id, raw: raw)
                switch outcome {
                case .accepted:
                    try? await results.discard(task.id)
                    onChanged?()                     // memories changed → resync store
                    return
                case .obsolete:                       // server already ran it
                    try? await results.discard(task.id)
                    return
                case .invalid(let detail):
                    guard tries <= config.repairTries else { break }
                    if task.attempts + tries >= config.giveUpAfter {
                        try? await api.abandonTask(id: task.id,
                                                   reason: String(detail.prefix(200)))
                        try? await results.discard(task.id)
                        return
                    }
                    convo = LLMClient.repairMessages(messages, rawReply: raw,
                                                     serverError: detail)
                }
            } catch {
                return   // network: held result drains later; server fallback is the net
            }
        }
        try? await api.abandonTask(id: task.id, reason: "invalid after repairs")
        try? await results.discard(task.id)
    }

    private enum DeliverOutcome { case accepted, obsolete, invalid(String) }

    /// {"detail":{"error":"invalid_json","detail":"<pydantic>"}} → the pydantic text.
    static func repairDetail(fromBody body: String) -> String {
        guard let data = body.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let detail = obj["detail"] else { return body }
        if let inner = detail as? [String: Any],
           let innerDetail = inner["detail"] as? String { return innerDetail }
        return (detail as? String) ?? body
    }

    private func deliver(taskId: String, raw: String) async throws -> DeliverOutcome {
        do {
            _ = try await api.postTaskResult(id: taskId, rawJson: raw)
            return .accepted
        } catch let failure as APIFailure {
            switch failure.httpStatus {
            case 409: return .obsolete                       // completed already / running
            case 422: return .invalid(failure.message)       // repair detail relayed
            default: throw failure                            // 5xx / network: retry later
            }
        }
    }
}

// MARK: - Persisted unposted results (outbox pattern for LLM inferences)

public struct HeldTaskResult: Sendable, Equatable {
    public var taskId: String
    public var raw: String
    public var attempts: Int
    public var createdAt: Date
}

public actor LocalTaskResults {
    private let db: SQLDatabase

    public init(path: String) throws {
        db = try SQLDatabase(path: path)
        try db.exec("""
        CREATE TABLE IF NOT EXISTS task_results(
            task_id    TEXT PRIMARY KEY,
            raw        TEXT NOT NULL,
            posted     INTEGER NOT NULL DEFAULT 0,
            attempts   INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        """)
    }

    public func hold(taskId: String, raw: String, now: Date = Date()) throws {
        try db.run("""
        INSERT INTO task_results(task_id, raw, posted, attempts, created_at)
        VALUES(?,?,0,0,?)
        ON CONFLICT(task_id) DO UPDATE SET raw=excluded.raw, posted=0
        """, [.text(taskId), .text(raw), .double(now.timeIntervalSince1970)])
    }

    public func pending() throws -> [HeldTaskResult] {
        try db.query("SELECT * FROM task_results WHERE posted=0 ORDER BY created_at")
            .compactMap(Self.result)
    }

    public func held(taskId: String) throws -> HeldTaskResult? {
        try db.query("SELECT * FROM task_results WHERE task_id=? AND posted=0",
                     [.text(taskId)]).first.flatMap(Self.result)
    }

    public func markPosted(_ taskId: String) throws {
        try db.run("UPDATE task_results SET posted=1 WHERE task_id=?", [.text(taskId)])
    }

    public func discard(_ taskId: String) throws {
        try db.run("DELETE FROM task_results WHERE task_id=?", [.text(taskId)])
    }

    public func bumpAttempts(_ taskId: String) throws {
        try db.run("UPDATE task_results SET attempts=attempts+1 WHERE task_id=?",
                   [.text(taskId)])
    }

    /// Give-up hatch: unposted results older than N attempts are abandoned explicitly
    /// so the caller can notify the server (fallback runs) and clear them.
    public func exhausted(maxAttempts: Int) throws -> [HeldTaskResult] {
        try db.query("SELECT * FROM task_results WHERE posted=0 AND attempts>=?",
                     [.int(Int64(maxAttempts))]).compactMap(Self.result)
    }

    private static func result(_ row: SQLRow) -> HeldTaskResult? {
        guard let id = row.text("task_id"), let raw = row.text("raw") else { return nil }
        return HeldTaskResult(taskId: id, raw: raw,
                              attempts: Int(row.int("attempts") ?? 0),
                              createdAt: Date(timeIntervalSince1970:
                                row.double("created_at") ?? 0))
    }
}

/// Simple async signal with timeout (poll loop sleeps, kick wakes it).
actor KickSignal {
    private var continuation: CheckedContinuation<Void, Never>?

    func signal() {
        continuation?.resume()
        continuation = nil
    }

    func waitOr(timeoutSeconds: Double) async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.waitForSignal() }
            group.addTask { try? await Task.sleep(for: .seconds(timeoutSeconds)) }
            await group.next()
            group.cancelAll()
        }
    }

    private func waitForSignal() async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            continuation = c
        }
    }
}
