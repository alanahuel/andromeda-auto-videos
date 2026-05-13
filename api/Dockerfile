# syntax=docker/dockerfile:1.7
# Build context MUST be the project root (see docker-compose.yml) so that the
# `shared/` directory is reachable.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:/root/.local/bin:$PATH" \
    PYTHONPATH=/app

# FFmpeg is required at runtime — the API now does the render in-process.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# uv is a single static binary — install from the official image without apt.
COPY --from=ghcr.io/astral-sh/uv:0.4.27 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first for cache reuse.
COPY api/pyproject.toml api/uv.lock* /app/
RUN uv sync --frozen --no-dev || uv sync --no-dev

# Copy source. Two top-level packages on PYTHONPATH: `src` and `shared`.
COPY api/src /app/src
COPY shared /app/shared

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); sys.exit(0 if r.status==200 else 1)" || exit 1

# Single worker: renders are CPU-bound and serialised via asyncio.Semaphore.
# Multiple uvicorn workers would each grab their own lock → races for the CPU.
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
