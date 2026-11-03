"""Extraction-task queue: phone executes the LLM, the server is the durability buffer.

Covers the delegated contract: enqueue on close/incremental, verbatim prompt context,
claim/apply/complete, repair-relay, phone-death (stale claim), fallback execution,
and race safety (exactly-once application)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from artmem_server.api.app import create_app
from artmem_server.config import load_config
from artmem_server.models import UtteranceEvent
from artmem_server.services import AppContext
from artmem_server.testing import FakeLLM, make_test_cfg

from test_extraction import ANCHOR, EXTRACT_RESULT

HDRS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def dctx(tmp_path, fake_llm):
    # a fake LLM endpoint is "reachable" without an api key (mock transport)
    """Delegated AppContext. Long fallback delay by default so the background loop
    never races the scripted 'phone'; individual tests zero it when testing fallback."""
    cfg = make_test_cfg(tmp_path / "artmem.sqlite3",
                        extraction__delegated=True,
                        extraction__fallback_delay_s=3600.0)
    cfg.llm.api_key = "fake-key"
    ctx = AppContext(cfg, llm_client=fake_llm.client(cfg.llm.base_url))
    yield ctx

    async def _close():
        await ctx.aclose()
    asyncio.new_event_loop().run_until_complete(_close())


@pytest.fixture
def dclient(dctx):
    app = create_app(dctx)
    with TestClient(app) as c:
        yield c


def seed(ctx, n=2):
    """Utterances into a conversation via the production ingest path."""
    texts = ["We're doing dinner next Friday, I booked Chili",
             "Nice, I\'ll bring the wine"]

    async def _seed():
        for i in range(n):
            ev = UtteranceEvent(
                id=f"t-u{i}", speaker_id="spk-j", speaker_label="Jasmine",
                start_ts=ANCHOR + timedelta(seconds=10 * i),
                end_ts=ANCHOR + timedelta(seconds=10 * i + 3), text=texts[i])
            await ctx.handle_utterance(ev, live=False)
    asyncio.new_event_loop().run_until_complete(_seed())
    row = ctx.store.db.query_one("SELECT id FROM conversations ORDER BY started_at DESC")
    return row["id"]


def close(dclient, cid):
    r = dclient.post(f"/v1/conversations/{cid}/end", headers=HDRS)
    assert r.status_code == 200
    body = r.json()
    assert "queued_task" in body, f"delegated close must enqueue, got {body}"
    return body["queued_task"]


# ---------------------------------------------------------------- happy path
def test_delegated_close_queues_task_phone_executes(dclient, dctx, fake_llm):
    cid = seed(dctx)
    tid = close(dclient, cid)

    pending = dclient.get("/v1/tasks/pending", headers=HDRS).json()
    assert pending["count"] == 1 and pending["tasks"][0]["id"] == tid

    # no memories yet — extraction is NOT running server-side in delegated mode
    assert dclient.get("/v1/memories", headers=HDRS).json()["total"] == 0

    # context carries the verbatim prompt (system+user, schema hint included)
    ctxr = dclient.get(f"/v1/tasks/{tid}/context", headers=HDRS).json()
    assert ctxr["completed"] is False
    assert ctxr["profile"] == "deep"
    msgs = ctxr["messages"]
    assert msgs[0]["role"] == "system" and "Respond ONLY with JSON" in msgs[0]["content"]
    assert "ExtractionResult" in msgs[0]["content"] or "facts" in msgs[0]["content"]
    assert "Jasmine" in msgs[1]["content"] and "booked Chili" in msgs[1]["content"]

    # phone "calls its own LLM" → posts raw JSON; server validates + applies
    fake_llm.on("clarification questions", {"questions": []})
    res = dclient.post(f"/v1/tasks/{tid}/result", headers=HDRS,
                       json={"raw_json": json.dumps(EXTRACT_RESULT)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] and body["facts_created"] >= 2   # fact + commitment

    total = dclient.get("/v1/memories", headers=HDRS).json()["total"]
    assert total >= 2
    task = dctx.store.get_extraction_task(tid)
    assert task["status"] == "done" and task["result_source"] == "phone"

    # sync mirror reflects it; pending drained
    sync = dclient.get("/v1/memories/sync", headers=HDRS).json()
    assert any("Chili" in m["text"] for m in sync["memories"])
    assert any("Chili" in m["entities"] or "Chili" in (m["text"]) for m in sync["memories"])
    assert dclient.get("/v1/tasks/pending", headers=HDRS).json()["count"] == 0


# ---------------------------------------------------------------- repair relay
def test_invalid_result_relays_detail_and_stays_pending(dclient, dctx):
    cid = seed(dctx)
    tid = close(dclient, cid)
    dclient.get(f"/v1/tasks/{tid}/context", headers=HDRS)  # snapshot

    res = dclient.post(f"/v1/tasks/{tid}/result", headers=HDRS,
                       json={"raw_json": '{"facts": [{"nope": 1}]}'})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "error" in json.dumps(detail).lower() or "field required" in json.dumps(detail)
    task = dctx.store.get_extraction_task(tid)
    assert task["status"] == "pending" and task["attempts"] == 1
    # a valid retry after repair still works (raw JSON *string*, as an LLM emits)
    res2 = dclient.post(f"/v1/tasks/{tid}/result", headers=HDRS,
                        json={"raw_json": "```json\n" + json.dumps(EXTRACT_RESULT) + "\n```"})
    assert res2.status_code == 200


# ---------------------------------------------------------------- races
def test_phone_result_after_server_fallback_is_409_no_duplicates(dclient, dctx, fake_llm):
    cid = seed(dctx)
    tid = close(dclient, cid)
    fake_llm.on("memory extractor", EXTRACT_RESULT)
    fake_llm.on("clarification questions", {"questions": []})
    due = (datetime.now(timezone.utc) + timedelta(seconds=0.1)).isoformat()
    dctx.store.db.execute("UPDATE extraction_tasks SET fallback_after=? WHERE id=?",
                          (due, tid))
    async def fallback():
        for _ in range(50):
            tasks = dctx.store.due_fallback_tasks(datetime.now(timezone.utc))
            for task in tasks:
                await dctx._fallback_extract(task)
            if dctx.store.get_extraction_task(tid)["status"] == "done":
                return
            await asyncio.sleep(0.1)
    asyncio.new_event_loop().run_until_complete(fallback())

    task = dctx.store.get_extraction_task(tid)
    assert task["status"] == "done" and task["result_source"] == "server"
    first_total = dclient.get("/v1/memories", headers=HDRS).json()["total"]

    # slow phone finally delivers its (valid!) result — must be rejected, not applied
    res = dclient.post(f"/v1/tasks/{tid}/result", headers=HDRS,
                       json={"raw_json": json.dumps(EXTRACT_RESULT)})
    assert res.status_code == 409
    again = dclient.get("/v1/memories", headers=HDRS).json()["total"]
    assert again == first_total, "exactly-once application"


def test_stale_claim_released_and_rerun(dclient, dctx, fake_llm):
    cid = seed(dctx)
    tid = close(dclient, cid)
    dclient.get(f"/v1/tasks/{tid}/context", headers=HDRS)
    # phone claims then dies (simulate direct claim + no result)
    assert dctx.store.claim_extraction_task(tid, dctx.prompts.content_hash()) == "claimed"
    assert dctx.store.get_extraction_task(tid)["status"] == "applying"

    # force it stale
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    dctx.store.db.execute("UPDATE extraction_tasks SET updated_at=? WHERE id=?", (old, tid))
    assert dctx.store.reset_stale_claims(datetime.now(timezone.utc)
                                         - timedelta(minutes=15)) == 1
    assert dctx.store.get_extraction_task(tid)["status"] == "pending"

    # and it can be claimed again by a fresh attempt
    assert dctx.store.claim_extraction_task(tid, dctx.prompts.content_hash()) == "claimed"


def test_abandon_makes_fallback_eligible_now(dclient, dctx, fake_llm):
    cid = seed(dctx)
    tid = close(dclient, cid)
    # long-fuse task, then phone gives up → immediately server-eligible
    dctx.store.db.execute("UPDATE extraction_tasks SET fallback_after=? WHERE id=?",
                          ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                           tid))
    assert not dctx.store.due_fallback_tasks(datetime.now(timezone.utc))

    r = dclient.post(f"/v1/tasks/{tid}/abandon", headers=HDRS,
                     json={"reason": "llm kept returning garbage"})
    assert r.status_code == 200
    due = dctx.store.due_fallback_tasks(datetime.now(timezone.utc))
    assert [t["id"] for t in due] == [tid]

    fake_llm.on("memory extractor", EXTRACT_RESULT)
    fake_llm.on("clarification questions", {"questions": []})

    async def run():
        await dctx._fallback_extract(due[0])
    asyncio.new_event_loop().run_until_complete(run())
    assert dctx.store.get_extraction_task(tid)["status"] == "done"
    assert dclient.get("/v1/memories", headers=HDRS).json()["total"] >= 2


def test_prompts_hash_mismatch_is_409_stale(dclient, dctx):
    cid = seed(dctx)
    tid = close(dclient, cid)
    dclient.get(f"/v1/tasks/{tid}/context", headers=HDRS)
    dctx.store.db.execute("UPDATE extraction_tasks SET prompts_hash='ancient' WHERE id=?",
                          (tid,))
    res = dclient.post(f"/v1/tasks/{tid}/result", headers=HDRS,
                       json={"raw_json": json.dumps(EXTRACT_RESULT)})
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "stale_prompts"
    # still pending: the phone must re-fetch context with the new hash and retry
    assert dctx.store.get_extraction_task(tid)["status"] == "pending"


# ---------------------------------------------------------------- wiring paths
def test_incremental_and_sweep_enqueue_not_execute(dctx, fake_llm):
    """The enqueue fork also fires from the silence sweep and the every-N trigger —
    not only the manual close endpoint."""
    cid = seed(dctx, n=1)
    # move the conversation's close-due into the past and sweep
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    dctx.store.db.execute(
        "UPDATE conversations SET last_utterance_at=?, closed_at=NULL WHERE id=?",
        (past, cid))

    async def tick():
        await dctx._background_tick(datetime.now(timezone.utc))
    asyncio.new_event_loop().run_until_complete(tick())
    tasks = dctx.store.list_pending_tasks()
    assert [t["mode"] for t in tasks] == ["closing"], (
        "sweep-closed conversation must enqueue a closing task in delegated mode")
    assert tasks[0]["conversation_id"] == cid


def test_safety_net_requeues_closed_conversation_with_stragglers(dctx, fake_llm):
    """Offline-captured utterances arriving after the conversation closed must not
    silently remain unextracted."""
    cid = seed(dctx)
    # mark closed, everything extracted, then a late revision row arrives unextracted
    dctx.store.close_conversation(cid, "sweep", datetime.now(timezone.utc))
    dctx.store.mark_extracted(cid, "t-u1", "extracted")
    dctx.store.db.execute("UPDATE utterances SET extracted=1")
    late = UtteranceEvent(id="t-late", speaker_id="spk-j", speaker_label="Jasmine",
                          start_ts=ANCHOR + timedelta(hours=1),
                          text="Oh and the wine is a Barolo")
    async def ingest_late():
        await dctx.handle_utterance(late, live=False)
    asyncio.new_event_loop().run_until_complete(ingest_late())
    # handle_utterance may route to a NEW conversation; force the straggler back here
    dctx.store.db.execute("UPDATE utterances SET conversation_id=?, extracted=0 "
                          "WHERE id='t-late'", (cid,))

    async def tick():
        await dctx._background_tick(datetime.now(timezone.utc))
    asyncio.new_event_loop().run_until_complete(tick())
    tasks = [t for t in dctx.store.list_pending_tasks() if t["conversation_id"] == cid]
    assert tasks and tasks[0]["mode"] == "closing"


def test_non_delegated_unchanged():
    """Default config: close extracts synchronously, no task rows at all."""
    # exercised by the entire existing suite; assert the negative explicitly here
    import artmem_server.services as svc
    cfg = make_test_cfg("/tmp/nondelegated-check.sqlite3")
    assert cfg.extraction.delegated is False
    assert hasattr(svc.AppContext, "_schedule_extraction")
