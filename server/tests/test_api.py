import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ambient_server.api.app import create_app
from ambient_server.models import UtteranceEvent
from ambient_server.testing import fake_embed_vector

TOKEN = "test-token"
HDRS = {"Authorization": f"Bearer {TOKEN}"}
BAD_HDRS = {"Authorization": "Bearer wrong"}


@pytest.fixture
def client(ctx):
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


def utt_json(id="u1", text="dinner will be chili tomorrow", label="Owner", **kw):
    now = datetime.now(timezone.utc)
    ev = UtteranceEvent(id=id, speaker_id=kw.pop("speaker_id", "spk-o"),
                        speaker_label=label, start_ts=now, text=text, **kw)
    return json.loads(ev.model_dump_json())


# ---------------------------------------------------------------- auth
def test_health_requires_token(client):
    assert client.get("/v1/health").status_code == 401
    assert client.get("/v1/health", headers=BAD_HDRS).status_code == 401
    r = client.get("/v1/health", headers=HDRS)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and "counts" in body and "prompts" in body


def test_ws_requires_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/v1/live") as ws:
            ws.receive_json()


# ---------------------------------------------------------------- ingest
def test_batch_ingest_idempotent(client, ctx, store):
    u = utt_json(id="b1")
    r = client.post("/v1/ingest/batch", headers=HDRS, json={"utterances": [u]})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted_ids"] == ["b1"]
    # replay from outbox → duplicate, no data change
    r2 = client.post("/v1/ingest/batch", headers=HDRS, json={"utterances": [u]})
    assert r2.json()["duplicate_ids"] == ["b1"]
    assert store.db.query_one("SELECT COUNT(*) n FROM utterances")["n"] == 1
    # batch catch-up must NOT surface live cards
    assert not ctx.engine.active


def test_batch_revision_updates_transcript(client, store):
    u = utt_json(id="r1", text="meeting tuesday")
    client.post("/v1/ingest/batch", headers=HDRS, json={"utterances": [u]})
    rev = utt_json(id="r1", text="Meeting Tuesday at 3", revision=1)
    r = client.post("/v1/ingest/batch", headers=HDRS, json={"utterances": [rev]})
    assert r.status_code == 200
    assert store.get_utterance("r1")["text"] == "Meeting Tuesday at 3"


def test_conversation_lifecycle_endpoints(client, store):
    for i in range(3):
        u = utt_json(id=f"c{i}", text=f"line number {i}",
                     label="Speaker-1", speaker_id="spk-1")
        client.post("/v1/ingest/batch", headers=HDRS, json={"utterances": [u]})
    convs = client.get("/v1/conversations", headers=HDRS).json()["conversations"]
    assert len(convs) == 1 and convs[0]["utterance_count"] == 3
    cid = convs[0]["id"]
    one = client.get(f"/v1/conversations/{cid}", headers=HDRS).json()
    assert len(one["utterances"]) == 3
    assert one["utterances"][0]["text"] == "line number 0"


# ---------------------------------------------------------------- memories/query
def seed(ctx, text, entities=()):
    mid = ctx.store.insert_memory(text, {"time_phrase": "soon"}, "event", 0.9, 0.9,
                                  None, None, [], embedding=fake_embed_vector(text))
    for name, kind in entities:
        ctx.store.attach_memory_entities(mid, [(ctx.store.upsert_entity(name, kind), "about")])
    return mid


def test_memories_search_and_filters(client, ctx):
    mid = seed(ctx, "Jasmine is organizing the birthday party",
               entities=[("Jasmine", "person")])
    r = client.get("/v1/memories", headers=HDRS, params={"q": "birthday party jasmine"})
    mems = r.json()["memories"]
    assert mems and mems[0]["id"] == mid
    assert mems[0]["entities"]
    # browse with no query still lists
    r2 = client.get("/v1/memories", headers=HDRS)
    assert r2.json()["total"] == 1
    # entity filter
    r3 = client.get("/v1/memories", headers=HDRS, params={"entity": "Jasmine"})
    assert r3.json()["total"] == 1
    r4 = client.get("/v1/memories", headers=HDRS, params={"entity": "Nobody"})
    assert r4.json()["total"] == 0
    # single fetch with provenance + patch retract + delete
    one = client.get(f"/v1/memories/{mid}", headers=HDRS)
    assert one.status_code == 200 and one.json()["status"] == "active"
    client.patch(f"/v1/memories/{mid}", headers=HDRS, json={"action": "retract"})
    assert client.get(f"/v1/memories/{mid}", headers=HDRS).json()["status"] == "retracted"
    assert client.delete(f"/v1/memories/{mid}", headers=HDRS).status_code == 200
    assert client.get(f"/v1/memories/{mid}", headers=HDRS).status_code == 404


