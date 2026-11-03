import XCTest
@testable import AmbientCore

// Phone-side storage + durable extraction: MemoryStore mirror, LLMClient wire shape,
// TaskWorker repair/abandon/crash-safety — against scripted fakes (no network).

final class MemoryStoreTests: XCTestCase {
    private func tmp() -> String {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("ms-\(UUID().uuidString)")
            .appendingPathComponent("memories.sqlite").path
    }

    private func synced(_ id: String, _ text: String, type: String = "event",
                        importance: Double = 0.7, status: String = "active",
                        recorded: String = "2026-11-03T10:00:00+00:00",
                        entities: [String] = []) -> SyncedMemory {
        SyncedMemory(id: id, text: text, type: type, importance: importance,
                     confidence: 0.9, eventStart: nil, eventEnd: nil,
                     validFrom: recorded, validTo: nil, recordedAt: recorded,
                     status: status, source: "extraction", speakerName: "Jasmine",
                     entities: entities)
    }

    func testFullSyncAndSearch() async throws {
        let store = try MemoryStore(path: tmp())
        try await store.replaceAll(with: [
            synced("m1", "Dinner friday will be chili", entities: ["Chili"]),
            synced("m2", "Maya adopted two cats"),
            synced("m3", "The garage door code is 4417"),
        ])
        let n = try await store.count()
        XCTAssertEqual(n, 3)

        let hits = try await store.search("what food for dinner")
        XCTAssertEqual(hits.first?.id, "m1", "chili/dinner hit ranks first")

        let prefix = try await store.search("garage")
        XCTAssertEqual(prefix.count, 1)
        let like = try await store.search("gara")       // LIKE substring fallback
        XCTAssertEqual(like.first?.id, "m3")

        // Stopword-only query → falls back to recents, no crash
        let recents = try await store.search("a b c")   // no usable tokens
        XCTAssertEqual(recents.count, 3)
    }

    func testSupersededRowsDemotedButPresent() async throws {
        let store = try MemoryStore(path: tmp())
        try await store.replaceAll(with: [
            synced("old", "Dinner will be sushi", status: "superseded"),
            synced("new", "Dinner will be chili"),
        ])
        let hits = try await store.search("dinner")
        XCTAssertEqual(hits.first?.id, "new", "active outranks superseded")
        let withInactive = try await store.search("dinner", includeInactive: true)
        XCTAssertEqual(withInactive.count, 2)
        let counts = try await store.statusCounts()
        XCTAssertEqual(counts["superseded"], 1)
        let total = try await store.count()
        XCTAssertEqual(total, 2, "history rows kept locally (spec §5.3 never delete)")
    }

    func testResyncReplacesFullStateIncludingDeletes() async throws {
        let store = try MemoryStore(path: tmp())
        try await store.replaceAll(with: [synced("a", "alpha"), synced("b", "beta")])
        try await store.replaceAll(with: [synced("b", "beta v2"), synced("c", "gamma")])
        let total = try await store.count()
        XCTAssertEqual(total, 2, "vanishing rows server-side are dropped locally")
        let b = try await store.memory(id: "b")
        XCTAssertEqual(b?.text, "beta v2")
        let gone = try await store.memory(id: "a")
        XCTAssertNil(gone)
    }

    func testEntitiesAndSpeakerRoundTrip() async throws {
        let store = try MemoryStore(path: tmp())
        try await store.replaceAll(with: [
            synced("m1", "likes espresso", entities: ["Jasmine", "espresso"])])
        let m = try await store.memory(id: "m1")
        XCTAssertEqual(m?.entities, ["Jasmine", "espresso"])
        XCTAssertEqual(m?.speakerName, "Jasmine")
    }
}

final class LLMClientTests: XCTestCase {
    actor FakeRecording: APIRecording {
        var lastRequest: URLRequest?
        var lastBody: Data?
        var reply: (Data, HTTPURLResponse)
        init(_ data: Data, _ resp: HTTPURLResponse) { reply = (data, resp) }
        func execute(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
            lastRequest = request
            lastBody = request.httpBody
            return reply
        }
    }

