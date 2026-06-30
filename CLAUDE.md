# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

The actual project lives at `render-service/`. The top-level directory is otherwise empty. All commands below assume `cd render-service` first.

```
render-service/
├── api/        FastAPI service: accepts multipart, enqueues renders (202), serves MP4 on poll
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

# Step 1: submit a 3-clip job — returns 202 {"job_id": "...", "status": "queued"}
JOB=$(curl -s -X POST http://localhost:8000/jobs \
  -H "X-API-Key: $RENDER_API_KEY" \
  -F "clip_hook=@hook.mp4" \
  -F "clip_cuerpo=@cuerpo.mp4" \
  -F "clip_cta=@cta.mp4" \
  -F "music=@music.mp3" \
  -F 'params={"orientation":"vertical","music_volume":0.3,"fade_in":2,"fade_out":2,"output_name":"smoke_test"}' \
  | jq -r .job_id)

# Step 2: poll until status is "done" or "failed"
curl -s http://localhost:8000/jobs/$JOB -H "X-API-Key: $RENDER_API_KEY" | jq .

# Step 3: download the MP4
curl -s http://localhost:8000/jobs/$JOB/result \
  -H "X-API-Key: $RENDER_API_KEY" -o out.mp4 -D -

# Single clip + music (any one of clip_hook/clip_cuerpo/clip_cta): no concat, just mix music onto it
# Same 3-step flow; POST returns 202 with job_id, then poll and download as above.
JOB=$(curl -s -X POST http://localhost:8000/jobs \
  -H "X-API-Key: $RENDER_API_KEY" \
  -F "clip_cuerpo=@una_pieza.mp4" \
  -F "music=@music.mp3" \
  -F 'params={"orientation":"vertical","music_volume":0.3,"fade_in":2,"fade_out":2,"output_name":"una_pieza"}' \
  | jq -r .job_id)
curl -s http://localhost:8000/jobs/$JOB -H "X-API-Key: $RENDER_API_KEY" | jq .
curl -s http://localhost:8000/jobs/$JOB/result \
  -H "X-API-Key: $RENDER_API_KEY" -o out.mp4 -D -
```

There is no lint/format tool configured.

## Architecture