def test_query_endpoint_direct_path(client, ctx, fake_llm):
    fake_llm.on("retrieved memories", "The park trip was last Saturday. [m:abcd]")
    seed(ctx, "We went to the park last Saturday with the kids")
    r = client.post("/v1/query", headers=HDRS, json={"q": "when did we go to the park"})
    body = r.json()
    assert r.status_code == 200
    assert "park" in body["card"]["headline"]
    assert body["answer"] is not None  # synthesized with citation
    assert body["memories"]
    # no memory → explicit miss card, never a fabricated answer
    r2 = client.post("/v1/query", headers=HDRS, json={"q": "quantum woo woo zzz"})
    assert r2.json()["card"]["headline"] == "No matching memory"


# ---------------------------------------------------------------- speakers
def test_speakers_rename_merge(client, store):
    from ambient_server.models import utc_now

    cid = store.create_conversation(utc_now())
    for i, lab in enumerate(["Speaker-1", "Speaker-2"]):
        store.insert_utterance(UtteranceEvent(
            id=f"s{i}", speaker_id=f"sid{i}", speaker_label=lab,
            start_ts=utc_now(), text="hi"), cid)
    r = client.patch("/v1/speakers/sid0", headers=HDRS, json={"name": "Jasmine",
                                                              "is_owner": True})
    assert r.status_code == 200
    spk = {s["id"]: s for s in client.get("/v1/speakers", headers=HDRS).json()["speakers"]}
    assert spk["sid0"]["name"] == "Jasmine" and spk["sid0"]["is_owner"] == 1
    r = client.post("/v1/speakers/merge", headers=HDRS,
                    json={"from_speaker_id": "sid1", "into_speaker_id": "sid0"})
    assert r.status_code == 200
    ids = {s["id"] for s in client.get("/v1/speakers", headers=HDRS).json()["speakers"]}
    assert ids == {"sid0"}
    assert client.delete("/v1/speakers/sid0", headers=HDRS).status_code == 200


# ---------------------------------------------------------------- clarifications
def test_clarifications_flow(client, store):
    from ambient_server.models import utc_now

    now = utc_now()
    cid = store.insert_clarification("unknown_entity", "What does ARTES mean?",
                                     {"subject": "ARTES"}, 0.8, 0.9, None, [], None, None,
                                     now, 14)
    op = client.get("/v1/clarifications", headers=HDRS).json()["clarifications"]
    assert len(op) == 1 and op[0]["id"] == cid
    digest = client.get("/v1/clarifications/digest", headers=HDRS).json()
    assert digest["questions"][0]["id"] == cid
    ans = client.post(f"/v1/clarifications/{cid}/answer", headers=HDRS,
                      json={"answer": "a telemetry standard we ship", "source": "text"})
    assert ans.status_code == 200 and ans.json()["answered"]
    assert ans.json()["memories_created"]           # retained as high-confidence memory
    # answer became a vocabulary entry (ASR boosting sync back to the phone)
    vocab = client.get("/v1/vocabulary", headers=HDRS).json()["vocabulary"]
    assert any(v["term"] == "artes" for v in vocab)
    # second answer → 400 already answered
    assert client.post(f"/v1/clarifications/{cid}/answer", headers=HDRS,
                       json={"answer": "again"}).status_code == 400


