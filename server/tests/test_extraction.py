import json
from datetime import datetime, timedelta, timezone


from artmem_server.models import UtteranceEvent

# Tuesday 2026-07-07 18:00 UTC — "next Friday" resolves to 2026-07-10
ANCHOR = datetime(2026, 7, 7, 18, 0, tzinfo=timezone.utc)

EXTRACT_RESULT = {
    "facts": [
        {"content": "dinner on friday will be Chili", "who_said": "Jasmine",
         "said_to": ["Spencer"], "type": "event", "time_phrase": "next Friday",
         "event_time": "2026-07-10", "place": "Chili",
         "entities": [{"name": "Chili", "kind": "place", "aliases": []},
                      {"name": "Jasmine", "kind": "person", "aliases": []}],
         "importance": 0.8, "confidence": 0.9},
    ],
    "commitments": [{"owner": "Spencer", "text": "bring the wine",
                     "due_phrase": "tomorrow night", "due_date": None, "confidence": 0.8}],
    "unknowns": [{"kind": "unknown_entity", "text": "ARTES",
                  "context": "we shipped ARTES last sprint", "importance": 0.7}],
    "entities": [{"name": "Chili", "kind": "place"}, {"name": "Jasmine", "kind": "person"},
                 {"name": "ARTES", "kind": "acronym"}],
    "summary": "Jasmine announced Friday dinner at Chili.",
}


def seed_conversation(store, texts):
    cid = store.create_conversation(ANCHOR)
    for i, (label, text) in enumerate(texts):
        ev = UtteranceEvent(id=f"u{i}", speaker_id=f"spk-{label}", speaker_label=label,
                            start_ts=ANCHOR + timedelta(seconds=10 * i),
                            end_ts=ANCHOR + timedelta(seconds=10 * i + 3), text=text)
        store.insert_utterance(ev, cid, None)
    return cid


async def test_extraction_attributed_memory_with_resolved_dates(ctx, fake_llm, store):
    fake_llm.on("memory extractor", EXTRACT_RESULT)
    fake_llm.on("clarification questions", {"questions": [
        {"kind": "unknown_entity", "question": "What does ARTES mean?", "subject": "ARTES",
         "importance": 0.7, "uncertainty": 0.9}]})
    cid = seed_conversation(store, [
        ("Jasmine", "We're doing dinner next Friday, I booked Chili"),
        ("Spencer", "Nice, I'll bring the wine"),
    ])
    store.db.execute("UPDATE conversations SET closed_at=? WHERE id=?",
                     ((ANCHOR + timedelta(minutes=4)).isoformat(), cid))

    outcome = await ctx.extraction.extract(cid, mode="closing")
    assert outcome.error is None and outcome.llm_used
    assert len(outcome.facts_created) == 2          # fact + commitment

    rows = store.db.query("SELECT * FROM memories ORDER BY recorded_at")
    texts = [r["text"] for r in rows]
    # spec §M3 acceptance: "Jasmine told Spencer dinner will be chili"-style attribution
    assert any(t.startswith("Jasmine told Spencer") and "Chili" in t for t in texts)
    fact_row = next(r for r in rows if "Chili" in r["text"])
    structured = json.loads(fact_row["structured_json"])
    assert structured["time_phrase"] == "next Friday"        # original phrase kept
    assert structured["time_resolved"].startswith("2026-07-10")  # deterministic resolve
    assert fact_row["event_start"].startswith("2026-07-10")
    assert fact_row["type"] == "event"
    # provenance links back to source utterances
    assert json.loads(fact_row["source_utterance_ids"])
    # commitment got its own memory with resolved due date
    commit = next(r for r in rows if r["type"] == "commitment")
    assert "bring the wine" in commit["text"]
    assert json.loads(commit["structured_json"])["due_resolved"].startswith("2026-07-08")

    # entities attached; ARTES flagged unresolved → clarification created
    ents = {e["name"] for e in store.entities_for_memory(fact_row["id"])}
    assert {"Chili", "Jasmine"} <= ents
    artes = store.resolve_entity_by_alias("ARTES")
    assert artes and artes["unresolved"] == 1
    clars = store.list_clarifications(("open",))
    assert any("ARTES" in c["question"] for c in clars)
    # conversation marked extracted, utterances marked extracted
    assert store.get_conversation(cid)["extraction_status"] == "closing"


async def test_extraction_llm_failure_degrades(ctx, fake_llm, store):
    # no marker registered and unknown default '{}' validates as empty ExtractionResult
    fake_llm.responses.clear()
    cid = seed_conversation(store, [("Owner", "hello")])
    outcome = await ctx.extraction.extract(cid, mode="closing")
    assert outcome.facts_created == []      # graceful no-op, no exception


