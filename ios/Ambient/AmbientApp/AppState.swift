import SwiftUI
import AmbientCore
import os

// Central wiring: AmbientCore components + settings + UI-visible state.
// Everything user-tunable lives in Settings (UserDefaults), never hardcoded.

enum NetStatus: String, Sendable { case unknown, offline, online, needsToken }

@MainActor
final class AppState: ObservableObject {
    static let shared = AppState()

    // settings keys (SettingsView writes these; everything reads them here)
    enum Keys {
        static let endpoints = "ambient.endpoints"
        static let token = "ambient.token"
        static let cardBudget = "ambient.cardBudget"
        static let haptics = "ambient.haptics"
        static let whisperRatio = "ambient.whisperRatio"
        static let shareLocation = "ambient.shareLocation"
        static let gaps = "ambient.gaps"
        static let phoneExtraction = "ambient.phoneExtraction"
        static let llmDeepURL = "ambient.llmDeepURL"
        static let llmDeepModel = "ambient.llmDeepModel"
        static let llmDeepKey = "ambient.llmDeepKey"
        static let llmFastURL = "ambient.llmFastURL"
        static let llmFastModel = "ambient.llmFastModel"
        static let llmFastKey = "ambient.llmFastKey"
    }

    @Published var status: NetStatus = .unknown
    @Published var queued = 0
    @Published var listening: ListeningState = .idle
    @Published var cards: [Card] = []
    @Published var toast: String?
    @Published var currentSpeaker = "Owner"

    private(set) var router: EndpointRouter?
    private(set) var outbox: OutboxStore?
    private(set) var uploader: Uploader?
    private(set) var socket: LiveSocket?
    private(set) var api: APIClient?
    private(set) var history: CardHistory?

    private let defaults = UserDefaults.standard
    private var machine = ListeningMachine()
    private var capture: (any AudioSource)?
    private var gate = SpeechGate()
    private var frameTask: Task<Void, Never>?
    private var queueTask: Task<Void, Never>?
    private let log = Logger(subsystem: "dev.graffunder.ambient", category: "app")

    // MARK: settings accessors
    var endpoints: [URL] {
        get {
            let raw = defaults.stringArray(forKey: Keys.endpoints)
                ?? ["http://ambient.local:8378"]
            return raw.compactMap { URL(string: $0) }
        }
        set { defaults.set(newValue.map(\.absoluteString), forKey: Keys.endpoints) }
    }
    var token: String {
        get { defaults.string(forKey: Keys.token) ?? "" }
        set { defaults.set(newValue, forKey: Keys.token) }
    }
    var cardBudget: Int {
        get { max(1, defaults.integer(forKey: Keys.cardBudget)).nonZeroFallback(3) }
        set { defaults.set(newValue, forKey: Keys.cardBudget) }
    }
    var hapticsOn: Bool {
        get { defaults.object(forKey: Keys.haptics) as? Bool ?? true }
        set { defaults.set(newValue, forKey: Keys.haptics) }
    }
    var whisperRatio: Double {
        get { defaults.object(forKey: Keys.whisperRatio) as? Double ?? 0.18 }
        set { defaults.set(newValue, forKey: Keys.whisperRatio) }
    }
    var shareLocation: Bool {
        get { defaults.bool(forKey: Keys.shareLocation) }
        set { defaults.set(newValue, forKey: Keys.shareLocation) }
    }
    var gaps: [Date] {
        get { (defaults.array(forKey: Keys.gaps) as? [Double] ?? []).map { Date(timeIntervalSince1970: $0) } }
        set { defaults.set(newValue.map(\.timeIntervalSince1970), forKey: Keys.gaps) }
    }

