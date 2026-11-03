#!/usr/bin/env python3
"""Export scoring-parity fixtures: golden inputs/outputs computed by the REAL server
functions (core/scoring.py, core/gating.py, relevance echo) into the Swift test suite.

Run from repo root after touching server scoring/gating/echo code:
    server/.venv/bin/python eval/export_scoring_fixtures.py
Then: cd ios/AmbientCore && swift test   (parity assertions must hold)

This is what "the tuned engine stays one engine" means concretely: the phone may
retrieve memories differently (local search), but given the same FEATURES the phone's
math must match the server's to floating-point accuracy, forever.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server" / "src"))

from ambient_server.config import RelevanceWeights  # noqa: E402
from ambient_server.core.gating import gate_candidates  # noqa: E402
from ambient_server.core.scoring import (  # noqa: E402
    ScoreFeatures, actionability_score, compute_score, dismissed_score, jaccard,
    recency_decay, semantic_from_sims, speaker_match,
)
from ambient_server.pipeline.relevance import _echo_coverage  # noqa: E402

def _cfg_with(**kw):
    from ambient_server.config import RelevanceConfig
    return RelevanceConfig(**kw)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
W = RelevanceWeights()

cases: list[dict] = []


def score_case(name: str, feats: dict, weights: dict | None = None) -> None:
    f = ScoreFeatures(**feats)
    w = RelevanceWeights(**weights) if weights else W
    cases.append({"kind": "score", "name": name, "features": feats,
                  "weights": weights or W.model_dump(),
                  "expected": compute_score(f, w)})


# --- score grid: corners + realistic combos
score_case("all-ones", {"semantic": 1, "entity_overlap": 1, "actionability": 1,
                        "importance": 1, "recency_decay": 1, "speaker_match": 1,
                        "recently_shown": 0, "dismissed_before": 0})
score_case("all-zero", {"semantic": 0, "entity_overlap": 0, "actionability": 0,
                        "importance": 0, "recency_decay": 0, "speaker_match": 0,
                        "recently_shown": 0, "dismissed_before": 0})
score_case("punished", {"semantic": .9, "entity_overlap": .8, "actionability": 1,
                        "importance": .9, "recency_decay": .9, "speaker_match": 1,
                        "recently_shown": 1, "dismissed_before": 1})
score_case("typical-hit", {"semantic": .62, "entity_overlap": .33, "actionability": .4,
                           "importance": .7, "recency_decay": .84, "speaker_match": .5,
                           "recently_shown": 0, "dismissed_before": .5})
score_case("weak-noise", {"semantic": .11, "entity_overlap": 0, "actionability": .25,
                          "importance": .3, "recency_decay": .2, "speaker_match": 0,
                          "recently_shown": 1, "dismissed_before": 0})
score_case("custom-weights", {"semantic": .5, "entity_overlap": .5, "actionability": .5,
                              "importance": .5, "recency_decay": .5, "speaker_match": .5,
                              "recently_shown": .5, "dismissed_before": .5},
             {"semantic": .5, "entity_overlap": .1, "actionability": .1,
              "importance": .1, "recency_decay": .1, "speaker_match": .05,
              "recently_shown": -.5, "dismissed_before": -.15})

# --- actionability corners (event deltas around every branch boundary)
for delta_h, name in [(-49, "past-beyond-48h"), (-47, "just-past"), (0, "now"),
                      (14 * 24 - 1, "horizon-inside"), (14 * 24 + 1, "horizon-outside"),
                      (89 * 24, "near-future"), (91 * 24, "far-future")]:
    cases.append({"kind": "actionability", "name": f"event-delta-{name}",
                  "memory_type": "event", "delta_h": delta_h, "about_speaker": False,
                  "has_date": True,
                  "expected": actionability_score("event", NOW + timedelta(hours=delta_h),
                                                  NOW, 14, False, True)})
for mtype, has_date, about, name in [("commitment", False, False, "commitment-no-date"),
                                     ("plan", True, False, "plan-with-date"),
                                     ("plan", False, False, "plan-no-date"),
                                     ("preference", False, False, "preference"),
                                     ("event", False, True, "event-speaker-boost")]:
    cases.append({"kind": "actionability", "name": name, "memory_type": mtype,
                  "delta_h": None, "about_speaker": about, "has_date": has_date,
                  "expected": actionability_score(mtype, None, NOW, 14, about, has_date)})

# --- recency decay (half-life semantics)
for age_h, name in [(0, "fresh"), (168, "one-half-life"), (336, "two-half-lives"),
                    (168 * 7, "week-old")]:
    cases.append({"kind": "recency", "name": name, "age_h": age_h, "half_life_h": 168,
                  "expected": recency_decay(NOW - timedelta(hours=age_h), NOW, 168)})

# --- speaker match / jaccard / dismissed / semantic
for a, b, name in [(True, True, "present-wins"), (False, True, "entity-only"),
                   (False, False, "none")]:
    cases.append({"kind": "speaker", "name": name, "present": a, "entity": b,
                  "expected": speaker_match(a, b)})
for a, b, name in [(["maya", "park"], ["maya", "park"], "identical"),
                   (["maya"], ["maya", "park"], "subset"),
                   (["a"], ["b"], "disjoint"), ([], ["b"], "empty-side")]:
    cases.append({"kind": "jaccard", "name": name, "a": a, "b": b,
                  "expected": jaccard(set(a), set(b))})
for n, name in [(0, "never"), (1, "once"), (2, "twice"), (5, "capped")]:
    cases.append({"kind": "dismissed", "name": name, "count": n,
                  "expected": dismissed_score(n)})
for w, u, name in [(0.5, None, "window-only"), (None, 0.8, "utt-only"),
                   (0.5, 0.8, "max-of-both"), (-0.2, -0.1, "negatives-clamped"),
                   (None, None, "no-embeddings")]:
    cases.append({"kind": "semantic", "name": name, "window_sim": w, "utt_sim": u,
                  "expected": semantic_from_sims(w, u)})

# --- echo gate coverage (real tokenizer from the engine)
echo_pairs = [
    ("Jasmine booked Chili for next Friday dinner",
     "yeah jasmine booked chili for next friday dinner totally", "verbatim-relay"),
    ("Jasmine booked Chili for next Friday dinner",
     "she also adopted another cat last month", "unrelated"),
    ("Maya adopted two cats in september",
     "maya adopted cats", "partial-relay"),
    ("ARTES ships friday", "", "empty-window"),
]
for mem, win, name in echo_pairs:
    cases.append({"kind": "echo", "name": name, "memory_text": mem, "window_text": win,
                  "expected": _echo_coverage(mem, win)})

# --- gating scenarios (threshold / cooldown / budget ordering)
gate_scores = [("m-hot", 0.91), ("m-warm", 0.62), ("m-edge", 0.55),
               ("m-cold", 0.54), ("m-cooled", 0.88), ("m-extra", 0.60)]
gate_feats = {mid: ScoreFeatures() for mid, _ in gate_scores}
gate_last = {"m-cooled": NOW - timedelta(seconds=599)}   # inside cooldown
decisions = gate_candidates([(mid, sc, gate_feats[mid]) for mid, sc in gate_scores],
                            NOW, gate_last, 1,
                            _cfg_with(max_cards=3, cooldown_s=600, gate_threshold=0.55))
cases.append({"kind": "gate", "name": "mixed-scenario", "scores":
              [{"memory_id": mid, "score": sc} for mid, sc in gate_scores],
              "cooldown_ids": {"m-cooled": 599},  # seconds since shown
              "active_cards": 1, "threshold": 0.55, "cooldown_s": 600, "max_cards": 3,
              "expected": [{"memory_id": d.memory_id, "allowed": d.allowed,
                            "reason": d.reason} for d in decisions]})


payload = {"generated_note": "server/.venv/bin/python eval/export_scoring_fixtures.py",
           "now": NOW.isoformat(), "cases": cases}
out = REPO / "ios" / "AmbientCore" / "Tests" / "AmbientCoreTests" / "Fixtures"
out.mkdir(parents=True, exist_ok=True)
(out / "scoring_parity.json").write_text(json.dumps(payload, indent=1))
print(f"wrote {len(cases)} parity cases → {out / 'scoring_parity.json'}")
