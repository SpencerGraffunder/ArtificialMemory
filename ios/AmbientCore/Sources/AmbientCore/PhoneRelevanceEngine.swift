import Foundation

// MARK: - On-device live relevance engine (owner decision 2026-11-03: everything
// doable on the phone happens on the phone). Mirrors the server's LiveEngine dataflow
// (spec §5.4) over the local memory mirror: rolling 60 s window → local candidates →
// same feature set → same scoring/gating (RelevanceScoring, parity-pinned) → cards.
// Deviations, deliberate and documented in DECISIONS.md:
//   • semantic feature = normalized local-search relevance (device has no server
//     embedding); an embedding provider can be injected to upgrade it.
//   • LLM judge step stays server-side (it needs the fast-model round trip anyway).

public struct EngineUtterance: Sendable, Equatable {
    public var text: String
    public var speakerLabel: String
    public var speakerId: String?
    public var ts: Date
    public init(text: String, speakerLabel: String, speakerId: String? = nil, ts: Date) {
        self.text = text; self.speakerLabel = speakerLabel
        self.speakerId = speakerId; self.ts = ts
    }
}

public struct CandidateContext: Sendable {
    /// local retrieval relevance 0…1 (semantic feature stand-in; see above)
    public var semantic: Double
    public var memory: StoredMemory
    public init(semantic: Double, memory: StoredMemory) {
        self.semantic = semantic; self.memory = memory
    }
}

public protocol CandidateRetrieval: Sendable {
    func candidates(windowText: String, latest: EngineUtterance) async throws
        -> [CandidateContext]
}

/// Default retrieval: local MemoryStore hybrid search, relevance → 0…1.
public struct StoreRetrieval: CandidateRetrieval {
    let store: MemoryStore
    public init(store: MemoryStore) { self.store = store }

    public func candidates(windowText: String, latest: EngineUtterance) async throws
        -> [CandidateContext] {
        let maxScore = try await store.search(latest.text).first?.score ?? 1
        let ranked = try await store.search(windowText)
        return ranked.map { m in
            CandidateContext(semantic: maxScore > 0 ? min(1, m.score / maxScore) : 0,
                             memory: m)
        }
    }
}

