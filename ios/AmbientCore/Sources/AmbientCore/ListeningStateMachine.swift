import Foundation

// MARK: - Listening state machine (spec §3.1). Pure: state + event → state + actions.
// The capture layer executes actions; tests pin every transition, no AVFoundation needed.

public enum ListeningState: String, Sendable, Equatable {
    case idle, listening, interrupted, paused, error
}

public enum ListeningEvent: Sendable, Equatable {
    case startRequested
    case engineStarted
    case userPaused
    case userResumed
    case interruptionBegan          // call / Siri / alarm
    case interruptionEnded          // .ended notification
    case engineDied                 // media-services reset / tap failed
    case fatal(String)
    case recovered                  // engine restarted successfully
}

public enum ListeningAction: Sendable, Equatable {
    case startEngine
    case stopEngine
    /// a capture gap must be marked in the timeline (spec: suspension/killed detection)
    case markGap(startedApproximately: Date?)
    case scheduleRetry(after: TimeInterval)
    case alertUser(String)
}

public struct ListeningMachine: Sendable {
    public private(set) var state: ListeningState = .idle
    public private(set) var interruptionStartedAt: Date?
    public var retryDelays: [TimeInterval] = [1, 2, 5, 10, 30]   // spec: recover < 5 s
    /// gaps longer than this always surface in the timeline
    public var gapThreshold: TimeInterval = 2
    private var retries = 0

    public init() {}

    /// Apply an event; returns new state + actions to execute.
    @discardableResult
    public mutating func handle(_ event: ListeningEvent, now: Date = Date())
        -> [ListeningAction] {
        switch (state, event) {
        case (_, .startRequested):
            state = .listening; retries = 0
            return [.startEngine]

        case (.listening, .userPaused):
            state = .paused
            return [.stopEngine]

        case (.paused, .userResumed):
            state = .listening; retries = 0
            return [.startEngine]

        case (.listening, .interruptionBegan):
            state = .interrupted
            interruptionStartedAt = now
            return [.stopEngine]

        case (.interrupted, .interruptionEnded):
            let gapStart = interruptionStartedAt
            let gap = gapStart.map { now.timeIntervalSince($0) } ?? 0
            state = .listening
            interruptionStartedAt = nil
            var actions: [ListeningAction] = [.startEngine]
            if gap >= gapThreshold {
                actions.insert(.markGap(startedApproximately: gapStart), at: 0)
            }
            return actions

        case (.listening, .engineDied), (.interrupted, .engineDied):
            state = .error
            interruptionStartedAt = interruptionStartedAt ?? now
            return recoveryActions(gapStart: interruptionStartedAt, now: now)

        case (.error, .recovered):
            state = .listening
            let gap = interruptionStartedAt.map { now.timeIntervalSince($0) } ?? 0
            interruptionStartedAt = nil
            retries = 0
            return gap >= gapThreshold ? [.markGap(startedApproximately: nil)] : []

        case (.error, .interruptionBegan):
            interruptionStartedAt = now
            return []

        case (.error, .interruptionEnded):
            // retry while an interruption ended — this is exactly the call-hung-up case
            return recoveryActions(gapStart: interruptionStartedAt ?? now, now: now)

        case (_, .fatal(let reason)):
            state = .error
            return [.alertUser(reason)]

        case (.idle, .engineStarted):
            state = .listening
            return []

        default:
            return []   // unknown transitions are inert (idempotent notifications)
        }
    }

    private mutating func recoveryActions(gapStart: Date?, now: Date) -> [ListeningAction] {
        let delay = retryDelays[min(retries, retryDelays.count - 1)]
        retries += 1
        var actions: [ListeningAction] = [.scheduleRetry(after: delay)]
        if let s = gapStart, now.timeIntervalSince(s) >= gapThreshold {
            actions.append(.markGap(startedApproximately: s))
        }
        return actions
    }
}
