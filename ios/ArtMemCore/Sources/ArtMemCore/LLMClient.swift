import Foundation

// MARK: - Phone-side OpenAI-compatible LLM client (owner decision: the phone talks to
// the LLM backend with its own credentials; the server keeps a key only as fallback).
// The prompt is rendered by the SERVER (GET /v1/tasks/{id}/context) so there is one
// prompt pipeline — this client executes messages, it never composes them.

public struct ChatMessage: Codable, Equatable, Sendable {
    public var role: String
    public var content: String
    public init(role: String, content: String) { self.role = role; self.content = content }
}

public struct LLMProfileConfig: Sendable, Equatable {
    public var baseURL: String      // https://api.openai.com/v1 or http://ollama:11434/v1
    public var model: String
    public var apiKey: String
    public var maxTokens: Int
    public init(baseURL: String = "", model: String = "", apiKey: String = "",
                maxTokens: Int = 2400) {
        self.baseURL = baseURL; self.model = model; self.apiKey = apiKey
        self.maxTokens = maxTokens
    }
    public var isConfigured: Bool {
        !baseURL.trimmingCharacters(in: .whitespaces).isEmpty
        && !model.trimmingCharacters(in: .whitespaces).isEmpty
    }
}

public struct PhoneLLMConfig: Sendable, Equatable {
    public var fast = LLMProfileConfig(maxTokens: 600)
    public var deep = LLMProfileConfig(maxTokens: 2400)
    public var timeoutSeconds: Double = 60
    public init() {}
}

public enum PhoneLLMError: Error, CustomStringConvertible, Equatable {
    case notConfigured(String)
    case http(Int, String)
    case transport(String)
    case malformed(String)
    public var description: String {
        switch self {
        case .notConfigured(let p): return "llm profile '\(p)' not configured"
        case .http(let code, let body): return "llm HTTP \(code): \(body)"
        case .transport(let why): return "llm unreachable: \(why)"
        case .malformed(let why): return "llm response malformed: \(why)"
        }
    }
}

public actor LLMClient {
    public var config: PhoneLLMConfig
    private let recording: any APIRecording        // URLSession (injectable for tests)

    public init(config: PhoneLLMConfig,
                recording: (any APIRecording) = URLSessionAPIRecording()) {
        self.config = config; self.recording = recording
    }

    public func update(_ newConfig: PhoneLLMConfig) { config = newConfig }

    /// One chat completion, JSON mode. Returns raw content (validation happens
    /// server-side via POST /v1/tasks/{id}/result).
    public func complete(profileName: String, messages: [ChatMessage]) async throws
        -> String {
        let profile = profileName == "fast" ? config.fast : config.deep
        guard profile.isConfigured else { throw PhoneLLMError.notConfigured(profileName) }
        var req = URLRequest(url: endpoint(baseURL: profile.baseURL))
        req.httpMethod = "POST"
        req.timeoutInterval = config.timeoutSeconds
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !profile.apiKey.isEmpty {
            req.setValue("Bearer \(profile.apiKey)", forHTTPHeaderField: "Authorization")
        }
        struct Body: Encodable {
            var model: String
            var messages: [ChatMessage]
            var temperature: Double
            var max_tokens: Int
            var response_format: Format
            struct Format: Encodable { var type: String }
        }
        req.httpBody = try ArtMemJSON.encoder(Body(
            model: profile.model, messages: messages, temperature: 0.2,
            max_tokens: profile.maxTokens,
            response_format: .init(type: "json_object")))
        let (data, http) = try await recording.execute(req)
        guard (200..<300).contains(http.statusCode) else {
            throw PhoneLLMError.http(http.statusCode,
                                     String(data: data.prefix(300), encoding: .utf8) ?? "?")
        }
        struct Resp: Decodable {
            struct Choice: Decodable { struct Msg: Decodable { var content: String? }
                                       var message: Msg }
            var choices: [Choice]
        }
        guard let resp = try? JSONDecoder().decode(Resp.self, from: data),
              let content = resp.choices.first?.message.content else {
            throw PhoneLLMError.malformed(String(data: data.prefix(200),
                                                 encoding: .utf8) ?? "?")
        }
        return content
    }

    /// Repair follow-up builder mirroring the server gateway (same wording contract:
    /// "reply again with ONLY valid JSON"). `error` is the server-relayed detail.
    public static func repairMessages(_ original: [ChatMessage], rawReply: String,
                                      serverError: String) -> [ChatMessage] {
        original + [
            ChatMessage(role: "assistant", content: String(rawReply.prefix(2000))),
            ChatMessage(role: "user", content: """
            Your previous reply was not valid JSON for the schema. \
            Error: \(String(serverError.prefix(800)))
            Reply again with ONLY valid JSON.
            """),
        ]
    }

    private func endpoint(baseURL: String) -> URL {
        var s = baseURL
        if s.hasSuffix("/") { s.removeLast() }
        if !s.hasSuffix("/v1") { s += "/v1" }     // tolerate bare host or with /v1
        return URL(string: s + "/chat/completions")!
    }
}
