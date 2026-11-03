# Decisions Log

Every non-trivial implementation decision, with rationale. Spec §0: decisions not
deferred to §13 are made here and logged.

## Owner-deferred defaults (spec §13) — placeholders, change in config

- **LLM models**: `fast` and `deep` profiles default to `gpt-4o-mini` / `gpt-4o` model
  *names* as placeholders against any OpenAI-compatible `base_url`. Owner sets real names.
- **Embeddings**: default `bge-small-en-v1.5` served by any OpenAI-compatible
  `/v1/embeddings` endpoint (`llm.embedding_base_url`, falls back to `llm.base_url`).
- **Web search**: self-hosted SearxNG (compose service included, `search.backend=searxng`).
- **Raw audio on server**: off by default (`storage.keep_raw_audio=false`); the protocol
  supports it later via an optional `audio_ref` field.

## Stack

- **Server Python 3.12, FastAPI, pydantic v2, httpx** — per spec. `pydantic-settings`
  for env + `config.yaml` overlay (env wins; secrets never in yaml).
- **No ORM.** Raw SQL + schema.sql migrations run at startup (versioned `user_version`
  pragma). A single-user app with a hand-designed bi-temporal schema does not benefit
  from an ORM's impedance; SQL keeps FTS5/vec features first-class.
- **SQLite WAL**, one writer connection guarded by an asyncio lock; readers share
  separate connections (WAL allows concurrent readers). Single container MVP.
- **sqlite-vec via the `sqlite-vec` PyPI wheel** (ships the loadable extension; no build
  step). At startup we attempt extension load + `vec0` virtual table; if unavailable we
  **fall back to brute-force cosine over float32 blobs** in Python over the candidate set
  (FTS5+entity+temporal union, bounded). Recall semantics identical; slower at >100k
  memories, irrelevant for single-user. Logged at startup.
- **iOS Swift 6, SwiftUI, iOS 18**; XcodeGen `project.yml` generates the .xcodeproj
  (checked-in project.yml is the source of truth; generated project is gitignored) so
  the project file never produces merge noise.
- **GRDB.swift** for iOS SQLite per spec (via SPM).
- **FluidAudio** (argmaxinc) for VAD/ASR/diarization per spec, consumed as an SPM
  dependency behind protocol seams (`VadEngine`, `AsrEngine`, `Diarizer`) so it can be
  swapped/mocked and the core compiles without it in tests.

## Architecture

- **AmbientCore Swift package** has zero UI imports; `AudioSource` and `CardSink`
  protocols defined up front (spec §3.7). FluidAudio conformance lives in a separate
  target `AmbientCoreFluidAudio` so `AmbientCore` tests run on any Mac without model files.
- **Whisper detection is client-side**: it needs raw RMS/spectral features from the audio
  tap; the phone classifies, flags `is_whisper`, and routes queries via `POST /v1/query`
  (server never sees whispered audio unless owner pins the result).
- **Server is the only place that stores memories.** The phone caches cards only
  (plus its local SQLite queue); no dual source of truth.
- **Conversation segmentation runs on the server** from utterance arrival timestamps
  (spec §5.1). The client's `conversation_hint` is honored as a strong hint; otherwise
  gap/location rules apply.
- **Idempotency by client `id`** (UUID) at utterance insert: `INSERT OR IGNORE` +
  accepted-ids response; revisions are a separate `utterance_revisions` table so live
  rows are never rewritten before extraction (extraction reads the latest revision).
- **Cards are server-generated**; the phone is a renderer + feedback logger. All score
  features travel with each card in `debug_features` so the replay harness and later
  logistic-regression weight fitting have data (spec §5.4.7).
- **LLM outputs are JSON**: JSON mode + pydantic validation + exactly one repair retry
  (second prompt includes the validation error), then hard failure → logged, skipped;
  extraction never blocks ingest (background queue).
- **Prompts in `server/prompts/*.md`** with `version:` front-matter, loaded at startup,
  hash stored with each extraction for reproducibility.

## Relevance engine (spec §5.4)

- Weights exactly as spec defaults, all in `config.yaml` under `relevance.*`.
- Gate: `score >= 0.55` AND per-memory cooldown (600 s) AND ≤ 3 live cards.
- `fast`-LLM judge on top-3 is behind `relevance.llm_judge_enabled` (default **false** —
  ships disabled, A/B-able in the replay harness).
