import Foundation

// MARK: - Whisper-to-self detection (spec §3.6). Pure signal features: quiet + breathy
// relative to the owner's calibrated speaking level; plus trigger-phrase fallback.

public struct WhisperConfig: Sendable, Equatable {
    /// RMS must fall below speakingRMS × rmsRatio to count as whisper-level
    public var rmsRatio: Double = 0.18
    /// whisper is breathy: zero-crossing rate stays high while energy is low
    public var minZcr: Double = 0.08
    /// fraction of frames in the utterance that must qualify
    public var frameAgreement: Double = 0.7
    /// fallback trigger phrases ("memory, …") — spec §3.6 fallbacks (a)
    public var triggerPhrases: [String] = ["memory,", "reminder,", "ambient,"]
    /// frames shorter than this are too short to judge (avoid clicks)
    public var minFrames: Int = 4
    public init() {}
}

public struct WhisperDetector: Sendable {
    public var config: WhisperConfig
    /// owner's calibrated normal-speaking RMS (from enrollment; spec §3.6)
    public var speakingRms: Double

    public init(config: WhisperConfig = WhisperConfig(), speakingRms: Double = 0.08) {
        self.config = config
        self.speakingRms = max(speakingRms, 1e-9)
    }

    public struct FrameStats: Sendable, Equatable {
        public var rms: Double
        public var zcr: Double   // zero crossings per sample
    }

    public static func frameStats(_ samples: [Float]) -> FrameStats {
        guard !samples.isEmpty else { return .init(rms: 0, zcr: 0) }
        var sum: Float = 0
        var crossings = 0
        for i in 0..<samples.count {
            sum += samples[i] * samples[i]
            if i > 0, (samples[i] >= 0) != (samples[i - 1] >= 0) { crossings += 1 }
        }
        return .init(rms: Double(sqrt(sum / Float(samples.count))),
                     zcr: Double(crossings) / Double(samples.count))
    }

    private func frameIsWhisper(_ s: FrameStats) -> Bool {
        s.rms < speakingRms * config.rmsRatio && s.zcr >= config.minZcr
    }

    /// Judge an utterance's frames. `isOwner` gates it: only the owner's quiet speech is
    /// a query; a guest leaning in is conversation content (spec §3.6).
    public func isWhisper(samplesPerFrame: [[Float]], isOwner: Bool) -> Bool {
        guard isOwner, samplesPerFrame.count >= config.minFrames else { return false }
        let whisperFrames = samplesPerFrame
            .map { Self.frameStats($0) }
            .filter(frameIsWhisper)
            .count
        return Double(whisperFrames) / Double(samplesPerFrame.count) >= config.frameAgreement
    }

    /// Fallback (a): explicit trigger phrase prefix on the transcript.
    public func matchesTriggerPhrase(_ transcript: String) -> String? {
        let lower = transcript.lowercased().trimmingCharacters(in: .whitespaces)
        for phrase in config.triggerPhrases where lower.hasPrefix(phrase) {
            return String(lower.dropFirst(phrase.count))
                .trimmingCharacters(in: .whitespaces)
        }
        return nil
    }
}
