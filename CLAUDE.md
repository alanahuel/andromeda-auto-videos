# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

The actual project lives at `render-service/`. The top-level directory is otherwise empty. All commands below assume `cd render-service` first.

```
render-service/
├── api/        FastAPI front door (enqueues RQ jobs; never touches ffmpeg or Drive)
├── worker/     RQ worker (downloads → ffmpeg → uploads → callbacks)
├── shared/     Pydantic v2 wire-contract models, imported by both as `from shared.models import ...`
└── docker-compose.yml
```

Two separate Python projects (`api/pyproject.toml`, `worker/pyproject.toml`), each with its own `uv.lock`. The `shared/` package is consumed by both via `PYTHONPATH=/app` in the Dockerfiles and via `pyproject.toml`'s `[tool.pytest.ini_options] pythonpath = [".", "../shared/.."]` for local pytest. **Both Docker builds need the project root as build context** so `shared/` is reachable.

## Common commands

All from `render-service/`:

```bash
# Bring the full stack up (api + worker + redis). Requires .env with RENDER_API_KEY and a Drive SA.
docker compose up --build

# First-time only: generate lockfiles (Dockerfile falls back to `uv sync --no-dev` resolve-on-build if missing)
cd api && uv lock && cd ../worker && uv lock && cd ..

# Local dev with hot-reload + bind-mounted source + bind-mounted sa.json
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose up

# Worker tests (ffmpeg command builders + probe parsing; subprocess is mocked, no ffmpeg needed)
cd worker && uv sync && uv run pytest -q
# Single test
cd worker && uv run pytest tests/test_ffmpeg_runner.py::test_concat_fastpath_when_all_match_and_orientation_matches -v

# Smoke test the API (job will fail at download but proves auth + enqueue work)
curl -s http://localhost:8000/health | jq
curl -s -X POST http://localhost:8000/jobs -H "X-API-Key: $RENDER_API_KEY" -H "Content-Type: application/json" -d @payload.json
```

There is no lint/format tool configured and no test suite for the API service — only the worker has tests.

## Architecture

The service receives a Make.com webhook with 3 Google Drive clip IDs + 1 music ID, assembles a video ad with ffmpeg, uploads it back to Drive, and POSTs the result to a caller-supplied `callback_url`. Three containers: `api`, `worker`, `redis`.

### Request lifecycle

1. `POST /jobs` (api/src/main.py) — validates `JobRequest` (Pydantic, `extra="forbid"`, exactly 3 clips with roles `hook`/`cuerpo`/`cta`), generates a UUID `job_id`, enqueues into RQ queue `renders`. **The API never imports the worker function** — it enqueues by string reference `WORKER_JOB_FUNCTION = "src.job_runner.run_job"` (api/src/queue_client.py). This keeps the API image free of ffmpeg and the Drive SDK.
2. Worker picks up the job and `run_job(job_id, payload)` (worker/src/job_runner.py) executes the strict sequence: validate → mkdir workdir → download 3 clips + music → `ffprobe` each clip → concat (fast or re-encoded) → mix music → upload → success callback. Any failure raises `_FriendlyError` whose Spanish message is forwarded to the callback (and to Make/Airtable). Workdir is removed in `finally` regardless.
3. The callback (worker/src/callback.py) retries 3× with exponential backoff (5s/15s/45s). The job is still marked `done`/`failed` in Redis whether or not the callback lands.

### Concat fast-path vs re-encode

`ffmpeg_runner.can_concat_without_reencode` (worker/src/ffmpeg_runner.py) returns true only when all three clips share codec, width/height, fps, audio codec, sample rate, AND match the requested `output.orientation`. If true, the worker uses ffmpeg's concat demuxer with `-c copy` (seconds). Otherwise it builds a `-filter_complex` graph that per-clip scales+crops/pads to 1080×1920 (vertical) or 1920×1080 (horizontal) at 30 fps, then concats. The strategy (`crop` vs `pad`) is controlled by `ORIENTATION_STRATEGY` env var.

### Silent clips

When a clip has no audio stream, the re-encode path injects a `lavfi anullsrc` input of matching duration as that clip's audio slot — so the `concat=` filter always sees 3 video+audio pairs. The music-mix step has a separate branch for the rare case the entire concat ended up audio-less.

### Shared models contract

`shared/models.py` is the single source of truth for the wire format. Both Dockerfiles `COPY shared /app/shared`. Changing this file requires rebuilding both images. The `JobRequest` validator enforces exactly 3 clips with the role set `{hook, cuerpo, cta}` — there is no support for more or fewer clips.

### Settings

`api/src/settings.py` and `worker/src/settings.py` are separate `pydantic-settings` `BaseSettings` classes. They share `REDIS_URL` and `LOG_LEVEL` but each has its own surface. Both use `@lru_cache` singletons via `get_settings()` — never instantiate `Settings()` directly.

Worker exposes one of two Drive SA channels (mutually exclusive): `GOOGLE_SERVICE_ACCOUNT_JSON` (file path) **or** `GOOGLE_SERVICE_ACCOUNT_JSON_B64` (env var with base64'd JSON). The worker fails fast on startup if neither is set.

### Concurrency

`WORKER_CONCURRENCY=1` is the production default. With concurrency > 1 the worker switches to `rq.worker_pool.WorkerPool` (forking). The host VPS has 2 vCPU / 7.8 GiB — do not raise above 2 (`mem_limit: 3g` in compose protects Redis/api from a runaway ffmpeg).

## Conventions to follow

- **Logging is structlog JSON to stdout.** Bind `job_id` via `structlog.contextvars.bind_contextvars` at the start of `run_job` and clear in `finally`. Standard event names: `job_enqueued`, `job_started`, `downloading_clips`, `concat_fast_path`/`concat_reencode_path`, `uploading_output`, `job_done`, `job_failed`.
- **Never log the Service Account JSON or `RENDER_API_KEY`.** `auth.require_api_key` already uses `hmac.compare_digest`.
- **User-facing error messages are in Spanish** (the audiovisual team reads them in Make/Airtable). `_FriendlyError` in `job_runner.py` is the sentinel that gets surfaced; everything else becomes a generic "Error inesperado".
- **Pydantic v2 only.** Use `model_validate` / `model_dump(mode="json")` (the API uses `mode="json"` so `HttpUrl` becomes a primitive before RQ pickles it).
- **FFmpeg builders return `list[str]`** and are pure (no subprocess). The `run()` and `probe()` functions are the only places that shell out — keeps the builders unit-testable. New ffmpeg invocations should follow this split.
- **All MP4 outputs use `-movflags +faststart`** so Drive/web seek immediately.
- **`output.name` is regex-validated** `^[A-Za-z0-9._-]+$` at the schema layer — don't add a second validation pass downstream.
