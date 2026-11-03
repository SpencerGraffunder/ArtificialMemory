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
xcodegen generate          # produces Ambient.xcodeproj from project.yml
open Ambient.xcodeproj     # run on a physical device; set server endpoint + token in Settings
swift test --package-path Packages/AmbientCore   # core logic tests, no UI
```

The app relies on the [Tailscale iOS app](https://tailscale.com/kb/1129/ios) being
connected to reach the home server; it tries an ordered list of endpoints (MagicDNS name
first, LAN IP fallback).

## Tests & eval

```bash
cd server && pytest                          # unit + API tests
cd ../eval && python -m replay.run --all     # replay fixture transcripts, print card eval report
```

See `docs/DECISIONS.md` for every implementation decision and its rationale, and
`docs/API.md` for the generated HTTP/WS API reference.
