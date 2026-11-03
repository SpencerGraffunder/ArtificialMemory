# ArtificialMemory — home server image (spec §2: single SQLite file, no external deps)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ARTMEM_STORAGE__DATA_DIR=/data \
    ARTMEM_STORAGE__DB_PATH=/data/artmem.sqlite3 \
    ARTMEM_STORAGE__EXPORT_DIR=/data/exports

WORKDIR /app
COPY --chmod=644 pyproject.toml ./
COPY --chmod=644 src ./src
RUN pip install .

# prompts are resolved relative to CWD in packaged installs (AppContext fallback).
# --chmod so a non-root runtime user can read them even if the host tree is 0600.
COPY --chmod=644 prompts ./prompts
COPY --chmod=644 healthcheck.py /app/healthcheck.py

# Belt-and-braces: some BuildKit versions serve a cached COPY layer that kept the
# host's 0700 bits, so the non-root runtime user can't read prompts/healthcheck.
# Explicitly make them world-readable on the real image FS before USER drop.
RUN chmod -R a+rX /app/prompts /app/healthcheck.py

RUN useradd --create-home --uid 10001 artmem \
    && mkdir -p /data/exports && chown -R artmem:artmem /data
USER artmem
VOLUME /data
EXPOSE 8378

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "/app/healthcheck.py"]

CMD ["sh", "-c", "uvicorn artmem_server.main:app --host 0.0.0.0 --port ${ARTMEM_SERVER__PORT:-8378}"]
