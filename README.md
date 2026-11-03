# Ambient Memory

An always-on artificial memory. The iPhone listens in the background, transcribes and
attributes speech to speakers on-device, and turns conversations into structured
memories. During live conversation it surfaces related memories as glanceable cards —
**scored on the phone, against a phone-side memory store, with no server round-trip**.
The home server is the durable backbone: it buffers ingest, mirrors memories, owns the
cross-device record, and finishes any LLM work the phone couldn't reach. After
conversations, clarifying questions ("who is this speaker?", "what does this acronym
mean?") improve memory quality.

Single-user, self-hosted. All data stays on your devices and server.

```
iPhone (AmbientCore + SwiftUI app)
  mic → VAD → streaming ASR → diarization → utterance events
  local SQLite: outbox queue, FULL memory mirror (FTS5), task-result buffer,
                speaker profiles, card history
  phone-side: relevance scoring + gating (parity-tested against the server engine),
              extraction prompts executed via your own OpenAI-compatible endpoint
        │  WebSocket (live) + HTTPS batch (catch-up) + task queue  — Tailscale/LAN
        ▼
Home server (Docker, FastAPI, Python 3.12)
  Ingest buffer → extraction task queue (phone claims; server is the fallback LLM)
  Conversation segmenter → bi-temporal Memory store → /v1/memories/sync mirror
  Live Relevance Engine → cards pushed over WebSocket (also used for batch/query cards)
  Clarification generator → inbox;  Lookup agent (memory first, then SearxNG)
  Web search: bundled SearxNG container — no search API keys leave the house
```

The phone persists every LLM result **before** posting it, so an in-flight extraction
survives app death and network loss; the server takes the task back only if the phone
never reports in. Full rationale: `docs/DECISIONS.md` ("Phone-first rebalance").

## Repo layout

```
ios/            Xcode project + AmbientCore Swift package (UI-free, iOS+macOS)
server/         FastAPI app, prompts/, tests/
eval/           replay harness, fixtures, scoring-parity exporter (feeds Swift CI)
docs/           DECISIONS.md, API.md (generated from OpenAPI)
.github/        self-hosted CI (this Mac is the build machine)
docker-compose.yml   ambient + searxng
deploy/         unraid-up.sh (one-paste bring-up), searxng config
```

## Quick start (server / Unraid)

One paste on the Unraid terminal (needs a fine-grained PAT with Contents:Read, the repo
is private):

```sh
PAT=github_pat_xxx sh -c 'curl -s https://raw.githubusercontent.com/SpencerGraffunder/ambient-memory/main/deploy/unraid-up.sh | PAT=$PAT sh'
```

It clones to `/mnt/user/appdata/ambient-memory`, generates `AMBIENT_TOKEN` +
`SEARXNG_SECRET` into `.env`, builds, and starts the stack. The printed token goes into
the iPhone app's Settings. Re-run any time to update.

Generic Docker: `cp .env.example .env` (set `AMBIENT_TOKEN`), `docker compose up -d`.

Health check: `curl -H "Authorization: Bearer $AMBIENT_TOKEN" http://localhost:8378/v1/health`

## Quick start (iOS)

```bash
cd ios
xcodegen generate            # Ambient.xcodeproj from project.yml (source of truth)
open Ambient.xcodeproj       # run on device; set endpoints + token + phone LLM in Settings
swift test --package-path AmbientCore --scratch-path ~/Library/Caches/AmbientCoreBuild
```

The Debug screen's demo emitter pushes scripted utterances through the exact production
path (outbox → server → WS card back), and "Seed demo memories" proves **phone-side**
card scoring in airplane mode — all before the FluidAudio ASR binding (M0 spike) lands.

If your checkout lives on a network share (SMB), Swift builds need a local scratch path
(the flag above) or codesigning fails on AppleDouble xattrs.

The app uses [Tailscale iOS](https://tailscale.com/kb/1129/ios) to reach the server; it
tries an ordered endpoint list (MagicDNS name first, LAN IP fallback).

## CI (GitHub Actions, self-hosted on the Mac)

Runner `mac-ambient-memory` (labels `mac`, `ambient`) — registered via
`~/actions-runner-ambient-memory` (LaunchAgent, survives reboots).

- **Server Tests** — pytest + eval replay + scoring-parity fixture drift-guard, on every
  server push (runs free: this is your hardware).
- **iOS Build** — `swift test` + xcodegen + simulator build + `.app` artifact;
  tag `v*` or manual dispatch (it is a laptop; builds are kept off plain pushes).

Scoring parity means: `eval/export_scoring_fixtures.py` generates golden inputs/outputs
from the *server's* Python scoring/gating/echo functions into the Swift test fixtures;
the Swift suite asserts 1e-12 agreement. Tune the engine on either side and CI tells
you when the other drifted.

## Tests & eval

```bash
cd server && .venv/bin/python -m pytest          # 112 tests
cd ../eval && python replay/run.py               # 12/12, P=1.00 R=1.00, 3.15 cards/h
cd ../ios/AmbientCore && swift test --scratch-path ~/Library/Caches/AmbientCoreBuild  # 55 tests
```

See `docs/DECISIONS.md` for every implementation decision and its rationale, and
`docs/API.md` for the generated HTTP/WS API reference.