def test_speaker_clarification_answer_renames_or_merges(client, store):
    from ambient_server.models import utc_now

    now = utc_now()
    store.upsert_speaker_from_utterance("sid-x", "Speaker-3", now)
    cid = store.insert_clarification("speaker_identity", "Who was Speaker-3?",
                                     {"subject": "Speaker-3"}, 0.9, 0.9, None, [],
                                     "sid-x", None, now, 14)
    r = client.post(f"/v1/clarifications/{cid}/answer", headers=HDRS,
                    json={"answer": "that's Jasmine"})
    assert r.status_code == 200
    assert store.get_speaker("sid-x")["name"] == "Jasmine"


# ---------------------------------------------------------------- vocabulary
def test_vocabulary_sync(client, store):
    store.upsert_vocabulary("ARTES", "acronym", "telemetry system")
    r = client.get("/v1/vocabulary", headers=HDRS).json()
    assert r["vocabulary"][0]["term"] == "artes"
    r2 = client.post("/v1/vocabulary", headers=HDRS,
                     json={"term": "HertzHunter", "kind": "project"})
    assert r2.status_code == 200
    assert any(v["term"] == "hertzhunter" for v in
               client.get("/v1/vocabulary", headers=HDRS).json()["vocabulary"])


# ---------------------------------------------------------------- websocket
def test_ws_utterance_roundtrip_and_heartbeat(client, ctx, store):
    seed(ctx, "Jasmine adopted a dog named Biscuit", entities=[("Jasmine", "person")])
    with client.websocket_connect("/v1/live", headers=HDRS) as ws:
        status = ws.receive_json()
        assert status["type"] == "status" and status["status"]["listening"]
        ws.send_json({"type": "heartbeat"})
        # heartbeat may arrive before/after card traffic; drain up to 3 msgs
        got_status = False
        for _ in range(2):
            m = ws.receive_json()
            if m["type"] == "status" and m.get("status", {}).get("ok"):
                got_status = True
                break
        assert got_status
        ws.send_json({"type": "utterance",
                      "utterance": utt_json(id="w-1",
                                            text="jasmine biscuit dog adoption update")})
        card = None
        for _ in range(4):
            m = ws.receive_json()
            if m["type"] == "card":
                card = m["card"]
                break
        assert card and "Biscuit" in card["headline"]
        # dismiss feedback → server echoes card_dismiss (spec WS feedback)
        ws.send_json({"type": "feedback",
                      "feedback": {"card_id": card["id"], "kind": "dismiss"}})
        m2 = ws.receive_json()
        assert m2["type"] == "card_dismiss" and m2["card_id"] == card["id"]


def test_ws_revision_message(client, store):
    with client.websocket_connect("/v1/live", headers=HDRS) as ws:
        ws.receive_json()
        u = utt_json(id="rev-1", text="friday dinneer")
        ws.send_json({"type": "utterance", "utterance": u})
        ws.send_json({"type": "utterance_revision",
                      "utterance": utt_json(id="rev-1", text="Friday dinner", revision=1)})
        seen = []
        for _ in range(3):
            m = ws.receive_json()
            seen.append(m)
            if m["type"] == "status" and "revision_applied" in (m.get("status") or {}):
                assert m["status"]["revision_applied"] is True
                break
        else:
            pytest.fail(f"no revision_applied status in {seen}")
        assert store.get_utterance("rev-1")["text"] == "Friday dinner"


def test_ws_malformed_message_does_not_kill_session(client):
    with client.websocket_connect("/v1/live", headers=HDRS) as ws:
        ws.receive_json()
        ws.send_json({"type": "garbage"})
        m = ws.receive_json()
        assert m["type"] == "status" and "error" in m["status"]
        ws.send_json({"type": "heartbeat"})
        ok = False
        for _ in range(2):
            m = ws.receive_json()
            if m["type"] == "status" and m["status"].get("ok"):
                ok = True
                break
        assert ok


# ---------------------------------------------------------------- export
def test_export_json_and_markdown(client, ctx):
    seed(ctx, "Dinner at Chili on Friday")
    j = client.get("/v1/export", headers=HDRS).json()
    assert j["memories"] and j["version"] == 1
    md = client.get("/v1/export", headers=HDRS, params={"fmt": "markdown"})
    assert md.status_code == 200
    assert "Dinner at Chili" in md.text
    assert "## Memories" in md.text
