import XCTest
@testable import AmbientCore

// Parity suite: eval/export_scoring_fixtures.py computes these expected values with
// the REAL server scoring/gating/echo functions. If a Swift number drifts from the
// server's, this fails — that is the guarantee that "phone-side scoring" means the
// SAME tuned engine running closer to the user.

private struct ParityFile: Decodable {
    struct Case: Decodable {
        enum CodingKeys: String, CodingKey {
            case kind, name, features, weights, memory_type, delta_h, about_speaker
            case has_date, age_h, half_life_h, present, entity, a, b, count
            case window_sim, utt_sim, memory_text, window_text, scores, cooldown_ids
            case active_cards, threshold, cooldown_s, max_cards, expected
        }
        var kind: String
        var name: String
        var features: [String: Double]?
        var weights: [String: Double]?
        var memory_type: String?
        var delta_h: Double?
        var about_speaker: Bool?
        var has_date: Bool?
        var age_h: Double?
        var half_life_h: Double?
        var present: Bool?
        var entity: Bool?
        var a: [String]?
        var b: [String]?
        var count: Int?
        var window_sim: Double?
        var utt_sim: Double?
        var memory_text: String?
        var window_text: String?
        var scores: [GateScore]?
        var cooldown_ids: [String: Double]?
        var active_cards: Int?
        var threshold: Double?
        var cooldown_s: Double?
        var max_cards: Int?
        var expected_double: Double?
        var expected_gate: [GateExpected]?
    }
    struct GateScore: Decodable { var memory_id: String; var score: Double }
    struct GateExpected: Decodable { var memory_id: String; var allowed: Bool
                                     var reason: String }
    var cases: [Case]
}

/// The fixture JSON uses "expected" polymorphically (number | gate array); custom decode.
private extension ParityFile.Case {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(String.self, forKey: .kind)
        name = try c.decode(String.self, forKey: .name)
        features = try c.decodeIfPresent([String: Double].self, forKey: .features)
        weights = try c.decodeIfPresent([String: Double].self, forKey: .weights)
        memory_type = try c.decodeIfPresent(String.self, forKey: .memory_type)
        delta_h = try c.decodeIfPresent(Double.self, forKey: .delta_h)
        about_speaker = try c.decodeIfPresent(Bool.self, forKey: .about_speaker)
        has_date = try c.decodeIfPresent(Bool.self, forKey: .has_date)
        age_h = try c.decodeIfPresent(Double.self, forKey: .age_h)
        half_life_h = try c.decodeIfPresent(Double.self, forKey: .half_life_h)
        present = try c.decodeIfPresent(Bool.self, forKey: .present)
        entity = try c.decodeIfPresent(Bool.self, forKey: .entity)
        a = try c.decodeIfPresent([String].self, forKey: .a)
        b = try c.decodeIfPresent([String].self, forKey: .b)
        count = try c.decodeIfPresent(Int.self, forKey: .count)
        window_sim = try c.decodeIfPresent(Double.self, forKey: .window_sim)
        utt_sim = try c.decodeIfPresent(Double.self, forKey: .utt_sim)
        memory_text = try c.decodeIfPresent(String.self, forKey: .memory_text)
        window_text = try c.decodeIfPresent(String.self, forKey: .window_text)
        scores = try c.decodeIfPresent([ParityFile.GateScore].self, forKey: .scores)
        cooldown_ids = try c.decodeIfPresent([String: Double].self, forKey: .cooldown_ids)
        active_cards = try c.decodeIfPresent(Int.self, forKey: .active_cards)
        threshold = try c.decodeIfPresent(Double.self, forKey: .threshold)
        cooldown_s = try c.decodeIfPresent(Double.self, forKey: .cooldown_s)
        max_cards = try c.decodeIfPresent(Int.self, forKey: .max_cards)
        if let d = try? c.decode(Double.self, forKey: .expected) {
            expected_double = d
        } else {
            expected_double = nil
        }
        if let g = try? c.decode([ParityFile.GateExpected].self, forKey: .expected) {
            expected_gate = g
        } else {
            expected_gate = nil
        }
    }
}

final class ScoringParityTests: XCTestCase {
    static let now = Date(timeIntervalSince1970: 1_793_697_600)   // 2026-11-03T12:00Z

    fileprivate static func loadCases() throws -> [ParityFile.Case] {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/scoring_parity.json")
        let data = try Data(contentsOf: url)
        // snake_case wire: decode raw dict style (no key conversion needed — keys match)
        return try JSONDecoder().decode(ParityFile.self, from: data).cases
    }

    static func weights(_ dict: [String: Double]?) -> RelevanceWeights {
        var w = RelevanceWeights()
        guard let dict else { return w }
        w.semantic = dict["semantic"] ?? w.semantic
        w.entityOverlap = dict["entity_overlap"] ?? w.entityOverlap
        w.actionability = dict["actionability"] ?? w.actionability
        w.importance = dict["importance"] ?? w.importance
        w.recencyDecay = dict["recency_decay"] ?? w.recencyDecay
        w.speakerMatch = dict["speaker_match"] ?? w.speakerMatch
        w.recentlyShown = dict["recently_shown"] ?? w.recentlyShown
        w.dismissedBefore = dict["dismissed_before"] ?? w.dismissedBefore
        return w
    }

