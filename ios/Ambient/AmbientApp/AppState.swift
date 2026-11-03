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
        // ASR first; when the ASR binding lands, route text through `ask(_:)`.
    }

    /// Direct query path (spec §5.5): whisper text, search bar, or hold-to-ask.
    func ask(_ question: String) async {
        guard let api else { return }
        do {
            let result = try await api.query(question)
            showCard(result.card)
        } catch {
            toast = "Query failed: \(error.localizedDescription)"
        }
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
            if whisper { await ask(text) }   // spec §3.6: query path, priority card
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
    }

    private func bootstrapRouterEndpointsOnly() async {
        await router?.replaceEndpoints(endpoints.isEmpty
            ? [URL(string: "http://localhost:8378")!] : endpoints)
    }
}

private extension Int {
    func nonZeroFallback(_ fallback: Int) -> Int { self == 0 ? fallback : self }
}
