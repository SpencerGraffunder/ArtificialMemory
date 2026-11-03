"""Shared pytest fixtures; heavy lifting lives in ambient_server.testing so the eval
replay harness reuses the exact same fake endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ambient_server.services import AppContext
from ambient_server.testing import FakeLLM, fake_embed_vector, make_test_cfg  # noqa: F401

__all__ = ["FakeLLM", "fake_embed_vector", "make_test_cfg", "ctx", "store", "fake_llm"]


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def ctx(tmp_path: Path, fake_llm: FakeLLM):
    """AppContext wired to the fake LLM; background loop NOT started."""
    cfg = make_test_cfg(tmp_path / "ambient.sqlite3")
    app_ctx = AppContext(cfg, llm_client=fake_llm.client(cfg.llm.base_url))
    yield app_ctx

    async def _close() -> None:
        await app_ctx.aclose()

    try:
        asyncio.new_event_loop().run_until_complete(_close())
    except RuntimeError:  # pragma: no cover
        pass


@pytest.fixture
def store(ctx: AppContext):
    return ctx.store
