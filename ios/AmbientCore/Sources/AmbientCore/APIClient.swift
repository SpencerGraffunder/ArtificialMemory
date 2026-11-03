import Foundation

// MARK: - REST client for the server API (spec §4.1). Token in a bearer header; base URL
// resolved through EndpointRouter so failover applies to REST and WS alike.

public protocol APIError: Error { var httpStatus: Int? { get } }

public struct APIFailure: Error, CustomStringConvertible, LocalizedError {
    public let message: String
    public let httpStatus: Int?
    public init(_ message: String, status: Int? = nil) { self.message = message; httpStatus = status }
    public var description: String { message }
    public var errorDescription: String? { message }
}

public protocol APIRecording: Sendable {
    func execute(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public struct URLSessionAPIRecording: APIRecording {
    public var session: URLSession
    public init(session: URLSession = .shared) { self.session = session }
    public func execute(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, resp) = try await session.data(for: request)
        guard let http = resp as? HTTPURLResponse else {
            throw APIFailure("no HTTP response")
        }
        return (data, http)
    }
}

public struct APIClient: Sendable {
    public var router: EndpointRouter
    public var token: String
    public var recording: any APIRecording

    public init(router: EndpointRouter, token: String,
                recording: (any APIRecording) = URLSessionAPIRecording()) {
        self.router = router; self.token = token; self.recording = recording
    }

    private func request(_ method: String, _ path: String, query: [String: String] = [:],
                         body: (any Encodable)? = nil) async throws -> Data {
        let base = await router.current()
        var comps = URLComponents(url: base.appendingPathComponent(path),
                                  resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try AmbientJSON.encoder(AnyEncodable(body))
        }
        let (data, http) = try await recording.execute(req)
        guard (200..<300).contains(http.statusCode) else {
            throw APIFailure(String(data: data, encoding: .utf8) ?? "HTTP \(http.statusCode)",
                             status: http.statusCode)
        }
        return data
    }

    // MARK: endpoints (spec §4.1)

    public func health() async throws -> HealthResponse {
        try AmbientJSON.decoder(HealthResponse.self, from: try await request("GET", "v1/health"))
    }

    public struct MemoryPage: Codable, Sendable { public var memories: [Memory]; public var total: Int }

    public func memories(q: String = "", entity: String = "", speaker: String = "",
                         from: String = "", to: String = "") async throws -> MemoryPage {
        var query: [String: String] = [:]
        if !q.isEmpty { query["q"] = q }
        if !entity.isEmpty { query["entity"] = entity }
        if !speaker.isEmpty { query["speaker"] = speaker }
        if !from.isEmpty { query["from"] = from }
        if !to.isEmpty { query["to"] = to }
        return try AmbientJSON.decoder(MemoryPage.self,
                                       from: try await request("GET", "v1/memories", query: query))
    }

    public func memory(id: String) async throws -> Memory {
        try AmbientJSON.decoder(Memory.self,
                                from: try await request("GET", "v1/memories/\(id)"))
    }

    public func deleteMemory(id: String) async throws {
        _ = try await request("DELETE", "v1/memories/\(id)")
    }

    public struct ConversationPage: Codable, Sendable {
        public var conversations: [ConversationSummary]
    }
    public struct ConversationSummary: Codable, Sendable, Identifiable {
        public var id: String
        public var startedAt: String
        public var endedAt: String?
        public var utteranceCount: Int?
        public var extractionStatus: String?
    }
    public func conversations() async throws -> ConversationPage {
        try AmbientJSON.decoder(ConversationPage.self,
                                from: try await request("GET", "v1/conversations"))
    }

    public struct ConversationDetail: Codable, Sendable {
        public var conversation: ConversationSummary
        public var utterances: [TranscriptLine]
    }
    public struct TranscriptLine: Codable, Sendable, Identifiable {
        public var id: String
        public var text: String
        public var speakerLabel: String?
        public var speakerName: String?
        public var startTs: String
        public var isWhisper: Int?
    }
    public func conversation(id: String) async throws -> ConversationDetail {
        try AmbientJSON.decoder(ConversationDetail.self,
                                from: try await request("GET", "v1/conversations/\(id)"))
    }

    public struct SpeakerPage: Codable, Sendable { public var speakers: [Speaker] }
    public func speakers() async throws -> SpeakerPage {
        try AmbientJSON.decoder(SpeakerPage.self,
                                from: try await request("GET", "v1/speakers"))
    }

    public func renameSpeaker(id: String, name: String) async throws {
        struct Body: Encodable { let name: String }
        _ = try await request("PATCH", "v1/speakers/\(id)", body: Body(name: name))
    }

    public func mergeSpeakers(from: String, into: String) async throws {
        struct Body: Encodable { let fromSpeakerId: String; let intoSpeakerId: String }
        _ = try await request("POST", "v1/speakers/merge",
                              body: Body(fromSpeakerId: from, intoSpeakerId: into))
    }

    public struct ClarificationPage: Codable, Sendable { public var clarifications: [Clarification] }
    public struct DigestPage: Codable, Sendable { public var questions: [Clarification] }
    public func clarifications(status: String = "open") async throws -> ClarificationPage {
        try AmbientJSON.decoder(ClarificationPage.self,
            from: try await request("GET", "v1/clarifications", query: ["status": status]))
    }

    public func clarificationDigest() async throws -> DigestPage {
        try AmbientJSON.decoder(DigestPage.self,
                                from: try await request("GET", "v1/clarifications/digest"))
    }

    public func answerClarification(id: String, answer: String) async throws -> JSONValue {
        struct Body: Encodable { let answer: String; let source: String }
        return try AmbientJSON.decoder(JSONValue.self,
            from: try await request("POST", "v1/clarifications/\(id)/answer",
                                    body: Body(answer: answer, source: "text")))
    }

    public struct VocabularyPage: Codable, Sendable { public var vocabulary: [VocabularyEntry] }
    public func vocabulary() async throws -> VocabularyPage {
        try AmbientJSON.decoder(VocabularyPage.self,
                                from: try await request("GET", "v1/vocabulary"))
    }

    public func query(_ q: String) async throws -> QueryResult {
        struct Body: Encodable { let q: String }
        return try AmbientJSON.decoder(QueryResult.self,
                                       from: try await request("POST", "v1/query",
                                                               body: Body(q: q)))
    }
}

/// Type-erasing Encodable wrapper (Swift stdlib name collides; ours is local).
struct AnyEncodable: Encodable {
    private let encodeFunc: (Encoder) throws -> Void
    init(_ wrapped: any Encodable) { encodeFunc = wrapped.encode }
    func encode(to encoder: Encoder) throws { try encodeFunc(encoder) }
}
