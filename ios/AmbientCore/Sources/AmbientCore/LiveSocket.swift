import Foundation

// MARK: - Live WebSocket client (spec §4.1 /v1/live). Reconnects with backoff; heartbeats;
// utterances sent as they finalize; cards/feedback as ServerEnvelope/ClientEnvelope.

public actor LiveSocket {
    public typealias Handler = @Sendable (ServerEnvelope) -> Void

    private let token: String
    private let session: URLSession
    private var task: URLSessionWebSocketTask?
    private var handler: Handler?
    private var connectTask: Task<Void, Never>?
    private var heartbeatTask: Task<Void, Never>?
    private var stopped = true
    private let backoff = BackoffConfig(base: 1, max: 60)
    private var failures = 0
    public private(set) var isConnected = false

    public init(token: String, session: URLSession = .shared) {
        self.token = token
        let cfg = URLSessionConfiguration.default
        cfg.waitsForConnectivity = true
        self.session = URLSession(configuration: cfg, delegate: nil, delegateQueue: nil)
    }

    public func connect(endpoint: URL, onEnvelope: @escaping Handler) {
        stopped = false
        handler = onEnvelope
        startConnection(endpoint: endpoint)
    }

    public func disconnect() {
        stopped = true
        heartbeatTask?.cancel(); heartbeatTask = nil
        connectTask?.cancel(); connectTask = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        isConnected = false
    }

    public func send(_ envelope: ClientEnvelope) {
        guard let data = try? AmbientJSON.encoder(envelope),
              let text = String(data: data, encoding: .utf8) else { return }
        task?.send(.string(text)) { [weak self] _ in
            Task { await self?.markDisconnectedIfNeeded() }
        }
    }

    public func sendHeartbeat() { send(.heartbeat) }
    public func sendFeedback(_ fb: FeedbackMessage) { send(.feedback(fb) ) }
    public func sendUtterance(_ u: UtteranceEvent, revision: Bool = false) {
        send(revision ? .utteranceRevision(u) : .utterance(u))
    }

    private func startConnection(endpoint: URL) {
        connectTask?.cancel()
        connectTask = Task { [weak self] in
            guard let self else { return }
            await self.open(endpoint: endpoint)
        }
    }

    private func open(endpoint: URL) {
        guard !stopped else { return }
        var req = URLRequest(url: endpoint.appendingPathComponent("v1/live"))
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let t = session.webSocketTask(with: req)
        task = t
        t.resume()
        failures += 1
        startReceiveLoop(task: t, endpoint: endpoint)
        startHeartbeat()
    }

    private func startReceiveLoop(task t: URLSessionWebSocketTask, endpoint: URL) {
        @Sendable func loop() {
            t.receive { [weak self] result in
                Task { [weak self] in
                    guard let self else { return }
                    switch result {
                    case .success(.string(let text)):
                        if let env = try? AmbientJSON.decoder(ServerEnvelope.self,
                                                              from: Data(text.utf8)) {
                            await self.deliver(env)
                        }
                        loop()
                    case .success:
                        loop()
                    case .failure:
                        await self.reconnect(endpoint: endpoint)
                    }
                }
            }
        }
        loop()
    }

    private func deliver(_ env: ServerEnvelope) {
        failures = 0
        isConnected = true
        handler?(env)
    }

    private func startHeartbeat() {
        heartbeatTask?.cancel()
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(25))
                guard let self, await self.isConnected else { continue }
                await self.sendHeartbeat()
            }
        }
    }

    private func markDisconnectedIfNeeded() { isConnected = false }

    private func reconnect(endpoint: URL) {
        guard !stopped else { return }
        isConnected = false
        let delay = backoff.delay(afterAttempts: failures)
        connectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self else { return }
            if await self.stopped { return }
            await self.open(endpoint: endpoint)
        }
    }
}
