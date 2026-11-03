"""Lookup agent (spec §5.4 questions + §5.5 direct query path).

- `aloud_question_card`: unanswered spoken question → memory first; if memory can't
  answer, web search + 1-2 sentence synthesis. NEVER presents web as memory and never
  stores it as memory unless the owner pins it (handled by the feedback path).
- `memory_card`: whisper / search-bar / hold-to-ask → grounded in retrieved memories
  only, recency-agnostic scoring profile, optional deep synthesis with [m:id] citations.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from uuid import uuid4

from ..config import AppConfig
from ..db.store import Store
from ..llm.gateway import LLMError, LLMGateway
from ..llm.prompts import PromptSet
from ..models import Card, utc_now
from .embeddings import EmbeddingService
from .search import SearchBackend

log = logging.getLogger("artmem.lookup")


class LookupAgent:
    def __init__(self, store: Store, llm: LLMGateway, prompts: PromptSet, cfg: AppConfig,
                 embedder: EmbeddingService, search: SearchBackend):
        self.store = store
        self.llm = llm
        self.prompts = prompts
        self.cfg = cfg
        self.embed = embedder
        self.search = search

    # ---------------------------------------------------------------- direct queries
    async def memory_card(self, question: str, priority: bool = True,
                          synthesize: bool | None = None) -> Card:
        """Recency-agnostic profile (no cooldown penalty); grounded answer or miss card."""
        do_synth = self.cfg.query.synthesize_answer if synthesize is None else synthesize
        hits = await self.retrieve(question)
        now = utc_now()
        if not hits:
            return Card(
                id=uuid4().hex, kind="query", headline="No matching memory",
                detail=question, origin="memory", priority=priority,
                debug_features={"query": question, "hits": 0},
                expires_at=now + timedelta(seconds=self.cfg.relevance.card_ttl_s),
            )
        answer = None
        if do_synth and len(hits) >= 1:
            memory_block = "\n".join(
                f"[m:{h.row['id'][:8]}] ({h.row['recorded_at'][:10]}) {h.row['text']}"
                for h in hits[:8]
            )
            try:
                rendered = self.prompts["synthesize"].render(question=question,
                                                             memories=memory_block)
                answer = (await self.llm.complete_text("deep", rendered,
                                                       "Answer using only those memories.")).strip()
            except LLMError as exc:
                log.warning("synthesis unavailable: %s", exc)
        top = hits[0]
        return Card(
            id=uuid4().hex, kind="query", memory_id=top.row["id"],
            headline=answer or top.row["text"],
            detail=question if answer else top.row["text"],
            source_chip=f"memory · {len(hits)} match{'es' if len(hits) != 1 else ''}",
            origin="memory", score=round(top.score, 4), priority=priority,
            source_utterance_ids=_utt_ids(top.row),
            expires_at=now + timedelta(seconds=self.cfg.relevance.card_ttl_s),
            debug_features={"query": question, "hits": len(hits),
                            "memory_ids": [h.row["id"] for h in hits[:8]],
                            "synthesized": bool(answer)},
        )

    async def retrieve(self, question: str):
        vec = await self.embed.embed_one(question)
        hits = self.store.search(question, vec, status="active", limit=self.cfg.query.top_k)
        out = []
        for h in hits:
            score = h.semantic * 0.7 + h.keyword * 0.3
            if score >= self.cfg.query.gate_threshold or (h.entity_hit and score > 0.1):
                h.score = score  # attach for card
                out.append(h)
        out.sort(key=lambda h: -h.score)
        return out

    # ---------------------------------------------------------------- spoken questions
    async def aloud_question_card(self, question: str, topic: str = "") -> Card | None:
        """Memory first, then web. Returns None only when neither can answer (silence
        beats a wrong card)."""
        hits = await self.retrieve(question if len(question) > 8 else topic or question)
        now = utc_now()
        if hits and hits[0].score >= 0.30:
            top = hits[0]
            chip_names = [e["name"] for e in self.store.entities_for_memory(top.row["id"])][:3]
            return Card(
                id=uuid4().hex, kind="lookup", memory_id=top.row["id"],
                headline=top.row["text"],
                detail="asked aloud: " + question[:160],
                source_chip="memory" + (" · " + ", ".join(chip_names) if chip_names else ""),
                origin="memory", score=round(top.score, 4), priority=True,
                source_utterance_ids=_utt_ids(top.row),
                expires_at=now + timedelta(seconds=self.cfg.relevance.card_ttl_s),
                debug_features={"question": question, "source": "memory",
                                "topic": topic, "hits": len(hits)},
            )
        # web fallback (owner-configured search; query string only, no transcripts — spec §6)
        query = topic or question
        if self.cfg.search.backend != "none" and query:
            results = await self.search.search(query, self.cfg.search.max_results)
            if results:
                block = "\n".join(f"- {r.title}: {r.snippet} ({r.url})" for r in results)
                try:
                    rendered = self.prompts["answer_web"].render(question=question, results=block)
                    answer = (await self.llm.complete_text("fast", rendered,
                                                           "Answer in 1-2 sentences.")).strip()
                except LLMError:
                    answer = results[0].snippet[:200]
                if answer and answer.upper() != "NO_ANSWER":
                    url = None
                    m = re.search(r"\((https?://[^)]+)\)\s*$", answer)
                    if m:
                        url = m.group(1)
                        answer = answer[: m.start()].strip()
                    if url is None:
                        url = results[0].url
                    return Card(
                        id=uuid4().hex, kind="lookup",
                        headline=answer[:280],
                        detail="asked aloud; answered from web (pin to keep as memory)",
                        source_chip="web · " + (url or "").replace("https://", "")[:40],
                        origin="web", url=url, score=0.6, priority=True,
                        expires_at=now + timedelta(seconds=self.cfg.relevance.card_ttl_s),
                        debug_features={"question": question, "source": "web", "url": url,
                                        "topic": topic},
                    )
        return None


def _utt_ids(row) -> list[str]:
    import json

    try:
        return json.loads(row["source_utterance_ids"])
    except (ValueError, KeyError):
        return []
