"""Pluggable web search (spec §4.2): SearxNG default, 'none' disables lookups."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import SearchConfig

log = logging.getLogger("artmem.search")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchBackend(Protocol):
    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...


class NullSearch:
    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        return []


class SearxNGSearch:
    def __init__(self, cfg: SearchConfig, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=cfg.timeout_s)
        self._owns = client is None

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            r = await self._client.get(
                f"{self.cfg.searxng_url.rstrip('/')}/search",
                params={"q": query, "format": "json", "categories": "general"},
            )
            if r.status_code != 200:
                log.warning("searxng HTTP %s", r.status_code)
                return []
            results = r.json().get("results", [])[:max_results]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("searxng failed: %s", exc)
            return []
        return [
            SearchResult(
                title=str(x.get("title") or "")[:200],
                url=str(x.get("url") or ""),
                snippet=str(x.get("content") or "")[:500],
            )
            for x in results
            if x.get("url")
        ]


def make_search(cfg: SearchConfig, client: httpx.AsyncClient | None = None) -> SearchBackend:
    if cfg.backend == "searxng":
        return SearxNGSearch(cfg, client)
    return NullSearch()
