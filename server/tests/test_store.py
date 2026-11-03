import json
from datetime import datetime, timedelta, timezone

from artmem_server.models import UtteranceEvent, utc_now


def utt(id="u1", text="hello there", speaker="spk-1", label="Owner", rev=0, **kw):
    now = datetime.now(timezone.utc)
    return UtteranceEvent(id=id, speaker_id=speaker, speaker_label=label,
                          start_ts=now - timedelta(minutes=5), text=text, revision=rev, **kw)


def test_utterance_idempotency_and_speaker_upsert(store):
    cid = store.create_conversation(utc_now())
    assert store.insert_utterance(utt(), cid, [0.1] * 64) == "inserted"
    assert store.insert_utterance(utt(), cid, [0.1] * 64) == "duplicate"
    assert store.db.query_one("SELECT COUNT(*) n FROM utterances")["n"] == 1
    spk = store.get_speaker("spk-1")
    assert spk["is_owner"] == 1 and spk["label"] == "Owner"
    store.insert_utterance(utt(id="u2", speaker="spk-9", label="Speaker-9"), cid, None)
    prov = store.get_speaker("spk-9")
    assert prov["status"] == "provisional" and prov["is_owner"] == 0


def test_revision_supersedes_and_keeps_history(store):
    cid = store.create_conversation(utc_now())
    store.insert_utterance(utt(text="dinner on friday"), cid, None)
    assert store.apply_revision("u1", 1, "dinner on Friday at Chili", "spk-1", "Owner")
    assert store.get_utterance("u1")["text"] == "dinner on Friday at Chili"
    # older/equal revision ignored
    assert not store.apply_revision("u1", 0, "ancient", None, None)
    assert not store.apply_revision("u1", 1, "same rev", None, None)
    revs = store.db.query("SELECT * FROM utterance_revisions")
    assert len(revs) == 1 and revs[0]["revision"] == 1
    # FTS updated to newest text
    hits = store.db.query("SELECT * FROM utterances_fts WHERE utterances_fts MATCH '\"Chili\"'")
    assert hits and hits[0]["utterance_id"] == "u1"


def test_memory_insert_provenance_and_supersede(store):
    cid = store.create_conversation(utc_now())
    store.insert_utterance(utt(id="u9", text="dinner will be chili"), cid, None)
    mid = store.insert_memory(
        "Jasmine told Spencer that dinner will be Chili.", {"time_phrase": "Friday"},
        "event", 0.8, 0.9, cid, "spk-1", ["u9"],
        event_start=utc_now() + timedelta(days=2), embedding=[0.2] * 64)
    row = store.get_memory(mid)
    assert row["status"] == "active" and row["valid_to"] is None
    assert json.loads(row["source_utterance_ids"]) == ["u9"]

    mid2 = store.insert_memory(
        "Jasmine told Spencer that dinner will be Sushi.", {}, "event", 0.8, 0.9,
        cid, "spk-1", ["u9"], embedding=[0.21] * 64)
    store.supersede(mid, mid2)
    old = store.get_memory(mid)
    assert old["status"] == "superseded" and old["superseded_by"] == mid2
    assert old["valid_to"] is not None            # bi-temporal close, never delete
    assert store.get_memory(mid2)["valid_to"] is None
    # active-only search must now hide the old one
    hits = store.search("dinner chili sushi", [0.2] * 64, limit=10)
    ids = {h.row["id"] for h in hits}
    assert mid2 in ids and mid not in ids
    # and with status=None both are visible (history view)
    ids_all = {h.row["id"] for h in store.search("dinner", None, status=None, limit=10)}
    assert {mid, mid2} <= ids_all


def test_hybrid_channels_attach_signals(store):
    eid = store.upsert_entity("Jasmine", "person")
    vec = [0.0] * 64
    vec[7] = 1.0
    mid = store.insert_memory("Jasmine is adopting a dog named Biscuit", {}, "personal_fact",
                              0.7, 0.9, None, None, [], embedding=vec)
    store.attach_memory_entities(mid, [(eid, "about")])
    hits = store.search("biscuit dog adoption", vec, entity_ids={eid}, limit=5)
    h = next(x for x in hits if x.row["id"] == mid)
    assert h.semantic > 0.9 and h.keyword > 0 and h.entity_hit


def test_fts_and_vector_cleanup_on_delete(store):
    mid = store.insert_memory("delete me please", {}, "other", 0.5, 0.5, None, None, [],
                              embedding=[0.3] * 64)
    assert store.search("delete", None, limit=5)
    assert store.delete_memory(mid)
    assert not store.search("delete", None, limit=5)
    assert store.db.query_one("SELECT * FROM memories_fts WHERE memory_id=?", (mid,)) is None


