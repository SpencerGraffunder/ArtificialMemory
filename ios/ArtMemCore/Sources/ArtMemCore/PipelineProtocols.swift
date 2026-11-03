import Foundation

// MARK: - Pipeline seams (spec §3.7): phone mic, watch mic, Bluetooth mic, glasses all
// implement AudioSource; phone UI, watch UI, Live Activity all implement CardSink.
// ArtMemCore imports NO UI/audio frameworks at the protocol level so watchOS and
// glasses targets can reuse everything here.

public struct AudioFrame: Sendable, Equatable {
    public var samples: [Float]     // 16 kHz mono Float32
    public var ts: Date
    public init(samples: [Float], ts: Date) { self.samples = samples; self.ts = ts }
}

public protocol AudioSource: Sendable {
    var frames: AsyncStream<AudioFrame> { get }
    func start() async throws
    func stop() async
}

/// Finalized utterance destination: the phone wires this to Outbox + LiveSocket.
public protocol UtteranceSink: Sendable {
    func ingest(_ event: UtteranceEvent) async
}

/// Transcription + diarization pipeline (FluidAudio-backed on device; see DECISIONS.md —
/// M0 spike must prove the model trio under battery/thermal budget before this is bound
/// to FluidAudio. Everything upstream of it is model-agnostic).
public protocol SpeechPipeline: Sendable {
    func start(source: any AudioSource) async throws
    func stop() async
    var utterances: AsyncStream<UtteranceEvent> { get }     // finalized only
    var partials: AsyncStream<PartialTranscript> { get }    // live display
}

public struct PartialTranscript: Sendable, Equatable {
    public var id: String
    public var text: String
    public var speakerLabel: String
    public init(id: String, text: String, speakerLabel: String) {
        self.id = id; self.text = text; self.speakerLabel = speakerLabel
    }
}

/// Card presentation destination (spec §3.7): SwiftUI stack, Live Activity, watch UI.
public protocol CardSink: Sendable {
    func show(_ card: Card) async
    func update(_ card: Card) async
    func dismiss(cardId: String) async
}

// MARK: - Fakes: drive previews, UI dev, and integration tests without models or mic.

public actor FakeSpeechPipeline: SpeechPipeline {
    private var utterContinuation: AsyncStream<UtteranceEvent>.Continuation
    private var partialContinuation: AsyncStream<PartialTranscript>.Continuation
    public nonisolated let utterances: AsyncStream<UtteranceEvent>
    public nonisolated let partials: AsyncStream<PartialTranscript>
    private var source: (any AudioSource)?

    public init() {
        (utterances, utterContinuation) = AsyncStream.makeStream(of: UtteranceEvent.self)
        (partials, partialContinuation) = AsyncStream.makeStream(of: PartialTranscript.self)
    }

    public func start(source: any AudioSource) async throws { self.source = source }
    public func stop() async {
        utterContinuation.finish(); partialContinuation.finish(); source = nil
    }

    /// Test/preview hook: push scripted traffic through the same streams.
    public func emit(_ event: UtteranceEvent) { utterContinuation.yield(event) }
    public func emitPartial(_ p: PartialTranscript) { partialContinuation.yield(p) }
}

public actor FakeAudioSource: AudioSource {
    public nonisolated let frames: AsyncStream<AudioFrame>
    private var continuation: AsyncStream<AudioFrame>.Continuation
    public private(set) var isRunning = false
    public init() { (frames, continuation) = AsyncStream.makeStream(of: AudioFrame.self) }
    public func start() async throws { isRunning = true }
    public func stop() async { isRunning = false }
    public func push(_ frame: AudioFrame) { if isRunning { continuation.yield(frame) } }
    public func finish() { continuation.finish() }
}

/// Collects cards for tests and the Debug screen.
public actor RecordingCardSink: CardSink {
    public private(set) var visible: [Card] = []          // max-3 stack semantics
    public private(set) var dismissedIds: [String] = []
    public var maxVisible = 3

    public init() {}
    public func show(_ card: Card) async {
        visible.removeAll { $0.id == card.id }
        if card.priority { visible.insert(card, at: 0) } else { visible.append(card) }
        // budget (spec §5.4): over the cap, evict the OLDEST ordinary card —
        // a commitment reminder must not push itself out
        while visible.count > maxVisible {
            if let i = visible.firstIndex(where: { !$0.priority }) ?? visible.indices.last {
                visible.remove(at: i)
            } else { break }
        }
    }
    public func update(_ card: Card) async {
        if let i = visible.firstIndex(where: { $0.id == card.id }) { visible[i] = card }
        else { await show(card) }
    }
    public func dismiss(cardId: String) async {
        visible.removeAll { $0.id == cardId }
        dismissedIds.append(cardId)
    }
}

// MARK: - Outbox-bound utterance sink: the single durability path (spec §3.4)

public actor OutboxSink: UtteranceSink {
    private let outbox: OutboxStore
    private let socket: LiveSocket?
    public var onQueued: (@Sendable (UtteranceEvent) -> Void)?

    public init(outbox: OutboxStore, socket: LiveSocket? = nil) {
        self.outbox = outbox; self.socket = socket
    }

    public func ingest(_ event: UtteranceEvent) async {
        // queue FIRST (durability), then send live opportunistically
        try? await outbox.append(event)
        onQueued?(event)
        await socket?.sendUtterance(event, revision: event.revision > 0)
    }
}
