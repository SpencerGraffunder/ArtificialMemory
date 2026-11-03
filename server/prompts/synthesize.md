---
version: 2
description: Direct query → 1-2 sentence answer grounded ONLY in retrieved memories
---
Answer the owner's question using ONLY the retrieved memories below. Cite memory ids as
[m:<id>]. If the memories do not answer, say exactly what is missing in one short
sentence; do not guess, do not use outside knowledge.

Question: {{question}}

Memories:
{{memories}}
