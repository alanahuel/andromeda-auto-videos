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

The service receives a Make.com multipart POST with up to 3 clip files (any subset of `clip_hook`/`clip_cuerpo`/`clip_cta`, **minimum 2** in role order) plus an optional music file, assembles a video ad with ffmpeg in-process, and streams the MP4 back in the HTTP response. When `music` is omitted the clips are treated as pre-edited with their own audio: the concat is written straight to the output and the mix step is skipped (existing audio passes through untouched — `fade_in`/`fade_out` then don't apply). Single container — no queue, no callbacks, no external storage.

### Request lifecycle

1. `POST /jobs` (api/src/main.py) — auth via `X-API-Key`, parses the multipart, validates `params` against `JobParams` (Pydantic, `extra="forbid"`, orientation literal, regex on `output_name`). All three clip fields are optional; the endpoint collects whichever subset was uploaded in role order (hook → cuerpo → cta) and rejects the request with `invalid_params` if fewer than 2 arrived.
2. `render_sync` (api/src/render_orchestrator.py) acquires the module-level `asyncio.Semaphore(1)`, persists each uploaded clip `UploadFile` into a per-job tmp workdir (`/tmp/render_<uuid>_*/`) using the role as the filename (`hook.mp4`, `cuerpo.mp4`, `cta.mp4`), plus `music` if present, passing `music=None` to the pipeline when no music file was uploaded, and dispatches the sync pipeline via `asyncio.to_thread` so the event loop stays responsive for `/health`. It forwards `clips: list[Path]` to `run_pipeline` in role order — the pipeline never sees the role names.
3. `run_pipeline` (api/src/ffmpeg_pipeline.py) runs the strict sequence: ffprobe each clip → decide fast-path vs re-encode → concat → ffprobe the concat → mix music (skipped when `music is None`; concat is written directly to the output instead) → write the output MP4 inside the workdir. Returns a `PipelineResult(duration_seconds, concat_strategy)`.
4. The orchestrator reads the output bytes into memory (clips < 200 MB by ops note), returns them via `StreamingResponse`, and cleans the workdir in `finally`.

### Error handling

**Every response carries `X-Status-Code`**: `ok` on a 2xx, the stable `ErrorCode` on any failure. This is the single header Make/Airtable should branch on regardless of success/failure (a successful response is the binary MP4 and can't carry a JSON `code`). Values: `SUCCESS_CODE` (`"ok"`) ∪ `ErrorCode`, typed as `ResultCode` in `shared/models.py`.

Every non-2xx from `POST /jobs` *also* returns the `ErrorResponse` JSON body (`shared/models.py`): `{"error": <safe Spanish message>, "code": <stable ErrorCode>, "job_id": <uuid|null>}` — same `code` as the header. **Branch on `code`, never on the message text** — the Spanish string is free to reword. `job_id` is also echoed in the `X-Job-Id` header on errors (null/absent when the failure precedes job creation, e.g. invalid params) so a failure correlates with the structlog `job_id`.

Known failures inside the pipeline raise `_FriendlyError(message, code=...)` whose Spanish message is safe to surface. Both `_FriendlyError` and the lower-level `FfmpegError` carry a stable `code`; `run_pipeline` re-raises `FfmpegError` as `_FriendlyError` preserving `exc.code`. The orchestrator translates `_FriendlyError → RenderError(message, code, job_id)`; any other exception becomes `RenderError("Error inesperado…", code="internal_error")` so internal details (paths, tracebacks) never reach the caller — only the logs.

`code → HTTP status` lives in `render_orchestrator._CODE_TO_STATUS` (the single mapping point):

| code | HTTP | meaning |
|---|---|---|
| `invalid_params` | 422 | `params` JSON failed `JobParams` validation |
| `clip_unreadable` | 422 | ffprobe could not read a clip (corrupt/format) |
| `clip_no_video` | 422 | a clip has no video stream |
| `empty_clip` | 422 | concatenated video has duration 0 |
| `probe_timeout` | 504 | ffprobe hung reading a clip |
| `ffmpeg_timeout` | 504 | ffmpeg hung during concat/mix |
| `render_failed` | 500 | ffmpeg exited non-zero (processing failure) |
| `internal_error` | 500 | unexpected; details only in logs |

Adding a new failure mode means: add the literal to `ErrorCode` in `shared/models.py`, set it at the raise site, and add a row to `_CODE_TO_STATUS` (anything unmapped defaults to 500).

### Concat fast-path vs re-encode

`ffmpeg_pipeline.can_concat_without_reencode` returns true only when all uploaded clips share codec, width/height, fps, audio codec, sample rate, AND match the requested `orientation`. If true, the pipeline uses ffmpeg's concat demuxer with `-c copy` (seconds). Otherwise it builds a `-filter_complex` graph that per-clip scales+crops/pads to 1080×1920 (vertical) or 1920×1080 (horizontal) at 30 fps, then concats with `concat=n=<len(clips)>`. The strategy (`crop` vs `pad`) is controlled by `ORIENTATION_STRATEGY`. The chosen strategy is reflected back in the response header `X-Concat-Strategy: fast|reencode`. The per-render UUID generated in `render_sync` is also surfaced as `X-Job-Id` so callers can correlate logs with the response.

### Silent clips

When a clip has no audio stream, the re-encode path injects a `lavfi anullsrc` input of matching duration as that clip's audio slot — so the `concat=` filter always sees N video+audio pairs (one per uploaded clip). The music-mix step has a separate branch for the rare case the entire concat ended up audio-less.

### Shared models contract

`shared/models.py` is the single source of truth for the wire format. The Dockerfile `COPY shared /app/shared`. Changing this file requires rebuilding the image. The `JobParams` model is the JSON payload of the multipart `params` field — there is no support for clip role overrides; clips are supplied as the named multipart fields `clip_hook`, `clip_cuerpo`, `clip_cta`. All three are optional but the request must include at least 2 of them; concat order matches the role order hook → cuerpo → cta regardless of which clips were uploaded.

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
