import Foundation

// MARK: - ArtMem wire protocol (mirrors server `artmem_server/models.py`).
// Dates are transported as ISO-8601 strings (pydantic emits/accepts them) so decoding
// never depends on formatter quirks (`+00:00` vs `Z`). Parse lazily via `.artmemDate`.

public enum ArtMemJSON {
    /// snake_case (wire) ⇄ camelCase (Swift)
    public static func decoder<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return try d.decode(type, from: data)
    }

    public static func encoder(_ value: some Encodable) throws -> Data {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return try e.encode(value)
    }
}

extension String {
    /// ISO8601 with or without fractional seconds, `Z` or `±hh:mm` offset.
    public var artmemDate: Date? {
        if let d = ArtMemJSONDate.fractional.date(from: self)
            ?? ArtMemJSONDate.basic.date(from: self) { return d }
        return ArtMemJSONDate.offsetDate(self)
    }
}

public enum ArtMemJSONDate {
    // ISO8601DateFormatter is not thread-safe by contract; every use is lock-guarded.
    nonisolated(unsafe) static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    nonisolated(unsafe) static let basic: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    /// Last resort for `+00:00`-style suffixes ISO8601DateFormatter rejects.
    static func offsetDate(_ s: String) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssXXXXX"
        return f.date(from: s) ?? {
            f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX"
            return f.date(from: s)
        }()
    }
    public static func isoNow() -> String { fractional.string(from: Date()) }
}

// MARK: - Arbitrary JSON (debug_features, status payloads, structured facts)

public enum JSONValue: Codable, Equatable, Sendable {
    case string(String), number(Double), bool(Bool), null
    case array([JSONValue]), object([String: JSONValue])

    public init(from d: Decoder) throws {
        let c = try d.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let v = try? c.decode(Bool.self) { self = .bool(v); return }
        if let v = try? c.decode(Double.self) { self = .number(v); return }
        if let v = try? c.decode(String.self) { self = .string(v); return }
        if let v = try? c.decode([JSONValue].self) { self = .array(v); return }
        self = .object(try c.decode([String: JSONValue].self))
    }

    public func encode(to e: Encoder) throws {
        var c = e.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
}

// MARK: - Utterance events (spec §3.3)

public struct UtteranceLocation: Codable, Equatable, Sendable {
    public var lat: Double
    public var lon: Double
    public var accuracyM: Double

    public init(lat: Double, lon: Double, accuracyM: Double = 0) {
        self.lat = lat; self.lon = lon; self.accuracyM = accuracyM
    }
}

public struct UtteranceEvent: Codable, Equatable, Sendable {
    public var id: String
    public var conversationHint: String?
    public var revision: Int
    public var startTs: String
    public var endTs: String?
    public var speakerId: String
    public var speakerLabel: String
    public var speakerConfidence: Double
    public var text: String
    public var asrConfidence: Double?
    public var isWhisper: Bool
    public var location: UtteranceLocation?
    public var device: String

    public init(id: String = UUID().uuidString, conversationHint: String? = nil,
                revision: Int = 0, startTs: String = ArtMemJSONDate.isoNow(),
                endTs: String? = nil, speakerId: String, speakerLabel: String,
                speakerConfidence: Double = 0, text: String,
                asrConfidence: Double? = nil, isWhisper: Bool = false,
                location: UtteranceLocation? = nil, device: String = "iphone") {
        self.id = id; self.conversationHint = conversationHint; self.revision = revision
        self.startTs = startTs; self.endTs = endTs; self.speakerId = speakerId
        self.speakerLabel = speakerLabel; self.speakerConfidence = speakerConfidence
        self.text = text; self.asrConfidence = asrConfidence; self.isWhisper = isWhisper
        self.location = location; self.device = device
    }
}

public struct IngestBatchResponse: Codable, Equatable, Sendable {
    public var acceptedIds: [String]
    public var duplicateIds: [String]
    public var serverTime: String
}

// MARK: - Cards (spec §5.4)

public enum CardKind: String, Codable, Sendable { case relevance, lookup, query }
public enum CardOrigin: String, Codable, Sendable { case memory, web }

public struct Card: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var kind: CardKind
    public var memoryId: String?
    public var headline: String
    public var detail: String
    public var sourceChip: String
    public var origin: CardOrigin
    public var url: String?
    public var score: Double
    public var priority: Bool
    public var sourceUtteranceIds: [String]
    public var expiresAt: String?
    public var debugFeatures: [String: JSONValue]
    public var createdAt: String

    public init(id: String, kind: CardKind = .relevance, memoryId: String? = nil,
                headline: String, detail: String = "", sourceChip: String = "memory",
                origin: CardOrigin = .memory, url: String? = nil, score: Double = 0,
                priority: Bool = false, sourceUtteranceIds: [String] = [],
                expiresAt: String? = nil, debugFeatures: [String: JSONValue] = [:],
                createdAt: String = ArtMemJSONDate.isoNow()) {
        self.id = id; self.kind = kind; self.memoryId = memoryId
        self.headline = headline; self.detail = detail; self.sourceChip = sourceChip
        self.origin = origin; self.url = url; self.score = score; self.priority = priority
        self.sourceUtteranceIds = sourceUtteranceIds; self.expiresAt = expiresAt
        self.debugFeatures = debugFeatures; self.createdAt = createdAt
    }
}

