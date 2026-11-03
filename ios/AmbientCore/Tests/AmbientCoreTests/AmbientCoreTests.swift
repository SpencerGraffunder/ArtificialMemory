import XCTest
@testable import AmbientCore

// Spec §12 M2: unit tests for the Swift package — wire formats, outbox durability and
// backoff, whisper detector, listening state machine, rolling buffer eviction,
// speaker matching, vocabulary merge, card budget. Deterministic: no mic, no network.

private func makeEvent(_ id: String, text: String = "t", revision: Int = 0)
    -> UtteranceEvent {
    UtteranceEvent(id: id, conversationHint: "home", revision: revision,
                   speakerId: "spk-1", speakerLabel: "Owner", text: text)
}

private func makeCard(_ id: String, priority: Bool = false, memoryId: String? = nil)
    -> Card {
    Card(id: id, kind: .relevance, memoryId: memoryId, headline: id, detail: "detail",
         sourceChip: "because", origin: .memory, url: nil, score: 1.0, priority: priority,
         sourceUtteranceIds: [], expiresAt: nil, debugFeatures: [:],
         createdAt: AmbientJSONDate.isoNow())
}

final class WireModelsTests: XCTestCase {
    func testUtteranceEventRoundTripUsesSnakeCase() throws {
        let e = UtteranceEvent(id: "u2", conversationHint: "car", speakerId: "spk-2",
                               speakerLabel: "Speaker-2", text: "quiet words",
                               asrConfidence: 0.91)
        let data = try AmbientJSON.encoder(e)
        let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertNotNil(obj["conversation_hint"], "wire is snake_case (spec §4.1)")
        XCTAssertNotNil(obj["asr_confidence"])
        XCTAssertNil(obj["asrConfidence"])
        let back = try AmbientJSON.decoder(UtteranceEvent.self, from: data)
        XCTAssertEqual(back, e)
    }

    func testUtteranceEventDecodesServerSnakeCase() throws {
        let json = """
        {"id":"u1","conversation_hint":"home-kitchen","revision":0,
         "start_ts":"2026-11-03T10:00:00Z","end_ts":null,"speaker_id":"spk-1",
         "speaker_label":"Speaker-1","speaker_confidence":0.4,"text":"I leave the door locked",
         "asr_confidence":0.93,"is_whisper":false,"location":null,"device":"iphone"}
        """.data(using: .utf8)!
        let e = try AmbientJSON.decoder(UtteranceEvent.self, from: json)
        XCTAssertEqual(e.conversationHint, "home-kitchen")
        XCTAssertEqual(e.asrConfidence, 0.93)
        XCTAssertFalse(e.isWhisper)
    }

    func testCardDecodesAndRoundTrips() throws {
        let json = """
        {"id":"c1","kind":"lookup","memory_id":"m1","headline":"Maya liked it",
         "detail":"said she liked the podcast","source_chip":"Maya, 3 days ago",
         "origin":"memory","url":null,"score":1.42,"priority":true,
         "source_utterance_ids":["u5"],"expires_at":null,"debug_features":{"semantic":0.6},
         "created_at":"2026-11-03T09:00:00Z"}
        """.data(using: .utf8)!
        let card = try AmbientJSON.decoder(Card.self, from: json)
        XCTAssertEqual(card.memoryId, "m1")
        XCTAssertTrue(card.priority)
        let re = try AmbientJSON.decoder(Card.self, from: try AmbientJSON.encoder(card))
        XCTAssertEqual(re, card)
    }

    func testFeedbackMessageShapeMatchesServer() throws {
        let fb = FeedbackMessage(cardId: "c9", kind: .thumbUp)
        let data = try AmbientJSON.encoder(fb)
        let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(obj["card_id"] as? String, "c9")
        XCTAssertEqual(obj["kind"] as? String, "thumb_up")
        XCTAssertNotNil(obj["at"] as? String)
    }