    private func resp(_ status: Int = 200) -> HTTPURLResponse {
        HTTPURLResponse(url: URL(string: "http://x")!, statusCode: status,
                        httpVersion: nil, headerFields: nil)!
    }
    private let ok = Data(#"{"choices":[{"message":{"content":"{\"facts\":[]}"}}]}"#
        .utf8)

    private func makeConfig() -> PhoneLLMConfig {
        var c = PhoneLLMConfig()
        c.deep = LLMProfileConfig(baseURL: "https://api.example.com/v1",
                                  model: "deep-model", apiKey: "sk-test")
        c.fast = LLMProfileConfig(baseURL: "https://api.example.com",   // bare host
                                  model: "fast-model", apiKey: "")
        return c
    }

    func testRequestShapeAndResponse() async throws {
        let fake = FakeRecording(ok, resp())
        let client = LLMClient(config: makeConfig(), recording: fake)
        let raw = try await client.complete(
            profileName: "deep",
            messages: [ChatMessage(role: "system", content: "s"),
                       ChatMessage(role: "user", content: "u")])
        XCTAssertEqual(raw, #"{"facts":[]}"#)
        let maybeReq = await fake.lastRequest
        let req = try XCTUnwrap(maybeReq)
        XCTAssertEqual(req.url?.absoluteString, "https://api.example.com/v1/chat/completions")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer sk-test")
        let maybeBody = await fake.lastBody
        let body = try XCTUnwrap(maybeBody)
        let obj = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        XCTAssertEqual(obj["model"] as? String, "deep-model")
        let fmt = obj["response_format"] as? [String: Any]
        XCTAssertEqual(fmt?["type"] as? String, "json_object", "JSON mode on the phone too")
        XCTAssertEqual((obj["messages"] as? [[String: Any]])?.count, 2)
    }

    func testBareHostGetsV1Appended() async throws {
        let fake = FakeRecording(ok, resp())
        let client = LLMClient(config: makeConfig(), recording: fake)
        _ = try await client.complete(profileName: "fast",
                                      messages: [ChatMessage(role: "user", content: "x")])
        let url = await fake.lastRequest?.url?.absoluteString
        XCTAssertEqual(url, "https://api.example.com/v1/chat/completions")
    }

    func testHttpErrorsPropagatedWithBody() async throws {
        let fake = FakeRecording(Data("rate limited".utf8), resp(429))
        let client = LLMClient(config: makeConfig(), recording: fake)
        do {
            _ = try await client.complete(profileName: "deep",
                                          messages: [ChatMessage(role: "user",
                                                                 content: "x")])
            XCTFail("expected throw")
        } catch let PhoneLLMError.http(code, body) {
            XCTAssertEqual(code, 429)
            XCTAssertEqual(body, "rate limited")
        }
    }

    func testUnconfiguredProfileThrows() async throws {
        let client = LLMClient(config: PhoneLLMConfig(),
                               recording: FakeRecording(ok, resp()))
        do {
            _ = try await client.complete(profileName: "deep",
                                          messages: [ChatMessage(role: "user",
                                                                 content: "x")])
            XCTFail("expected notConfigured")
        } catch PhoneLLMError.notConfigured(let p) { XCTAssertEqual(p, "deep") }
    }

    func testRepairMessagesMirrorServerContract() {
        let original = [ChatMessage(role: "system", content: "S"),
                        ChatMessage(role: "user", content: "U")]
        let repaired = LLMClient.repairMessages(original, rawReply: "not json {{{",
                                                serverError: "Field required: facts.0.content")
        XCTAssertEqual(repaired.count, 4)
        XCTAssertEqual(repaired[2].role, "assistant")
        XCTAssertEqual(repaired[3].content.contains("ONLY valid JSON"), true)
        XCTAssertEqual(repaired[3].content.contains("facts.0.content"), true)
    }
}

final class TaskWorkerTests: XCTestCase {
    // Scripted TasksAPI: records everything, per-path responses preloaded.
    actor FakeTasksAPI: TasksAPI {
        struct Call: Equatable { let kind: String; let id: String }
        var calls: [Call] = []
        var pendingPage = PendingTasksPage(tasks: [], count: 0, promptsHash: "h")
        var context: [String: TaskContextResponse] = [:]
        var resultOutcome: [String: Result<TaskResultResponse, Error>] = [:]
        var sequences: [String: [Result<TaskResultResponse, Error>]] = [:]
        var syncMemories: [SyncedMemory] = []

        func pendingTasks() throws -> PendingTasksPage {
            calls.append(.init(kind: "pending", id: "-")); return pendingPage
        }
        func taskContext(id: String) throws -> TaskContextResponse {
            calls.append(.init(kind: "context", id: id))
            return context[id]!
        }
        func postTaskResult(id: String, rawJson: String) throws -> TaskResultResponse {
            calls.append(.init(kind: "result:\(rawJson)", id: id))
            if var seq = sequences[id], !seq.isEmpty {
                let first = seq.removeFirst()
                sequences[id] = seq
                switch first {
                case .success(let r): return r
                case .failure(let e): throw e
                }
            }
            switch resultOutcome[id]! {
            case .success(let r): return r
            case .failure(let e): throw e
            }
        }
        func abandonTask(id: String, reason: String) throws {
            calls.append(.init(kind: "abandon:\(reason)", id: id))
        }
        func memoriesSync() throws -> [SyncedMemory] { syncMemories }
    }