public enum FeedbackKind: String, Codable, Sendable {
    case dismiss, thumbUp = "thumb_up", thumbDown = "thumb_down", pin, unpin, expand
}

public struct FeedbackMessage: Codable, Equatable, Sendable {
    public var cardId: String
    public var kind: FeedbackKind
    public var at: String
    public init(cardId: String, kind: FeedbackKind, at: String = ArtMemJSONDate.isoNow()) {
        self.cardId = cardId; self.kind = kind; self.at = at
    }
}

// MARK: - WebSocket envelopes (spec §4.1)

public enum ClientEnvelope: Codable, Sendable {
    case utterance(UtteranceEvent)
    case utteranceRevision(UtteranceEvent)
    case heartbeat
    case feedback(FeedbackMessage)

    enum CodingKeys: String, CodingKey { case type, utterance, feedback, ts }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let type = try c.decode(String.self, forKey: .type)
        let make: (UtteranceEvent) -> ClientEnvelope
        switch type {
        case "utterance": make = ClientEnvelope.utterance
        case "utterance_revision": make = ClientEnvelope.utteranceRevision
        case "heartbeat": self = .heartbeat; return
        case "feedback":
            self = .feedback(try c.decode(FeedbackMessage.self, forKey: .feedback)); return
        default: throw DecodingError.dataCorruptedError(
            forKey: .type, in: c, debugDescription: "unknown envelope \(type)")
        }
        _ = make
        self = type == "utterance"
            ? .utterance(try c.decode(UtteranceEvent.self, forKey: .utterance))
            : .utteranceRevision(try c.decode(UtteranceEvent.self, forKey: .utterance))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(ArtMemJSONDate.isoNow(), forKey: .ts)
        switch self {
        case .utterance(let u):
            try c.encode("utterance", forKey: .type); try c.encode(u, forKey: .utterance)
        case .utteranceRevision(let u):
            try c.encode("utterance_revision", forKey: .type)
            try c.encode(u, forKey: .utterance)
        case .heartbeat:
            try c.encode("heartbeat", forKey: .type)
        case .feedback(let f):
            try c.encode("feedback", forKey: .type); try c.encode(f, forKey: .feedback)
        }
    }
}

public enum ServerEnvelope: Codable, Equatable, Sendable {
    case card(Card)
    case cardUpdate(Card)
    case cardDismiss(cardId: String)
    case status([String: JSONValue])

    enum CodingKeys: String, CodingKey { case type, card, cardId, status, ts }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .type) {
        case "card": self = .card(try c.decode(Card.self, forKey: .card))
        case "card_update": self = .cardUpdate(try c.decode(Card.self, forKey: .card))
        case "card_dismiss":
            self = .cardDismiss(cardId: try c.decode(String.self, forKey: .cardId))
        default:
            self = .status(try c.decodeIfPresent([String: JSONValue].self, forKey: .status) ?? [:])
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .card(let card):
            try c.encode("card", forKey: .type); try c.encode(card, forKey: .card)
        case .cardUpdate(let card):
            try c.encode("card_update", forKey: .type); try c.encode(card, forKey: .card)
        case .cardDismiss(let id):
            try c.encode("card_dismiss", forKey: .type); try c.encode(id, forKey: .cardId)
        case .status(let s):
            try c.encode("status", forKey: .type); try c.encode(s, forKey: .status)
        }
    }
}

// MARK: - Browse/API payloads (server returns ISO strings; keep them as-is)

public struct Memory: Codable, Equatable, Sendable, Identifiable {
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
    public var speakerId: String?
    public var speakerName: String?
    public var conversationId: String?
    public var sourceUtteranceIds: [String]
    public var entities: [JSONValue]
    public var structured: [String: JSONValue]
}

public struct Speaker: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var label: String?
    public var name: String?
    public var isOwner: Int
    public var lastSeen: String?
    public var firstSeen: String?
    public var utteranceCount: Int?
}

public struct Clarification: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var kind: String
    public var question: String
    public var contextJson: String
    public var importance: Double
    public var uncertainty: Double
    public var expectedValue: Double?
    public var status: String
    public var conversationId: String?
    public var relatedSpeakerId: String?
    public var relatedEntityId: String?
    public var relatedMemoryIdsJson: String?
    public var answer: String?
    public var answeredAt: String?
    public var answerSource: String?
    public var remindedAt: String?
    public var createdAt: String
    public var expiresAt: String

    /// Decoded `context.subject` — the "ARTES"/"Speaker-3" this question is about.
    public var subject: String? {
        guard let data = contextJson.data(using: .utf8),
              case .object(let obj) = (try? JSONDecoder().decode(JSONValue.self, from: data)),
              case .string(let s)? = obj["subject"] else { return nil }
        return s
    }
}

public struct VocabularyEntry: Codable, Equatable, Sendable {
    public var term: String
    public var kind: String
    public var definition: String?
    public var entity_id: String?
    public var source: String?
    public var boost: Double?
}

public struct QueryResult: Codable, Equatable, Sendable {
    public var card: Card
    public var memories: [Memory]
    public var answer: String?
}

public struct HealthResponse: Codable, Equatable, Sendable {
    public var ok: Bool
    public var serverTime: String
    public var prompts: [String: JSONValue]?
    public var counts: [String: JSONValue]?
}