    func testServerEnvelopeDecoding() throws {
        let cardJSON = """
        {"type":"card","card":{"id":"c1","kind":"relevance","memory_id":null,
         "headline":"h","detail":"d","source_chip":"s","origin":"memory","url":null,
         "score":0.7,"priority":false,"source_utterance_ids":[],"expires_at":null,
         "debug_features":{},"created_at":"2026-11-03T09:00:00Z"},"ts":"2026-11-03T09:00:00Z"}
        """.data(using: .utf8)!
        let env = try AmbientJSON.decoder(ServerEnvelope.self, from: cardJSON)
        switch env {
        case .card(let c): XCTAssertEqual(c.id, "c1")
        default: XCTFail("expected card envelope, got \(env)")
        }
        let dismiss = #"{"type":"card_dismiss","card_id":"c1"}"#.data(using: .utf8)!
        if case .cardDismiss(let id) = try AmbientJSON.decoder(ServerEnvelope.self,
                                                               from: dismiss) {
            XCTAssertEqual(id, "c1")
        } else { XCTFail("card_dismiss decoding") }
    }

    func testClientEnvelopeEncoding() throws {
        let data = try AmbientJSON.encoder(
            ClientEnvelope.feedback(FeedbackMessage(cardId: "c2", kind: .dismiss)))
        let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(obj["type"] as? String, "feedback")
        let payload = obj["feedback"] as? [String: Any]
        XCTAssertEqual(payload?["kind"] as? String, "dismiss")
        XCTAssertNotNil(obj["ts"], "client envelopes carry ts for server clocks")
    }
}

final class BackoffRouterTests: XCTestCase {
    func testExponentialGrowthAndCap() {
        let b = BackoffConfig(base: 1, max: 300)
        XCTAssertEqual(b.delay(afterAttempts: 1), 1, accuracy: 0.001)
        XCTAssertEqual(b.delay(afterAttempts: 2), 2, accuracy: 0.001)
        XCTAssertEqual(b.delay(afterAttempts: 9), 256, accuracy: 0.001)
        XCTAssertEqual(b.delay(afterAttempts: 30), 300, accuracy: 0.001,
                       "cap at max — spec: backoff to 5 min")
    }

    func testFailoverRotationAndLastGood() async throws {
        let a = URL(string: "http://a.local:8000")!
        let b = URL(string: "http://b.local:8000")!
        let c = URL(string: "http://tailscale-ip:8000")!
        let r = EndpointRouter(endpoints: [a, b, c])
        let start = await r.current()
        XCTAssertEqual(start, a)
        let next = await r.markFailure(a)
        XCTAssertEqual(next, b, "failover rotates to next endpoint (spec §3.4)")
        await r.markSuccess(c)
        let good = await r.current()
        XCTAssertEqual(good, c, "last good endpoint remembered")
    }
}