The service receives a Make.com multipart POST with up to 3 clip files (any subset of `clip_hook`/`clip_cuerpo`/`clip_cta`, **minimum 1** in role order) plus an optional music file, assembles a video ad with ffmpeg in-process, and makes the resulting MP4 available for polling via `GET /jobs/{id}/result` (async 202 contract — see Request lifecycle). With 2+ clips they are concatenated; with a single clip there is nothing to concat but it still flows through the concat step (a `-c copy` pass on the fast-path, a normalising re-encode otherwise) so music can be mixed onto it — useful when the team wants to drop music on one finished piece. When `music` is omitted the clips are treated as pre-edited with their own audio: the concat is written straight to the output and the mix step is skipped (existing audio passes through untouched — `fade_in`/`fade_out` then don't apply). Single container — no queue, no callbacks, no external storage.

### Request lifecycle

1. `POST /jobs` (api/src/main.py) — auth via `X-API-Key`, parses the multipart, validates `params` against `JobParams` (Pydantic, `extra="forbid"`, orientation literal, regex on `output_name`). All three clip fields are optional; the endpoint collects whichever subset was uploaded in role order (hook → cuerpo → cta) and rejects immediately with `invalid_params` if none arrived. If `pending_count() >= max_pending_jobs` (default 20) the endpoint rejects with `too_busy` / 429. Otherwise `enqueue_job` persists each uploaded `UploadFile` into a per-job tmp workdir (`/tmp/render_<uuid>_*/`) using the role as the filename (`hook.mp4`, `cuerpo.mp4`, `cta.mp4`), plus `music` if present. The job is registered in the in-memory `job_registry` with status `queued` and the endpoint returns **202** `{"job_id": "<uuid>", "status": "queued"}` immediately; the render runs as a FastAPI `BackgroundTask`.
2. `execute_job` (api/src/render_orchestrator.py) — the background task — acquires the module-level `asyncio.Semaphore(1)`, transitions the job to `processing`, and dispatches the synchronous pipeline via `asyncio.to_thread` so the event loop stays responsive for `/health` and status polling. It forwards `clips: list[Path]` to `run_pipeline` in role order (1 or more) — the pipeline never sees the role names.
3. `run_pipeline` (api/src/ffmpeg_pipeline.py) runs the strict sequence: ffprobe each clip → decide fast-path vs re-encode → concat → ffprobe the concat → mix music (skipped when `music is None`; concat is written directly to the output instead) → write the output MP4 on disk inside the workdir. Returns a `PipelineResult(duration_seconds, concat_strategy)`. On success `execute_job` transitions the job to `done`; on any error it transitions to `failed` and records the error code and message.
4. `GET /jobs/{id}` — returns JSON `JobStatusResponse {job_id, status, code, duration_seconds?, concat_strategy?, error?}` (200 if the job is known, 404 `not_found` otherwise). `status` cycles `queued → processing → done | failed`.
5. `GET /jobs/{id}/result` — streams the MP4 via `FileResponse` when `status == "done"` (200). Returns 409 `not_ready` while the job is still `queued` or `processing`, failed-render error JSON if `status == "failed"`, and 404 `not_found` if the job is unknown or has expired. On first download, the workdir TTL is shrunk to `DOWNLOAD_GRACE_SECONDS` (60 s) so Make can retry the download without re-rendering, after which the reaper deletes the workdir.

**In-memory registry + reaper:** Job metadata lives in `job_registry` (a process-local dict with a thread lock). The MP4 itself lives on disk in the per-job workdir until the reaper purges it. The reaper runs every `reaper_interval_seconds` (default 60 s) and deletes workdirs whose TTL has elapsed (`job_retention_seconds`, default 1800 s from job creation). State is per-process and lost on restart — a poll for a lost job returns 404 and Make resubmits.

**Recommended Make flow:** `POST /jobs` → store `job_id` → Sleep 3–5 s → `GET /jobs/{id}` (loop: `queued`/`processing` → wait and retry; `done` → `GET /jobs/{id}/result` to download MP4; `failed` → branch on `code`).

### Error handling

**Every response carries `X-Status-Code`**: `ok` on a 2xx, the stable `ErrorCode` on any failure. This is the single header Make/Airtable should branch on regardless of success/failure (the `GET /jobs/{id}/result` success response is a binary MP4 and can't carry a JSON `code`, so the header is the only machine-readable signal on that response). Values: `SUCCESS_CODE` (`"ok"`) ∪ `ErrorCode`, typed as `ResultCode` in `shared/models.py`.

Every non-2xx from `POST /jobs` *also* returns the `ErrorResponse` JSON body (`shared/models.py`): `{"error": <safe Spanish message>, "code": <stable ErrorCode>, "job_id": <uuid|null>}` — same `code` as the header. **Branch on `code`, never on the message text** — the Spanish string is free to reword. `job_id` is also echoed in the `X-Job-Id` header on errors (null/absent when the failure precedes job creation, e.g. invalid params) so a failure correlates with the structlog `job_id`.

Known failures inside the pipeline raise `_FriendlyError(message, code=...)` whose Spanish message is safe to surface. Both `_FriendlyError` and the lower-level `FfmpegError` carry a stable `code`; `run_pipeline` re-raises `FfmpegError` as `_FriendlyError` preserving `exc.code`. The background task `execute_job` catches `_FriendlyError` and records the failure on the job via `job_registry.mark_failed(job_id, code=exc.code, error=str(exc))`; any other exception is caught and recorded via `mark_failed(job_id, code="internal_error", error="Error inesperado en el render — revisa los logs del servicio.")` so internal details (paths, tracebacks) never reach the caller — only the logs. `RenderError` is now raised only by `enqueue_job` (the synchronous `too_busy` rejection when pending count exceeds `max_pending_jobs`), which `POST /jobs` surfaces as a 429 response.

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
| `not_found` | 404 | job unknown or expired (GET endpoints) |
| `not_ready` | 409 | render still in progress (`GET /jobs/{id}/result` only) |
| `too_busy` | 429 | `max_pending_jobs` (20) in flight; retry later |

Adding a new failure mode means: add the literal to `ErrorCode` in `shared/models.py`, set it at the raise site, and add a row to `_CODE_TO_STATUS` (anything unmapped defaults to 500).

### Concat fast-path vs re-encode

`ffmpeg_pipeline.can_concat_without_reencode` returns true only when all uploaded clips share codec, width/height, fps, audio codec, sample rate, AND match the requested `orientation`. If true, the pipeline uses ffmpeg's concat demuxer with `-c copy` (seconds). Otherwise it builds a `-filter_complex` graph that per-clip scales+crops/pads to 1080×1920 (vertical) or 1920×1080 (horizontal) at 30 fps, then concats with `concat=n=<len(clips)>`. The strategy (`crop` vs `pad`) is controlled by `ORIENTATION_STRATEGY`. The chosen strategy is reflected back in the response header `X-Concat-Strategy: fast|reencode`. The per-render UUID (the `job_id`) is surfaced as `X-Job-Id` on every response so callers can correlate logs with the result.

### Silent clips

When a clip has no audio stream, the re-encode path injects a `lavfi anullsrc` input of matching duration as that clip's audio slot — so the `concat=` filter always sees N video+audio pairs (one per uploaded clip). The music-mix step has a separate branch for the rare case the entire concat ended up audio-less.

### Shared models contract

`shared/models.py` is the single source of truth for the wire format. The Dockerfile `COPY shared /app/shared`. Changing this file requires rebuilding the image. The `JobParams` model is the JSON payload of the multipart `params` field — there is no support for clip role overrides; clips are supplied as the named multipart fields `clip_hook`, `clip_cuerpo`, `clip_cta`. All three are optional but the request must include at least 1 of them; concat order matches the role order hook → cuerpo → cta regardless of which clips were uploaded.

### Settings

`api/src/settings.py` is a `pydantic-settings` `BaseSettings`: `render_api_key`, `orientation_strategy`, `log_level`. `@lru_cache` singleton via `get_settings()` — never instantiate `Settings()` directly. There is no Redis, no Drive, no per-job-timeout env var — the ffmpeg hard timeout is hardcoded to 600 s in `ffmpeg_pipeline.FFMPEG_TIMEOUT_SECONDS`.

Three additional env vars control the async job lifecycle:
- `JOB_RETENTION_SECONDS` (default 1800) — how long a job's workdir (including the MP4) is kept before the reaper deletes it.
- `REAPER_INTERVAL_SECONDS` (default 60) — how often the reaper sweep runs.
- `MAX_PENDING_JOBS` (default 20) — maximum number of `queued`+`processing` jobs; `POST /jobs` returns 429 `too_busy` when the limit is reached.

### Concurrency

`uvicorn --workers 1` in the Dockerfile is mandatory: renders are serialised via a process-local `asyncio.Semaphore(1)`. Multiple uvicorn workers would each carry their own semaphore → races for the CPU and OOM risk. The host VPS has 2 vCPU / 7.8 GiB and `mem_limit: 6g` in compose protects the host.

## Conventions to follow

- **Logging is structlog JSON to stdout.** Bind `job_id` via `structlog.contextvars.bind_contextvars` at the start of `execute_job` and clear in `finally`. Standard event names: `job_started`, `concat_fast_path` / `concat_reencode_path`, `job_done`, `job_failed`, `job_failed_unexpected`.
- **Never log `RENDER_API_KEY`.** `auth.require_api_key` already uses `hmac.compare_digest`.
- **User-facing error messages are in Spanish** (the audiovisual team reads them in Make/Airtable). `_FriendlyError` in `ffmpeg_pipeline.py` is the sentinel that gets surfaced; everything else becomes a generic "Error inesperado".
- **Pydantic v2 only.** Use `model_validate_json` to parse the multipart `params` field.
- **FFmpeg builders return `list[str]`** and are pure (no subprocess). The `run()` and `probe()` functions are the only places that shell out — keeps the builders unit-testable. New ffmpeg invocations should follow this split.
- **All MP4 outputs use `-movflags +faststart`** so web players seek immediately.
- **`output_name` is regex-validated** `^[A-Za-z0-9._-]+$` at the schema layer — don't add a second validation pass downstream.
- **The output MP4 is kept on disk** in the per-job workdir and served via `FileResponse` by `GET /jobs/{id}/result`. The reaper deletes the workdir (and the MP4) after `JOB_RETENTION_SECONDS`. After the first successful download the TTL is shrunk to `DOWNLOAD_GRACE_SECONDS` (60 s).
