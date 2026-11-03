---
version: 1
description: Fuzzy-matched new entity → does it refer to an existing entity?
---
A memory mentioned "{{new_name}}" (kind: {{new_kind}}). Decide if it refers to one of the
existing entities below (same person/place/project, allowing nicknames and acronyms).
Answer with the matching id, or null. Only match if you are sure.

Existing entities:
{{candidates}}
