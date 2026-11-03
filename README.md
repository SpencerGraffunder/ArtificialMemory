# Ambient Memory

An always-on artificial memory. The iPhone listens in the background, transcribes and
attributes speech to speakers on-device, and streams transcript segments to a self-hosted
server. The server's LLM turns conversations into structured memories, and during live
conversation surfaces related memories as glanceable cards on the phone. After
conversations, it asks clarifying questions ("who is this speaker?", "what does this
acronym mean?") to improve memory quality.

Single-user, self-hosted. All data stays on your devices and server.

```
iPhone (AmbientCore + SwiftUI app)
  mic → VAD → streaming ASR → streaming diarization → utterance events
  local SQLite: outbox queue, speaker profiles, card history
        │  WebSocket (live) + HTTPS batch (catch-up)  over Tailscale or LAN
        ▼
Home server (Docker, FastAPI, Python 3.12)
  Ingest → Conversation segmenter → Extractor (LLM) → bi-temporal Memory store
  Live Relevance Engine (fast path) → cards pushed over WebSocket
  Clarification generator → inbox + digest notification
  Lookup agent (memory first, then web search) for unanswered questions
  LLM gateway: any OpenAI-compatible endpoint (vLLM / llama.cpp / Ollama)
```

## Repo layout

```
ios/            Xcode project + AmbientCore Swift package
server/         FastAPI app, prompts/, tests/
eval/           replay harness, fixture transcripts, scoring scripts
docs/           DECISIONS.md, API.md (generated from OpenAPI)
docker-compose.yml
```

## Quick start (server)

```bash
cd server
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml   # set llm.base_url / model / api_key
uvicorn ambient_server.main:app --host 0.0.0.0 --port 8378
```

Or with Docker on the home server:

```bash
cp .env.example .env                  # AMBIENT_TOKEN=...
docker compose up -d
```

Health check: `curl -H "Authorization: Bearer $AMBIENT_TOKEN" http://localhost:8378/v1/health`

## Quick start (iOS)

```bash
cd ios
xcodegen generate            # Ambient.xcodeproj from project.yml (project.yml is the source of truth)
open Ambient.xcodeproj       # run on a physical device; set endpoints + token in Settings
swift test --package-path AmbientCore   # 33 core-logic tests, no UI
```

The core package (`ios/AmbientCore`) is UI-free (compiles for iOS and macOS) — the app
consumes its `AudioSource` / `CardSink` protocols. The Debug screen has a demo
utterance emitter that pushes through the real outbox → WebSocket → card path, so the
full loop is testable on a device before the FluidAudio ASR binding (M0 spike) lands.

The core package (`ios/AmbientCore`) is UI-free and compiles for iOS and macOS — the
SwiftUI app target (card stack, settings, browse) consumes its `AudioSource` /
`CardSink` protocols and lands with milestone M3.

If your checkout lives on a network share (SMB), Swift builds must run with a local
scratch path or codesigning fails on AppleDouble xattrs:
`swift test --scratch-path ~/Library/Caches/AmbientCoreBuild`.

The app relies on the [Tailscale iOS app](https://tailscale.com/kb/1129/ios) being
connected to reach the home server; it tries an ordered list of endpoints (MagicDNS name
first, LAN IP fallback).

## Tests & eval

```bash
cd server && .venv/bin/python -m pytest          # unit + API tests
cd ../eval && python replay/run.py               # replay fixtures: precision/recall/card-rate
```

Current eval: **12/12 fixtures pass · precision 1.00 · recall 1.00 · 3.15 cards/hour**
(spec target 1–4/h). Details: `eval/README.md`.

See `docs/DECISIONS.md` for every implementation decision and its rationale, and
`docs/API.md` for the generated HTTP/WS API reference.
