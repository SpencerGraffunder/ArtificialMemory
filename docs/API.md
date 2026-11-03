# Ambient Memory — Server API

Base URL: `http://<home-server>:8378` · Machine-readable spec: [`openapi.json`](openapi.json) (served live at `/openapi.json`).

## Auth

Every route — including `/v1/health` and the WebSocket — requires:

```
Authorization: Bearer $AMBIENT_TOKEN
```

- Missing/invalid token → `401`.
- Server started without `AMBIENT_TOKEN` → `503` on all routes (fail-closed).
- WebSocket with a bad token closes with code `4401`.

## Ingestion (phone → server)

### `POST /v1/ingest/batch`
Catch-up upload after offline periods (the iOS outbox drains through this). Body:

```json
{"utterances": [ {UtteranceEvent}, … ]}
```

Idempotent per `id` (duplicates are answered, not re-inserted); a higher `revision`
supersedes. Response:

```json
{"accepted_ids": ["u1"], "duplicate_ids": ["u0"], "server_time": "…ISO…"}
```

The client deletes entries from its outbox **only** after their ids appear in
`accepted_ids ∪ duplicate_ids`.

`UtteranceEvent` fields (snake_case on the wire):
`id` (UUID, client-generated), `conversation_hint?`, `revision` (int, 0 = original),
`start_ts`, `end_ts?` (ISO-8601), `speaker_id`, `speaker_label`, `speaker_confidence`,
`text`, `asr_confidence?`, `is_whisper`, `location? {lat, lon, accuracy_m}`, `device`.

### `WebSocket /v1/live`
Real-time channel — the phone sends finalized utterances as they happen, the server
pushes cards the moment they are born. All messages are JSON envelopes with a `type`.

Client → server:

| `type` | payload | meaning |
|---|---|---|
| `utterance` | `utterance: UtteranceEvent` | finalized speech (live scoring path) |
| `utterance_revision` | `utterance` (revision ≥ 1) | ASR correction of an earlier utterance |
| `feedback` | `feedback: {card_id, kind, at}` | `dismiss · thumb_up · thumb_down · pin · unpin · expand` |
| `heartbeat` | — | keepalive; server answers with `status` |

Server → client:

| `type` | payload | meaning |
|---|---|---|
| `card` | `card: Card` | new card to display |
| `card_update` | `card` | commitment/expiry reminder for a live card |
| `card_dismiss` | `card_id` | remove card (owner dismissed it from any surface) |
| `status` | `status: {…}` | welcome/heartbeat/error/`revision_applied` info |

`Card`: `id, kind (relevance|lookup|query), memory_id?, headline, detail, source_chip,
origin (memory|web), url?, score, priority, source_utterance_ids[], expires_at?,
debug_features{}, created_at`.

## Memories

| Route | Notes |
|---|---|
| `GET /v1/memories` | params: `q` (semantic+FTS), `entity`, `speaker`, `from`, `to` (ISO), `include_inactive`, `limit=50`, `offset`. → `{memories, total}` |
| `GET /v1/memories/{id}` | full record + provenance |
| `PATCH /v1/memories/{id}` | `{"action": "retract" \| "delete"}` — retract keeps the row with `status='retracted'`; delete removes it |
| `DELETE /v1/memories/{id}` | hard delete; cards referencing it are nulled, not orphaned |

Memories are bi-temporal: `valid_from/valid_to` (when the fact held) vs
`recorded_at` (when we learned it); superseded facts keep `status='superseded'`.

## Conversations & speakers

| Route | Notes |
|---|---|
| `GET /v1/conversations` | recent segments (silence-gap segmentation) |
| `GET /v1/conversations/{id}` | ordered transcript with speaker labels |
| `POST /v1/conversations/{id}/end` | force segment close |
| `GET /v1/speakers` | label, name?, is_owner, counts, first/last seen |
| `PATCH /v1/speakers/{id}` | `{name}` — “that Speaker-2 is Jasmine” |
| `POST /v1/speakers/merge` | `{from_speaker_id, into_speaker_id}` |
| `DELETE /v1/speakers/{id}` | forget the voice cluster (references nulled) |

## Clarifications (the system asks the owner)

| Route | Notes |
|---|---|
| `GET /v1/clarifications?status=open` | queue ranked by importance × uncertainty |
| `GET /v1/clarifications/digest` | the few worth opening the app for (expiry-aware) |
| `POST /v1/clarifications/{id}/answer` | `{answer, source: "text"}` → becomes a memory; the answered acronym/name also joins the ASR vocabulary |

## Vocabulary (ASR boosting)

| Route | Notes |
|---|---|
| `GET /v1/vocabulary` | names/acronyms the phone should bias transcription toward |
| `POST /v1/vocabulary` | `{term, kind?, definition?}` |

## Query (spoken “hey memory” path)

`POST /v1/query` with `{"q": "when did maya say she liked it?"}` →
`{card, memories[], answer?}`. Memory-first; web (SearxNG) only when memories clearly
don’t answer (spec §5.5).

## Feedback (REST twin of the WS message)

`POST /v1/feedback` `{card_id, kind, at}` — feeds the relevance weight fitting.

## Meta

| Route | Notes |
|---|---|
| `GET /v1/health` | `{ok, server_time, prompts, counts}` (authed: no anonymous info leaks) |
| `GET /v1/export` | full dump `?fmt=json\|markdown` — spec §6 data ownership (json downloads as attachment) |

## Example

```bash
T=$AMBIENT_TOKEN
curl -s -H "Authorization: Bearer $T" localhost:8378/v1/health
curl -s -H "Authorization: Bearer $T" -H 'content-type: application/json' \
     -d '{"q":"what does ARTES stand for?"}' localhost:8378/v1/query
```
