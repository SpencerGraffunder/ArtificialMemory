"""Test/eval utilities: deterministic fake OpenAI-compatible endpoint + bag-of-words
embeddings + config factory. Used by the server test-suite and the eval replay harness
so both exercise the identical HTTP code paths as a real vLLM/Ollama backend."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx

from .config import AppConfig, load_config

EMBED_DIM = 64


def fake_embed_vector(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic bag-of-words hashed embedding: shared words ⇒ high cosine."""
    v = [0.0] * dim
    for tok in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


class FakeLLM:
    """httpx.MockTransport serving /chat/completions + /embeddings + /models.

    Chat responses route on a marker substring of the SYSTEM prompt:
    `fake.on("memory extractor", {...ExtractionResult json...})`. The response may be a
    dict (JSON-mode content), a raw string, or a callable(messages)->dict|str — so the
    replay harness can script turn-by-turn behaviour."""

    def __init__(self):
        self.responses: list[tuple[str, object]] = []
        self.calls: list[dict] = []
        self.embed_error = False

    def on(self, marker: str, response) -> None:
        self.responses.append((marker, response))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, base_url: str = "http://fake-llm/v1") -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, transport=self.transport)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fake"}]})
        if path.endswith("/embeddings"):
            if self.embed_error:
                return httpx.Response(503, json={"error": "down"})
            body = json.loads(request.content)
            data = [{"index": i, "embedding": fake_embed_vector(t)}
                    for i, t in enumerate(body["input"])]
            return httpx.Response(200, json={"data": data})
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            self.calls.append(body)
            system = body["messages"][0]["content"]
            for marker, resp in self.responses:
                if marker in system:
                    payload = resp(body["messages"]) if callable(resp) else resp
                    content = payload if isinstance(payload, str) else json.dumps(payload)
                    return httpx.Response(200, json={
                        "choices": [{"message": {"content": content}}]})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "{}"}}]})
        return httpx.Response(404, json={"error": f"unhandled {path}"})


def make_test_cfg(db_path: str | Path, **overrides) -> AppConfig:
    """AppConfig with test-token auth, fake LLM base_url, deterministic weights intact."""
    cfg = load_config(config_path=Path("nonexistent.yaml"), env={"ARTMEM_TOKEN": "test-token"})
    cfg.storage.db_path = str(db_path)
    cfg.storage.export_dir = str(Path(db_path).parent / "exports")
    cfg.llm.embedding_dim = EMBED_DIM
    cfg.llm.base_url = "http://fake-llm/v1"
    cfg.search.backend = "none"
    for key, value in overrides.items():
        *parents, leaf = key.split("__")
        node = cfg
        for p in parents:
            node = getattr(node, p)
        setattr(node, leaf, value)
    return cfg