public actor PhoneRelevanceEngine {
    public struct ActiveCard: Sendable, Equatable {
        public var card: Card
        public var shownAt: Date
        public var lastTopicActivity: Date
        public var pinned: Bool
    }

    public private(set) var active: [ActiveCard] = []
    private var window: [EngineUtterance] = []
    private var lastShown: [String: Date] = [:]

    public var config = RelevanceConfig()
    /// speakers → entity names they are "about" (server graph analog, filled from
    /// the memory mirror: entities named identically to a speaker label)
    public var speakerEntityNames: @Sendable (String?) -> Set<String> = { _ in [] }
    public var dismissCount: @Sendable (String) async -> Int = { _ in 0 }
    /// semantic override hook: inject on-device embeddings here later
    public var semanticProvider: (@Sendable (String, StoredMemory) -> Double?)?

    private let retrieval: any CandidateRetrieval

    public init(retrieval: any CandidateRetrieval) { self.retrieval = retrieval }

    public func setConfig(_ c: RelevanceConfig) { config = c }

    /// Actor-safe configuration from outside (closures can't be assigned cross-actor).
    public func configure(
        dismiss: (@Sendable (String) async -> Int)? = nil,
        semantic: (@Sendable (String, StoredMemory) -> Double?)? = nil,
        speakerEnts: (@Sendable (String?) -> Set<String>)? = nil,
        cooldown: [String: Date]? = nil
    ) {
        if let dismiss { dismissCount = dismiss }
        if let semantic { semanticProvider = semantic }
        if let speakerEnts { speakerEntityNames = speakerEnts }
        if let cooldown { primeCooldown(cooldown) }
    }

    /// Seed cooldown after relaunch from card history: recently shown memories
    /// keep their cooldown across app restarts (iOS kills suspended apps).
    public func primeCooldown(_ dates: [String: Date]) {
        for (memoryId, shownAt) in dates where (lastShown[memoryId] ?? .distantPast) < shownAt {
            lastShown[memoryId] = shownAt
        }
    }

    /// One finalized utterance → cards to show/update (empty = nothing worth surfacing).
    public func onUtterance(_ u: EngineUtterance) async throws -> [Card] {
        window.append(u)
        window.removeAll { u.ts.timeIntervalSince($0.ts) > config.windowSeconds }
        let windowText = window.map(\.text).joined(separator: " ")

        let candidates = try await retrieval.candidates(windowText: windowText, latest: u)
        let now = u.ts
        let speakerEnts = speakerEntityNames(u.speakerId ?? u.speakerLabel)
        let windowEnts = Set(MemoryStore.tokens(windowText))  // content-word surface forms

        var scored: [(memoryId: String, score: Double)] = []
        for cand in candidates {
            let m = cand.memory
            // echo gate: conversation is relaying this memory verbatim → skip
            if RelevanceMath.echoCoverage(memoryText: m.text, windowText: windowText)
                >= config.echoSuppress { continue }

            var f = ScoreFeatures()
            f.semantic = semanticProvider?(windowText, m) ?? cand.semantic
            f.entityOverlap = RelevanceMath.jaccard(
                Set(m.entities.flatMap { MemoryStore.tokens($0) }), windowEnts)
            f.actionability = RelevanceMath.actionability(
                type: m.type, eventStart: m.eventStart, now: now,
                horizonDays: config.actionabilityHorizonDays,
                aboutCurrentSpeaker: speakerEnts.contains(where: {
                    m.entities.map { $0.lowercased() }.contains($0.lowercased())
                }),
                hasDate: m.eventStart != nil)
            f.importance = m.importance
            f.recencyDecay = RelevanceMath.recencyDecay(
                recordedAt: m.recordedAt, now: now,
                halfLifeHours: config.recencyHalfLifeHours)
            f.speakerMatch = RelevanceMath.speakerMatch(
                memorySpeakerPresent: (m.speakerName ?? "") == u.speakerLabel,
                memorySharesEntityWithSpeaker: speakerEnts.contains(where: {
                    m.text.lowercased().contains($0.lowercased())
                }))
            f.recentlyShown = lastShown[m.id].map {
                now.timeIntervalSince($0) < config.cooldownSeconds ? 1.0 : 0.0
            } ?? 0
            f.dismissedBefore = RelevanceMath.dismissedScore(
                dismissCount: await dismissCount(m.id))
            scored.append((m.id, RelevanceMath.computeScore(f, config.weights)))
        }
        scored.sort { $0.score > $1.score }

        let decisions = RelevanceMath.gate(scored, now: now, lastShown: lastShown,
                                           activeCardCount: active.count, cfg: config)
        var byId = Dictionary(uniqueKeysWithValues: candidates.map { ($0.memory.id, $0.memory) })
        var fresh: [Card] = []
        for d in decisions where d.allowed {
            guard let mem = byId[d.memoryId] else { continue }
            lastShown[d.memoryId] = now
            let card = Self.buildCard(memory: mem, score: d.score, now: now)
            // replace-in-place (no flashing): same memory replaces its old card slot
            if let i = active.firstIndex(where: { $0.card.memoryId == d.memoryId }) {
                active[i] = .init(card: card, shownAt: now, lastTopicActivity: now,
                                  pinned: active[i].pinned)
            } else {
                active.append(.init(card: card, shownAt: now, lastTopicActivity: now,
                                    pinned: false))
            }
            fresh.append(card)
        }
        // topic activity touches matching live cards (keeps them alive while relevant)
        let windowTokens = Set(MemoryStore.tokens(windowText))
        for i in active.indices {
            if !windowTokens.isDisjoint(with: Set(MemoryStore.tokens(active[i].card.headline))) {
                active[i].lastTopicActivity = now
            }
        }
        return fresh
    }

    /// Fade expired cards → return ids to remove from the UI.
    public func tick(now: Date) -> [String] {
        var gone: [String] = []
        active = active.filter { entry in
            let expire = RelevanceMath.shouldExpire(
                shownAt: entry.shownAt, now: now, pinned: entry.pinned,
                lastTopicActivity: entry.lastTopicActivity, cfg: config)
            if expire { gone.append(entry.card.id) }
            return !expire
        }
        return gone
    }

    public func pin(cardId: String) {
        if let i = active.firstIndex(where: { $0.card.id == cardId }) {
            active[i].pinned = true
        }
    }

    public func remove(cardId: String) {
        active.removeAll { $0.card.id == cardId }
    }

    static func buildCard(memory: StoredMemory, score: Double, now: Date) -> Card {
        let age = memory.recordedAt.map { " · " + Self.humanAge($0, now) } ?? ""
        let who = memory.speakerName.map { "\($0)," } ?? ""
        let chip = [who + "recorded \(Self.shortDate(memory.recordedAt ?? now))",
                    ""].joined().isEmpty ? "memory" : "\(who) recorded\(age)"
        return Card(
            id: "local-" + String(UUID().uuidString.prefix(8)) + "-"
                + String(memory.id.prefix(8)),
            kind: .relevance, memoryId: memory.id,
            headline: memory.text, detail: memory.text,
            sourceChip: chip.trimmingCharacters(in: .whitespaces),
            origin: .memory, url: nil, score: score, priority: false,
            sourceUtteranceIds: [], expiresAt: nil,
            debugFeatures: [:], createdAt: AmbientJSONDate.isoNow())
    }

    static func shortDate(_ d: Date) -> String {
        let f = DateFormatter(); f.dateStyle = .medium; f.timeStyle = .none
        return f.string(from: d)
    }

    static func humanAge(_ d: Date, _ now: Date) -> String {
        let days = Int(now.timeIntervalSince(d) / 86_400)
        switch days {
        case 0: return "today"
        case 1: return "yesterday"
        case ..<7: return "\(days) days ago"
        case ..<30: return "\(days / 7) wk ago"
        default: return "\(days / 30) mo ago"
        }
    }
}
