# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

The actual project lives at `render-service/`. The top-level directory is otherwise empty. All commands below assume `cd render-service` first.

```
render-service/
├── api/        FastAPI service: validates multipart in, runs ffmpeg in-process, streams MP4 out
├── shared/     Pydantic v2 wire-contract models, imported as `from shared.models import ...`
└── docker-compose.yml
```

Single Python project (`api/pyproject.toml`) with its own `uv.lock`. The `shared/` package is consumed via `PYTHONPATH=/app` in the Dockerfile and via `[tool.pytest.ini_options] pythonpath = [".", "../shared/.."]` for local pytest. **The Docker build needs the project root as build context** so `shared/` is reachable.

## Common commands

All from `render-service/`:

```bash
# Bring the stack up (just the api container). Requires .env with RENDER_API_KEY.
docker compose up --build

# First-time only: generate lockfile (Dockerfile falls back to `uv sync --no-dev` resolve-on-build if missing)
cd api && uv lock && cd ..

# Local dev with hot-reload + bind-mounted source
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose up

# Tests (ffmpeg command builders + endpoint integration with run_pipeline mocked; subprocess is mocked, no ffmpeg needed)
cd api && uv sync && uv run pytest -q
# Single test
cd api && uv run pytest tests/test_ffmpeg_pipeline.py::test_concat_fastpath_when_all_match_and_orientation_matches -v

# Smoke test the API (the render itself will fail unless you upload real media, but auth + multipart parsing are exercised)
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/jobs \
  -H "X-API-Key: $RENDER_API_KEY" \
  -F "clip_hook=@hook.mp4" \
  -F "clip_cuerpo=@cuerpo.mp4" \
  -F "clip_cta=@cta.mp4" \
  -F "music=@music.mp3" \
  -F 'params={"orientation":"vertical","music_volume":0.3,"fade_in":2,"fade_out":2,"output_name":"smoke_test"}' \
  -o out.mp4 -D -
```

There is no lint/format tool configured.

## Architecture

The service receives a Make.com multipart POST with 3 clip files + 1 music file, assembles a video ad with ffmpeg in-process, and streams the MP4 back in the HTTP response. Single container — no queue, no callbacks, no external storage.

### Request lifecycle

1. `POST /jobs` (api/src/main.py) — auth via `X-API-Key`, parses the multipart, validates `params` against `JobParams` (Pydantic, `extra="forbid"`, orientation literal, regex on `output_name`).
2. `render_sync` (api/src/render_orchestrator.py) acquires the module-level `asyncio.Semaphore(1)`, persists the four `UploadFile`s into a per-job tmp workdir (`/tmp/render_<uuid>_*/`), and dispatches the sync pipeline via `asyncio.to_thread` so the event loop stays responsive for `/health`.
3. `run_pipeline` (api/src/ffmpeg_pipeline.py) runs the strict sequence: ffprobe each clip → decide fast-path vs re-encode → concat → ffprobe the concat → mix music → write the output MP4 inside the workdir. Returns a `PipelineResult(duration_seconds, concat_strategy)`.
4. The orchestrator reads the output bytes into memory (clips < 200 MB by ops note), returns them via `StreamingResponse`, and cleans the workdir in `finally`.

### Error handling

Known failures inside the pipeline raise `_FriendlyError` whose Spanish message is safe to surface (carries over from the old worker). The orchestrator translates `_FriendlyError → RenderError(str(e))`. Any other exception becomes a generic `RenderError("Error inesperado en el render — revisa los logs del servicio.")` so internal details (paths, tracebacks) never reach the caller. `RenderError` is mapped to HTTP 500 with body `{"error": <message>}`.

### Concat fast-path vs re-encode

`ffmpeg_pipeline.can_concat_without_reencode` returns true only when all three clips share codec, width/height, fps, audio codec, sample rate, AND match the requested `orientation`. If true, the pipeline uses ffmpeg's concat demuxer with `-c copy` (seconds). Otherwise it builds a `-filter_complex` graph that per-clip scales+crops/pads to 1080×1920 (vertical) or 1920×1080 (horizontal) at 30 fps, then concats. The strategy (`crop` vs `pad`) is controlled by `ORIENTATION_STRATEGY`. The chosen strategy is reflected back in the response header `X-Concat-Strategy: fast|reencode`. The per-render UUID generated in `render_sync` is also surfaced as `X-Job-Id` so callers can correlate logs with the response.

### Silent clips

When a clip has no audio stream, the re-encode path injects a `lavfi anullsrc` input of matching duration as that clip's audio slot — so the `concat=` filter always sees 3 video+audio pairs. The music-mix step has a separate branch for the rare case the entire concat ended up audio-less.

### Shared models contract

`shared/models.py` is the single source of truth for the wire format. The Dockerfile `COPY shared /app/shared`. Changing this file requires rebuilding the image. The `JobParams` model is the JSON payload of the multipart `params` field — there is no support for clip role overrides; the three clips are always supplied as the named multipart fields `clip_hook`, `clip_cuerpo`, `clip_cta`.

### Settings

`api/src/settings.py` is a `pydantic-settings` `BaseSettings`: `render_api_key`, `orientation_strategy`, `log_level`. `@lru_cache` singleton via `get_settings()` — never instantiate `Settings()` directly. There is no Redis, no Drive, no per-job-timeout env var — the ffmpeg hard timeout is hardcoded to 600 s in `ffmpeg_pipeline.FFMPEG_TIMEOUT_SECONDS`.

### Concurrency

`uvicorn --workers 1` in the Dockerfile is mandatory: renders are serialised via a process-local `asyncio.Semaphore(1)`. Multiple uvicorn workers would each carry their own semaphore → races for the CPU and OOM risk. The host VPS has 2 vCPU / 7.8 GiB and `mem_limit: 6g` in compose protects the host.

## Conventions to follow

- **Logging is structlog JSON to stdout.** Bind `job_id` via `structlog.contextvars.bind_contextvars` at the start of `render_sync` and clear in `finally`. Standard event names: `job_started`, `concat_fast_path` / `concat_reencode_path`, `job_done`, `job_failed`, `job_failed_unexpected`.
- **Never log `RENDER_API_KEY`.** `auth.require_api_key` already uses `hmac.compare_digest`.
- **User-facing error messages are in Spanish** (the audiovisual team reads them in Make/Airtable). `_FriendlyError` in `ffmpeg_pipeline.py` is the sentinel that gets surfaced; everything else becomes a generic "Error inesperado".
- **Pydantic v2 only.** Use `model_validate_json` to parse the multipart `params` field.
- **FFmpeg builders return `list[str]`** and are pure (no subprocess). The `run()` and `probe()` functions are the only places that shell out — keeps the builders unit-testable. New ffmpeg invocations should follow this split.
- **All MP4 outputs use `-movflags +faststart`** so web players seek immediately.
- **`output_name` is regex-validated** `^[A-Za-z0-9._-]+$` at the schema layer — don't add a second validation pass downstream.
- **The output MP4 is held in memory** before the workdir is cleaned. Acceptable while clips stay < 200 MB; if that ceiling moves, switch to streaming from disk and delay cleanup until the response is fully sent.
