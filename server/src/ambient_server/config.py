"""Configuration: defaults < config.yaml < environment (AMBIENT_*, `__` nests).

Secrets are env-only: AMBIENT_TOKEN, LLM_API_KEY, EMBEDDING_API_KEY.
Everything else is configurable in config.yaml (see config.example.yaml).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8378


class LLMConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = ""  # env-only: LLM_API_KEY
    model_fast: str = "gpt-4o-mini"
    model_deep: str = "gpt-4o"
    max_tokens_fast: int = 600
    max_tokens_deep: int = 2400
    timeout_s: float = 30.0
    embedding_base_url: str = ""
    embedding_api_key: str = ""  # env-only: EMBEDDING_API_KEY
    embedding_model: str = "bge-small-en-v1.5"
    embedding_dim: int = 384

    @property
    def resolved_embedding_base_url(self) -> str:
        return self.embedding_base_url or self.base_url

    @property
    def resolved_embedding_api_key(self) -> str:
        return self.embedding_api_key or self.api_key


class SearchConfig(BaseModel):
    backend: str = "searxng"  # searxng | none
    searxng_url: str = "http://127.0.0.1:8888"
    max_results: int = 5
    timeout_s: float = 8.0


class ConversationConfig(BaseModel):
    silence_gap_s: float = 180.0
    incremental_every: int = 12
    location_move_m: float = 800.0


class RelevanceWeights(BaseModel):
    semantic: float = 0.40
    entity_overlap: float = 0.20
    actionability: float = 0.15
    importance: float = 0.10
    recency_decay: float = 0.10
    speaker_match: float = 0.05
    recently_shown: float = -0.30
    dismissed_before: float = -0.30


class RelevanceConfig(BaseModel):
    window_s: float = 60.0
    partial_stability_s: float = 1.5
    top_k: int = 20
    gate_threshold: float = 0.55
    cooldown_s: float = 600.0
    max_cards: int = 3
    card_ttl_s: float = 45.0
    topic_shift_cosine: float = 0.30
    echo_suppress: float = 0.72  # ≥ this share of a memory's content words just spoken live → no card
    llm_judge_enabled: bool = False
    keyword_fallback: bool = True
    question_wait_s: float = 6.0
    weights: RelevanceWeights = Field(default_factory=RelevanceWeights)
    actionability_horizon_days: int = 14


class RecallConfig(BaseModel):
    half_life_h: float = 168.0


class ExtractionConfig(BaseModel):
    # delegated=true: conversations queue as extraction_tasks for the phone to execute
    # with its own LLM credentials; the server only runs them itself after
    # fallback_delay_s (in-flight requests survive phone death — the durability buffer).
    delegated: bool = False
    fallback_delay_s: float = 600.0
    incremental_fallback_delay_s: float = 300.0
    claim_stale_after_s: float = 900.0     # phone died mid-claim → releasable
    supersession_enabled: bool = True
    supersede_similarity: float = 0.82   # vector sim above which an old memory gets LLM-checked
    disambiguate_llm: bool = True        # fast-LLM disambiguation in the 0.60–0.87 fuzzy band


class QueryConfig(BaseModel):
    top_k: int = 25
    gate_threshold: float = 0.20
    synthesize_answer: bool = True


class ClarificationConfig(BaseModel):
    max_per_digest: int = 5
    expiry_days: int = 14
    remind_after_days: int = 7
    digest_local_time: str = "21:30"
    timezone: str = "America/Los_Angeles"

    @field_validator("digest_local_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        hh, _, mm = v.partition(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError("digest_local_time must be HH:MM")
        return v


class StorageConfig(BaseModel):
    data_dir: str = "./data"
    db_path: str = "./data/ambient.sqlite3"
    keep_raw_audio: bool = False
    export_dir: str = "./data/exports"


class TelemetryConfig(BaseModel):
    log_level: str = "INFO"


class AppConfig(BaseModel):
    token: str = ""  # env-only: AMBIENT_TOKEN
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    conversations: ConversationConfig = Field(default_factory=ConversationConfig)
    relevance: RelevanceConfig = Field(default_factory=RelevanceConfig)
    recall: RecallConfig = Field(default_factory=RecallConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    query: QueryConfig = Field(default_factory=QueryConfig)
    clarifications: ClarificationConfig = Field(default_factory=ClarificationConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none"):
        return None
    if raw == "":
        return ""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """AMBIENT_RELEVANCE__GATE_THRESHOLD=0.6 -> {"relevance": {"gate_threshold": 0.6}}.

    Single-underscore AMBIENT_TOKEN is a top-level key. AMBIENT_LLM__BASE_URL nests.
    """
    tree: dict[str, Any] = {}
    for key, value in env.items():
        if not key.startswith("AMBIENT_"):
            continue
        parts = key[len("AMBIENT_") :].lower().split("__")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _coerce(value)
    return tree


def load_config(
    config_path: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
) -> AppConfig:
    env = dict(os.environ) if env is None else env
    data: dict[str, Any] = {}
    path = config_path or env.get("AMBIENT_CONFIG") or Path("config.yaml")
    p = Path(path)
    if p.is_file():
        loaded = yaml.safe_load(p.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{p} must contain a YAML mapping")
        data = loaded
    data = _deep_merge(data, _env_overrides(env))
    # Secrets: env-only, never read from yaml even if someone put them there.
    token = env.get("AMBIENT_TOKEN", "")
    llm_over = data.setdefault("llm", {}) if isinstance(data.get("llm", {}), dict) else {}
    llm_over = data.setdefault("llm", {})
    llm_over["api_key"] = env.get("LLM_API_KEY", "")
    llm_over["embedding_api_key"] = env.get("EMBEDDING_API_KEY", "")
    data["token"] = token
    return AppConfig.model_validate(data)