final class OutboxTests: XCTestCase {
    private func tempPath() -> String {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("outbox-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("outbox.sqlite").path
    }

    func testFifoOrderingByIdentity() async throws {
        let store = try OutboxStore(path: tempPath())
        for id in ["c", "a", "b"] { try await store.append(makeEvent(id)) }
        let batch = try await store.pendingBatch()
        XCTAssertEqual(batch.map(\.id), ["c", "a", "b"],
                       "insertion order preserved regardless of id sort")
    }

    func testDuplicateSuppressionAndRevisionSupersede() async throws {
        let store = try OutboxStore(path: tempPath())
        let first = try await store.append(makeEvent("u", text: "one"))
        XCTAssertTrue(first)
        let dupe = try await store.append(makeEvent("u", text: "one"))
        XCTAssertFalse(dupe, "same id + revision = duplicate, ignored")
        let older = try await store.append(makeEvent("u", text: "older", revision: 0))
        XCTAssertFalse(older)
        let rev = try await store.append(makeEvent("u", text: "fixed", revision: 1))
        XCTAssertTrue(rev, "higher revision supersedes (spec §3.2.4)")
        let batch = try await store.pendingBatch()
        XCTAssertEqual(batch.count, 1)
        XCTAssertEqual(batch.first?.event.text, "fixed")
    }

    func testPerEntryBackoffSchedule() async throws {
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        var cfg = OutboxConfig()
        cfg.backoff = BackoffConfig(base: 2, max: 60)
        let store = try OutboxStore(path: tempPath(), config: cfg)
        try await store.append(makeEvent("u1"))
        try await store.scheduleRetry(ids: ["u1"], now: t0)   // 1st failure → +2 s
        let tooEarly = try await store.pendingBatch(now: t0.addingTimeInterval(1.9))
        XCTAssertTrue(tooEarly.isEmpty, "not due before backoff elapses")
        let due = try await store.pendingBatch(now: t0.addingTimeInterval(2.1))
        XCTAssertEqual(due.map(\.id), ["u1"])
        try await store.scheduleRetry(ids: ["u1"], now: t0.addingTimeInterval(2.1))
        let later = try await store.pendingBatch(now: t0.addingTimeInterval(5.9))
        XCTAssertTrue(later.isEmpty, "2nd failure backs off 4 s")
        let finallyDue = try await store.pendingBatch(now: t0.addingTimeInterval(6.1))
        XCTAssertEqual(finallyDue.count, 1)
    }

    func testSentRemovesUnknownIdsIgnored() async throws {
        let store = try OutboxStore(path: tempPath())
        try await store.append(makeEvent("x"))
        try await store.append(makeEvent("y"))
        try await store.markSent(ids: ["x", "ghost"])
        let n = try await store.pendingCount()
        XCTAssertEqual(n, 1)
    }

    func testDiskCapDropsOldestWithAlerts() async throws {
        var cfg = OutboxConfig()
        cfg.maxBytes = 6_000
        let store = try OutboxStore(path: tempPath(), config: cfg)
        let words = String(repeating: "word ", count: 120)
        for i in 0..<30 { try await store.append(makeEvent("u\(i)", text: words)) }
        let dropped = try await store.enforceCapacity()
        XCTAssertGreaterThan(dropped, 0, "cap exceeded → drops happen")
        let alerts = await store.dropAlerts
        XCTAssertEqual(alerts.count, dropped, "every drop alerts the owner (spec §3.4)")
        let remaining = try await store.allEntries()
        XCTAssertEqual(remaining.map(\.id).first, "u\(30 - remaining.count)",
                       "oldest dropped first")
        let bytes = try await store.totalBytes()
        XCTAssertLessThanOrEqual(bytes, 6_000)
    }

    func testSurvivesRelaunch() async throws {
        let path = tempPath()
        let s1 = try OutboxStore(path: path)
        try await s1.append(makeEvent("keep"))
        // simulate app relaunch: fresh store on the same file (kill -9 safe: WAL)
        let s2 = try OutboxStore(path: path)
        let batch = try await s2.pendingBatch()
        XCTAssertEqual(batch.map(\.id), ["keep"], "zero data loss across kill/reboot")
    }

    func testUploaderDrainsRetriesAndRotates() async throws {
        final class ScriptedTransport: IngestTransport, @unchecked Sendable {
            private(set) var sent: [[UtteranceEvent]] = []
            var failuresLeft = 0
            func send(_ batch: [UtteranceEvent], endpoint: URL) async throws
                -> IngestBatchResponse {
                sent.append(batch)
                if failuresLeft > 0 {
                    failuresLeft -= 1
                    throw URLError(.cannotConnectToHost)
                }
                return IngestBatchResponse(acceptedIds: batch.map(\.id), duplicateIds: [],
                                           serverTime: AmbientJSONDate.isoNow())
            }
        }
        let t = ScriptedTransport()
        t.failuresLeft = 1

        let store = try OutboxStore(path: tempPath())
        try await store.append(makeEvent("k1"))
        let a = URL(string: "http://a")!, b = URL(string: "http://b")!
        let router = EndpointRouter(endpoints: [a, b])
        let up = Uploader(outbox: store, transport: t, router: router)

        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        let first = try await up.drainOnce(now: t0)
        XCTAssertEqual(first.last?.sent, 0)
        XCTAssertEqual(first.last?.endpointRotated, true)
        let queued = try await store.pendingCount()
        XCTAssertEqual(queued, 1, "failed send stays queued for retry")

        _ = try await up.drainOnce(now: t0.addingTimeInterval(300))
        let drained = try await store.pendingCount()
        XCTAssertEqual(drained, 0, "outbox drained after server confirms ids")
        XCTAssertEqual(t.sent.count, 2)
        let cur = await router.current()
        XCTAssertEqual(cur, b,
                       "rotates on failure; stays on the endpoint that worked")
    }

    func testPartialConfirmationRetunesUnconfirmed() async throws {
        final class SubsetTransport: IngestTransport, @unchecked Sendable {
            func send(_ batch: [UtteranceEvent], endpoint: URL) async throws
                -> IngestBatchResponse {
                // server crashed mid-batch: confirms only the first id
                IngestBatchResponse(acceptedIds: [batch[0].id], duplicateIds: [],
                                    serverTime: AmbientJSONDate.isoNow())
            }
        }
        let store = try OutboxStore(path: tempPath())
        try await store.append(makeEvent("s1"))
        try await store.append(makeEvent("s2"))
        let up = Uploader(outbox: store, transport: SubsetTransport(),
                          router: EndpointRouter(endpoints: [URL(string: "http://a")!]))
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        let report = try await up.drainOnce(now: t0)
        XCTAssertEqual(report.last?.sent, 1)
        XCTAssertEqual(report.last?.rescheduled, 1)
        let remaining = try await store.pendingBatch(now: t0.addingTimeInterval(10_000))
        XCTAssertEqual(remaining.map(\.id), ["s2"], "confirmed entry leaves, rest retries")
    }
}

final class WhisperAndStateMachineTests: XCTestCase {
    /// square-ish wave: high zero-crossing rate (breathy), amplitude `amp`
    private func breathy(amp: Float, frames: Int = 8, samplesPer: Int = 160) -> [[Float]] {
        (0..<frames).map { _ in
            (0..<samplesPer).map { ($0 / 2) % 2 == 0 ? amp : -amp }
        }
    }