async def test_extraction_clarify_llm_failure_falls_back_to_raw_questions(ctx, fake_llm, store):
    fake_llm.on("memory extractor", EXTRACT_RESULT)
    fake_llm.on("clarification questions", "NOT JSON AT ALL {{{ bad")  # fails validation twice
    cid = seed_conversation(store, [("Jasmine", "dinner next Friday at Chili")])
    outcome = await ctx.extraction.extract(cid, mode="closing")
    assert outcome.error is None                     # extraction itself succeeded
    assert outcome.clarifications_created            # fallback questions still queued
    q = store.get_clarification(outcome.clarifications_created[0])
    assert "ARTES" in q["question"]                  # generic "who/what is X" phrasing


async def test_supersession_closes_old_memory_not_delete(ctx, fake_llm, store):
    fake_llm.on("memory extractor", {
        "facts": [{"content": "dinner on friday will be Sushi", "who_said": "Jasmine",
                   "said_to": ["Spencer"], "type": "event", "time_phrase": "next Friday",
                   "entities": [{"name": "Jasmine", "kind": "person"}],
                   "importance": 0.8, "confidence": 0.9}],
        "commitments": [], "unknowns": [], "entities": [], "summary": "changed plans"})
    fake_llm.on("Two memories about possibly the same fact", {"relation": "replaces"})
    cid1 = seed_conversation(store, [("Jasmine", "dinner next Friday will be Chili")])
    o1 = await ctx.extraction.extract(cid1, mode="incremental")
    old_id = o1.facts_created[0]
    cid2 = store.create_conversation(ANCHOR + timedelta(hours=1))
    ev = UtteranceEvent(id="v1", speaker_id="spk-Jasmine", speaker_label="Jasmine",
                        start_ts=ANCHOR + timedelta(hours=1),
                        text="actually dinner on friday will be Sushi")
    store.insert_utterance(ev, cid2, None)
    o2 = await ctx.extraction.extract(cid2, mode="incremental")
    new_id = o2.facts_created[0]
    old = store.get_memory(old_id)
    assert old["status"] == "superseded" and old["superseded_by"] == new_id
    assert old["valid_to"] is not None                # closed, history intact
    assert store.get_memory(old_id, include_inactive=True) is not None  # never deleted


async def test_incremental_then_closing_extracts_only_new_utterances(ctx, fake_llm, store):
    call = {"n": 0}

    def resp(messages):
        call["n"] += 1
        return {
            "facts": [{"content": f"fact number {call['n']}", "who_said": "Owner",
                       "type": "personal_fact", "importance": 0.5, "confidence": 0.9}],
            "commitments": [], "unknowns": [], "entities": [], "summary": "s"}

    fake_llm.on("memory extractor", resp)
    cid = seed_conversation(store, [("Owner", "one"), ("Owner", "two")])
    o1 = await ctx.extraction.extract(cid, mode="incremental")
    assert len(o1.facts_created) == 1
    # no new utterances → second run extracts nothing more
    o2 = await ctx.extraction.extract(cid, mode="incremental")
    assert o2.facts_created == []
    # a new utterance is the only thing the next pass sees
    ev = UtteranceEvent(id="w3", speaker_id="spk-Owner", speaker_label="Owner",
                        start_ts=ANCHOR + timedelta(minutes=1), text="three")
    store.insert_utterance(ev, cid, None)
    o3 = await ctx.extraction.extract(cid, mode="closing")
    assert len(o3.facts_created) == 1


async def test_whisper_utterances_never_extracted(ctx, fake_llm, store):
    fake_llm.on("memory extractor", lambda m: EXTRACT_RESULT_WHISPER)
    cid = seed_conversation(store, [("Owner", "normal line")])
    ev = UtteranceEvent(id="w-sp", speaker_id="spk-Owner", speaker_label="Owner",
                        start_ts=ANCHOR + timedelta(seconds=30),
                        text="memory when did we go to the park", is_whisper=True)
    store.insert_utterance(ev, cid, None)
    await ctx.extraction.extract(cid, mode="closing")
    user_msg = fake_llm.calls[-1]["messages"][1]["content"]
    assert "park" not in user_msg


EXTRACT_RESULT_WHISPER = {
    "facts": [{"content": "went to the park", "who_said": "Owner", "type": "personal_fact",
               "importance": 0.3, "confidence": 0.9}],
    "commitments": [], "unknowns": [], "entities": [], "summary": "",
}