    // Phone-side LLM + extraction (owner decision 2026-11-03: do on the phone what
    // costs nothing extra there; the server keeps the in-flight buffering role).
    var phoneExtractionOn: Bool {
        get { defaults.object(forKey: Keys.phoneExtraction) as? Bool ?? true }
        set { defaults.set(newValue, forKey: Keys.phoneExtraction) }
    }
    var phoneLLM: PhoneLLMConfig {
        var c = PhoneLLMConfig()
        c.deep = LLMProfileConfig(
            baseURL: defaults.string(forKey: Keys.llmDeepURL) ?? "",
            model: defaults.string(forKey: Keys.llmDeepModel) ?? "",
            apiKey: defaults.string(forKey: Keys.llmDeepKey) ?? "")
        c.fast = LLMProfileConfig(
            baseURL: defaults.string(forKey: Keys.llmFastURL) ?? "",
            model: defaults.string(forKey: Keys.llmFastModel) ?? "",
            apiKey: defaults.string(forKey: Keys.llmFastKey) ?? "")
        return c
    }

    private init() {
        Task { await bootstrap() }
    }

    // MARK: wiring

    func bootstrap() async {
        let fm = FileManager.default
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Ambient", isDirectory: true)
        let audio = fm.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("AmbientAudio", isDirectory: true)
        do {
            let outbox = try OutboxStore(path: base.appendingPathComponent("outbox.sqlite").path)
            let history = try CardHistory(path: base.appendingPathComponent("history.sqlite").path)
            let router = EndpointRouter(endpoints: endpoints.isEmpty
                ? [URL(string: "http://localhost:8378")!] : endpoints)
            let socket = token.isEmpty ? nil : LiveSocket(token: token)
            let sink = OutboxSink(outbox: outbox, socket: socket)
            let api = APIClient(router: router, token: token)
            let uploader = Uploader(outbox: outbox,
                                    transport: HTTPIngestTransport(token: token),
                                    router: router)
            self.outbox = outbox; self.history = history; self.router = router
            self.api = api; self.socket = socket; self.uploader = uploader
            self.sink = sink
            try? audioBuffer = RollingAudioBuffer(directory: audio)
            await uploader.start()
            await wirePhoneStack(base: base, api: api, history: history)

            if token.isEmpty {
                status = .needsToken
            } else {
                status = .offline
                await reconnectSocket()
                await refreshHealth()
            }
            startQueueTicker()
        } catch {
            log.error("bootstrap failed: \(error.localizedDescription)")
            toast = "Local storage error: \(error.localizedDescription)"
        }
    }

    private(set) var sink: OutboxSink?
    private var audioBuffer: RollingAudioBuffer?

    // MARK: phone-first runtime (storage + scoring + LLM tasks live on the phone)

    @Published var memoryCount = 0
    @Published var heldResults = 0
    private(set) var memoryStore: MemoryStore?
    private(set) var taskResults: LocalTaskResults?
    private(set) var phoneEngine: PhoneRelevanceEngine?
    private(set) var taskWorker: TaskWorker?
    private var phoneLoop: Task<Void, Never>?

    private func wirePhoneStack(base: URL, api: APIClient, history: CardHistory) async {
        do {
            let mem = try MemoryStore(path: base.appendingPathComponent("memories.sqlite").path)
            let res = try LocalTaskResults(path: base.appendingPathComponent("task_results.sqlite").path)
            let eng = PhoneRelevanceEngine(retrieval: StoreRetrieval(store: mem))
            await eng.configure(
                dismiss: { id in (try? await history.dismissCount(memoryId: id)) ?? 0 },
                cooldown: (try? await history.lastShownByMemory()) ?? [:])
            self.memoryStore = mem
            self.taskResults = res
            self.phoneEngine = eng
            let llm = LLMClient(config: phoneLLM)
            taskWorker = TaskWorker(api: api, llm: phoneLLM.deep.isConfigured ? llm : nil, results: res)
            memoryCount = (try? await mem.count()) ?? 0
            startPhoneLoops()
            await syncMemories()   // first mirror ASAP
        } catch {
            log.error("phone stack failed: \(error.localizedDescription)")
        }
    }

    /// Pull the full memory mirror from the server (rows may be pruned server-side;
    /// we re-swap the whole table — see MemoryStore.replaceAll).
    func syncMemories() async {
        guard let api, let memoryStore else { return }
        do {
            let rows = try await api.memoriesSync()
            try await memoryStore.replaceAll(with: rows)
            memoryCount = (try? await memoryStore.count()) ?? memoryCount
        } catch {
            log.info("memory sync deferred: offline")   // next loop retries; nothing lost
        }
    }