    static func features(_ dict: [String: Double]) -> ScoreFeatures {
        var f = ScoreFeatures()
        f.semantic = dict["semantic"] ?? f.semantic
        f.entityOverlap = dict["entity_overlap"] ?? f.entityOverlap
        f.actionability = dict["actionability"] ?? f.actionability
        f.importance = dict["importance"] ?? f.importance
        f.recencyDecay = dict["recency_decay"] ?? f.recencyDecay
        f.speakerMatch = dict["speaker_match"] ?? f.speakerMatch
        f.recentlyShown = dict["recently_shown"] ?? f.recentlyShown
        f.dismissedBefore = dict["dismissed_before"] ?? f.dismissedBefore
        return f
    }

    func testParityWithServerEngine() throws {
        let cases = try Self.loadCases()
        XCTAssertEqual(cases.count, 43, "fixture freshness — regenerate the export")
        for c in cases {
            switch c.kind {
            case "score":
                let got = RelevanceMath.computeScore(Self.features(c.features ?? [:]),
                                                     Self.weights(c.weights))
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "score parity [\(c.name)]")
            case "actionability":
                let event = c.delta_h.map { Self.now.addingTimeInterval($0 * 3600) }
                let got = RelevanceMath.actionability(
                    type: c.memory_type ?? "other", eventStart: event, now: Self.now,
                    horizonDays: 14, aboutCurrentSpeaker: c.about_speaker ?? false,
                    hasDate: c.has_date ?? false)
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "actionability parity [\(c.name)]")
            case "recency":
                let rec = Self.now.addingTimeInterval(-(c.age_h ?? 0) * 3600)
                let got = RelevanceMath.recencyDecay(
                    recordedAt: rec, now: Self.now,
                    halfLifeHours: c.half_life_h ?? 168)
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "recency parity [\(c.name)]")
            case "speaker":
                let got = RelevanceMath.speakerMatch(memorySpeakerPresent: c.present ?? false,
                                                     memorySharesEntityWithSpeaker: c.entity ?? false)
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "speaker parity [\(c.name)]")
            case "jaccard":
                let got = RelevanceMath.jaccard(Set(c.a ?? []), Set(c.b ?? []))
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "jaccard parity [\(c.name)]")
            case "dismissed":
                let got = RelevanceMath.dismissedScore(dismissCount: c.count ?? 0)
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "dismissed parity [\(c.name)]")
            case "semantic":
                let got = RelevanceMath.semanticFromSims(c.window_sim, c.utt_sim)
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "semantic parity [\(c.name)]")
            case "echo":
                let got = RelevanceMath.echoCoverage(
                    memoryText: c.memory_text ?? "", windowText: c.window_text ?? "")
                XCTAssertEqual(got, c.expected_double ?? .nan, accuracy: 1e-12,
                               "echo parity [\(c.name)] — tokenizer drift!")
            case "gate":
                var cfg = RelevanceConfig()
                cfg.gateThreshold = c.threshold ?? 0.55
                cfg.cooldownSeconds = c.cooldown_s ?? 600
                cfg.maxCards = c.max_cards ?? 3
                let lastShown = (c.cooldown_ids ?? [:]).mapValues {
                    Self.now.addingTimeInterval(-$0)
                }
                let decisions = RelevanceMath.gate(
                    (c.scores ?? []).map { (memoryId: $0.memory_id, score: $0.score) },
                    now: Self.now, lastShown: lastShown,
                    activeCardCount: c.active_cards ?? 0, cfg: cfg)
                let expected = c.expected_gate ?? []
                XCTAssertEqual(decisions.count, expected.count, "gate count [\(c.name)]")
                for (got, want) in zip(decisions, expected) {
                    XCTAssertEqual(got.memoryId, want.memory_id, "gate id order [\(c.name)]")
                    XCTAssertEqual(got.allowed, want.allowed, "\(want.memory_id) allowed")
                    XCTAssertEqual(got.reason, want.reason, "\(want.memory_id) reason")
                }
            default:
                XCTFail("unknown parity case \(c.kind)")
            }
        }
    }
}

// MARK: - Engine behavior (cooldown, budget, echo suppression, fade, pin)

/// Feed exact candidate features; retrieval is bypassed intentionally — these pin
/// the engine dataflow, while the parity suite pins the math.
private actor StubRetrieval: CandidateRetrieval {
    var next: [CandidateContext] = []
    func set(_ c: [CandidateContext]) { next = c }
    func candidates(windowText: String, latest: EngineUtterance) throws
        -> [CandidateContext] { next }
}

final class PhoneRelevanceEngineTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    private func mem(_ id: String, _ text: String, type: String = "event",
                     importance: Double = 0.8, eventStart: Date? = nil,
                     recorded: Date? = nil, entities: [String] = [],
                     speaker: String? = nil) -> StoredMemory {
        StoredMemory(id: id, text: text, type: type, importance: importance,
                     confidence: 0.9, eventStart: eventStart, eventEnd: nil,
                     validFrom: recorded ?? t0, validTo: nil,
                     recordedAt: recorded ?? t0.addingTimeInterval(-3600),
                     status: "active", source: "extraction", speakerName: speaker,
                     entities: entities, score: 0)
    }

    private func strong(_ m: StoredMemory, semantic: Double = 0.95) -> CandidateContext {
        CandidateContext(semantic: semantic, memory: m)
    }

    fileprivate func makeEngine() -> (PhoneRelevanceEngine, StubRetrieval) {
        let stub = StubRetrieval()
        return (PhoneRelevanceEngine(retrieval: stub), stub)
    }

    func utter(_ text: String, label: String = "Spencer") -> EngineUtterance {
        EngineUtterance(text: text, speakerLabel: label, speakerId: nil, ts: t0)
    }

    func testStrongHitSurfacesCard() async throws {
        let (engine, stub) = makeEngine()
        let m = mem("m1", "Dinner friday at Chili", eventStart: t0.addingTimeInterval(86_400))
        await stub.set([strong(m)])
        let cards = try await engine.onUtterance(utter("we should do dinner friday then"))
        XCTAssertEqual(cards.count, 1)
        XCTAssertEqual(cards[0].memoryId, "m1")
        XCTAssertEqual(cards[0].kind, .relevance)
        XCTAssertEqual(cards[0].origin, .memory)
        let active = await engine.active
        XCTAssertEqual(active.count, 1)
    }

    func testCooldownBlocksImmediateResurface() async throws {
        let (engine, stub) = makeEngine()
        let m = mem("m1", "Dinner friday at Chili",
                    eventStart: t0.addingTimeInterval(86_400))
        await stub.set([strong(m)])
        let first = try await engine.onUtterance(utter("dinner friday"))
        XCTAssertEqual(first.count, 1)
        let second = try await engine.onUtterance(
            EngineUtterance(text: "again about dinner friday", speakerLabel: "X",
                            ts: t0.addingTimeInterval(5)))
        XCTAssertTrue(second.isEmpty, "cooldown (600 s) must hold")
    }

    func testBudgetCapsAtThree() async throws {
        let (engine, stub) = makeEngine()
        let candidates = (1...5).map { i in
            strong(mem("m\(i)", "standalone unique fact number \(i)",
                       eventStart: t0.addingTimeInterval(Double(i) * 86_400)))
        }
        try await stub.set(candidates)
        let cards = try await engine.onUtterance(
            utter("zeta omega prime delta sigma tau gamma beta"))
        XCTAssertEqual(cards.count, 3, "max_cards budget")
    }

    func testEchoSuppressionBlocksRelayCards() async throws {
        let (engine, stub) = makeEngine()
        let m = mem("m1", "Jasmine booked Chili for next Friday dinner",
                    eventStart: t0.addingTimeInterval(3 * 86_400))
        try await stub.set([strong(m)])
        // relaying the memory verbatim → no card (the conversation IS the memory)
        let cards = try await engine.onUtterance(
            utter("yeah jasmine booked chili for next friday dinner"))
        XCTAssertTrue(cards.isEmpty, "echo gate on-device too")
    }

    func testFadeAndPinLifecycle() async throws {
        let (engine, stub) = makeEngine()
        let m = mem("m1", "Dinner friday at Chili",
                    eventStart: t0.addingTimeInterval(86_400))
        try await stub.set([strong(m)])
        let cards = try await engine.onUtterance(utter("dinner friday"))
        let cardId = cards[0].id

        // pinned: survives TTL
        await engine.pin(cardId: cardId)
        let goneWhilePinned = await engine.tick(now: t0.addingTimeInterval(120))
        XCTAssertTrue(goneWhilePinned.isEmpty)

        // unpinned: fades after 45 s topic inactivity
        await engine.remove(cardId: cardId)
        let cards2 = try await engine.onUtterance(
            EngineUtterance(text: "dinner friday once more", speakerLabel: "Y",
                            ts: t0.addingTimeInterval(700)))
        if cards2.isEmpty {
            // cooldown kept it from resurfacing; re-show via fresh cooldown window
            let gone = await engine.tick(now: t0.addingTimeInterval(700 + 46))
            XCTAssertTrue(gone.isEmpty, "no active cards to fade")
        } else {
            let gone = await engine.tick(now: t0.addingTimeInterval(700 + 44))
            XCTAssertTrue(gone.isEmpty, "inside TTL still visible")
            let goneLater = await engine.tick(now: t0.addingTimeInterval(700 + 46))
            XCTAssertEqual(goneLater, [cards2[0].id], "fades past TTL")
        }
    }
}
