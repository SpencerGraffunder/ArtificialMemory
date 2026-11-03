import Foundation

// MARK: - VAD gate + endpointing (spec §3.2): frames in → utterance boundaries out.
// Pure and deterministic: the ASR front-end (FluidAudio/whisper) consumes bounded
// segments; endpointing must not wait forever on a half-finished sentence, and must
// not finalize mid-pause. Whisper flag comes from WhisperDetector at segment end.

public struct SpeechGateConfig: Sendable, Equatable {
    /// RMS above this = speech (calibrated per owner; spec §3.6 enrollment)
    public var rmsThreshold: Double = 0.015
    /// hangover: silence this long ends the utterance (endpoint)
    public var silenceEndpoint: TimeInterval = 0.8
    /// hard cap per utterance; longer stretches get force-ended (ASR window limits)
    public var maxUtterance: TimeInterval = 30
    /// segments shorter than this are dropped (coughs, page turns)
    public var minUtterance: TimeInterval = 0.35
    public var sampleRate: Double = 16_000
    public init() {}
}

public enum GateEvent: Sendable, Equatable {
    case speechStart(at: Date)
    case utteranceEnd(start: Date, end: Date, samples: [[Float]])
    case silence(at: Date)          // for the "nobody here / silence window" timer
}

/// Feed consecutive equal-size frames; receive boundary events.
public struct SpeechGate: Sendable {
    public var config: SpeechGateConfig
    private var inSpeech = false
    private var speechBegan: Date?
    private var silenceRun: TimeInterval = 0
    private var utteranceSamples: [[Float]] = []

    public init(config: SpeechGateConfig = SpeechGateConfig()) { self.config = config }

    public mutating func reset() {
        inSpeech = false; speechBegan = nil; silenceRun = 0; utteranceSamples = []
    }

    /// Process one frame of samples at wall-clock `at` (frame duration derived from
    /// sample count and configured rate).
    public mutating func ingest(_ samples: [Float], at: Date) -> [GateEvent] {
        let frameSeconds = Double(samples.count) / config.sampleRate
        let rms = WhisperDetector.frameStats(samples).rms
        let voiced = rms >= config.rmsThreshold
        var events: [GateEvent] = []

        if voiced, !inSpeech {
            inSpeech = true
            speechBegan = at
            silenceRun = 0
            utteranceSamples = [samples]
            events.append(.speechStart(at: at))
        } else if voiced {
            silenceRun = 0
            utteranceSamples.append(samples)
        } else if inSpeech {
            silenceRun += frameSeconds
            if silenceRun >= config.silenceEndpoint {
                events.append(contentsOf: end(at: at))
            } else {
                // within hangover: keep the pause inside the utterance
                utteranceSamples.append(samples)
            }
        } else {
            events.append(.silence(at: at))
        }

        if inSpeech, let began = speechBegan,
           at.timeIntervalSince(began) >= config.maxUtterance {
            events.append(contentsOf: end(at: at))   // ASR window cap
        }
        return events
    }

    /// Flush at app background/pause so nothing is stranded.
    public mutating func flush(at: Date) -> [GateEvent] {
        inSpeech ? end(at: at) : []
    }

    private mutating func end(at: Date) -> [GateEvent] {
        defer { inSpeech = false; speechBegan = nil; silenceRun = 0; utteranceSamples = [] }
        guard let began = speechBegan else { return [] }
        let endAt = at.addingTimeInterval(-min(silenceRun, config.silenceEndpoint))
        guard endAt.timeIntervalSince(began) >= config.minUtterance else {
            return []   // too short: dropped, no event (spec: no junk transcriptions)
        }
        return [.utteranceEnd(start: began, end: endAt, samples: utteranceSamples)]
    }
}
