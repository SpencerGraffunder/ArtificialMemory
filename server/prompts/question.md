---
version: 2
description: Live utterance → is this a question, and was it addressed to someone specific
---
You classify one spoken utterance from a live conversation for the artificial memory system.

Decide:
- is_question: the speaker is asking something they want answered (Wh-questions, yes/no
  questions, "does anyone know...", self-directed musing counts if it seeks a fact;
  rhetorical questions excluded).
- addressed_to_someone: the question is directed at a specific present person ("Marcus,
  did you ship it?") rather than the room/the owner/the air ("when did we last go to the
  park?", "what was that movie called?").
- topic: ≤6 words naming what the question is about, for retrieval.

Utterance (speaker {{speaker_label}}):
"{{text}}"

Conversation so far (last ~60s):
{{window}}
