# Ambient Memory — home server image (spec §2: single SQLite file, no external deps)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AMBIENT_STORAGE__DATA_DIR=/data \
    AMBIENT_STORAGE__DB_PATH=/data/ambient.sqlite3 \
    AMBIENT_STORAGE__EXPORT_DIR=/data/exports

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# prompts are resolved relative to CWD in packaged installs (AppContext fallback)
COPY prompts ./prompts
COPY healthcheck.py /app/healthcheck.py

RUN useradd --create-home --uid 10001 ambient \
    && mkdir -p /data/exports && chown -R ambient:ambient /data
USER ambient
VOLUME /data
EXPOSE 8378

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "/app/healthcheck.py"]

CMD ["sh", "-c", "uvicorn ambient_server.main:app --host 0.0.0.0 --port ${AMBIENT_SERVER__PORT:-8378}"]