def test_speaker_rename_merge_and_delete(store):
    cid = store.create_conversation(utc_now())
    store.insert_utterance(utt(id="a", speaker="s1", label="Speaker-1"), cid, None)
    store.insert_utterance(utt(id="b", speaker="s2", label="Speaker-2"), cid, None)
    mid = store.insert_memory("fact by s2", {}, "personal_fact", 0.5, 0.5, cid, "s2", ["b"])
    store.rename_speaker("s1", "Jasmine")
    assert store.get_speaker("s1")["name"] == "Jasmine"
    assert store.get_speaker("s1")["status"] == "named"
    assert store.merge_speakers("s2", "s1")
    assert store.get_utterance("b")["speaker_id"] == "s1"
    assert store.get_memory(mid)["speaker_id"] == "s1"
    assert store.get_speaker("s2")["status"] == "merged"
    labels = {s["id"] for s in store.list_speakers()}
    assert "s2" not in labels                      # merged hidden from list
    store.delete_speaker("s1")
    assert store.get_memory(mid) is None           # cascade deletes attributed memories
    assert store.get_utterance("a")["speaker_id"] is None  # transcript retained


def test_conversation_sweep_and_location_routing_helpers(store, ctx):
    from artmem_server.services import haversine_m

    assert haversine_m(37.7749, -122.4194, 37.7749, -122.4194) == 0
    assert 500 < haversine_m(37.7749, -122.4194, 37.7849, -122.4194) < 1500
    cid = store.create_conversation(utc_now() - timedelta(hours=2))
    store.insert_utterance(utt(id="old", text="long ago"), cid, None)
    store.db.execute("UPDATE conversations SET last_utterance_at=? WHERE id=?",
                     ((utc_now() - timedelta(minutes=10)).isoformat(), cid))
    closed = store.sweep_due_closes(utc_now())
    assert (cid, "silence") in closed
    assert store.get_conversation(cid)["close_reason"] == "silence"


def test_clarifications_dedupe_answer_expiry(store):
    now = utc_now()
    a = store.insert_clarification("unknown_entity", "What does ARTES mean?",
                                   {"subject": "ARTES"}, 0.8, 0.6, None, [], None, None,
                                   now, 14)
    assert a
    b = store.insert_clarification("unknown_entity", "What does ARTES mean? (dup)",
                                   {"subject": "artes"}, 0.8, 0.6, None, [], None, None,
                                   now, 14)
    assert b is None                                # dedupe by kind+subject
    assert store.answer_clarification(a, "a telemetry standard", "text")
    assert store.get_clarification(a)["status"] == "answered"
    assert not store.answer_clarification(a, "again", "text")  # one-shot


def test_vocabulary_roundtrip(store):
    store.upsert_vocabulary("ARTES", "acronym", "test system")
    store.upsert_vocabulary("artes", "acronym", "real-time telemetry system")
    vocab = store.list_vocabulary()
    assert len(vocab) == 1
    assert vocab[0]["definition"] == "real-time telemetry system"


def test_feedback_and_feature_logging(store):
    mid = store.insert_memory("shown fact", {}, "other", 0.5, 0.5, None, None, [])
    feats = {"score": 0.9, "features": {"semantic": 0.8}}
    store.record_card_shown("c1", mid, "relevance", 0.9, feats, None, utc_now())
    assert store.record_feedback("c1", "dismiss", utc_now()) == mid
    assert store.dismiss_counts()[mid] == 1
    assert store.last_shown_map()[mid] is not None
    log = store.feedback_log()
    assert log and json.loads(log[0]["features_json"])["score"] == 0.9


def test_export_snapshot(store):
    store.insert_memory("x", {}, "other", 0.5, 0.5, None, None, [])
    data = store.export_all()
    assert data["version"] == 1 and len(data["memories"]) == 1
    assert data["exported_at"]


def test_observation_and_entity_graph(store):
    e1 = store.upsert_entity("Jasmine", "person")
    e2 = store.upsert_entity("Chili", "place")
    ts = utc_now()
    store.log_entity_edge(e1, e2, "co_mentioned", ts)
    store.log_entity_edge(e1, e2, "co_mentioned", ts + timedelta(minutes=1))
    edges = store.entity_edges_for({e1})
    assert edges and edges[0]["weight"] == 2.0
    m = store.insert_memory("Jasmine booked Chili", {}, "event", 0.6, 0.8, None, None, [])
    store.attach_memory_entities(m, [(e1, "about"), (e2, "located_at")])
    store.set_entity_observation(e1, "Jasmine is a friend who books restaurants.", 1)
    assert store.get_entity_observation(e1)["source_memory_count"] == 1
    due = {e["id"] for e in store.entities_needing_observation(min_new=1)}
    assert e2 in due and e1 not in due            # observed entity satisfied; new one due
