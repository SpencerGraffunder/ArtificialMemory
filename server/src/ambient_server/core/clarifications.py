"""Clarification ranking, expiry, digest selection (spec §7). Pure logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class ClarificationRow:
    id: str
    kind: str
    question: str
    importance: float
    uncertainty: float
    created_at: datetime
    expires_at: datetime
    status: str
    reminded: bool


def expected_value(importance: float, uncertainty: float) -> float:
    """Rank by importance × uncertainty (spec §7), highest expected value first."""
    return importance * uncertainty


def rank(rows: list[ClarificationRow], now: datetime, max_items: int) -> list[ClarificationRow]:
    live = [r for r in rows if r.status in ("open", "reminded") and r.expires_at > now]
    return sorted(live, key=lambda r: expected_value(r.importance, r.uncertainty), reverse=True)[
        :max_items
    ]


def next_transitions(
    rows: list[ClarificationRow], now: datetime, expiry_days: int, remind_after_days: int
) -> list[tuple[str, str]]:
    """(id, new_status) for unanswered questions: remind once, then expire.

    Unanswered questions expire after expiry_days with one reminder (spec §7)."""
    out: list[tuple[str, str]] = []
    for r in rows:
        age = now - r.created_at
        if r.status not in ("open", "reminded"):
            continue
        if now >= r.expires_at:
            out.append((r.id, "expired"))
            continue
        if r.status == "open" and age >= timedelta(days=remind_after_days) and not r.reminded:
            out.append((r.id, "reminded"))
    return out


def dedupe_key(kind: str, subject: str) -> str:
    """Open-question dedupe: same kind + normalized subject shouldn't be asked twice."""
    from .normalize import normalize_name

    return f"{kind}:{normalize_name(subject)}"
