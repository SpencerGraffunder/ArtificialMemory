from pathlib import Path

import pytest

from artmem_server.llm.prompts import PromptSet

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

EXPECTED = {
    "extract": {"anchor", "today", "transcript", "known_entities", "vocabulary"},
    "question": {"speaker_label", "text", "window"},
    "judge": {"window", "memory_text", "memory_context"},
    "synthesize": {"question", "memories"},
    "clarify": {"unknowns", "transcript", "roster", "known_terms"},
    "observe": {"entity_name", "entity_kind", "memories"},
    "disambiguate": {"new_name", "new_kind", "candidates"},
    "contradict": {"old_text", "new_text", "old_recorded", "new_recorded"},
    "answer_web": {"question", "results"},
}


@pytest.fixture(scope="module")
def prompts() -> PromptSet:
    ps = PromptSet.load(PROMPTS_DIR)
    assert ps.refs(), "no prompts found"
    return ps


def test_all_prompts_present_and_versioned(prompts):
    for name in EXPECTED:
        assert name in prompts.refs(), f"missing prompt {name}"
        assert prompts[name].version >= 1
        assert prompts[name].description


def test_render_complete_no_leftover_placeholders(prompts):
    sample = dict(
        anchor="2026-07-07T18:00:00+00:00", today="2026-07-07 (Tuesday)",
        transcript="[18:00:03] Jasmine: hi", known_entities="Jasmine (person)",
        vocabulary="artes", speaker_label="Owner", text="when is dinner?",
        window="Owner: when is dinner?", memory_text="dinner is chili",
        memory_context="type=event", question="when is dinner?", memories="[m:x] dinner",
        unknowns="[]", roster="Owner (named)", known_terms="artes",
        entity_name="Jasmine", entity_kind="person", new_name="ARTES", new_kind="acronym",
        candidates="- id=1 ARTES (acronym)", old_text="dinner friday", new_text="dinner sat",
        old_recorded="2026-07-01", new_recorded="today", results="- t (u)",
    )
    for name, vars_needed in EXPECTED.items():
        p = prompts[name]
        rendered = p.render(**{k: v for k, v in sample.items() if k in vars_needed})
        assert "{{" not in rendered, f"{name} has unrendered placeholders"


def test_missing_var_raises(prompts):
    with pytest.raises(KeyError):
        prompts["extract"].render(anchor="x")  # everything else missing


def test_content_hash_stable(prompts):
    h1 = prompts.content_hash()
    h2 = PromptSet.load(PROMPTS_DIR).content_hash()
    assert h1 == h2 and len(h1) == 16


def test_extract_prompt_contains_fixture_transcript_and_attribution_rule(prompts):
    body = prompts["extract"].body
    # unit test "using fixture transcripts" per spec §4.2
    assert "Jasmine told Spencer" in body or "dinner will be Chili" in body
    assert "who_said" in body and "said_to" in body