    func testQuietBreathyDetectedAsWhisper() {
        let d = WhisperDetector(speakingRms: 0.08)
        XCTAssertTrue(d.isWhisper(samplesPerFrame: breathy(amp: 0.003), isOwner: true),
                      "quiet + breathy + owner = whisper-to-self")
        XCTAssertFalse(d.isWhisper(samplesPerFrame: breathy(amp: 0.09), isOwner: true),
                       "normal volume is not whisper")
        XCTAssertFalse(d.isWhisper(samplesPerFrame: breathy(amp: 0.003), isOwner: false),
                       "a guest leaning in is conversation content, not a query")
        XCTAssertFalse(d.isWhisper(samplesPerFrame: breathy(amp: 0.003, frames: 2),
                                   isOwner: true), "too short to judge")
    }

    func testTriggerPhraseFallback() {
        let d = WhisperDetector()
        XCTAssertEqual(d.matchesTriggerPhrase("Memory, when did Maya last say she liked it?"),
                       "when did maya last say she liked it?")
        XCTAssertNil(d.matchesTriggerPhrase("memory in general is great"),
                     "trigger must be a prefix phrase, not any mention")
    }

    func testStateMachineFullDay() {
        var m = ListeningMachine()
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertEqual(m.handle(.startRequested, now: t0), [.startEngine])
        XCTAssertEqual(m.state, .listening)

        // incoming call for 45 s
        XCTAssertEqual(m.handle(.interruptionBegan, now: t0), [.stopEngine])
        XCTAssertEqual(m.state, .interrupted)
        let ended = m.handle(.interruptionEnded, now: t0.addingTimeInterval(45))
        XCTAssertEqual(m.state, .listening)
        XCTAssertEqual(ended, [.markGap(startedApproximately: t0), .startEngine],
                       "long interruption marks the timeline gap and resumes")

        // short blip: no gap
        _ = m.handle(.interruptionBegan, now: t0.addingTimeInterval(60))
        let quick = m.handle(.interruptionEnded, now: t0.addingTimeInterval(60.5))
        XCTAssertFalse(quick.contains { if case .markGap = $0 { return true }
                                         return false }, "0.5 s blip is not a gap")

        // engine dies (media services reset) → bounded backoff retries 1,2,5,10,30
        let died = m.handle(.engineDied, now: t0.addingTimeInterval(120))
        XCTAssertEqual(m.state, .error)
        XCTAssertEqual(died.first, .scheduleRetry(after: 1))
        let diedAgain = m.handle(.interruptionEnded, now: t0.addingTimeInterval(121.5))
        XCTAssertEqual(diedAgain.first, .scheduleRetry(after: 2))
        _ = m.handle(.recovered, now: t0.addingTimeInterval(124))
        XCTAssertEqual(m.state, .listening)

        // user pause/resume
        XCTAssertEqual(m.handle(.userPaused, now: t0), [.stopEngine])
        XCTAssertEqual(m.state, .paused)
        XCTAssertEqual(m.handle(.userResumed, now: t0), [.startEngine])
        XCTAssertEqual(m.state, .listening)

        // spurious duplicate notification: inert
        XCTAssertEqual(m.handle(.interruptionEnded, now: t0), [])
    }
}

final class RollingBufferAndProfilesTests: XCTestCase {
    func testEvictionRespectsRetentionWindow() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let chunks = [(id: "old", endedAt: now.addingTimeInterval(-700)),
                      (id: "fresh", endedAt: now.addingTimeInterval(-5))]
        XCTAssertEqual(audioEvictions(chunks: chunks, now: now, retention: 600), ["old"],
                       "10-min default window (spec §3.2.8)")
        XCTAssertEqual(audioEvictions(chunks: chunks, now: now, retention: 3600), [])
    }

