"""Embedding helper: never hard-fails callers — returns None when the embedding server
is unreachable so recall degrades to keyword-only instead of breaking ingest."""

from __future__ import annotations

import logging

from ..llm.gateway import LLMError, LLMGateway

log = logging.getLogger("ambient.embed")


class EmbeddingService:
    def __init__(self, gateway: LLMGateway | None):
        self._gw = gateway
        self._warned = False

    @property
    def enabled(self) -> bool:
        return self._gw is not None

    async def embed_one(self, text: str) -> list[float] | None:
        out = await self.embed_many([text])
        return out[0] if out else None

    async def embed_many(self, texts: list[str]) -> list[list[float] | None]:
        if self._gw is None or not texts:
            return [None] * len(texts)
        try:
            vecs = await self._gw.embed(texts)
            self._warned = False
            return vecs
        except LLMError as exc:
            if not self._warned:
                log.warning("embeddings unavailable (%s); keyword-only recall", exc)
                self._warned = True
            return [None] * len(texts)
