"""Live Relevance Engine (spec §5.4) + unanswered-question handling (§5.4 questions).

Holds the rolling conversation window, active card set, and topic state. Emits a list of
events ("card" | "card_update" | "card_dismiss") that the WS layer / replay harness
consume. Every decision is logged with its full feature vector (spec §5.4.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from ..config import AppConfig
from ..core.dates import resolve_time_phrase
from ..core.gating import gate_candidates, is_topic_shift, should_expire_card
from ..core.normalize import extract_proper_nouns
from ..core.scoring import (
    ScoreFeatures,
    actionability_score,
    compute_score,
    dismissed_score,
    jaccard,
    recency_decay,
    speaker_match,
)
from ..db.store import Store
from ..llm.gateway import LLMError, LLMGateway
from ..llm.prompts import PromptSet
from ..models import Card, JudgeDecision, QuestionClassification, iso, parse_iso, utc_now
from .embeddings import EmbeddingService
from .lookup import LookupAgent

log = logging.getLogger("ambient.relevance")

_QUESTION_HINT = re.compile(
    r"\b(who|what|when|where|why|how|which|is|are|was|were|did|do|does|can|could|will|"
    r"would|should|anyone|everybody|remember|tell|know)\b",
    re.IGNORECASE,
)
_DATE_HINT = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|tomorrow|"
    r"tonight|today|yesterday|next|this|in|the)\s*"
    r"(week|month|weekend|day|night|morning|evening|[A-Z][a-z]+day|\d{1,2}(st|nd|rd|th)?)?",
    re.IGNORECASE,
)


@dataclass
class ActiveCard:
    card: Card
    shown_at: datetime
    last_topic_activity: datetime
    pinned: bool = False


@dataclass
class EngineEvent:
    type: str                 # card | card_update | card_dismiss | status
    card: Card | None = None
    card_id: str | None = None


@dataclass
class _PendingQuestion:
    utterance_id: str
    text: str
    speaker_id: str
    asked_at: datetime
    task: asyncio.TimerHandle | None = None


class LiveEngine:
    def __init__(self, store: Store, llm: LLMGateway, prompts: PromptSet, cfg: AppConfig,
                 embedder: EmbeddingService, lookup: LookupAgent):
        self.store = store
        self.llm = llm
        self.prompts = prompts
        self.cfg = cfg
        self.embed = embedder
        self.lookup = lookup
        self.card_sink = None  # async(ev: EngineEvent) -> None; WS broadcaster / harness
        self.window: list[dict] = []           # {id,text,speaker_id,start_ts}
        self.window_vec: list[float] | None = None
        self.prev_window_vec: list[float] | None = None
        self.active: dict[str, ActiveCard] = {}
        self._pending_q: dict[str, _PendingQuestion] = {}
        self._speaker_entities_cache: dict[str, tuple[float, set[str]]] = {}

    # ------------------------------------------------------------------ ingest hook
    async def on_utterance(self, row) -> list[EngineEvent]:
        """row: sqlite Row from utterances (post-insert)."""
        now = utc_now()
        utt = {
            "id": row["id"], "text": row["text"], "speaker_id": row["speaker_id"],
            "speaker_label": row["speaker_label"], "start_ts": parse_iso(row["start_ts"]),
            "is_whisper": bool(row["is_whisper"]),
        }
        # cancel pending question timers answered by ANOTHER speaker
        for qid, pq in list(self._pending_q.items()):
            if pq.speaker_id != utt["speaker_id"] and row["start_ts"] >= iso(pq.asked_at):
                if pq.task:
                    pq.task.cancel()
                del self._pending_q[qid]

        events: list[EngineEvent] = []
        if utt["is_whisper"]:
            # whispered owner query: never conversation content → priority card (spec §3.6)
            card = await self.lookup.memory_card(utt["text"], priority=True)
            if card:
                events.extend(self._show_card(card, now))
            return events

        self.window.append(utt)
        cutoff = now - timedelta(seconds=self.cfg.relevance.window_s)
        self.window = [u for u in self.window if u["start_ts"] >= cutoff][-40:]

        events: list[EngineEvent] = []
        await self.arm_question_timer(utt, now)
        events.extend(await self._evaluate(now))
        return events

    # ------------------------------------------------------------------ main pipeline
    async def _evaluate(self, now: datetime) -> list[EngineEvent]:
        events: list[EngineEvent] = []
        window_text = " ".join(u["text"] for u in self.window)
        if not window_text.strip():
            return events

        # Question etiquette (spec §5.4): when an answer-wait timer is armed for the
        # latest utterance, the question path owns surfacing — no preempting the humans
        # present with an immediate relevance card.
        if self.window[-1]["id"] in self._pending_q:
            return events

        vecs = await self.embed.embed_many([window_text, self.window[-1]["text"]])
        window_vec = vecs[0]  # latest-utterance vec unused while window dominates scoring

        # 5. topic shift → expire unpinned cards
        if is_topic_shift(self.window_vec, window_vec or [0.0], self.cfg.relevance):
            for cid in [c for c, ac in self.active.items()
                        if not ac.pinned and ac.card.kind == "relevance"]:
                events.append(EngineEvent("card_dismiss", card_id=cid))
                self.active.pop(cid, None)
            log.debug("topic shift detected")
        self.prev_window_vec, self.window_vec = self.window_vec, window_vec or self.window_vec

        # 1/2. cheap triggers + candidate retrieval
        ent_ids_ctx, ent_names_ctx = self.store.matched_entity_forms(window_text)
        context_entities = (set(extract_proper_nouns(window_text)) | ent_names_ctx
                            | {v["term"] for v in self.store.list_vocabulary()[:400]})
        entity_ids = ent_ids_ctx
        time_range = self._mentioned_range(window_text, self.window[-1]["start_ts"])

        hits = self.store.search(window_text, window_vec, entity_ids=entity_ids or None,
                                 time_range=time_range, status="active",
                                 limit=self.cfg.relevance.top_k)
        if not hits:
            return events

        # 3. scoring
        last_shown = self.store.last_shown_map()
        dismiss_ct = self.store.dismiss_counts()
        current_speaker = self.window[-1]["speaker_id"]
        w = self.cfg.relevance.weights
        scored: list[tuple[str, float, ScoreFeatures]] = []
        mem_entities: dict[str, list[str]] = {}
        for h in hits:
            row = h.row
            # echo gate: the live conversation is literally relaying this memory right now
            # → a card would duplicate, not recall (spec: cards must ADD remembered info)
            if _echo_coverage(row["text"], window_text) >= self.cfg.relevance.echo_suppress:
                log.debug("gate echo_suppressed %s", row["id"])
                continue
            ents = self.store.entities_for_memory(row["id"])
            mem_entities[row["id"]] = [e["name"] for e in ents]
            mem_ent_ids = {e["id"] for e in ents}
            features = ScoreFeatures(
                semantic=(h.semantic if h.semantic > 0 else
                          (0.55 * h.keyword if self.cfg.relevance.keyword_fallback else 0.0)),
                entity_overlap=jaccard({n.casefold() for n in mem_entities[row["id"]] if n},
                                       {n.casefold() for n in context_entities if n}),
                actionability=actionability_score(
                    row["type"],
                    parse_iso(row["event_start"]) if row["event_start"] else None,
                    now, self.cfg.relevance.actionability_horizon_days,
                    about_current_speaker=(row["speaker_id"] == current_speaker)
                    or bool(mem_ent_ids & self._speaker_entities(current_speaker)),
                    has_date=bool(row["event_start"]),
                ),
                importance=row["importance"],
                recency_decay=recency_decay(parse_iso(row["recorded_at"]), now,
                                            self.cfg.recall.half_life_h),
                speaker_match=speaker_match(
                    row["speaker_id"] == current_speaker,
                    bool(mem_ent_ids & self._speaker_entities(current_speaker)),
                ),
                recently_shown=1.0 if row["id"] in last_shown
                and (now - last_shown[row["id"]]).total_seconds() < self.cfg.relevance.cooldown_s
                else 0.0,
                dismissed_before=dismissed_score(dismiss_ct.get(row["id"], 0)),
                raw={"keyword": round(h.keyword, 3), "entity_hit": h.entity_hit,
                     "temporal_hit": h.temporal_hit, "window_id": self.window[-1]["id"]},
            )
            scored.append((row["id"], compute_score(features, w), features))
        scored.sort(key=lambda t: -t[1])

        # 4. gate: threshold + cooldown + budget
        decisions = gate_candidates(scored, now, last_shown, len(self.active), self.cfg.relevance)
        allowed = [d for d in decisions if d.allowed]

        # optional fast-LLM judge on top-3 (flag; A/B in replay harness)
        if allowed and self.cfg.relevance.llm_judge_enabled:
            allowed = (await self._llm_judge(allowed[:3], window_text)) + allowed[3:]

        rows_by_id = {h.row["id"]: h.row for h in hits}
        for d in allowed:
            row = rows_by_id[d.memory_id]
            if any(ac.card.memory_id == d.memory_id for ac in self.active.values()):
                continue  # already on screen: updated in place, no re-show flicker
            card = self._build_card(row, d, mem_entities.get(d.memory_id, []), now)
            events.extend(self._show_card(card, now))
        for d in decisions:
            if not d.allowed:
                log.debug("gate %s %s (%.2f)", d.reason, d.memory_id, d.score)
        return events

    def _show_card(self, card: Card, now: datetime) -> list[EngineEvent]:
        feats = {**card.debug_features, "_headline": card.headline, "_detail": card.detail,
                 "_url": card.url}
        self.store.record_card_shown(card.id, card.memory_id, card.kind, card.score, feats,
                                     self.window[-1]["id"] if self.window else None, now)
        self.active[card.id] = ActiveCard(card, now, now, pinned=False)
        return [EngineEvent("card", card=card)]

    def _build_card(self, row, decision, entity_names: list[str], now: datetime) -> Card:
        structured = json.loads(row["structured_json"])
        detail_bits = []
        if structured.get("time_phrase"):
            resolved = structured.get("time_resolved")
            detail_bits.append(structured["time_phrase"] + (f" → {resolved[:10]}" if resolved else ""))
        if structured.get("place"):
            detail_bits.append(structured["place"])
        if entity_names:
            detail_bits.append("· " + ", ".join(entity_names[:4]))
        speaker = self.store.get_speaker(row["speaker_id"]) if row["speaker_id"] else None
        who = (speaker["name"] or speaker["label"]) if speaker else None
        age = _human_age(parse_iso(row["recorded_at"]), now)
        chip = " · ".join(x for x in (who, age) if x)
        return Card(
            id=uuid4().hex, kind="relevance", memory_id=row["id"],
            headline=row["text"], detail=" · ".join(detail_bits),
            source_chip=chip, origin="memory", score=round(decision.score, 4),
            source_utterance_ids=json.loads(row["source_utterance_ids"]),
            expires_at=now + timedelta(seconds=self.cfg.relevance.card_ttl_s),
            debug_features={
                "score": decision.score, "reason": decision.reason,
                "gate_threshold": self.cfg.relevance.gate_threshold,
                "features": decision.features.as_dict(),
            },
        )

    # ------------------------------------------------------------------ questions
    async def arm_question_timer(self, utt: dict, now: datetime) -> None:
        """Cheap regex trigger → fast-LLM classify → arm the 2–5 s answer wait (spec §5.4).
        Awaited inline (one extra fast call only on hint-matching lines) so the relevance
        path can defer to the question path for the same utterance."""
        if not _QUESTION_HINT.search(utt["text"] or ""):
            return  # cheap no-LLM trigger; questions without any hint word are rare
        try:
            cls = await self.llm.complete_json(
                "fast", self.prompts["question"], QuestionClassification,
                user=utt["text"], text=utt["text"],
                speaker_label=utt["speaker_label"] or "Speaker",
                window=_window_text(self.window),
            )
        except (LLMError, KeyError) as exc:
            log.debug("question classify skipped: %s", exc)
            return
        if not cls.is_question or cls.addressed_to_someone:
            return
        pq = _PendingQuestion(utt["id"], utt["text"], utt["speaker_id"] or "",
                              utt["start_ts"], None)
        self._pending_q[utt["id"]] = pq
        loop = asyncio.get_running_loop()
        pq.task = loop.call_later(
            self.cfg.relevance.question_wait_s,
            lambda: asyncio.ensure_future(self._answer_question(pq, cls.topic)),
        )

    async def _answer_question(self, pq: _PendingQuestion, topic: str) -> None:
        """Nobody answered within question_wait_s → lookup agent (spec §5.4)."""
        self._pending_q.pop(pq.utterance_id, None)
        # re-check: an utterance by another speaker may have arrived during the wait
        if any(u["start_ts"] >= pq.asked_at and u["speaker_id"] != pq.speaker_id
               for u in self.window):
            return
        card = await self.lookup.aloud_question_card(pq.text, topic)
        if card:
            for ev in self._show_card(card, utc_now()):
                await self._broadcast(ev)

    async def _broadcast(self, ev: EngineEvent) -> None:
        if self.card_sink is not None:
            await self.card_sink(ev)


    # ------------------------------------------------------------------ lifecycle
    async def tick(self, now: datetime | None = None) -> list[EngineEvent]:
        """Fade unpinned cards after card_ttl_s of topic inactivity (spec §5.4.6)."""
        now = now or utc_now()
        events = []
        for cid, ac in list(self.active.items()):
            if should_expire_card(ac.shown_at, now, ac.pinned, ac.last_topic_activity,
                                  self.cfg.relevance):
                events.append(EngineEvent("card_dismiss", card_id=cid))
                self.active.pop(cid)
        return events

    def on_feedback(self, card_id: str, kind: str) -> str | None:
        memory_id = self.store.record_feedback(card_id, kind, utc_now())
        ac = self.active.get(card_id)
        if ac is not None:
            if kind in ("dismiss", "thumb_down"):
                self.active.pop(card_id, None)
            elif kind == "pin":
                ac.pinned = True
                ac.card.priority = True
            elif kind == "unpin":
                ac.pinned = False
        return memory_id

    # ------------------------------------------------------------------ helpers
    def _speaker_entities(self, speaker_id: str | None) -> set[str]:
        if not speaker_id:
            return set()
        cached = self._speaker_entities_cache.get(speaker_id)
        if cached and cached[0] > utc_now().timestamp() - 60:
            return cached[1]
        spk = self.store.get_speaker(speaker_id)
        ids: set[str] = set()
        if spk and spk["name"]:
            ids = self.store.entity_ids_for_names({spk["name"], spk["label"]})
        self._speaker_entities_cache[speaker_id] = (utc_now().timestamp(), ids)
        return ids

    def _mentioned_range(self, window_text: str, anchor: datetime) -> tuple[datetime, datetime] | None:
        """Cheap trigger extraction: mentioned dates → a coarse [earliest, latest] range
        for temporal candidate matching."""
        ranges = []
        for m in re.finditer(
            r"\b(?:next|this|last|coming)\s+(monday|tuesday|wednesday|thursday|friday|saturday|"
            r"sunday|week|weekend|month)|\btomorrow\b|\btonight\b|\btoday\b|\byesterday\b|"
            r"\bthe\s+\d{1,2}(?:st|nd|rd|th)\b|\bin\s+\d+\s+(?:days?|weeks?|months?)\b|"
            r"\b(?:january|february|march|april|may|june|july|august|september|october|"
            r"november|december)\s+\d{1,2}(?:st|nd|rd|th)?\b",
            window_text, re.IGNORECASE,
        ):
            r = resolve_time_phrase(m.group(0), anchor)
            if r:
                ranges.append((r.start, r.end or r.start))
        if not ranges:
            return None
        return min(r[0] for r in ranges), max(r[1] for r in ranges)

    async def _llm_judge(self, top, window_text: str):
        rows_by_id = {d.memory_id: self.store.get_memory(d.memory_id) for d in top}
        kept = []
        for d in top:
            row = rows_by_id[d.memory_id]
            if row is None:
                continue
            try:
                dec = await self.llm.complete_json(
                    "fast", self.prompts["judge"], JudgeDecision,
                    user=f'Show "{row["text"]}"?',
                    window=window_text[:2000],
                    memory_context=f"type={row['type']} importance={row['importance']}",
                )
            except LLMError:
                kept.append(d)  # judge unavailable → don't block
                continue
            d.features.raw["judge"] = {"worth": dec.worth_surfacing, "why": dec.reason[:120]}
            d.score = d.score if dec.worth_surfacing else min(d.score, self.cfg.relevance.gate_threshold - 0.01)
            if dec.worth_surfacing:
                kept.append(d)
        return kept


_ECHO_STOPWORDS = frozenset((
    "a an the and or but if then than that this these those is are was were be been being "
    "do does did doing i you he she it we they me him her them my your his our their "
    "of in on at to for with from by as about into over under up down out off again more "
    "so not no yes just really very too also here there when where why how who whom whose "
    "what which who will would can could should may might must have has had having am "
    "its said say says going gonna kinda like well okay oh hey hi ok yeah").split())


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", (text or "").casefold())
            if t not in _ECHO_STOPWORDS}


def _echo_coverage(memory_text: str, window_text: str) -> float:
    """Share of the memory's CONTENT words just spoken in the live window.
    1.0 means the conversation is relaying the memory verbatim → card adds nothing."""
    mem = _content_tokens(memory_text)
    if not mem:
        return 0.0
    spoken = _content_tokens(window_text)
    return len(mem & spoken) / len(mem)


def _window_text(window: list[dict]) -> str:
    return "\n".join(f"{u['speaker_label'] or 'Speaker'}: {u['text']}" for u in window[-12:])


def _human_age(dt: datetime, now: datetime) -> str:
    s = max(0, int((now - dt).total_seconds()))
    if s < 3600:
        return f"{max(1, s // 60)}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    if s < 30 * 86400:
        return f"{s // 86400}d ago"
    return dt.strftime("%b %d")