    actor FakeLLMExec: TaskExecuting {
        private var messages: [[ChatMessage]] = []
        var replies: [String]
        init(_ replies: [String]) { self.replies = replies }
        func receivedMessages() -> [[ChatMessage]] { messages }
        func complete(profileName: String, messages: [ChatMessage]) throws -> String {
            self.messages.append(messages)
            return replies.isEmpty ? "LAST" : replies.removeFirst()
        }
    }

    private func tmp(_ name: String) -> String {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("tw-\(UUID().uuidString)")
            .appendingPathComponent(name).path
    }

    private func pendTask(_ id: String = "t1", attempts: Int = 0) -> PendingTask {
        PendingTask(id: id, conversationId: "c1", mode: "closing", attempts: attempts,
                    createdAt: "now", fallbackAfter: "later")
    }

    private func ctxResp(_ id: String = "t1") -> TaskContextResponse {
        TaskContextResponse(completed: false, reason: nil, taskId: id,
                            conversationId: "c1", mode: "closing", profile: "deep",
                            messages: [ChatMessage(role: "system", content: "S"),
                                       ChatMessage(role: "user", content: "U")],
                            promptsHash: "h", utteranceCount: 3)
    }

    private func ok(_ n: Int = 2) -> TaskResultResponse {
        TaskResultResponse(ok: true, factsCreated: n, memories: ["m"], clarifications: 0)
    }

    struct API500: Error {}
    struct API422: Error {}

    func makeWorker() async -> (TaskWorker, FakeTasksAPI, FakeLLMExec, LocalTaskResults) {
        let api = FakeTasksAPI()
        let llm = FakeLLMExec(["GOOD1", "GOOD2"])
        let results = try! LocalTaskResults(path: tmp("results.sqlite"))
        return (TaskWorker(api: api, llm: llm, results: results), api, llm, results)
    }

    func testHappyPathExecutesAndReports() async throws {
        let (worker, api, llm, results) = await makeWorker()
        await api.setPending([pendTask()])
        await api.setContext(["t1": ctxResp()])
        await api.setResult("t1", .success(ok()))

        await worker.runPendingOnce()
        let calls = await api.calls
        XCTAssertEqual(calls.map(\.kind), ["pending", "context", "result:GOOD1"])
        let msgs = await llm.receivedMessages()
        XCTAssertEqual(msgs.first?.first?.content, "S", "server-rendered prompt verbatim")
        let held = try await results.pending()
        XCTAssertTrue(held.isEmpty, "posted result cleaned up")
    }

