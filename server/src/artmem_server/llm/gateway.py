"""OpenAI-compatible LLM gateway: two named profiles (fast/deep), JSON mode with
pydantic validation + one repair retry, and an embeddings client.

Design notes (DECISIONS.md):
- JSON outputs: request response_format json_object; validate against the caller's
  pydantic model; on failure one retry whose user message includes the validation
  error. Hard failure afterwards raises LLMError (callers must degrade, never block
  ingest).
- Embeddings: /embeddings on llm.resolved_embedding_base_url. If unreachable the
  caller degrades to keyword-only recall (vector=None).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import LLMConfig
from .prompts import Prompt

log = logging.getLogger("artmem.llm")

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


def build_json_messages(prompt: Prompt, model: type[BaseModel], user: str,
                        **extra: object) -> list[dict[str, str]]:
    """The exact messages a JSON completion would send — remote executors (phone LLM)
    get these verbatim so there is one prompt pipeline, not two."""
    system = (
        prompt.render(**extra)
        + "\n\nRespond ONLY with JSON matching this schema (no prose, no code fences):\n"
        + _schema_hint(model)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def repair_followup_messages(messages: list[dict[str, str]], raw: str,
                             error: object) -> list[dict[str, str]]:
    return messages + [
        {"role": "assistant", "content": raw[:2000]},
        {"role": "user", "content": "Your previous reply was not valid JSON for the "
         f"schema. Error: {str(error)[:800]}\nReply again with ONLY valid JSON."},
    ]


def _schema_hint(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


class LLMGateway:
    def __init__(self, cfg: LLMConfig, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=cfg.timeout_s,
            headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
        )
        self._owns_client = client is None
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embed_dim: int | None = cfg.embedding_dim or None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- chat
    async def _chat(self, profile: str, messages: list[dict], json_mode: bool) -> str:
        model = self.cfg.model_fast if profile == "fast" else self.cfg.model_deep
        max_tokens = self.cfg.max_tokens_fast if profile == "fast" else self.cfg.max_tokens_deep
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"llm unreachable ({profile}): {exc}") from exc
        if r.status_code != 200:
            raise LLMError(f"llm HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"malformed llm response: {str(data)[:200]}") from exc

    async def complete_json(self, profile: str, prompt: Prompt, model: Type[T],
                            user: str, **extra: object) -> T:
        """Schema-constrained JSON; one repair retry; LLMError otherwise."""
        return await self.complete_json_messages(
            profile, build_json_messages(prompt, model, user=user, **extra), model)

    async def validate_json(self, raw: str, model: Type[T]) -> T:
        """Validate a remote executor's reply; raises ValidationError with the detail the
        phone needs for its repair-retry (mirrors the local retry's error text)."""
        return model.model_validate(_parse_json(raw))

    async def complete_json_messages(self, profile: str, messages: list[dict[str, str]],
                                     model: Type[T]) -> T:
        raw = await self._chat(profile, messages, json_mode=True)
        try:
            return model.model_validate(_parse_json(raw))
        except (ValueError, ValidationError) as first:
            raw2 = await self._chat(
                profile, repair_followup_messages(messages, raw, first), json_mode=True)
            try:
                return model.model_validate(_parse_json(raw2))
            except (ValueError, ValidationError) as second:
                raise LLMError(f"llm JSON invalid after retry: {second}") from second

    async def complete_text(self, profile: str, system: str, user: str) -> str:
        return await self._chat(profile, [{"role": "system", "content": system},
                                          {"role": "user", "content": user}], json_mode=False)

    # ---------------------------------------------------------------- embeddings
    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        todo = [t for t in texts if hashlib.md5(t.encode()).hexdigest() not in self._embed_cache]
        if todo:
            base = self.cfg.resolved_embedding_base_url.rstrip("/")
            headers = (
                {"Authorization": f"Bearer {self.cfg.resolved_embedding_api_key}"}
                if self.cfg.resolved_embedding_api_key else {}
            )
            try:
                client = self._client
                # embeddings may live on a different base_url → absolute URL request
                r = await client.post(
                    f"{base}/embeddings",
                    json={"model": self.cfg.embedding_model, "input": todo},
                    headers=headers,
                )
                if r.status_code != 200:
                    raise LLMError(f"embeddings HTTP {r.status_code}: {r.text[:200]}")
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                vecs = [d["embedding"] for d in data]
            except httpx.HTTPError as exc:
                raise LLMError(f"embeddings unreachable: {exc}") from exc
            for t, v in zip(todo, vecs, strict=True):
                if self._embed_dim is None:
                    self._embed_dim = len(v)
                if len(v) != self._embed_dim:
                    raise LLMError(
                        f"embedding dim {len(v)} != configured {self._embed_dim}; "
                        "fix llm.embedding_dim or the embedding model"
                    )
                self._embed_cache[hashlib.md5(t.encode()).hexdigest()] = v
                while len(self._embed_cache) > 4096:
                    self._embed_cache.popitem(last=False)
        return [self._embed_cache[hashlib.md5(t.encode()).hexdigest()] for t in texts]

    @property
    def embed_dim(self) -> int | None:
        return self._embed_dim

    async def probe(self) -> dict[str, Any]:
        """Health info for /v1/health: model reachability without side effects."""
        out: dict[str, Any] = {"llm_base_url": self.cfg.base_url, "model_fast": self.cfg.model_fast,
                               "model_deep": self.cfg.model_deep,
                               "embedding_model": self.cfg.embedding_model}
        try:
            r = await self._client.get("/models")
            out["llm_reachable"] = r.status_code == 200
        except httpx.HTTPError:
            out["llm_reachable"] = False
        try:
            await self.embed(["probe"])
            out["embeddings_reachable"] = True
            out["embedding_dim"] = self._embed_dim
        except LLMError as exc:
            out["embeddings_reachable"] = False
            out["embeddings_error"] = str(exc)[:160]
        return out


def _parse_json(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # tolerate models that surround JSON with prose
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            return json.loads(raw[s : e + 1])
        raise