    /// Debug aid: server-free memories so phone-side card scoring can be exercised
    /// fully offline (Debug screen). Existing rows are preserved.
    func seedDemoMemories() async {
        guard let memoryStore else { return }
        let now = Date().isoWire
        func m(_ id: String, _ t: String, _ type: String, _ ents: [String]) -> SyncedMemory {
            SyncedMemory(id: "demo-" + id, text: t, type: type, importance: 0.85,
                         confidence: 0.95, eventStart: nil, eventEnd: nil,
                         validFrom: now, validTo: nil, recordedAt: now,
                         status: "active", source: "demo", speakerName: "Jasmine",
                         entities: ents)
        }
        let demo = [
            m("chili", "Dinner next Friday will be chili, booked at Chili's",
              "event", ["Jasmine", "Chili"]),
            m("maya", "Maya adopted two cats in September", "fact", ["Maya"]),
            m("deck", "Deck renovation is due in two weeks — contractor hired by Nov 17",
              "commitment", ["Deck"]),
        ]
        let existing = (try? await memoryStore.recent(limit: 500,
                                                      includeInactive: true)) ?? []
        let keep = existing.map { e in
            SyncedMemory(id: e.id, text: e.text, type: e.type,
                         importance: e.importance, confidence: e.confidence,
                         eventStart: e.eventStart?.isoWire, eventEnd: e.eventEnd?.isoWire,
                         validFrom: (e.validFrom ?? e.recordedAt ?? Date()).isoWire,
                         validTo: e.validTo?.isoWire,
                         recordedAt: (e.recordedAt ?? Date()).isoWire,
                         status: e.status, source: e.source,
                         speakerName: e.speakerName, entities: e.entities)
        }
        try? await memoryStore.replaceAll(with: keep + demo)
        memoryCount = (try? await memoryStore.count()) ?? memoryCount
        toast = "Seeded 3 demo memories — now emit 'what about friday dinner?'"
    }

