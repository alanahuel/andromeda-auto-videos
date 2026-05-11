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

# FFmpeg + minimal extras (ca-certificates for Drive HTTPS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fail the build if ffmpeg or ffprobe somehow aren't on PATH.
RUN ffmpeg -version && ffprobe -version

COPY --from=ghcr.io/astral-sh/uv:0.4.27 /uv /usr/local/bin/uv

WORKDIR /app

COPY worker/pyproject.toml worker/uv.lock* /app/
RUN uv sync --frozen --no-dev || uv sync --no-dev

COPY worker/src /app/src
COPY shared /app/shared

# Workers don't accept inbound traffic — no EXPOSE / no HEALTHCHECK.
CMD ["uv", "run", "python", "-m", "src.worker"]