- Question handling: `fast` LLM classifies question + addressee per utterance; answer-wait
  timer (6 s default) starts only for questions "addressed to nobody in particular"; if no
  other speaker talks in the window, the lookup agent runs. Timer runs server-side in an
  asyncio task.
- **Topic shift**: cosine between consecutive 60 s rolling-window embeddings < threshold
  (0.30) → expire unpinned cards, re-retrieve.

## Dates

- Relative-date resolver is deterministic code (no LLM) in `ambient_server/core/dates.py`,
  operating on the extractor's *original phrase* fields; the extractor's job is only to
  emit `time_phrase` + anchor. Cases tested: "next Friday", "the 14th", "in two weeks",
  "tomorrow night", bare weekday, "this weekend", ranges. Two-word noon/midnight handled;
  ambiguous "next <weekday>" resolves to the *upcoming* one strictly after the anchor
  (documented in code; the original phrase is always kept so it is never lossy).

## Privacy engineering (spec §6)

- Server binds nothing public; compose maps to LAN only, Tailscale is the transport.
  Bearer token required on **every** endpoint including WS (query token is rejected;
  header only) and `/v1/health` (so an unreachable vs locked-down server are distinguishable:
  unauthenticated → 401, not connection refused).
- Bcrypt of tokens not used: token is a 256-bit random secret compared in constant time.
- Export endpoint streams JSON + Markdown; delete cascades utterances→memories→vectors.

---

# Engine tuning decisions (post-harness, 2026-11-03)

## sqlite-vec metric portability

- Installed wheel sqlite-vec **v0.1.9 computes L2 and silently ignores
  `distance_metric=cosine`** (verified: cosine-declared vec0 table returned the L2
  distance for the same vectors). Rather than pin a newer build, `SqliteVecIndex` stores
  **L2-normalized** vectors and converts with `cos = 1 − d²/2` (monotone-equivalent on
  unit vectors). Works on every vec version, and matches the brute-force fallback exactly.
  Embedding services that already normalize (bge, nomic) pass through unchanged.

## Question etiquette (learned from eval fixture 11)

- The fast-LLM question classifier now runs **awaited, before scoring** for hint-matching
  utterances. When an answer-wait timer is armed, the relevance path skips that
  utterance entirely: an immediate card would preempt the humans in the room. If nobody
  answers within `question_wait_s`, the lookup path owns the card; another speaker
  answering cancels the timer AND the utterance exchange produces no card.
- Consequence: `question_wait_s` is etiquette, not just lookup timing — raise it in noisy
  rooms, lower it for solo use.

## Echo gate (beyond spec, big precision win)

- **A card must ADD remembered information.** If ≥ `relevance.echo_suppress` (default
  0.72) of a memory's CONTENT words (stopwords removed) were just spoken in the live
  window, the conversation is relaying the memory — surfacing it interrupts with zero
  new information. Hard-blocked before scoring (`gate echo_suppressed`).
- This is what the replay harness surfaced: without it the eval FP sources were all
  "card parroting the last sentence". Eval went P 0.70 → 1.00 with no recall loss.
- Fixture design follows the same rule: spoken topics pair with memories about
  consequences (kickoff, renewal, gear location), not restatements.

## Test/eval shared fake endpoint

- `ambient_server.testing` ships FakeLLM (MockTransport serving /chat/completions +
  /embeddings) + hashed bag-of-words embeddings + config factory. Unit tests and the
  replay harness drive the identical HTTP paths as real vLLM/Ollama.
- Replay harness installs a **simulated clock** (module-level `utc_now` patch) so window
  trimming, decay, cooldowns and conversation gaps follow fixture timestamps while wall
  time stays compressed (~1 s/fixture). Card capture covers BOTH paths: utterance return
  values and async timer broadcasts (utterance-path cards return to the caller; only
  timer cards go through `card_sink` → WS fan-out).