    func testWavAssembly() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("buf-\(UUID().uuidString)")
        var cfg = RollingAudioConfig()
        cfg.chunkSeconds = 1
        let buf = try RollingAudioBuffer(directory: dir, config: cfg)
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        try await buf.append([Float](repeating: 0.5, count: 16_000), at: t0)
        let wav = try await buf.wav(from: t0.addingTimeInterval(-1),
                                    to: t0.addingTimeInterval(1))
        XCTAssertEqual(wav.count, 44 + 16_000 * 2, "RIFF header + int16 mono PCM")
        XCTAssertEqual(String(data: wav.prefix(4), encoding: .ascii), "RIFF")
        XCTAssertEqual(String(data: wav[8..<12], encoding: .ascii), "WAVE")
    }

    func testBufferSweepsDuringAppend() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("buf-\(UUID().uuidString)")
        var cfg = RollingAudioConfig()
        cfg.retentionSeconds = 1
        let buf = try RollingAudioBuffer(directory: dir, config: cfg)
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        try await buf.append([Float](repeating: 0.1, count: 100), at: t0)
        try await buf.append([Float](repeating: 0.1, count: 100),
                             at: t0.addingTimeInterval(10))
        let left = try await buf.chunkCount()
        XCTAssertEqual(left, 1, "old chunk deleted on write (delete as soon as past window)")
    }

    func testSpeakerMatchThresholdAndProvisionalNames() async throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("spk-\(UUID().uuidString)")
            .appendingPathComponent("db.sqlite").path
        let profiles = try SpeakerProfiles(path: path, matchThreshold: 0.62)
        let alice: [Float] = [1, 0, 0, 0, 0, 0, 0, 0]
        let aliceSample: [Float] = [0.95, 0.31, 0, 0, 0, 0, 0, 0]   // cos ≈ 0.95
        let bob: [Float] = [0, 0, 0, 0, 1, 0, 0, 0]                 // cos = 0

        let created = try await profiles.createProvisional(embedding: alice)
        XCTAssertEqual(created.label, "Speaker-1")
        let hit = try await profiles.match(aliceSample)
        XCTAssertEqual(hit?.id, created.id, "same voice matches above threshold")
        let miss = try await profiles.match(bob)
        XCTAssertNil(miss, "different voice below threshold → provisional")
        let second = try await profiles.createProvisional(embedding: bob)
        XCTAssertEqual(second.label, "Speaker-2", "provisional names increment")

        try await profiles.rename(id: created.id, to: "Alice")
        let all = try await profiles.all()
        XCTAssertEqual(all.first(where: { $0.id == created.id })?.displayName, "Alice")

        // merge provisional into named (spec §7)
        try await profiles.merge(from: second.id, into: created.id)
        let afterMerge = try await profiles.all()
        XCTAssertEqual(afterMerge.count, 1, "source profile disappears")
        await profiles.setMatchThreshold(0.4)
        let broad = try await profiles.match(bob)
        XCTAssertNotNil(broad, "merged profile covers both voices")
    }

    func testOwnerEnrollmentUnique() async throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("spk-\(UUID().uuidString)")
            .appendingPathComponent("db.sqlite").path
        let profiles = try SpeakerProfiles(path: path)
        let first = try await profiles.enrollOwner(embedding: [1, 0, 0, 0])
        let second = try await profiles.enrollOwner(embedding: [0.9, 0.1, 0, 0])
        XCTAssertEqual(first.id, second.id, "re-enrollment folds into the owner profile")
        let owner = try await profiles.owner()
        XCTAssertEqual(owner?.isOwner, true)
    }

    func testCosineBasics() {
        XCTAssertEqual(cosineSimilarity([1, 0], [1, 0]), 1, accuracy: 1e-9)
        XCTAssertEqual(cosineSimilarity([1, 0], [0, 1]), 0, accuracy: 1e-9)
        XCTAssertEqual(cosineSimilarity([1, 0], [-1, 0]), -1, accuracy: 1e-9)
        XCTAssertEqual(cosineSimilarity([], []), 0)
        XCTAssertEqual(cosineSimilarity([1, 2], [1]), 0, "dim mismatch → 0")
    }
}