    /// Battery-aware loop: task queue every 15 s while active, memory sync every
    /// ~10 min, engine fade tick with it.
    private func startPhoneLoops() {
        phoneLoop?.cancel()
        phoneLoop = Task { [weak self] in
            var cycle = 0
            while !Task.isCancelled {
                guard let self else { return }
                cycle += 1
                if self.phoneExtractionOn, self.phoneLLM.deep.isConfigured {
                    await self.taskWorker?.runPendingOnce()
                }
                if cycle % 40 == 0 { await self.syncMemories() }
                // fade expired phone cards (server cards fade via WS cardDismiss)
                if let engine = self.phoneEngine {
                    let gone = await engine.tick(now: .init())
                    if !gone.isEmpty {
                        await MainActor.run {
                            self.cards.removeAll { gone.contains($0.id) }
                        }
                    }
                }
                self.heldResults = (try? await self.taskResults?.pending())?.count ?? 0
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }

    /// Score the utterance on-device the moment it's captured — cards appear with
    /// no server round-trip, and this works fully offline once the mirror exists.
    func runPhonePass(text: String, speakerId: String? = nil) async {
        guard let phoneEngine else { return }
        let u = EngineUtterance(text: text, speakerLabel: currentSpeaker,
                                speakerId: speakerId, ts: .init())
        guard let fresh = try? await phoneEngine.onUtterance(u) else { return }
        for card in fresh { await MainActor.run { self.showCard(card) } }
    }

    func reconnectSocket() async {
        guard let socket, let url = await router?.current() else { return }
        await socket.connect(endpoint: url) { [weak self] env in
            Task { @MainActor in self?.handle(env) }
        }
    }

    func refreshHealth() async {
        guard let api else { return }
        do {
            _ = try await api.health()
            status = .online
            if let outbox { queued = try await outbox.pendingCount() }
        } catch {
            status = token.isEmpty ? .needsToken : .offline
        }
    }

    private func handle(_ env: ServerEnvelope) {
        status = .online
        switch env {
        case .card(let card), .cardUpdate(let card):
            showCard(card)
        case .cardDismiss(let id):
            cards.removeAll { $0.id == id }
        case .status(let s):
            if case .string(let err)? = s["error"] { toast = err }
        }
    }

    // MARK: cards (spec §3.5: max N on screen, priority first)

    func showCard(_ card: Card) {
        cards.removeAll { $0.id == card.id }
        if card.priority { cards.insert(card, at: 0) } else { cards.append(card) }
        while cards.count > cardBudget {
            if let i = cards.firstIndex(where: { !$0.priority }) ?? cards.indices.last {
                cards.remove(at: i)
            } else { break }
        }
        if hapticsOn, card.score > 1.0 || card.priority {
            UIImpactFeedbackGenerator(style: .soft).impactOccurred()
        }
        Task { try? await history?.recordShown(card: card) }
    }

    func feedback(_ card: Card, kind: FeedbackKind) {
        if kind == .dismiss { cards.removeAll { $0.id == card.id } }
        if kind == .pin {
            if card.id.hasPrefix("local-") {
                Task { await phoneEngine?.pin(cardId: card.id) }  // survives TTL fade
            }
            if let i = cards.firstIndex(where: { $0.id == card.id }) {
                var pinned = card
                pinned.priority = true
                cards[i] = pinned
            }
        }
        Task {
            try? await history?.recordFeedback(card: card, kind: kind)
            await socket?.sendFeedback(FeedbackMessage(cardId: card.id, kind: kind))
        }
    }

    // MARK: listening (pure machine drives real engine actions)

    func toggleListening() {
        let actions = machine.handle(listening == .listening ? .userPaused : .startRequested)
        listening = machine.state
        Task { await execute(actions) }
    }

    func machineEvent(_ event: ListeningEvent) {
        let actions = machine.handle(event)
        listening = machine.state
        Task { await execute(actions) }
    }

    private func execute(_ actions: [ListeningAction]) async {
        for action in actions {
            switch action {
            case .startEngine: await startCapture()
            case .stopEngine: await stopCapture()
            case .markGap(let started):
                gaps.append(started ?? Date())
                toast = "Listening gap recorded — transcripts will show the hole"
            case .scheduleRetry(let after):
                try? await Task.sleep(for: .seconds(after))
                await startCapture()
            case .alertUser(let why): toast = why
            }
        }
    }

    private func startCapture() async {
        #if os(iOS)
        guard capture == nil else { return }
        let cap = iOSAudioCapture { [weak self] event in
            Task { @MainActor in self?.machineEvent(event) }
        }
        capture = cap
        gate = SpeechGate()
        do {
            try await cap.start()
            machineEvent(.engineStarted)
        } catch {
            toast = "Mic unavailable: \(error.localizedDescription)"
        }
        frameTask = Task { [weak self] in
            guard let self else { return }
            for await frame in cap.frames {
                await self.consume(frame: frame)
            }
        }
        #endif
    }

    private func stopCapture() async {
        frameTask?.cancel(); frameTask = nil
        await capture?.stop()
        capture = nil
        let stranded = gate.flush(at: .init())
        _ = stranded
    }

    private func consume(frame: AudioFrame) async {
        // rolling buffer keeps raw audio for the offline refinement pass (spec §3.2.8)
        try? await audioBuffer?.append(frame.samples, at: frame.ts)
        let events = gate.ingest(frame.samples, at: frame.ts)
        for event in events {
            switch event {
            case .speechStart:
                break
            case .silence:
                await socket?.sendHeartbeat()
            case .utteranceEnd(let start, let end, let samples):
                await finalize(samples: samples, start: start, end: end)
            }
        }
    }

    /// A gated speech segment. ASR binding (FluidAudio, M0 spike b) lands here;
    /// until then segments are counted and the Debug screen's demo sender proves
    /// the pipeline end-to-end. Whisper classification + query routing are live.
    private func finalize(samples: [[Float]], start: Date, end: Date) async {
        let whisper = WhisperDetector(config: {
            var c = WhisperConfig(); c.rmsRatio = whisperRatio; return c
        }(), speakingRms: 0.08)
        let isOwner = currentSpeaker == "Owner"
        guard whisper.isWhisper(samplesPerFrame: samples, isOwner: isOwner) else { return }
        // Whispered speech is a QUERY, never conversation content — but text needs
        // ASR first; when the ASR binding lands, route text through `ask(_:)` AND
        // feed non-whisper transcripts through `runPhonePass(text:)` here — the
        // phone-side scoring loop is already wired and offline-capable.
    }

    /// Direct query path (spec §5.5): whisper text, search bar, or hold-to-ask.
    /// Local-first — the phone mirror answers instantly and works with no network;
    /// the server adds LLM synthesis + SearxNG when reachable.
    func ask(_ question: String) async {
        if let answer = await localAnswer(for: question) {
            showCard(answer)
            if status != .online { return }   // offline: this is the answer we have
        }
        guard let api else {
            if status != .online { toast = "Answered from on-device memory" }
            return
        }
        do {
            let result = try await api.query(question)
            showCard(result.card)
        } catch {
            if let answer = await localAnswer(for: question) {
                showCard(answer)   // server died mid-question — mirror still serves
            } else {
                toast = "Query failed: \(error.localizedDescription)"
            }
        }
    }

    private func localAnswer(for question: String) async -> Card? {
        guard let memoryStore else { return nil }
        let hits = (try? await memoryStore.search(question)) ?? []
        guard let top = hits.first, top.score > 0.5 else { return nil }
        var headline = top.text
        if headline.count > 140 { headline = String(headline.prefix(139)) + "…" }
        return Card(id: "local-q-" + String(UUID().uuidString.prefix(8)) + "-" + top.id,
                    kind: .lookup, memoryId: top.id, headline: headline,
                    detail: hits.dropFirst().prefix(2).map(\.text).joined(separator: " · "),
                    sourceChip: "on-device memory", origin: .memory, url: nil,
                    score: top.score, priority: true, sourceUtteranceIds: [],
                    expiresAt: nil, debugFeatures: [:],
                    createdAt: Date().isoWire)
    }

    /// Debug/demo path: push a scripted utterance through the exact production route
    /// (outbox first, then live WS) — proves end-to-end without ASR models.
    func emitDemoUtterance(_ text: String, whisper: Bool = false) {
        guard let sink else { return }
        let event = UtteranceEvent(conversationHint: nil, speakerId: "demo",
                                   speakerLabel: currentSpeaker, text: text,
                                   isWhisper: whisper, device: "iphone-demo")
        Task {
            await sink.ingest(event)
            if whisper {
                await ask(text)   // spec §3.6: query path, priority card
            } else {
                await runPhonePass(text: text, speakerId: "demo")
            }
            queued = (try? await outbox?.pendingCount()) ?? queued
        }
    }

    private func startQueueTicker() {
        queueTask?.cancel()
        queueTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if let outbox = self.outbox {
                    self.queued = (try? await outbox.pendingCount()) ?? self.queued
                }
                if let lastDrop = await self.outbox?.dropAlerts.last {
                    self.toast = "Outbox full — dropped \(lastDrop.id) (2 GB cap)"
                }
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    /// Rebuild connections after endpoint/token edits.
    func settingsChanged() async {
        await socket?.disconnect()
        socket = token.isEmpty ? nil : LiveSocket(token: token)
        api = APIClient(router: EndpointRouter(endpoints: endpoints), token: token)
        if let uploader { _ = uploader }   // router endpoints refresh
        await bootstrapRouterEndpointsOnly()
        await reconnectSocket()
        await refreshHealth()
        // phone LLM settings may have changed: rebuild the worker against them
        if let api, let taskResults {
            let llm = LLMClient(config: phoneLLM)
            taskWorker = TaskWorker(api: api, llm: phoneLLM.deep.isConfigured ? llm : nil,
                                    results: taskResults)
        }
    }

    private func bootstrapRouterEndpointsOnly() async {
        await router?.replaceEndpoints(endpoints.isEmpty
            ? [URL(string: "http://localhost:8378")!] : endpoints)
    }
}

private extension Int {
    func nonZeroFallback(_ fallback: Int) -> Int { self == 0 ? fallback : self }
}
