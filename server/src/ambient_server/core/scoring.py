"""Relevance scoring (spec §5.4 step 3). Pure functions; all weights from config.

score = 0.40*semantic + 0.20*entity_overlap + 0.15*actionability
      + 0.10*importance + 0.10*recency_decay + 0.05*speaker_match
      - 0.30*recently_shown - 0.30*dismissed_before

Every feature is produced here from primitive observations so the replay harness and the
future logistic-regression weight fitting have identical, logged inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from ..config import RelevanceWeights


@dataclass
class ScoreFeatures:
    """Primitive observations for one (candidate memory, live context) pair."""

    semantic: float = 0.0            # max cosine sim vs window / latest utterance, clamped
    entity_overlap: float = 0.0      # Jaccard(memory entities, context entities)
    actionability: float = 0.0       # see actionability_score()
    importance: float = 0.5          # memory.importance
    recency_decay: float = 0.0       # 0.5 ** (age_h / half_life_h)
    speaker_match: float = 0.0       # 1 same speaker, 0.5 speaker entity present, else 0
    recently_shown: float = 0.0      # 1 = inside cooldown window
    dismissed_before: float = 0.0    # min(1, dismiss_count * 0.5)
    # raw context used to derive the above (logged, not scored)
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def recency_decay(recorded_at: datetime, now: datetime, half_life_h: float) -> float:
    if half_life_h <= 0:
        return 1.0
    age_h = max(0.0, (now - recorded_at).total_seconds() / 3600.0)
    return 0.5 ** (age_h / half_life_h)


def actionability_score(
    memory_type: str,
    event_start: datetime | None,
    now: datetime,
    horizon_days: int,
    about_current_speaker: bool,
    has_date: bool,
) -> float:
    """Boost dates, commitments, events within ~horizon days, facts about current speaker."""
    score = 0.0
    if event_start is not None:
        delta = event_start - now
        if timedelta(hours=-48) <= delta <= timedelta(days=horizon_days):
            score = 1.0                     # imminent event/commitment
        elif timedelta(0) < delta <= timedelta(days=90):
            score = 0.6
        elif delta > timedelta(days=90):
            score = 0.35
        else:
            score = 0.25                    # recent past
    elif memory_type == "commitment":
        score = 0.8
    elif memory_type in ("event", "plan"):
        score = 0.4 if has_date else 0.2
    if about_current_speaker:
        score = min(1.0, score + 0.4)
    return score


def speaker_match(
    memory_speaker_present: bool,
    memory_shares_entity_with_speaker: bool,
) -> float:
    if memory_speaker_present:
        return 1.0
    if memory_shares_entity_with_speaker:
        return 0.5
    return 0.0


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dismissed_score(dismiss_count: int) -> float:
    return min(1.0, dismiss_count * 0.5)


def compute_score(f: ScoreFeatures, w: RelevanceWeights) -> float:
    return (
        w.semantic * f.semantic
        + w.entity_overlap * f.entity_overlap
        + w.actionability * f.actionability
        + w.importance * f.importance
        + w.recency_decay * f.recency_decay
        + w.speaker_match * f.speaker_match
        + w.recently_shown * f.recently_shown
        + w.dismissed_before * f.dismissed_before
    )


def semantic_from_sims(sim_window: float | None, sim_utterance: float | None) -> float:
    """Clamped cosine similarity, preferring the max of window/latest-utterance."""
    vals = [max(0.0, s) for s in (sim_window, sim_utterance) if s is not None]
    return max(vals) if vals else 0.0
