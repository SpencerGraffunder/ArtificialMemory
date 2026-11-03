"""Retain pipeline (spec §5.2/§5.3): conversation transcript → structured memories with
provenance, resolved dates, entity links, supersession, and clarification unknowns.

Runs incrementally (every N utterances) and at conversation close. LLM failures degrade
to a logged no-op — ingest is never blocked by extraction (DECISIONS.md)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import AppConfig
from ..core.dates import resolve_time_phrase
from ..core.normalize import EntityCandidate, resolve_entity
from ..db.store import Store
from ..llm.gateway import LLMError, LLMGateway, build_json_messages
from ..llm.prompts import PromptSet
from ..models import (
    ClarifyList,
    ContradictDecision,
    DisambiguationResult,
    ExtractionResult,
    parse_iso,
)
from .embeddings import EmbeddingService

log = logging.getLogger("artmem.extract")


@dataclass
class ExtractionJob:
    conversation: dict
    rows: list
    anchor: "datetime"
    messages: list
    mode: str = "closing"


@dataclass
class ExtractionOutcome:
    conversation_id: str
    mode: str = "closing"
    facts_created: list[str] = field(default_factory=list)
    memory_texts: list[str] = field(default_factory=list)
    superseded: list[tuple[str, str]] = field(default_factory=list)
    clarifications_created: list[str] = field(default_factory=list)
    error: str | None = None
    prompts: dict[str, str] = field(default_factory=dict)
    llm_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id, "mode": self.mode,
            "facts_created": self.facts_created, "memory_texts": self.memory_texts,
            "superseded": [{"old": o, "new": n} for o, n in self.superseded],
            "clarifications_created": self.clarifications_created, "error": self.error,
            "prompts": self.prompts, "llm_used": self.llm_used,
        }


def format_transcript(rows, with_confidence: bool = False) -> str:
    lines = []
    for r in rows:
        ts = parse_iso(r["start_ts"]).strftime("%H:%M:%S")
        label = r["speaker_label"] or "Speaker-?"
        conf = f" (asr~{r['asr_confidence']:.2f})" if with_confidence and r["asr_confidence"] else ""
        lines.append(f"[{ts}] {label}: {r['text']}{conf}")
    return "\n".join(lines)


class ExtractionPipeline:
    def __init__(
        self, store: Store, llm: LLMGateway, prompts: PromptSet, cfg: AppConfig,
        embedder: EmbeddingService | None = None,
    ):
        self.store = store
        self.llm = llm
        self.prompts = prompts
        self.cfg = cfg
        self.embed = embedder or EmbeddingService(None)

    # ------------------------------------------------------------------ main entry
    def build_job(self, conversation_id: str, mode: str = "closing",
                  up_to_utt_id: str | None = None) -> "ExtractionJob | None":
        """Everything except the LLM call: transcript slice, anchor, and the EXACT
        messages a local extraction would send (one prompt pipeline, no drift)."""
        outcome = ExtractionOutcome(conversation_id=conversation_id, mode=mode)
        conv = self.store.get_conversation(conversation_id)
        if conv is None:
            return None
        rows = self.store.unextracted_utterances(
            conversation_id, conv["last_extracted_utterance_id"], to_utt_id=up_to_utt_id)
        rows = [r for r in rows if not r["is_whisper"]]
        if not rows:
            return None
        anchor = parse_iso(rows[-1]["start_ts"])
        transcript = format_transcript(rows, with_confidence=True)
        messages = build_json_messages(
            self.prompts["extract"], ExtractionResult,
            user=f"Transcript ({len(rows)} utterances):\n{transcript}",
            anchor=anchor.isoformat(), today=anchor.strftime("%Y-%m-%d (%A)"),
            transcript=transcript, known_entities=self._known_entities_text(),
            vocabulary=self._vocabulary_text())
        return ExtractionJob(conversation=conv, rows=rows, anchor=anchor,
                             messages=messages, mode=mode)

    async def apply_result(self, job: "ExtractionJob", result: ExtractionResult,
                           outcome: ExtractionOutcome | None = None) -> ExtractionOutcome:
        """Post-LLM half: entities, memories, commitments, supersession, clarifications."""
        conv, rows, anchor, mode = job.conversation, job.rows, job.anchor, job.mode
        if outcome is None:
            outcome = ExtractionOutcome(conversation_id=conv["id"], mode=mode)
        outcome.llm_used = True
        outcome.prompts = {"extract": self.prompts["extract"].ref}

        entity_ids = await self._resolve_entities(result, outcome)

        # ---- facts → memories
        embeddings = await self.embed.embed_many([f.content for f in result.facts])
        last_utt_id = rows[-1]["id"]
        for fact, emb in zip(result.facts, embeddings, strict=True):
            mem_id = await self._insert_fact(fact, conv, rows, anchor, entity_ids, emb, outcome)
            if mem_id:
                outcome.facts_created.append(mem_id)
                outcome.memory_texts.append(fact.content)
        # ---- commitments
        for c in result.commitments:
            emb = (await self.embed.embed_one(c.text)) if self.embed.enabled else None
            due = resolve_time_phrase(c.due_phrase, anchor)
            text = f"{c.owner or 'Owner'} committed to: {c.text}"
            mid = self.store.insert_memory(
                text=text,
                structured={"owner": c.owner, "text": c.text, "due_phrase": c.due_phrase,
                            "due_resolved": due.start_iso if due else None,
                            "due_resolved_end": due.end_iso if due else None,
                            "llm_due_date": c.due_date},
                type="commitment", importance=0.7, confidence=c.confidence,
                conversation_id=conv["id"], speaker_id=self._speaker_for(c.owner),
                source_utterance_ids=[r["id"] for r in rows[-6:]],
                event_start=due.start if due else None, event_end=due.end if due else None,
                embedding=emb,
            )
            outcome.facts_created.append(mid)
            outcome.memory_texts.append(text)

        self.store.mark_extracted(conv["id"], last_utt_id,
                                  "closing" if mode == "closing" else "incremental")
        if mode == "closing":
            self.store.db.execute(
                "UPDATE utterances SET extracted=1 WHERE conversation_id=?", (conv["id"],))
            await self._generate_clarifications(conv, result, rows, outcome)
            if result.summary:
                self.store.db.execute("UPDATE conversations SET summary=? WHERE id=?",
                                      (result.summary, conv["id"]))
        return outcome

    async def extract(self, conversation_id: str, mode: str = "closing") -> ExtractionOutcome:
        outcome = ExtractionOutcome(conversation_id=conversation_id, mode=mode)
        job = self.build_job(conversation_id, mode)
        if job is None:
            return outcome
        try:
            result = await self.llm.complete_json_messages("deep", job.messages,
                                                           ExtractionResult)
        except LLMError as exc:
            log.error("extraction failed for %s: %s", conversation_id, exc)
            outcome.error = str(exc)[:300]
            return outcome
        return await self.apply_result(job, result, outcome)

    async def apply_remote_result(self, raw_json: str, conversation_id: str, mode: str,
                                  up_to_utt_id: str | None) -> ExtractionResult:
        """Remote executor (phone) produced raw JSON; validate against the same schema.
        Raises ValidationError so the caller can relay repair details back."""
        job = self.build_job(conversation_id, mode, up_to_utt_id=up_to_utt_id)
        if job is None:
            raise ValueError("conversation has no unextracted utterances for this task")
        return await self.llm.validate_json(raw_json, ExtractionResult)

    # ------------------------------------------------------------------ helpers
    def _known_entities_text(self) -> str:
        parts = []
        for e in self.store.entity_candidates():
            aliases = json.loads(e["aliases"])
            parts.append(f"{e['name']} ({e['kind']}" + (f", aka {', '.join(aliases)})" if aliases else ")"))
        return "; ".join(parts[:200]) or "(none yet)"

    def _vocabulary_text(self) -> str:
        terms = [f"{v['term']}" for v in self.store.list_vocabulary()]
        return ", ".join(terms[:300]) or "(none yet)"

    def _speaker_for(self, label: str | None) -> str | None:
        if not label:
            return None
        row = self.store.speaker_by_label(label)
        return row["id"] if row else None

    async def _resolve_entities(self, result: ExtractionResult,
                                outcome: ExtractionOutcome) -> dict[str, str]:
        """Resolve all fact/commitment entities; LLM disambiguation for the fuzzy band."""
        candidates = [
            EntityCandidate(id=e["id"], name=e["name"], kind=e["kind"],
                            aliases=json.loads(e["aliases"]))
            for e in self.store.entity_candidates()
        ]
        name_to_id: dict[str, str] = {}
        refs = list(result.entities)
        for f in result.facts:
            refs.extend(f.entities)
        for ref in refs:
            if not ref.name.strip():
                continue
            res = resolve_entity(ref.name, ref.kind, candidates)
            eid = res.matched_id
            if eid is None and 0.60 <= res.score < 0.87 and self.cfg.extraction.disambiguate_llm:
                eid = await self._llm_disambiguate(ref, candidates)
            if eid is None:
                eid = self.store.upsert_entity(ref.name, ref.kind, ref.aliases,
                                               unresolved=res.needs_clarification)
                candidates.append(EntityCandidate(id=eid, name=ref.name, kind=ref.kind,
                                                  aliases=ref.aliases))
            else:
                # remember the spoken form as an alias for next time
                self.store.upsert_entity(ref.name, ref.kind, ref.aliases, entity_id=eid)
            name_to_id[ref.name.casefold()] = eid
            for a in ref.aliases:
                name_to_id[a.casefold()] = eid
        return name_to_id

    async def _llm_disambiguate(self, ref, candidates: list[EntityCandidate]) -> str | None:
        near = sorted(candidates, key=lambda c: -resolve_entity(ref.name, c.kind, [c]).score)[:6]
        if not near:
            return None
        try:
            dec = await self.llm.complete_json(
                "fast", self.prompts["disambiguate"], DisambiguationResult,
                user=f'"{ref.name}" ({ref.kind}) vs candidates',
                new_name=ref.name, new_kind=ref.kind,
                candidates="\n".join(f"- id={c.id} {c.name} ({c.kind}, aka {', '.join(c.aliases)})"
                                     for c in near),
            )
            if dec.match_id and any(c.id == dec.match_id for c in near):
                return dec.match_id
        except LLMError:
            pass
        return None

    async def _insert_fact(self, fact, conv, rows, anchor, entity_ids, emb,
                           outcome) -> str | None:
        resolved = resolve_time_phrase(fact.time_phrase, anchor)
        event_start = resolved.start if resolved else None
        event_end = resolved.end if resolved else None
        resolved_gran = resolved.granularity if resolved else None
        if event_start is None and fact.event_time:
            try:
                dt = parse_iso(fact.event_time)
                event_start, event_end = dt, dt
                resolved_gran = "llm"
            except ValueError:
                pass

        if fact.who_said and fact.said_to:
            text = f"{fact.who_said} told {', '.join(fact.said_to)} that {fact.content}"
        elif fact.who_said:
            text = f"{fact.who_said}: {fact.content}"
        else:
            text = fact.content

        structured = fact.model_dump()
        structured.update(
            time_resolved=event_start.isoformat() if event_start else None,
            time_resolved_end=event_end.isoformat() if event_end else None,
            time_granularity=resolved_gran,
        )
        speaker_id = self._speaker_for(fact.who_said)
        mid = self.store.insert_memory(
            text=text, structured=structured, type=fact.type, importance=fact.importance,
            confidence=fact.confidence, conversation_id=conv["id"], speaker_id=speaker_id,
            source_utterance_ids=[r["id"] for r in rows[-12:]],
            event_start=event_start, event_end=event_end, embedding=emb,
        )
        links: list[tuple[str, str]] = []
        for e in fact.entities:
            eid = entity_ids.get(e.name.casefold())
            if eid:
                links.append((eid, "about"))
        if fact.place:
            eid = entity_ids.get(fact.place.casefold())
            if not eid:
                exact = self.store.resolve_entity_name(fact.place)
                eid = exact["id"] if exact else self.store.upsert_entity(
                    fact.place, "place", unresolved=False)
            links.append((eid, "located_at"))
        if speaker_id:
            spk = self.store.get_speaker(speaker_id)
            if spk and spk["name"]:
                eid = self.store.entity_ids_for_names({spk["name"]})
                links.extend((e, "said_by") for e in eid)
        self.store.attach_memory_entities(mid, links)
        ent_ids = {e for e, _ in links}
        for a in ent_ids:
            for b in ent_ids:
                if a < b:
                    self.store.log_entity_edge(a, b, "co_mentioned", anchor)
        # provenance-linked entity observations dirty marker
        self._mark_observation_dirty(ent_ids)
        await self._maybe_supersede(mid, text, emb, ent_ids, outcome)
        return mid

    def _mark_observation_dirty(self, entity_ids: set[str]) -> None:
        dirty = set(json.loads(self.store.kv_get("observation_dirty") or "[]"))
        dirty |= entity_ids
        self.store.kv_set("observation_dirty", json.dumps(sorted(dirty)))

    async def _maybe_supersede(self, new_id: str, text: str, emb, ent_ids: set[str],
                               outcome) -> None:
        """Bi-temporal: if a new memory replaces an old one, CLOSE the old (never delete)."""
        if not self.cfg.extraction.supersession_enabled:
            return
        hits = self.store.search(text, emb, entity_ids=ent_ids or None, status="active", limit=6)
        for h in hits:
            old = h.row
            if old["id"] == new_id or h.semantic < self.cfg.extraction.supersede_similarity:
                continue
            if not (ent_ids & {e["id"] for e in self.store.entities_for_memory(old["id"])}):
                continue  # near-duplicate text but different subject → not supersession
            try:
                dec = await self.llm.complete_json(
                    "fast", self.prompts["contradict"], ContradictDecision,
                    user="Decide the relation.",
                    old_text=old["text"], new_text=text,
                    old_recorded=old["recorded_at"][:10], new_recorded="today",
                )
            except LLMError:
                return
            if dec.relation == "replaces":
                self.store.supersede(old["id"], new_id)
                outcome.superseded.append((old["id"], new_id))
                return

    # ------------------------------------------------------------------ clarifications
    async def _generate_clarifications(self, conv, result: ExtractionResult, rows,
                                       outcome) -> None:
        if not result.unknowns:
            return
        unknowns_json = json.dumps([u.model_dump() for u in result.unknowns],
                                   ensure_ascii=False)
        roster = "; ".join(
            f"{s['label']}{'' if s['label'].startswith('Speaker-') else ' (named)'}"
            for s in self.store.list_speakers()[:50]
        ) or "(empty)"
        known_terms = ", ".join(v["term"] for v in self.store.list_vocabulary()[:200]) or "(none)"
        excerpt = format_transcript(rows)[:3000]
        try:
            cl = await self.llm.complete_json(
                "deep", self.prompts["clarify"], ClarifyList,
                user="Generate clarification questions.",
                unknowns=unknowns_json, transcript=excerpt, roster=roster,
                known_terms=known_terms,
            )
            qs = cl.questions
            outcome.prompts["clarify"] = self.prompts["clarify"].ref
        except LLMError:
            # degrade: ask about unknowns verbatim (spec §7 list, generic phrasing)
            qs = []
            for u in result.unknowns:
                q = {
                    "speaker_identity": f"Who is {u.text}?",
                    "unknown_entity": f"What does or who is “{u.text}”?",
                    "ambiguous_ref": f"What did “{u.text}” refer to?",
                    "low_confidence_fact": f"Can you confirm: “{u.text}”?",
                    "conflict": f"Which of these still stands: “{u.text}”?",
                }.get(u.kind, f"About “{u.text}” — what should I know?")
                qs.append(_FallbackQ(u.kind, q, u.text, u.importance, 0.8))
        now = datetime.now(timezone.utc)
        for q in qs:
            cid = self.store.insert_clarification(
                kind=q.kind, question=q.question,
                context={"subject": q.subject, "transcript_excerpt": excerpt[:600]},
                importance=q.importance, uncertainty=q.uncertainty,
                conversation_id=conv["id"],
                related_memory_ids=[m for m in outcome.facts_created][:8],
                related_speaker_id=self._speaker_for(q.subject),
                related_entity_id=None,
                now=now, expiry_days=self.cfg.clarifications.expiry_days,
            )
            if cid:
                outcome.clarifications_created.append(cid)


class _FallbackQ:
    __slots__ = ("kind", "question", "subject", "importance", "uncertainty")

    def __init__(self, kind, question, subject, importance, uncertainty):
        self.kind, self.question, self.subject = kind, question, subject
        self.importance, self.uncertainty = importance, uncertainty