final class VocabularyHistoryPipelineTests: XCTestCase {
    private func tmp(_ name: String) -> String {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("v-\(UUID().uuidString)")
            .appendingPathComponent(name).path
    }

    func testVocabularySyncKeepsLocalBoostsFirst() async throws {
        let store = try VocabularyStore(path: tmp("voc.sqlite"))
        try await store.syncFromServer([
            VocabularyEntry(term: "ARTES", kind: "acronym", definition: "project",
                            entity_id: nil, source: nil, boost: nil),
            VocabularyEntry(term: "Jasmine", kind: "name", definition: nil,
                            entity_id: nil, source: nil, boost: nil),
        ])
        try await store.addLocal(term: "Duffel")
        try await store.syncFromServer([
            VocabularyEntry(term: "SearxNG", kind: "acronym", definition: nil,
                            entity_id: nil, source: nil, boost: nil)])   // resync
        let terms = try await store.boostTerms()
        XCTAssertEqual(terms.first, "duffel", "owner-local terms first")
        XCTAssertFalse(terms.contains("jasmine"), "server snapshot replaced on sync")
        XCTAssertTrue(terms.contains("searxng"))
        XCTAssertEqual(Set(terms).count, terms.count, "deduped")
    }

    func testCardHistoryDismissCount() async throws {
        let hist = try CardHistory(path: tmp("hist.sqlite"))
        let card = makeCard("c1", memoryId: "m1")
        try await hist.recordShown(card: card)
        try await hist.recordFeedback(card: card, kind: .dismiss)
        try await hist.recordFeedback(card: card, kind: .dismiss)
        let n = try await hist.dismissCount(memoryId: "m1")
        XCTAssertEqual(n, 2, "dismissed_before feature source (spec §5.4)")
        let recent = try await hist.recent(limit: 10)
        XCTAssertEqual(recent.count, 3)
    }

    func testOutboxSinkQueuesBeforeSending() async throws {
        let store = try OutboxStore(path: tmp("sink.sqlite"))
        let sink = OutboxSink(outbox: store, socket: nil)
        await sink.ingest(makeEvent("u9", text: "hello"))
        await sink.ingest(UtteranceEvent(id: "u9", conversationHint: "home",
                                         revision: 1, speakerId: "spk-1",
                                         speakerLabel: "Owner", text: "hello!"))
        let batch = try await store.pendingBatch()
        XCTAssertEqual(batch.count, 1)
        XCTAssertEqual(batch[0].event.text, "hello!",
                       "durability first: live path is best-effort, queue is truth")
    }

    func testCardStackBudgetThree() async throws {
        let sink = RecordingCardSink()
        await sink.show(makeCard("a")); await sink.show(makeCard("b"))
        await sink.show(makeCard("c")); await sink.show(makeCard("d"))
        var visible = await sink.visible
        XCTAssertEqual(visible.map(\.id), ["b", "c", "d"], "max 3, oldest evicted")
        await sink.show(makeCard("urgent", priority: true))
        visible = await sink.visible
        XCTAssertEqual(visible.map(\.id), ["urgent", "c", "d"],
                       "priority enters at front; budget (spec §5.4) still 3")
        await sink.dismiss(cardId: "c")
        visible = await sink.visible
        XCTAssertEqual(visible.map(\.id), ["urgent", "d"])
        let log = await sink.dismissedIds
        XCTAssertEqual(log, ["c"])
    }

