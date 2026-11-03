"""Entity name normalization and fuzzy resolution against the existing entity table.

Used by the extraction pipeline: every entity the LLM emits is resolved to an existing
entity id when possible (alias hit, exact-normalized hit, or high-similarity fuzzy hit);
unresolved ones are created flagged `unresolved` and picked up by the clarification flow.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_STOPWORD_PREFIXES = ("the ", "a ", "an ", "my ", "our ", "his ", "her ", "their ", "your ")


def normalize_name(name: str) -> str:
    """Canonical comparison form: NFKD, casefold, strip accents/punct, collapse spaces."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold().strip()
    for pre in _STOPWORD_PREFIXES:
        if s.startswith(pre):
            s = s[len(pre) :]
            break
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def similarity(a: str, b: str) -> float:
    """0..1 char-sequence similarity between normalized names."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # acronym <-> expansion: ARTES vs "advanced realtime telemetry eval system"
    tokens = nb.split()
    if len(na) <= 6 and na.isascii() and len(tokens) >= 2 and len(na) == len(tokens):
        initials = "".join(t[0] for t in tokens)
        if initials == na:
            return 0.95
    return SequenceMatcher(None, na, nb).ratio()


@dataclass
class EntityCandidate:
    id: str
    name: str
    kind: str
    aliases: list[str]


@dataclass
class EntityResolution:
    matched_id: str | None
    score: float
    needs_clarification: bool  # created-new or weak fuzzy match


def resolve_entity(
    name: str,
    kind: str,
    existing: list[EntityCandidate],
    *,
    exact_threshold: float = 0.999,
    fuzzy_threshold: float = 0.87,
) -> EntityResolution:
    """Return an existing entity match or flag for creation + clarification."""
    target = normalize_name(name)
    if not target:
        return EntityResolution(None, 0.0, True)
    best_id: str | None = None
    best = 0.0
    for cand in existing:
        if cand.kind != kind and kind != "other":
            # allow cross-kind exact matches ("Jasmine" person vs other) only when exact
            scores = [similarity(name, cand.name), *[similarity(name, a) for a in cand.aliases]]
            exact = max(scores, default=0.0)
            if exact >= exact_threshold:
                return EntityResolution(cand.id, exact, False)
            continue
        scores = [similarity(name, cand.name), *[similarity(name, a) for a in cand.aliases]]
        s = max(scores, default=0.0)
        if s > best:
            best, best_id = s, cand.id
    if best >= exact_threshold:
        return EntityResolution(best_id, best, False)
    if best >= fuzzy_threshold:
        return EntityResolution(best_id, best, False)
    return EntityResolution(None, best, True)


def extract_proper_nouns(text: str) -> list[str]:
    """Cheap trigger extraction (spec §5.4 step 1, no LLM): capitalized tokens/sequences
    not at sentence start, plus all-caps acronyms."""
    out: list[str] = []
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b", text):
        frag = m.group(1)
        start = m.start(1)
        before = text[:start].rstrip()
        if before.endswith((".", "!", "?", "\n")) or before == "":
            continue  # sentence-initial; could be a name but too noisy without LLM
        out.append(frag)
    for m in re.finditer(r"\b([A-Z]{2,6})s?\b", text):
        tok = m.group(1)
        if tok not in {"I", "OK", "USA", "TV", "SQL", "API", "CEO", "DIY", "AM", "PM"}:
            out.append(tok)
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen
