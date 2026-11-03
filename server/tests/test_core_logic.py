from datetime import datetime, timedelta, timezone

import pytest

from ambient_server.config import RelevanceConfig, RelevanceWeights
from ambient_server.core.clarifications import (
    ClarificationRow,
    dedupe_key,
    expected_value,
    next_transitions,
    rank,
)
from ambient_server.core.gating import gate_candidates, is_topic_shift, should_expire_card
from ambient_server.core.normalize import normalize_name, resolve_entity, similarity, EntityCandidate
from ambient_server.core.scoring import (
    ScoreFeatures,
    actionability_score,
    compute_score,
    dismissed_score,
    jaccard,
    recency_decay,
)

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
W = RelevanceWeights()


def feat(**kw) -> ScoreFeatures:
    base = dict(semantic=0, entity_overlap=0, actionability=0, importance=0.5,
                recency_decay=0, speaker_match=0, recently_shown=0, dismissed_before=0)
    base.update(kw)
    return ScoreFeatures(**base)


# ---------------------------------------------------------------- scoring formula
def test_formula_matches_spec_weights():
    f = feat(semantic=1, entity_overlap=1, actionability=1, importance=1, recency_decay=1,
             speaker_match=1)
    assert abs(compute_score(f, W) - (0.4 + 0.2 + 0.15 + 0.1 + 0.1 + 0.05)) < 1e-9


def test_penalties_are_negative_weights():
    clean = feat(semantic=1)
    dirty = feat(semantic=1, recently_shown=1, dismissed_before=1)
    assert compute_score(clean, W) - compute_score(dirty, W) == pytest.approx(0.6)


def test_recency_half_life_168h():
    assert recency_decay(NOW - timedelta(hours=168), NOW, 168) == pytest.approx(0.5)
    assert recency_decay(NOW, NOW, 168) == pytest.approx(1.0)
    assert recency_decay(NOW - timedelta(hours=336), NOW, 168) == pytest.approx(0.25)


def test_actionability_imminent_event_beats_far_future():
    near = actionability_score("event", NOW + timedelta(days=3), NOW, 14, False, True)
    far = actionability_score("event", NOW + timedelta(days=120), NOW, 14, False, True)
    assert near == 1.0 and far < near


def test_actionability_commitment_and_current_speaker():
    assert actionability_score("commitment", None, NOW, 14, False, False) == 0.8
    with_speaker = actionability_score("personal_fact", None, NOW, 14, True, False)
    assert with_speaker == 0.4


def test_jaccard_edges():
    assert jaccard(set(), {"a"}) == 0
    assert jaccard({"a"}, {"a"}) == 1
    assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3


def test_dismissed_saturates_at_one():
    assert dismissed_score(0) == 0
    assert dismissed_score(1) == 0.5
    assert dismissed_score(3) == 1.0


# ---------------------------------------------------------------- gating
def test_gate_threshold_cooldown_budget():
    cfg = RelevanceConfig()
    f = feat(semantic=1)
    cands = [("m-high", 0.9, f), ("m-low", 0.30, f), ("m-cool", 0.8, f), ("m-4", 0.7, f)]
    decisions = gate_candidates(cands, NOW, {"m-cool": NOW - timedelta(seconds=60)}, 0, cfg)
    by = {d.memory_id: d for d in decisions}
    assert by["m-high"].allowed and by["m-high"].reason == "ok"
    assert not by["m-low"].allowed and by["m-low"].reason == "below_threshold"
    assert not by["m-cool"].allowed and by["m-cool"].reason == "cooldown"
    assert by["m-4"].allowed  # cooldown passed → budget remains


def test_gate_budget_fills_best_first_and_blocks_rest():
    cfg = RelevanceConfig()
    f = feat(semantic=1)
    cands = [(f"m{i}", 0.9 - i * 0.05, f) for i in range(5)]
    decisions = gate_candidates(cands, NOW, {}, 1, cfg)  # 1 active card, max 3
    allowed = [d.memory_id for d in decisions if d.allowed]
    assert allowed == ["m0", "m1"]


def test_cooldown_expiry_allows_reshow():
    cfg = RelevanceConfig()
    decisions = gate_candidates([("m", 0.9, feat())], NOW,
                                {"m": NOW - timedelta(seconds=cfg.cooldown_s + 1)}, 0, cfg)
    assert decisions[0].allowed


def test_topic_shift_and_card_expiry():
    cfg = RelevanceConfig()
    assert is_topic_shift([1.0, 0.0], [0.0, 1.0], cfg) is True
    assert is_topic_shift([1.0, 0.1], [1.0, 0.12], cfg) is False
    assert is_topic_shift(None, [1.0, 0.0], cfg) is False
    assert should_expire_card(NOW - timedelta(seconds=46), NOW, False, None, cfg) is True
    assert should_expire_card(NOW - timedelta(seconds=46), NOW, True, None, cfg) is False
    assert should_expire_card(NOW - timedelta(seconds=60), NOW, False,
                              NOW - timedelta(seconds=10), cfg) is False


# ---------------------------------------------------------------- clarifications
def row(i, imp, unc, age_days=1, status="open", expires_in_days=13):
    return ClarificationRow(
        id=str(i), kind="unknown_entity", question=f"q{i}", importance=imp, uncertainty=unc,
        created_at=NOW - timedelta(days=age_days),
        expires_at=NOW + timedelta(days=expires_in_days), status=status, reminded=False,
    )


def test_rank_importance_times_uncertainty():
    rows = [row(1, 0.2, 0.5), row(2, 0.9, 0.9), row(3, 0.9, 0.2)]
    got = [r.id for r in rank(rows, NOW, 5)]
    assert got == ["2", "3", "1"]


def test_rank_excludes_expired_answered_and_caps():
    rows = [row(1, 0.9, 0.9), row(2, 0.9, 0.9, expires_in_days=-1),
            row(3, 0.9, 0.9, status="answered"), row(4, 0.5, 0.5)]
    got = [r.id for r in rank(rows, NOW, 5)]
    assert got == ["1", "4"]
    assert [r.id for r in rank(rows, NOW, 1)] == ["1"]


def test_expected_value():
    assert expected_value(0.8, 0.5) == 0.4


def test_expiry_and_single_reminder():
    r_open_old = row(1, 0.5, 0.5, age_days=8, expires_in_days=6)
    r_expiring = row(2, 0.5, 0.5, age_days=13, expires_in_days=-0.5)
    r_reminded = row(3, 0.5, 0.5, age_days=10, expires_in_days=4, status="reminded")
    r_young = row(4, 0.5, 0.5, age_days=2)
    t = dict(next_transitions([r_open_old, r_expiring, r_reminded, r_young], NOW, 14, 7))
    assert t == {"1": "reminded", "2": "expired"}


def test_dedupe_key():
    assert dedupe_key("speaker_identity", "Speaker-3") == dedupe_key(
        "speaker_identity", "the speaker-3")
    assert dedupe_key("unknown_entity", "ARTES") != dedupe_key("speaker_identity", "ARTES")


# ---------------------------------------------------------------- entity normalization
def test_normalize_name():
    assert normalize_name("The ARTES!") == "artes"
    assert normalize_name("Jasmine  Rodríguez") == "jasmine rodriguez"
    assert normalize_name("my project") == "project"


def test_fuzzy_and_alias_resolution():
    existing = [EntityCandidate(id="e1", name="Jasmine Rodriguez", kind="person",
                                aliases=["Jaz"]),
                EntityCandidate(id="e2", name="Chili", kind="place", aliases=[])]
    assert resolve_entity("Jaz", "person", existing).matched_id == "e1"
    assert resolve_entity("Jasmine Rodriguez", "person", existing).matched_id == "e1"
    assert resolve_entity("Jacqueline", "person", existing).matched_id is None
    assert resolve_entity("the chili", "place", existing).matched_id == "e2"


def test_acronym_initialism_similarity():
    assert similarity("ARTES", "advanced realtime telemetry eval system") > 0.9
    assert similarity("ARTES", "banana") < 0.4
