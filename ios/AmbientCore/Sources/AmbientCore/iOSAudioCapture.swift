#if os(iOS)
import AVFoundation
import Foundation

// MARK: - iPhone capture (spec §3.1): AVAudioSession .playAndRecord + .mixWithOthers,
// engine input tap, 16 kHz mono Float32 conversion, interruption/route recovery driving
// the pure ListeningMachine. UI-free: emits AudioFrames; the app renders state.
// Compiled for iOS targets only (`#if os(iOS)`); logic lives in the pure state machine.

public final class iOSAudioCapture: NSObject, AudioSource, @unchecked Sendable {
    public let frames: AsyncStream<AudioFrame>
    private let continuation: AsyncStream<AudioFrame>.Continuation

    private let engine = AVAudioEngine()
    private let targetSampleRate: Double
    private var converter: AVAudioConverter?
    private lazy var targetFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                                  sampleRate: targetSampleRate,
                                                  channels: 1, interleaved: false)!
    private let lock = NSLock()
    private let onMachineEvent: (@Sendable (ListeningEvent) -> Void)?
    public private(set) var isRunning = false

    public init(sampleRate: Double = 16_000,
                onMachineEvent: (@Sendable (ListeningEvent) -> Void)? = nil) {
        self.targetSampleRate = sampleRate
        var cont: AsyncStream<AudioFrame>.Continuation!
        self.frames = AsyncStream(AudioFrame.self, bufferingPolicy: .bufferingNewest(50)) {
            cont = $0
        }
        self.continuation = cont
        self.onMachineEvent = onMachineEvent
        super.init()
    }

    public func start() async throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default,
                                options: [.mixWithOthers, .allowBluetooth,
                                          .defaultToSpeaker])
        try session.setActive(true, options: .notifyOthersOnDeactivation)
        rebuildTap()

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(handleInterruption(_:)),
                       name: AVAudioSession.interruptionNotification, object: session)
        nc.addObserver(self, selector: #selector(handleRouteChange(_:)),
                       name: AVAudioSession.routeChangeNotification, object: session)
        nc.addObserver(self, selector: #selector(handleConfigurationChange(_:)),
                       name: NSNotification.Name.AVAudioEngineConfigurationChange,
                       object: engine)

        engine.prepare()
        do {
            try engine.start()
            lock.withLock { isRunning = true }
            onMachineEvent?(.engineStarted)
        } catch {
            onMachineEvent?(.engineDied)
            throw error
        }
    }

    public func stop() async {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.withLock { isRunning = false }
        NotificationCenter.default.removeObserver(self)
        try? AVAudioSession.sharedInstance()
            .setActive(false, options: .notifyOthersOnDeactivation)
    }

    deinit { NotificationCenter.default.removeObserver(self) }

    // MARK: tap + conversion

    private func rebuildTap() {
        engine.inputNode.removeTap(onBus: 0)
        let input = engine.inputNode
        let inputFormat = input.inputFormat(forBus: 0)
        converter = AVAudioConverter(from: inputFormat, to: targetFormat)
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self]
            buffer, _ in
            self?.process(buffer)
        }
    }

    private func process(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        let ratio = targetFormat.sampleRate / max(1, buffer.format.sampleRate)
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 16
        guard let out = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity)
        else { return }

        var consumed = false
        var convertError: NSError?
        let status = converter.convert(to: out, error: &convertError) { _, outStatus in
            if consumed || buffer.frameLength == 0 {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return buffer
        }
        guard convertError == nil, status != .error, out.frameLength > 0,
              let channel = out.floatChannelData?.pointee else { return }
        let samples = Array(UnsafeBufferPointer(start: channel, count: Int(out.frameLength)))
        continuation.yield(AudioFrame(samples: samples, ts: Date()))
    }

    // MARK: notifications → pure state machine events

    @objc private func handleInterruption(_ note: Notification) {
        guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        switch type {
        case .began:
            onMachineEvent?(.interruptionBegan)
        case .ended:
            onMachineEvent?(.interruptionEnded)
            let opts = AVAudioSession.InterruptionOptions(
                rawValue: note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
            if opts.contains(.shouldResume) {
                rebuildTap()
                do { try engine.start(); onMachineEvent?(.recovered) }
                catch { onMachineEvent?(.engineDied) }
            }
        @unknown default:
            onMachineEvent?(.engineDied)
        }
    }

    @objc private func handleRouteChange(_ note: Notification) {
        // headphone/Bluetooth switch changes the input format: rebuild the tap cleanly
        guard isRunningLive() else { return }
        rebuildTap()
    }

    @objc private func handleConfigurationChange(_ note: Notification) {
        guard isRunningLive() else { return }
        rebuildTap()   // device exclusive takeover arrives here; tap is gone
    }

    private func isRunningLive() -> Bool {
        lock.withLock { isRunning }
    }
}
#endif
