"""Gating, cooldown, card budget, topic shift (spec §5.4 steps 4–6). Pure logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import RelevanceConfig
from .scoring import ScoreFeatures


@dataclass
class GateDecision:
    memory_id: str
    score: float
    allowed: bool
    reason: str  # ok | below_threshold | cooldown | budget
    features: ScoreFeatures


def gate_candidates(
    candidates: list[tuple[str, float, ScoreFeatures]],  # (memory_id, score, features)
    now: datetime,
    last_shown: dict[str, datetime],   # memory_id -> last shown ts
    active_card_count: int,
    cfg: RelevanceConfig,
) -> list[GateDecision]:
    """Apply threshold, per-memory cooldown, and the card budget (max 3 on screen).

    Candidates must arrive sorted by score descending; budget fills best-first.
    """
    decisions: list[GateDecision] = []
    budget = max(0, cfg.max_cards - active_card_count)
    for memory_id, score, feats in candidates:
        if score < cfg.gate_threshold:
            decisions.append(GateDecision(memory_id, score, False, "below_threshold", feats))
            continue
        last = last_shown.get(memory_id)
        if last is not None and (now - last).total_seconds() < cfg.cooldown_s:
            decisions.append(GateDecision(memory_id, score, False, "cooldown", feats))
            continue
        if budget <= 0:
            decisions.append(GateDecision(memory_id, score, False, "budget", feats))
            continue
        budget -= 1
        decisions.append(GateDecision(memory_id, score, True, "ok", feats))
    return decisions


def is_topic_shift(prev_window_embedding: list[float] | None,
                   cur_window_embedding: list[float],
                   cfg: RelevanceConfig) -> bool:
    """Cosine between rolling windows below threshold = conversation changed topic."""
    if prev_window_embedding is None:
        return False
    from ..db.vector_index import cosine

    return cosine(prev_window_embedding, cur_window_embedding) < cfg.topic_shift_cosine


def should_expire_card(shown_at: datetime, now: datetime, pinned: bool,
                       last_topic_activity: datetime | None, cfg: RelevanceConfig) -> bool:
    """Cards fade after card_ttl_s of topic inactivity unless pinned."""
    if pinned:
        return False
    anchor = max(shown_at, last_topic_activity or shown_at)
    return (now - anchor).total_seconds() > cfg.card_ttl_s