    func test422TriggersRepairThenSuccess() async throws {
        let (worker, api, llm, _) = await makeWorker()
        await api.setPending([pendTask()])
        await api.setContext(["t1": ctxResp()])
        struct Bad422: Error {}
        // first POST fails with the server-relayed pydantic detail; second succeeds
        await api.setResultSequence("t1", [.failure(APIFailure(#"{"detail":{"error":"invalid_json","detail":"Field required: facts.0.content"}}"#, status: 422)),
                                           .success(ok())])
        await worker.runPendingOnce()
        let calls = await api.calls
        let posts = calls.filter { $0.kind.hasPrefix("result") }
        XCTAssertEqual(posts.count, 2, "repaired re-POST")
        let msgs = await llm.receivedMessages()
        XCTAssertEqual(msgs.count, 2)
        XCTAssertEqual(msgs[1].count, 4, "repair conversation grows (system,user,assistant,user)")
        XCTAssertTrue(msgs[1][3].content.contains("ONLY valid JSON"))
    }

    func testGiveUpAbandonsToServer() async throws {
        let (worker, api, _, results) = await makeWorker()
        await api.setPending([pendTask(attempts: 2)])          // near giveUpAfter=3
        await api.setContext(["t1": ctxResp()])
        await api.setResultSequence("t1", [
            .failure(APIFailure(#"{"detail":{"detail":"bad"}}"#, status: 422)),
            .failure(APIFailure(#"{"detail":{"detail":"bad"}}"#, status: 422)),
        ])
        await worker.runPendingOnce()
        let calls = await api.calls
        XCTAssertTrue(calls.contains { $0.kind.hasPrefix("abandon") },
                      "hand off to the server's fallback LLM")
        let held = try await results.pending()
        XCTAssertTrue(held.isEmpty, "abandoned result discarded")
    }

    func test409ObsoleteResultDiscarded() async throws {
        let (worker, api, _, results) = await makeWorker()
        await api.setPending([pendTask()])
        await api.setContext(["t1": ctxResp()])
        await api.setResult("t1", .failure(APIFailure("server already ran it", status: 409)))
        await worker.runPendingOnce()
        let calls = await api.calls
        XCTAssertFalse(calls.contains { $0.kind.hasPrefix("abandon") },
                       "server ran it — not our problem, no abandon")
        let held = try await results.pending()
        XCTAssertTrue(held.isEmpty)
    }

    func testCrashBetweenLLMAndPostIsDeliveredLater() async throws {
        // Simulate: inference persisted, network died mid-POST, worker restarts.
        let (_, api, _, results) = await makeWorker()
        try await results.hold(taskId: "t9", raw: "CACHED-BEFORE-CRASH")
        await api.setResult("t9", .success(ok()))

        // a fresh worker on the same store drains persisted results first
        let fresh = TaskWorker(api: api, llm: nil, results: results)
        await fresh.drainPersistedResults()
        let calls = await api.calls
        XCTAssertEqual(calls.map(\.kind), ["result:CACHED-BEFORE-CRASH"],
                       "inference survives app death — the whole point of the buffer")
        let held = try await results.pending()
        XCTAssertTrue(held.isEmpty)
    }

    func testNetworkFailureKeepsResultHeldAndCountsAttempts() async throws {
        let (worker, api, _, results) = await makeWorker()
        try await results.hold(taskId: "t9", raw: "RAW")
        await api.setResult("t9", .failure(URLError(.timedOut)))
        await worker.drainPersistedResults()
        let held = try await results.pending()
        XCTAssertEqual(held.count, 1, "network hiccup must not burn the inference")
        XCTAssertEqual(held.first?.attempts, 1)
    }

    func testAlreadyHeldTaskSkipsRefetch() async throws {
        let (worker, api, llm, results) = await makeWorker()
        try await results.hold(taskId: "t1", raw: "HELD")
        await api.setPending([pendTask()])
        await api.setResult("t1", .success(ok()))
        await worker.runPendingOnce()
        let calls = await api.calls
        XCTAssertFalse(calls.contains { $0.kind.hasPrefix("context") },
                       "no duplicate inference when we already hold a result")
        XCTAssertEqual(calls.map(\.kind), ["result:HELD", "pending"])
    }
}

// Scripting sugar for the fake
extension TaskWorkerTests.FakeTasksAPI {
    func setPending(_ tasks: [PendingTask]) {
        pendingPage = .init(tasks: tasks, count: tasks.count, promptsHash: "h")
    }
    func setContext(_ map: [String: TaskContextResponse]) { context = map }
    func setResult(_ id: String, _ r: Result<TaskResultResponse, Error>) {
        resultOutcome[id] = r
    }
    func setResultSequence(_ id: String, _ seq: [Result<TaskResultResponse, Error>]) {
        sequences[id] = seq
    }
}
