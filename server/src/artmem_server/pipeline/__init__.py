"""Pipeline: extraction, relevance, lookup, search. Orchestration that ties
store + LLM + embeddings together (no FastAPI imports here; the replay harness
drives these classes directly)."""
