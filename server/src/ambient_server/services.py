"""Application services: composition root + orchestration.

`AppContext` wires store/llm/prompts/pipelines and exposes the two operations the
transports (HTTP, WS, replay harness) share:
  - handle_utterance(ev)  → conversation routing, ingest, live evaluation, incremental hooks
  - apply_feedback(...)   → feedback logging + web-card pinning
The background task runs: conversation close sweep → closing extraction, clarification
expiry/reminders/digest, entity observation regeneration, card fading.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

from .config import AppConfig
from .core.clarifications import ClarificationRow, next_transitions, rank
from .db.connection import Database
from .db.store import Store
from .llm.gateway import LLMGateway, LLMError
from .llm.prompts import PromptSet
from .models import UtteranceEvent, iso, parse_iso, utc_now
from .pipeline.embeddings import EmbeddingService
from .pipeline.extraction import ExtractionPipeline
from .pipeline.lookup import LookupAgent
from .pipeline.relevance import EngineEvent, LiveEngine
from .pipeline.search import make_search

log = logging.getLogger("ambient.services")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class AppContext:
    """Everything the server does, decoupled from FastAPI so the replay harness can drive it."""

    def __init__(self, cfg: AppConfig, data_dir: Path | None = None,
                 llm_client=None, search_client=None):
        """llm_client / search_client: httpx.AsyncClient injection for tests and the
        replay harness (fake LLM endpoints)."""
        self.cfg = cfg
        db_path = Path(cfg.storage.db_path)
        if data_dir is not None:
            db_path = data_dir / "ambient.sqlite3"
        self.db = Database(db_path)
        self.db.migrate()
        self.store = Store(self.db, cfg, embed_dim=cfg.llm.embedding_dim or 384)
        self.llm = LLMGateway(cfg.llm, client=llm_client)
        prompts_dir = Path(__file__).resolve().parents[2] / "prompts"
        if not prompts_dir.is_dir():  # packaged install / tests run from another cwd
            cand = Path.cwd() / "prompts"
            prompts_dir = cand if cand.is_dir() else prompts_dir
        self.prompts = PromptSet.load(prompts_dir)
        self.embedder = EmbeddingService(self.llm)
        self.search = make_search(cfg.search, client=search_client)
        self.extraction = ExtractionPipeline(self.store, self.llm, self.prompts, cfg,
                                             self.embedder)
        self.lookup = LookupAgent(self.store, self.llm, self.prompts, cfg, self.embedder,
                                  self.search)
        self.engine: LiveEngine = LiveEngine(self.store, self.llm, self.prompts, cfg,
                                             self.embedder, self.lookup)
        self.card_listeners: list[Callable[[EngineEvent], Coroutine]] = []
        self.engine.card_sink = self._dispatch
        self._bg_task: asyncio.Task | None = None
        self._extraction_tasks: set[asyncio.Task] = set()
        self.card_sink = None

    # ---------------------------------------------------------------- lifecycle
    async def start_background(self) -> None:
        if self._bg_task is None:
            self._bg_task = asyncio.ensure_future(self._background_loop())

    async def aclose(self) -> None:
        if self._bg_task:
            self._bg_task.cancel()
            self._bg_task = None
        for t in list(self._extraction_tasks):
            t.cancel()
        await self.llm.aclose()
        if hasattr(self.search, "aclose"):
            await self.search.aclose()
        self.db.close()

    def add_card_listener(self, fn: Callable[[EngineEvent], Coroutine]) -> None:
        self.card_listeners.append(fn)

    def remove_card_listener(self, fn: Callable[[EngineEvent], Coroutine]) -> None:
        if fn in self.card_listeners:
            self.card_listeners.remove(fn)

    async def _dispatch(self, ev: EngineEvent) -> None:
        for fn in list(self.card_listeners):
            try:
                await fn(ev)
            except Exception:  # noqa: BLE001 - one dead listener must not stop others
                log.exception("card listener failed")

    # ---------------------------------------------------------------- utterance ingest
    async def handle_utterance(self, ev: UtteranceEvent, live: bool = True) -> tuple[str, list[EngineEvent]]:
        """Route to a conversation (hint → open → new), store, evaluate live.

        `live=False` is for batch catch-up: stored + extraction hooks run, but the live
        relevance engine does not (out-of-order replays must not surface stale cards —
        spec §3.4). Returns (conversation_id_or_None, engine events)."""
        now = utc_now()
        conv_id: str | None = None
        if ev.is_whisper:
            # whispered queries are never conversation content (spec §3.6); store as a
            # query record row with no conversation
            conv_id = None
            vec = await self.embedder.embed_one(ev.text)
            status = self.store.insert_utterance(ev, conv_id, vec)
            events = await self.engine.on_utterance(self.store.get_utterance(ev.id)) \
                if status == "inserted" else []
            if status == "duplicate":
                events = []
            return conv_id, events

        conv = self._route_conversation(ev, now)
        conv_id = conv
        vec = await self.embedder.embed_one(ev.text)
        status = self.store.insert_utterance(ev, conv_id, vec)
        if status == "duplicate":
            return conv_id, []
        row = self.store.get_utterance(ev.id)
        events = await self.engine.on_utterance(row) if live else []
        # incremental extraction every N utterances (spec §5.2)
        n = self.cfg.conversations.incremental_every
        if n and conv_id:
            conv_row = self.store.get_conversation(conv_id)
            if conv_row and conv_row["utterance_count"] % n == 0:
                self._spawn(self.extraction.extract(conv_id, mode="incremental"),
                            label=f"incremental-extract:{conv_id}")
        return conv_id, events

    def _route_conversation(self, ev: UtteranceEvent, now: datetime) -> str:
        cfg = self.cfg.conversations
        open_convs = self.store.open_conversations()
        # 1. explicit client hint wins if open
        if ev.conversation_hint:
            hint = self.store.get_conversation(ev.conversation_hint)
            if hint is not None and hint["closed_at"] is None:
                return hint["id"]
        ts = ev.end_ts or ev.start_ts
        # 2. nearest open conversation satisfying gap + location rules
        for c in open_convs:
            last = parse_iso(c["last_utterance_at"])
            if ts - last > timedelta(seconds=cfg.silence_gap_s):
                self.store.close_conversation(c["id"], "silence", last + timedelta(seconds=cfg.silence_gap_s))
                continue
            if ev.location and c["location_lat"] is not None:
                d = haversine_m(c["location_lat"], c["location_lon"],
                                ev.location.lat, ev.location.lon)
                if d > cfg.location_move_m:
                    self.store.close_conversation(c["id"], "location", now)
                    continue
            self._maybe_update_conv_location(c, ev)
            return c["id"]
        # 3. new conversation
        cid = self.store.create_conversation(now)
        if ev.location:
            self.store.db.execute(
                "UPDATE conversations SET location_lat=?, location_lon=?, location_accuracy_m=? "
                "WHERE id=?",
                (ev.location.lat, ev.location.lon, ev.location.accuracy_m, cid),
            )
        return cid

    def _maybe_update_conv_location(self, conv, ev: UtteranceEvent) -> None:
        if ev.location and conv["location_lat"] is None:
            self.store.db.execute(
                "UPDATE conversations SET location_lat=?, location_lon=?, location_accuracy_m=? "
                "WHERE id=?",
                (ev.location.lat, ev.location.lon, ev.location.accuracy_m, conv["id"]),
            )

    async def apply_revision(self, ev: UtteranceEvent) -> bool:
        changed = self.store.apply_revision(ev.id, ev.revision, ev.text, ev.speaker_id,
                                            ev.speaker_label)
        return changed

    def apply_feedback(self, card_id: str, kind: str) -> str | None:
        memory_id = self.engine.on_feedback(card_id, kind)
        # spec §5.4: web answers are never memories unless the owner pins them
        if kind == "pin":
            row = self.store.db.query_one(
                "SELECT c.id, c.kind, m.id AS mid FROM cards_shown c "
                "LEFT JOIN memories m ON m.id = c.memory_id WHERE c.id=?", (card_id,))
            if row and row["kind"] == "lookup" and row["mid"] is None:
                card_row = self.store.db.query_one(
                    "SELECT features_json FROM cards_shown WHERE id=?", (card_id,))
                feats = json.loads(card_row["features_json"]) if card_row else {}
                answer = feats.get("_headline") or feats.get("question") or ""
                # pinned web answer becomes a memory with explicit provenance
                if answer:
                    self.store.insert_memory(
                        text=answer, structured={"pinned_web_answer": feats},
                        type="personal_fact", importance=0.6, confidence=0.9,
                        conversation_id=None, speaker_id=None, source_utterance_ids=[],
                        source="lookup_pinned",
                    )
        return memory_id

    # ---------------------------------------------------------------- extraction helpers
    def _spawn(self, coro, label: str) -> None:
        task = asyncio.ensure_future(coro)
        self._extraction_tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._extraction_tasks.discard(task)
            if not t.cancelled() and t.exception() is not None:
                log.error("%s failed: %s", label, t.exception())

        task.add_done_callback(done)

    async def close_conversation(self, conversation_id: str, reason: str = "manual") -> dict:
        conv = self.store.get_conversation(conversation_id)
        if conv is None or conv["closed_at"] is not None:
            return {"error": "not open"}
        self.store.close_conversation(conversation_id, reason, utc_now())
        outcome = await self.extraction.extract(conversation_id, mode="closing")
        return outcome.to_dict()

    # ---------------------------------------------------------------- background
    async def _background_loop(self) -> None:
        while True:
            try:
                await self._background_tick(utc_now())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("background tick failed")
            await asyncio.sleep(10)

    async def _background_tick(self, now: datetime) -> None:
        # 1. close conversations past the silence gap; run closing extraction
        for cid, reason in self.store.sweep_due_closes(now):
            self._spawn(self._closing_extract(cid), label=f"closing-extract:{cid}")
        # 2. fade expired cards
        for ev in await self.engine.tick(now):
            await self._dispatch(ev)
        # 3. clarification expiry/reminders
        rows = [self._clar_row(r) for r in self.store.list_all_clarifications()]
        for cid, status in next_transitions(rows, now, self.cfg.clarifications.expiry_days,
                                            self.cfg.clarifications.remind_after_days):
            if status == "expired":
                self.store.set_clarification_status(cid, "expired")
            elif status == "reminded":
                self.store.mark_reminded(cid)
        # 4. observation regeneration queue drain (max 2/tick to keep LLM load smooth)
        await self._drain_observations(limit=2)
        # 5. daily digest marking (app reads /v1/clarifications; push later)
        await self._maybe_mark_digest(now)

    async def _closing_extract(self, cid: str) -> None:
        outcome = await self.extraction.extract(cid, mode="closing")
        if outcome.facts_created:
            log.info("closed+extracted %s: %d memories", cid, len(outcome.facts_created))

    def _clar_row(self, d: dict) -> ClarificationRow:
        return ClarificationRow(
            id=d["id"], kind=d["kind"], question=d["question"], importance=d["importance"],
            uncertainty=d["uncertainty"], created_at=parse_iso(d["created_at"]),
            expires_at=parse_iso(d["expires_at"]), status=d["status"], reminded=bool(d["reminded"]),
        )

    async def _drain_observations(self, limit: int) -> None:
        dirty = json.loads(self.store.kv_get("observation_dirty") or "[]")
        if not dirty:
            due = self.store.entities_needing_observation(min_new=1)
            dirty = [d["id"] for d in due]
        if not dirty:
            return
        batch, rest = dirty[:limit], dirty[limit:]
        self.store.kv_set("observation_dirty", json.dumps(rest))
        for eid in batch:
            ent = self.store.get_entity(eid)
            if ent is None:
                continue
            mems = self.store.active_memories_for_entity(eid, limit=40)
            if not mems:
                continue
            block = "\n".join(f"- ({m['recorded_at'][:10]}) {m['text']}" for m in mems)
            try:
                rendered = self.prompts["observe"].render(entity_name=ent["name"],
                                                          entity_kind=ent["kind"], memories=block)
                text = (await self.llm.complete_text("deep", rendered,
                                                     "Write the observation summary.")).strip()
                if text:
                    self.store.set_entity_observation(eid, text[:1200], len(mems))
            except LLMError:
                # keep it queued for the next drain
                rest.append(eid)
        self.store.kv_set("observation_dirty", json.dumps(sorted(set(rest))))

    async def _maybe_mark_digest(self, now: datetime) -> None:
        cfg = self.cfg.clarifications
        try:
            tz = ZoneInfo(cfg.timezone)
        except Exception:  # noqa: BLE001 - bad tz config shouldn't kill the loop
            tz = timezone.utc
        local = now.astimezone(tz)
        hh, mm = (int(x) for x in cfg.digest_local_time.split(":"))
        if (local.hour, local.minute) < (hh, mm):
            return
        key = f"digest_{local.date().isoformat()}"
        if self.store.kv_get(key):
            return
        rows = [self._clar_row(r) for r in self.store.list_clarifications(("open", "reminded"))]
        picked = rank(rows, now, cfg.max_per_digest)
        if not picked:
            return
        unsent = [r.id for r in picked
                  if not self.store.get_clarification(r.id)["digest_sent_at"]]
        if unsent:
            self.store.mark_digest_sent(unsent, now)
        self.store.kv_set(key, iso(now))

    # ---------------------------------------------------------------- clarification answers
    async def answer_clarification(self, clarification_id: str, answer: str,
                                   source: str = "text") -> dict[str, Any]:
        row = self.store.get_clarification(clarification_id)
        if row is None:
            return {"error": "not found"}
        if row["status"] not in ("open", "reminded"):
            return {"error": f"already {row['status']}"}
        ctx = json.loads(row["context_json"])
        subject = ctx.get("subject", "")
        ok = self.store.answer_clarification(clarification_id, answer, source)
        result: dict[str, Any] = {"id": clarification_id, "answered": ok,
                                  "memories_created": [], "vocabulary_added": []}
        # every answer is retained as a high-confidence memory (spec §7)
        mem_id = self.store.insert_memory(
            text=f"{row['question']} → {answer}",
            structured={"clarification_id": clarification_id, "kind": row["kind"],
                        "subject": subject, "answer": answer, "source": source},
            type="personal_fact", importance=0.85, confidence=0.98,
            conversation_id=row["conversation_id"], speaker_id=None,
            source_utterance_ids=[], source="user_clarification",
        )
        result["memories_created"].append(mem_id)

        kind = row["kind"]
        if kind == "speaker_identity" and row["related_speaker_id"]:
            name = _extract_name(answer)
            if name:
                existing = self.store.db.query_one(
                    "SELECT id FROM speakers WHERE (name=? OR label=?) AND id!=? AND merged_into IS NULL",
                    (name, name, row["related_speaker_id"]))
                if existing:
                    self.store.merge_speakers(row["related_speaker_id"], existing["id"])
                    result["merged_into"] = existing["id"]
                else:
                    self.store.rename_speaker(row["related_speaker_id"], name)
                    self.store.upsert_vocabulary(name, kind="name", source="clarification")
                    eid = self.store.upsert_entity(name, "person", unresolved=False)
                    result["vocabulary_added"].append(name)
                    self._mark_dirty({eid})
        elif kind == "unknown_entity" and subject:
            term = subject or answer[:40]
            self.store.upsert_vocabulary(term, kind="acronym", definition=answer,
                                         source="clarification")
            eid = self.store.resolve_entity_name(term)
            if eid:
                self.store.upsert_entity(term, "acronym", aliases=[answer[:60]],
                                         unresolved=False, entity_id=eid["id"])
                ent_id = eid["id"]
            else:
                ent_id = self.store.upsert_entity(term, "acronym", aliases=[answer[:60]],
                                                  unresolved=False)
            result["vocabulary_added"].append(term)
            self._mark_dirty({ent_id})
            # re-attach the newly-known entity to the memories this clarification referenced
            for mid in json.loads(row["related_memory_ids"]):
                self.store.attach_memory_entities(mid, [(ent_id, "about")])
        else:
            pass  # generic: the retained QA memory is the improvement
        return result

    def _mark_dirty(self, entity_ids: set[str]) -> None:
        dirty = set(json.loads(self.store.kv_get("observation_dirty") or "[]"))
        dirty |= entity_ids
        self.store.kv_set("observation_dirty", json.dumps(sorted(dirty)))


def _extract_name(answer: str) -> str | None:
    """Pull a name out of 'that's Jasmine' / 'Jasmine' / 'I think it was Raj's sister'."""
    a = answer.strip().rstrip(".")
    a = a.lower()
    for prefix in ("that's ", "that is ", "this is ", "it's ", "it is ", "she is ", "he is ",
                   "she's ", "he's ", "their ", "his ", "my ", "the "):
        if a.startswith(prefix):
            a = a[len(prefix):]
    words = [w for w in a.split() if w.isalpha() and w not in
             ("the", "a", "an", "was", "i", "think", "believe", "probably", "guess")]
    if not words:
        return None
    name = " ".join(words[:2])
    return name.title()