    func testFakePipelineStreamsUtterances() async throws {
        let pipe = FakeSpeechPipeline()
        try await pipe.start(source: FakeAudioSource())
        // AsyncStream buffers unbounded: emit first, collect after (deterministic)
        await pipe.emit(makeEvent("1", text: "one"))
        await pipe.emit(makeEvent("2", text: "two"))
        let got = await withTaskGroup(of: [String].self) { group in
            group.addTask {
                var r: [String] = []
                for await u in pipe.utterances {
                    r.append(u.text)
                    if r.count == 2 { return r }
                }
                return r
            }
            group.addTask {
                try? await Task.sleep(for: .seconds(3))
                return []   // watchdog
            }
            let first = await group.next() ?? []
            group.cancelAll()
            return first
        }
        XCTAssertEqual(got, ["one", "two"])
        await pipe.stop()
    }
}

final class SpeechGateTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)
    private func voice(_ n: Int) -> [[Float]] {
        Array(repeating: (0..<1_600).map { ($0 / 2) % 2 == 0 ? Float(0.05) : -0.05 },
              count: n)                                   // 0.1 s frames @16 kHz
    }
    private func quiet(_ n: Int) -> [[Float]] {
        Array(repeating: [Float](repeating: 0, count: 1_600), count: n)
    }
    private func run(_ gate: inout SpeechGate, _ frames: [[Float]], from: Date)
        -> [GateEvent] {
        var out: [GateEvent] = []
        for (i, f) in frames.enumerated() {
            out.append(contentsOf: gate.ingest(f, at: from.addingTimeInterval(
                Double(i) * 0.1)))
        }
        return out
    }

    func testEndpointingHangoverAndShortCoughDrop() {
        var gate = SpeechGate()
        // cough: 0.2 s then silence → nothing finalizes (min utterance)
        var events = run(&gate, quiet(2) + voice(2) + quiet(20), from: t0)
        XCTAssertFalse(events.contains { if case .utteranceEnd = $0 { return true }
                                         return false })

        // speech, 0.3 s pause (kept inside utterance), more speech, long silence
        gate.reset()
        events = run(&gate, voice(10) + quiet(3) + voice(5) + quiet(10), from: t0)
        let ends = events.compactMap { e -> (Date, Date)? in
            if case .utteranceEnd(let s, let e, _) = e { return (s, e) }
            return nil
        }
        XCTAssertEqual(ends.count, 1, "mid-pause must NOT endpoint")
        XCTAssertEqual(ends[0].0.timeIntervalSince(t0), 0, accuracy: 0.05)
        XCTAssertEqual(ends[0].1.timeIntervalSince(t0), 1.8, accuracy: 0.11,
                       "end = where silence began, not where endpoint fired")
    }

    func testMaxUtteranceCapAndFlush() {
        var gate = SpeechGate()
        let events = run(&gate, voice(320), from: t0)   // 32 s nonstop
        let ends = events.compactMap { e -> Date? in
            if case .utteranceEnd(_, let e, _) = e { return e }
            return nil
        }
        XCTAssertEqual(ends.count, 1, "ASR window cap force-ends long speech")
        XCTAssertEqual(ends[0].timeIntervalSince(t0), 30, accuracy: 0.15)

        gate.reset()
        _ = run(&gate, voice(10), from: t0)
        let flushed = gate.flush(at: t0.addingTimeInterval(1.0))
        if case .utteranceEnd(let s, let e, _) = flushed.first! {
            XCTAssertEqual(s, t0)
            XCTAssertEqual(e.timeIntervalSince(t0), 1.0, accuracy: 0.001)
        } else { XCTFail("flush must finalize the in-flight utterance") }
    }

    func testSilenceEventsForQuietWindowTimer() {
        var gate = SpeechGate()
        let events = run(&gate, quiet(5), from: t0)
        let silences = events.filter { if case .silence = $0 { return true }; return false }
        XCTAssertEqual(silences.count, 5, "server silence-window timer needs client beats")
    }
}
