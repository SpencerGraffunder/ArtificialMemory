"""uvicorn entrypoint: ARTMEM_TOKEN, ARTMEM_CONFIG, optional config.yaml.

Run:  uvicorn artmem_server.main:app --host 0.0.0.0 --port 8378
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from .api.app import create_app
from .config import load_config
from .services import AppContext

logging.basicConfig(
    level=os.environ.get("ARTMEM_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
log = logging.getLogger("artmem")

cfg = load_config()
ctx = AppContext(cfg)


@asynccontextmanager
async def lifespan(app):  # noqa: ANN201
    log.info(
        "artmem server %s up; db=%s vec=%s prompts_hash=%s",
        __import__("artmem_server").__version__, ctx.db.path, ctx.db.using_vec,
        ctx.prompts.content_hash(),
    )
    if not cfg.token:
        log.warning("ARTMEM_TOKEN is not set — every endpoint will return 503")
    await ctx.start_background()
    try:
        yield
    finally:
        await ctx.aclose()


app = create_app(ctx)
app.router.lifespan_context = lifespan