# iOS app (M1–M6 UI) — 2026-11-03
- **Project generation**: `ios/project.yml` (xcodegen) is the source of truth; `Ambient.xcodeproj` is generated and git-ignored. Swift 6 language mode, `SWIFT_STRICT_CONCURRENCY: complete`, iOS 18 minimum, `UIBackgroundModes: [audio, processing, fetch]`, mic usage string in Info.plist.
- **AppState is the only wiring point**: AmbientCore actors (OutboxStore, Uploader, EndpointRouter, LiveSocket, APIClient, CardHistory, SpeechGate, iOSAudioCapture, RollingAudioBuffer, WhisperDetector) connect through one @MainActor object; views never touch the package directly except Codable types.
- **ASR/diarization remain unbound by design.** Spec M0 spike (b) (FluidAudio under battery/thermal) requires a physical iPhone — cannot be honest from a Mac. The seam is `AppState.finalize(samples:start:end:)` (gated speech segments) and everything downstream (outbox → WS → server → cards back) is exercised on-device via the Debug screen's demo emitter, which uses the exact production path. Same for owner enrollment UI, pyannote offline refinement, and Live Activity/Dynamic Island (needs a widget target — next slice). Tracked as pending bindings in Debug UI.
- **Whisper routing is live logic** (WhisperDetector + configurable rms-ratio in Settings): owner + quiet+breathy → query path → priority card; trigger phrase "memory," is the documented fallback. Without ASR bound, whispered text can't be transcribed yet — the classifier gates and the query route is proven by the emitter's "flag as whisper query" toggle (routes through POST /v1/query like §5.5).
- **Server gaps found while binding UI** (fixed server-side first): `/v1/clarifications/digest` returns `{"questions": [...]}` (client DigestPage); conversations list/detail now have typed Codable models (`ConversationSummary`, `TranscriptLine`) instead of JSONValue soup.
- **Naming**: app screen `ConversationTimelineView` (SwiftUI owns `TimelineView`).

## Phone-first rebalance (owner directive, 2026-11-03) — supersedes §"the server is the only place that stores memories"

Owner instruction: *"Anything that can be done on the phone without any real tradeoffs,
do them there. Keep the server for the buffering stuff — to not lose an in-flight LLM
request. Storage on the phone. Use the existing SearxNG."*

- **Memory storage lives on the phone.** `MemoryStore` (ios/AmbientCore) is a full
  mirrored SQLite+FTS5 store fed by `GET /v1/memories/sync` (full-state swap; the
  server may prune, the phone keeps its archive — spec §5.3's never-delete enforced
  client-side). Phone-side recall (search, ranking, cards) works with no network.
- **Relevance scoring runs on the phone** for the live loop:
  `PhoneRelevanceEngine` + `RelevanceMath` port `core/scoring.py`, `core/gating.py`
  and the echo gate exactly. Parity is CI-enforced:
  `eval/export_scoring_fixtures.py` regenerates golden cases from the *server's*
  functions into `ios/.../Fixtures/scoring_parity.json`; `ScoringParityTests` asserts
  Swift matches to 1e-12 (43 cases: score grid, actionability branch corners, decay,
  jaccard, semantic, echo tokenizer, full gate ordering/cooldown/budget).
  The server still scores for WS push, batch replay, and query cards — the phone is
  an additional executor of the same tuned engine, not a different engine.
- **LLM extraction executes on the phone** (`TaskWorker`): the server renders prompts
  and hands them over `GET /v1/tasks/{id}/context` (prompts_hash guards stale claims);
  the phone calls its own OpenAI-compatible endpoints (`Settings → Phone-side LLM`),
  persists the raw result **before** POSTing (`task_results` buffer — inference
  survives app death and network loss), POSTs `{"raw_json": …}`, and the server applies
  the shared parse→persist path. Server-relayed repair (422 + pydantic detail) mirrors
  the gateway's single-retry contract. 409 = server already handled → discard.
  Give-up/network-starvation → `POST /v1/tasks/{id}/abandon` → the server's own
  fallback LLM pass runs (the original in-flight-request guarantee, inverted: the
  server is the safety net for the phone).
- **Server keeps**: durable ingest buffer (outbox drains into it), extraction queue +
  fallback timing, dedup/supersession/entity graph, cross-device cards, SearxNG
  web search (existing Unraid instance; `SEARCH_PROVIDER=searxng` + `SEARXNG_BASE_URL`,
  default `http://searxng:8080` on the compose network).
- Tradeoffs accepted: phone LLM endpoints are user-configured (local vLLM or hosted
  key in Settings); extraction latency on cellular depends on the chosen endpoint —
  the server fallback deadline covers silence; parity tests re-export is a manual
  step (fixture file freshness is asserted by the test's case count + CI regeneration
  note in the file header).
