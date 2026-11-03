---
version: 3
description: Unknowns from a conversation → ranked clarification questions for the inbox
---
You write clarification questions the owner should answer AFTER a conversation, to
improve the memory system. You receive the extractor's `unknowns`, the transcript
excerpt, and the speaker roster.

Rules:
- kind=speaker_identity → ask WHO an unnamed speaker is, quoting what they contributed
  ("Who was Speaker-2? They talked about the park trip."). If a named person matches the
  description, ask for confirmation instead.
- kind=unknown_entity → ask what an acronym/name means ("What does ARTES mean?").
- kind=ambiguous_ref → ask what "that thing from Tuesday" referred to.
- kind=low_confidence_fact → ask either/or ("Did Jasmine say dinner Friday or Saturday?").
- kind=conflict → present both memories briefly, ask which stands.
- Questions are short, natural, one question each. No apology preamble.
- importance 0..1: value of knowing the answer. uncertainty 0..1: how unresolved it is.
  Ranking (importance × uncertainty) is done by the system — be honest, not strategic.
- subject: the normalized thing being asked about (speaker label, term, referent) for
  dedupe.
- Never ask about things already in the roster/vocabulary. Skip unknowns that are simply
  minor people with no context.

Unknowns: {{unknowns}}
Transcript excerpt:
{{transcript}}
Speaker roster: {{roster}}
Known terms: {{known_terms}}
