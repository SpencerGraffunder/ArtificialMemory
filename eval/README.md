# Eval — relevance replay harness

Feeds labeled fixture transcripts through the **real pipeline** (LiveEngine, store,
supersession, question timers — only the LLM endpoint is faked) and scores the cards it
would have shown: **precision, recall, cards/hour**. This is the tuning loop for
`relevance.*` weights, `gate_threshold`, `echo_suppress`, cooldowns.

## Run

```bash
cd server
.venv/bin/python ../eval/replay/run.py                  # all fixtures
.venv/bin/python ../eval/replay/run.py --fixture 04 --verbose
.venv/bin/python ../eval/replay/run.py --json ../eval/report.json
```

Exit code 0 = every fixture passed its labels, `max_total_cards` and
`post.min_clarifications`. Current baseline: **12/12, P=1.00, R=1.00, 3.15 cards/hour**
(spec target zone: 1–4/hour sustained conversation).

## How it works

* Deterministic fake endpoint (`artmem_server.testing.FakeLLM` + hashed bag-of-words
  embeddings) — same HTTP code paths as real vLLM/Ollama, zero GPU/network.
* A **simulated clock** replaces `utc_now` in every app module for the duration of a
  fixture, so rolling windows, decay, cooldowns and conversation gaps all follow the
  fixture's timestamps while wall-clock sleeps stay compressed (~1 s per fixture).
* Question answer-waits run at 0.3 s wall; the harness outlasts the timer before the
  next line when nobody answers, and delivers would-be answers before it when someone does.
* Cards are captured from both paths (utterance return values + async timer broadcast).

## Fixture format (`fixtures/*.json`)

```jsonc
{
  "id": "event-mention-dinner",
  "anchor": "2026-07-07T18:00:00+00:00",   // simulated wall clock
  "seed_memories": [                        // inserted directly (extraction is separate)
    {"text": "...", "type": "event", "importance": 0.9, "confidence": 0.95,
     "event_days_ahead": 3, "entities": [["Jasmine", "person"]]}
  ],
  "utterances": [
    {"t": 6, "speaker": "Owner", "label": "Owner", "text": "…",
     "question": true, "whisper": false}    // optional flags
  ],
  "labels": [
    {"after_utterance": 1, "expect": "card", "memory_contains_any": ["dinner", "chili"]},
    {"after_utterance": 2, "expect": "silence"}
  ],
  "max_total_cards": 1,                     // anti-spam budget (optional)
  "post": {"min_clarifications": 1},        // after closing extraction (optional)
  "extraction": {...}, "clarify": {...}     // scripted LLM replies per prompt marker
}
```

Scoring rules: card-labels match within 12 s simulated after the utterance; silence
windows end when the next utterance begins (a card caused by the next line belongs to
the next label); any card matching no label counts against precision.

## Fixture design rule — a card must ADD remembered info

The engine hard-blocks **echoes**: if ≥ `relevance.echo_suppress` (default 0.72) of a
memory's content words were just spoken in the live window, the conversation is already
relaying that memory and a card would interrupt with zero new information. Fixtures
therefore pair each spoken topic with memories that carry *consequences* of the topic
(kickoff meeting, vendor renewal, gear location), not its restatement — the same rule
that keeps real users from swiping left.

## Scenario coverage (spec §12 M7)

event mentions · project/person topic shifts · unanswered questions (lookup path) ·
questions answered by humans (etiquette suppression) · whispered owner queries ·
acronym introduction → clarification inbox · small-talk FP probe · repeated-mention
cooldown/spam probe · multi-topic day (cards/hour).

## Tuning workflow

1. Edit `server/config.yaml` (copy from `config.example.yaml`) — weights, threshold,
   echo_suppress, cooldowns.
2. Re-run the harness; compare `report.json` across runs (`git diff` it).
3. Unit tests (`server/tests/test_live.py`) pin the behavioral contracts; fixtures pin
   end-to-end moments. Keep both green together.
