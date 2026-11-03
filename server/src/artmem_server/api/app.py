"""HTTP API (spec §4.1). Bearer token on every route including /v1/health."""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from ..core.clarifications import ClarificationRow, rank
from .. import __version__
from ..models import (
    ClientEnvelope,
    FeedbackMessage,
    IngestBatchRequest,
    IngestBatchResponse,
    QueryRequest,
    ServerEnvelope,
    SpeakerMerge,
    SpeakerPatch,
    iso,
    parse_iso,
    utc_now,
)
from ..pipeline.relevance import EngineEvent
from ..services import AppContext

log = logging.getLogger("artmem.api")


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(
        title="ArtificialMemory",
        version=__version__,
        description="Self-hosted artificial memory: ingest, extraction, live relevance, "
                    "clarifications. Auth: `Authorization: Bearer <token>` on every route.",
    )
    token = ctx.cfg.token

    def auth(request: Request) -> None:
        if not token:
            raise HTTPException(503, "server has no ARTMEM_TOKEN configured")
        hdr = request.headers.get("authorization", "")
        supplied = hdr[7:] if hdr.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(401, "invalid token")

    def ws_authorized(ws: WebSocket) -> bool:
        hdr = ws.headers.get("authorization", "")
        supplied = hdr[7:] if hdr.lower().startswith("bearer ") else ""
        return bool(token) and hmac.compare_digest(supplied, token)

    # ---------------------------------------------------------------- health/meta
    @app.get("/v1/health", dependencies=[Depends(auth)], tags=["meta"])
    async def health() -> dict[str, Any]:
        stats = ctx.store.stats()
        llm = await ctx.llm.probe()
        return {
            "ok": True, "version": __version__, "time": iso(utc_now()),
            "counts": stats, "llm": llm,
            "prompts": ctx.prompts.refs(), "prompts_hash": ctx.prompts.content_hash(),
        }

    # ---------------------------------------------------------------- ingest
    @app.post("/v1/ingest/batch", response_model=IngestBatchResponse,
              dependencies=[Depends(auth)], tags=["ingest"])
    async def ingest_batch(req: IngestBatchRequest) -> IngestBatchResponse:
        accepted: list[str] = []
        duplicates: list[str] = []
        for ev in req.utterances:
            if ev.revision > 0:
                await ctx.apply_revision(ev)
                accepted.append(ev.id)
                continue
            if ctx.store.get_utterance(ev.id) is not None:
                duplicates.append(ev.id)
                continue
            await ctx.handle_utterance(ev, live=False)  # catch-up: no live cards (spec §3.4)
            accepted.append(ev.id)
        return IngestBatchResponse(accepted_ids=accepted, duplicate_ids=duplicates,
                                   server_time=utc_now())

    # ---------------------------------------------------------------- memories
    @app.get("/v1/memories", dependencies=[Depends(auth)], tags=["memories"])
    async def memories(q: str = "", entity: str = "", speaker: str = "",
                       from_: str = Query("", alias="from"), to: str = "",
                       include_inactive: bool = False,
                       limit: int = 50, offset: int = 0) -> dict[str, Any]:
        from ..models import parse_iso

        entity_ids: set[str] | None = None
        if entity:
            entity_ids = ctx.store.entity_ids_for_names({entity})
            if not entity_ids:
                return {"memories": [], "total": 0}
        vec = await ctx.embedder.embed_one(q) if q else None
        time_range = None
        if from_ or to:
            try:
                a = parse_iso(from_) if from_ else None
                b = parse_iso(to) if to else None
                time_range = (a or b, b or a)
            except ValueError as exc:
                raise HTTPException(422, f"bad from/to: {exc}") from exc
        hits = ctx.store.search(q, vec, entity_ids=entity_ids, time_range=time_range,
                                speaker_id=speaker or None,
                                status=None if include_inactive else "active",
                                limit=min(limit, 200))
        if not q and not entity_ids and not time_range:
            # plain browse: newest first
            rows = ctx.store.db.query(
                "SELECT * FROM memories WHERE status=? " if not include_inactive else
                "SELECT * FROM memories WHERE 1=1 ",
                (("active",) if not include_inactive else ()))
            rows = sorted(rows, key=lambda r: r["recorded_at"], reverse=True)[offset:offset + limit]
            return {"memories": [ctx.store.memory_out(r).model_dump() for r in rows],
                    "total": len(rows)}
        out = [ctx.store.memory_out(h.row).model_dump() for h in hits]
        return {"memories": out, "total": len(out)}

    @app.get("/v1/memories/sync", dependencies=[Depends(auth)], tags=["memories"])
    async def memories_sync() -> dict[str, Any]:
        """Full state incl. superseded/retracted (phone mirrors locally; status field
        drives 'still true?' UI). Count changes ride WS heartbeats."""
        return {"server_time": iso(utc_now()), "memories": ctx.store.memories_sync()}

    @app.get("/v1/memories/{memory_id}", dependencies=[Depends(auth)], tags=["memories"])
    async def get_memory(memory_id: str) -> dict[str, Any]:
        row = ctx.store.get_memory(memory_id)
        if row is None:
            raise HTTPException(404, "no such memory")
        out = ctx.store.memory_out(row).model_dump()
        out["superseded_by_memories"] = [dict(r) for r in ctx.store.list_supersedes(memory_id)]
        return out

    @app.patch("/v1/memories/{memory_id}", dependencies=[Depends(auth)], tags=["memories"])
    async def patch_memory(memory_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = ctx.store.get_memory(memory_id)
        if row is None:
            raise HTTPException(404, "no such memory")
        action = body.get("action")
        if action == "retract":
            ctx.store.retract(memory_id)
        elif action == "delete":
            ctx.store.delete_memory(memory_id)
        elif action == "pin":
            pass  # pinning is a card-level action; memory-level no-op for now
        else:
            raise HTTPException(422, "action must be retract|delete")
        return {"ok": True, "id": memory_id, "action": action}

    @app.delete("/v1/memories/{memory_id}", dependencies=[Depends(auth)], tags=["memories"])
    async def delete_memory(memory_id: str) -> dict[str, Any]:
        if not ctx.store.delete_memory(memory_id):
            raise HTTPException(404, "no such memory")
        return {"ok": True, "id": memory_id}

    # ---------------------------------------------------------------- conversations
    @app.get("/v1/conversations", dependencies=[Depends(auth)], tags=["conversations"])
    async def conversations(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        rows = ctx.store.conversations_list(min(limit, 200), offset)
        return {"conversations": [dict(r) for r in rows]}

    @app.get("/v1/conversations/{conversation_id}", dependencies=[Depends(auth)],
             tags=["conversations"])
    async def conversation(conversation_id: str) -> dict[str, Any]:
        row = ctx.store.get_conversation(conversation_id)
        if row is None:
            raise HTTPException(404, "no such conversation")
        utts = [dict(r) for r in ctx.store.conversation_utterances(conversation_id)]
        return {"conversation": dict(row), "utterances": utts}

    @app.post("/v1/conversations/{conversation_id}/end", dependencies=[Depends(auth)],
              tags=["conversations"])
    async def end_conversation(conversation_id: str) -> dict[str, Any]:
        return await ctx.close_conversation(conversation_id, "manual")

    # ---------------------------------------------------------------- speakers
    @app.get("/v1/speakers", dependencies=[Depends(auth)], tags=["speakers"])
    async def speakers() -> dict[str, Any]:
        return {"speakers": ctx.store.list_speakers()}

    @app.patch("/v1/speakers/{speaker_id}", dependencies=[Depends(auth)], tags=["speakers"])
    async def patch_speaker(speaker_id: str, patch: SpeakerPatch) -> dict[str, Any]:
        if ctx.store.get_speaker(speaker_id) is None:
            raise HTTPException(404, "no such speaker")
        if patch.name:
            ctx.store.rename_speaker(speaker_id, patch.name)
            ctx.store.upsert_vocabulary(patch.name, kind="name", source="manual")
        if patch.is_owner:
            ctx.store.set_owner(speaker_id)
        return {"ok": True, "id": speaker_id}

    @app.post("/v1/speakers/merge", dependencies=[Depends(auth)], tags=["speakers"])
    async def merge_speakers(body: SpeakerMerge) -> dict[str, Any]:
        ok = ctx.store.merge_speakers(body.from_speaker_id, body.into_speaker_id)
        if not ok:
            raise HTTPException(400, "merge failed (same id or missing speaker)")
        return {"ok": True}

    @app.delete("/v1/speakers/{speaker_id}", dependencies=[Depends(auth)], tags=["speakers"])
    async def delete_speaker(speaker_id: str) -> dict[str, Any]:
        if not ctx.store.delete_speaker(speaker_id):
            raise HTTPException(404, "no such speaker")
        return {"ok": True}

    # ---------------------------------------------------------------- clarifications
    @app.get("/v1/clarifications", dependencies=[Depends(auth)], tags=["clarifications"])
    async def clarifications(status: str = "open") -> dict[str, Any]:
        statuses = tuple(s.strip() for s in status.split(",")) if status != "all" else \
            ("open", "reminded", "answered", "expired")
        return {"clarifications": ctx.store.list_clarifications(statuses)}

    @app.get("/v1/clarifications/digest", dependencies=[Depends(auth)],
             tags=["clarifications"])
    async def clarifications_digest() -> dict[str, Any]:
        rows_dicts = ctx.store.list_clarifications(("open", "reminded"))
        rows = [
            ClarificationRow(
                id=d["id"], kind=d["kind"], question=d["question"], importance=d["importance"],
                uncertainty=d["uncertainty"], created_at=parse_iso(d["created_at"]),
                expires_at=parse_iso(d["expires_at"]), status=d["status"],
                reminded=bool(d["reminded"]),
            )
            for d in rows_dicts
        ]
        picked = rank(rows, utc_now(), ctx.cfg.clarifications.max_per_digest)
        return {"questions": [d for d in rows_dicts if d["id"] in {p.id for p in picked}]}

    @app.post("/v1/clarifications/{clarification_id}/answer", dependencies=[Depends(auth)],
              tags=["clarifications"])
    async def answer_clarification(clarification_id: str, body: dict[str, Any]) -> dict[str, Any]:
        answer = str(body.get("answer", "")).strip()
        if not answer:
            raise HTTPException(422, "answer required")
        result = await ctx.answer_clarification(clarification_id, answer,
                                                str(body.get("source", "text")))
        if "error" in result:
            raise HTTPException(400 if result["error"] != "not found" else 404, result["error"])
        return result

    # ---------------------------------------------------------------- vocabulary
    @app.get("/v1/vocabulary", dependencies=[Depends(auth)], tags=["vocabulary"])
    async def vocabulary() -> dict[str, Any]:
        return {"vocabulary": ctx.store.list_vocabulary(),
                "speakers_version": int(ctx.store.kv_get("speakers_version") or "0")}

    @app.post("/v1/vocabulary", dependencies=[Depends(auth)], tags=["vocabulary"])
    async def add_vocabulary(body: dict[str, Any]) -> dict[str, Any]:
        term = str(body.get("term", "")).strip()
        if not term:
            raise HTTPException(422, "term required")
        ctx.store.upsert_vocabulary(term, kind=str(body.get("kind", "other")),
                                    definition=body.get("definition"), source="manual")
        return {"ok": True, "term": term.lower()}

    # ---------------------------------------------------------------- extraction tasks
    # Phone executes the LLM with its own credentials; this queue survives phone death.
    @app.get("/v1/tasks/pending", dependencies=[Depends(auth)], tags=["tasks"])
    async def tasks_pending() -> dict[str, Any]:
        rows = ctx.store.list_pending_tasks()
        return {"tasks": rows, "count": len(rows),
                "prompts_hash": ctx.prompts.content_hash()}

    @app.get("/v1/tasks/{task_id}/context", dependencies=[Depends(auth)], tags=["tasks"])
    async def task_context(task_id: str) -> dict[str, Any]:
        task = ctx.store.get_extraction_task(task_id)
        if task is None:
            raise HTTPException(404, "no such task")
        job = ctx.extraction.build_job(task["conversation_id"], task["mode"])
        if job is None:
            # nothing unextracted (revision or race) — close it out, no LLM needed
            ctx.store.complete_extraction_task(task_id, "phone", error=None)
            return {"completed": True, "reason": "no unextracted utterances"}
        last_utt = job.rows[-1]["id"]
        ctx.store.set_task_snapshot(task_id, last_utt, ctx.prompts.content_hash())
        return {
            "completed": False,
            "task_id": task_id,
            "conversation_id": task["conversation_id"],
            "mode": task["mode"],
            "profile": "deep",
            "messages": job.messages,              # verbatim server-rendered prompt
            "prompts_hash": ctx.prompts.content_hash(),
            "utterance_count": len(job.rows),
        }

    @app.post("/v1/tasks/{task_id}/result", dependencies=[Depends(auth)], tags=["tasks"])
    async def task_result(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        raw = str(body.get("raw_json", ""))
        task = ctx.store.get_extraction_task(task_id)
        if task is None:
            raise HTTPException(404, "no such task")
        verdict = ctx.store.claim_extraction_task(task_id, ctx.prompts.content_hash())
        if verdict == "stale_hash":
            raise HTTPException(409, {"error": "stale_prompts",
                                      "prompts_hash": ctx.prompts.content_hash()})
        if verdict == "not_claimable":
            raise HTTPException(409, {"error": "already_completed_or_running",
                                      "status": task["status"]})
        try:
            result = await ctx.extraction.apply_remote_result(
                raw, task["conversation_id"], task["mode"], task["last_utt_id"])
        except Exception as exc:  # noqa: BLE001 — schema errors relay for phone-side repair
            ctx.store.release_task_to_pending(task_id, error=str(exc)[:300])
            raise HTTPException(422, {"error": "invalid_json",
                                      "detail": str(exc)[:1200],
                                      "hint": "repair your JSON against the schema and "
                                              "re-POST; POST /abandon to hand off"}) from exc
        fresh = ctx.store.get_extraction_task(task_id)
        job = ctx.extraction.build_job(task["conversation_id"], task["mode"],
                                       up_to_utt_id=fresh["last_utt_id"])
        if job is None:
            ctx.store.complete_extraction_task(task_id, "phone")
            return {"ok": True, "facts_created": 0, "memories": [], "clarifications": 0}
        outcome = await ctx.extraction.apply_result(job, result)
        ctx.store.complete_extraction_task(task_id, "phone")
        await ctx._dispatch(EngineEvent(type="status"))
        return {"ok": True, "facts_created": len(outcome.facts_created),
                "memories": outcome.memory_texts,
                "clarifications": len(outcome.clarifications_created)}

    @app.post("/v1/tasks/{task_id}/abandon", dependencies=[Depends(auth)], tags=["tasks"])
    async def task_abandon(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ctx.store.abandon_extraction_task(task_id, str(body.get("reason", "phone gave up")))
        return {"ok": True, "server_will_run": True}

    # ---------------------------------------------------------------- prompts (parity)
    @app.get("/v1/prompts", dependencies=[Depends(auth)], tags=["meta"])
    async def prompts() -> dict[str, Any]:
        return {"prompts_hash": ctx.prompts.content_hash(),
                "prompts": {name: {"version": p.version, "body": p.body}
                            for name, p in ctx.prompts.prompts.items()}}

    # ---------------------------------------------------------------- query
    @app.post("/v1/query", response_model=dict, dependencies=[Depends(auth)], tags=["query"])
    async def query(req: QueryRequest) -> dict[str, Any]:
        card = await ctx.lookup.memory_card(req.q, priority=False, synthesize=req.synthesize)
        mems = []
        for mid in card.debug_features.get("memory_ids", []):
            row = ctx.store.get_memory(mid)
            if row is not None:
                mems.append(ctx.store.memory_out(row).model_dump())
        return {"card": card.model_dump(), "memories": mems,
                "answer": card.headline if card.debug_features.get("synthesized") else None}

    # ---------------------------------------------------------------- export
    @app.get("/v1/export", dependencies=[Depends(auth)], tags=["meta"])
    async def export(fmt: str = "json") -> Any:
        data = ctx.store.export_all()
        if fmt == "markdown":
            return PlainTextResponse(_to_markdown(data), media_type="text/markdown")
        fname = f"artmem-export-{utc_now().strftime('%Y%m%d')}.json"
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # ---------------------------------------------------------------- feedback (HTTP mirror of WS)
    @app.post("/v1/feedback", dependencies=[Depends(auth)], tags=["live"])
    async def feedback(body: FeedbackMessage) -> dict[str, Any]:
        memory_id = ctx.apply_feedback(body.card_id, body.kind)
        return {"ok": True, "card_id": body.card_id, "memory_id": memory_id}

    # ---------------------------------------------------------------- websocket
    @app.websocket("/v1/live")
    async def live(ws: WebSocket) -> None:
        if not ws_authorized(ws):
            await ws.close(code=4401)
            return
        await ws.accept()

        async def send_event(ev) -> None:
            env = ServerEnvelope(
                type=ev.type, card=ev.card, card_id=ev.card_id,
                status={"active_cards": len(ctx.engine.active),
                        "stats": ctx.store.stats()} if ev.type == "status" else None,
            )
            await ws.send_json(env.model_dump(mode="json"))

        ctx.add_card_listener(send_event)
        await ws.send_json(ServerEnvelope(
            type="status",
            status={"server_time": iso(utc_now()), "listening": True,
                    "stats": ctx.store.stats(),
                    "active_cards": len(ctx.engine.active)},
        ).model_dump(mode="json"))
        try:
            while True:
                msg = await ws.receive_json()
                try:
                    env = ClientEnvelope.model_validate(msg)
                except Exception as exc:  # noqa: BLE001
                    await ws.send_json({"type": "status", "status": {"error": str(exc)[:200]}})
                    continue
                if env.type == "heartbeat":
                    await ws.send_json(ServerEnvelope(
                        type="status",
                        status={"server_time": iso(utc_now()), "ok": True,
                                "queue_hint": ctx.store.stats(),
                                "pending_tasks": ctx.store.pending_task_count(),
                                "memories": ctx.store.stats()["memories"]},
                    ).model_dump(mode="json"))
                elif env.type in ("utterance", "utterance_revision") and env.utterance:
                    if env.type == "utterance_revision":
                        changed = await ctx.apply_revision(env.utterance)
                        await ws.send_json(ServerEnvelope(
                            type="status",
                            status={"revision_applied": changed, "id": env.utterance.id},
                        ).model_dump(mode="json"))
                        continue
                    conv_id, events = await ctx.handle_utterance(env.utterance)
                    for ev in events:
                        await send_event(ev)
                elif env.type == "feedback" and env.feedback:
                    memory_id = ctx.apply_feedback(env.feedback.card_id, env.feedback.kind)
                    if env.feedback.kind in ("dismiss",):
                        await ws.send_json(ServerEnvelope(
                            type="card_dismiss", card_id=env.feedback.card_id,
                        ).model_dump(mode="json"))
                    _ = memory_id
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("ws session error")
        finally:
            ctx.remove_card_listener(send_event)

    return app


def _to_markdown(data: dict[str, Any]) -> str:
    """Human-readable Markdown dump (spec §6 export)."""
    out = ["# ArtificialMemory export", "", f"Exported: {data['exported_at']}", ""]
    mems = {m["id"]: m for m in data["memories"]}
    out.append(f"## Memories ({len(mems)})")
    out.append("")
    for m in data["memories"]:
        status = "" if m["status"] == "active" else f" [{m['status']}]"
        out.append(f"- **{m['text']}**  ({m['type']}, imp {m['importance']:.2f}, "
                   f"conf {m['confidence']:.2f}, recorded {m['recorded_at'][:10]}){status}")
        st = json.loads(m["structured_json"]) if isinstance(m["structured_json"], str) \
            else m["structured_json"]
        if st.get("time_phrase"):
            out.append(f"  - time: “{st['time_phrase']}” → {st.get('time_resolved') or 'unresolved'}")
        if m["event_start"]:
            out.append(f"  - event: {m['event_start'][:10]}"
                       + (f" → {m['event_end'][:10]}" if m["event_end"] else ""))
        out.append(f"  - source utterances: {m['source_utterance_ids']}")
    convs = {c["id"]: c for c in data["conversations"]}
    out.append("")
    out.append(f"## Conversations ({len(convs)})")
    for c in data["conversations"]:
        out.append(f"\n### {c['started_at']} → {c['closed_at'] or 'open'} "
                   f"({c['utterance_count']} utterances)")
        if c.get("summary"):
            out.append(f"*{c['summary']}*")
    utts_by_conv: dict[str, list[dict]] = {}
    for u in data["utterances"]:
        utts_by_conv.setdefault(u["conversation_id"] or "-", []).append(u)
    for cid, utts in utts_by_conv.items():
        out.append(f"\n#### conversation {cid}")
        for u in sorted(utts, key=lambda x: x["start_ts"]):
            out.append(f"- `{u['start_ts'][11:19]}` **{u['speaker_label']}**: {u['text']}")
    out.append("")
    out.append(f"## Speakers ({len(data['speakers'])})")
    for s in data["speakers"]:
        out.append(f"- {s['label']}" + (f" ({s['name']})" if s["name"] else "")
                   + (" [owner]" if s["is_owner"] else "") + f" — {s['status']}")
    out.append("")
    out.append(f"## Vocabulary ({len(data['vocabulary'])})")
    for v in data["vocabulary"]:
        out.append(f"- **{v['term']}** ({v['kind']})"
                   + (f": {v['definition']}" if v["definition"] else ""))
    return "\n".join(out)
