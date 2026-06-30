# Async render jobs (202 + polling) — Design

**Date:** 2026-06-30
**Status:** Approved, pending implementation plan

## Problem

`POST /jobs` renders synchronously: it holds the HTTP connection open for the
entire render and only then streams the MP4 back. Make.com's HTTP module caps
its wait at 300 s (already configured at the maximum). When a render takes
longer than 300 s — because it hit the slow `libx264` re-encode path on the
2 vCPU box, or because it was queued behind other renders on the
`Semaphore(1)` — Make raises `ModuleTimeoutError` and abandons the connection
while the server keeps working and eventually logs `job_done` / `200 OK`. The
server's own `FFMPEG_TIMEOUT_SECONDS = 600` is double Make's 300 s ceiling, so
the service can never fail fast enough to protect the caller.

The synchronous request→render→response coupling is the root cause. This design
removes it.

## Goal

Decouple submission from completion. `POST /jobs` returns immediately with a
`job_id`; the render runs in the background; Make polls a status endpoint and
downloads the result once ready. No individual HTTP call stays open longer than
a few milliseconds, so Make's 300 s cap stops mattering.

## Non-goals

- No Redis, no external object storage, no SQLite — state stays process-local,
  consistent with the existing "single container, no queue, no callbacks"
  posture (CLAUDE.md).
- No surviving a container restart: in-flight jobs are lost on restart. That is
  acceptable — Make gets a `404 not_found` on its next poll and can resubmit.
- No change to the ffmpeg pipeline itself (`ffmpeg_pipeline.py` already writes
  the MP4 to disk, which is exactly what the result endpoint serves).

## HTTP contract

All endpoints require `X-API-Key` (unchanged auth). `X-Status-Code` is present
on **every** response — Make branches on that one header regardless of
success/failure, exactly as today.

| Method | Path | Success | Notes |
|---|---|---|---|
| `POST` | `/jobs` | **202** `{job_id, status:"queued"}` | Returns as soon as the uploads are persisted. Headers: `X-Job-Id`, `X-Status-Code: ok`. Returns **429** `too_busy` if the pending queue is full. Returns **422** `invalid_params` as today for bad `params`/no clips. |
| `GET` | `/jobs/{id}` | **200** JSON status | Always JSON: `{job_id, status, code, duration_seconds?, concat_strategy?, error?}`. `status ∈ {queued, processing, done, failed}`. `X-Status-Code` = `ok` while healthy, the render's `ErrorCode` when `failed`. **404** `not_found` if the id is unknown/expired. |
| `GET` | `/jobs/{id}/result` | **200** MP4 binary | Only when `status == done`; served via `FileResponse` (streamed from disk, never loaded into RAM). **409** `not_ready` (JSON) while `queued`/`processing`. **404** `not_found` if unknown/expired. If the job `failed`, returns the `ErrorResponse` JSON with the render's `code` and its mapped HTTP status. |

### Status response model

`shared/models.py` gains:

```python
JobStatus = Literal["queued", "processing", "done", "failed"]

class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    code: ResultCode               # "ok" while healthy, ErrorCode when failed
    duration_seconds: float | None = None
    concat_strategy: str | None = None
    error: str | None = None       # Spanish message, only when failed
```

### New error codes

Added to `ErrorCode` in `shared/models.py` and to `_CODE_TO_STATUS`:

| code | HTTP | meaning |
|---|---|---|
| `not_found` | 404 | job id unknown or already expired/purged |
| `not_ready` | 409 | `/result` requested while still `queued`/`processing` |
| `too_busy` | 429 | pending queue is at `max_pending_jobs`; resubmit later |

The existing render `ErrorCode`s are unchanged; a failed render stores its code
in the job record and surfaces it via both `/jobs/{id}` and `/jobs/{id}/result`.

### Recommended Make flow

POST → store `job_id` → Sleep (3–5 s) → GET `/jobs/{id}` in a loop (router:
`queued`/`processing` → wait again; `done` → GET `/jobs/{id}/result`; `failed`
→ error branch keyed on `code`). Each call returns in milliseconds.

## Components

Each unit has one purpose, a small interface, and is testable in isolation.

### `job_registry.py` [new]

Process-local job store. A `dict[str, JobRecord]` guarded by a lock.

`JobRecord` (dataclass): `job_id`, `status`, `code`, `output_name`,
`result_path: Path | None`, `duration_seconds`, `concat_strategy`,
`error: str | None`, `workdir: Path`, `created_at`, `finished_at`.

(`created_at`/`finished_at` are set from an injected monotonic clock so the
module has no hidden dependency on wall-clock and stays unit-testable.)

Interface:
- `create(job_id, *, output_name, workdir) -> JobRecord` — registers a `queued` job.
- `get(job_id) -> JobRecord | None`
- `mark_processing(job_id)`
- `mark_done(job_id, *, result_path, duration_seconds, concat_strategy)`
- `mark_failed(job_id, *, code, error)`
- `pending_count() -> int`
- `sweep_expired(now, ttl_seconds) -> list[Path]` — drops records older than the
  TTL, returns their workdirs for the caller to delete.
- `pop_workdir(job_id) -> Path | None` — for delete-after-download.

Knows nothing about FastAPI or ffmpeg.

### `render_orchestrator.py` [refactor]

`render_sync` is replaced by:
- `enqueue_job(*, clips, music, params) -> str` — persists the uploaded clips +
  optional music into the per-job workdir (fast disk writes, still inside the
  request so the multipart body is available), registers the job as `queued`,
  schedules the background task, and returns the `job_id`. Raises a `RenderError`
  with `too_busy` if `pending_count() >= max_pending_jobs`.
- `_run_job(job_id, *, clip_paths, music_path, output_path, params)` — the
  background coroutine. Acquires the existing module-level `Semaphore(1)`, marks
  the job `processing`, runs `run_pipeline` via `asyncio.to_thread`, then
  `mark_done` (the MP4 stays on disk at `output_path`) or, on `_FriendlyError` /
  unexpected exception, `mark_failed` with the stable code. Binds/clears the
  `job_id` structlog contextvar around the work. The MP4 is **never** read into
  memory.

The `Semaphore(1)`, the 600 s ffmpeg timeout, and the structlog event names
(`job_started`, `concat_*`, `job_done`, `job_failed*`) are preserved.

### `main.py` [refactor]

Three endpoints (`POST /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/result`) plus
`/health` (unchanged). A FastAPI `lifespan` starts and cancels the reaper task.

### Reaper

Background task started in the `lifespan`. Every `reaper_interval_seconds` it
calls `sweep_expired(now, job_retention_seconds)` and `rmtree`s each returned
workdir. Delete-after-download: a successful `GET /result` schedules its
workdir for removal after a short grace (60 s) so a Make retry can still fetch
it.

### `ffmpeg_pipeline.py`

Unchanged.

## Concurrency and limits

- `Semaphore(1)` keeps exactly one render running on the 2 vCPU box. The "queue"
  is now background tasks awaiting the semaphore — **no HTTP connection is held
  open** while they wait.
- Bounded queue: `POST /jobs` returns **429 `too_busy`** when
  `pending_count() >= max_pending_jobs` (default 20), bounding disk/RAM growth
  if Make fires hundreds.
- `FFMPEG_TIMEOUT_SECONDS = 600` stays — it no longer races Make's 300 s window.

## New settings (`settings.py`)

`pydantic-settings` fields on the existing `Settings`:
- `job_retention_seconds: int = 1800` — reaper TTL.
- `reaper_interval_seconds: int = 60`.
- `max_pending_jobs: int = 20`.

## Cleanup of prior artifact

Remove the orphaned `api/src/__pycache__/queue_client.cpython-314.pyc` (leftover
from an abandoned attempt; no corresponding `.py`).

## Testing

- `job_registry` (unit, no I/O): create/get, status transitions, `pending_count`,
  `sweep_expired` returns and drops expired records, `pop_workdir`.
- Endpoints (with `run_pipeline` mocked as today, subprocess never invoked):
  - `POST /jobs` → 202 + `job_id`, `X-Status-Code: ok`.
  - status lifecycle `queued` → `processing` → `done` observable via `GET /jobs/{id}`.
  - `GET /result` → 409 `not_ready` while running, 200 MP4 when done, 404 for
    unknown id.
  - failed render → `GET /jobs/{id}` shows `status:failed` + correct `code`;
    `/result` returns the error JSON with mapped status.
  - 429 `too_busy` once the pending queue is full.
- Existing endpoint tests that assume a synchronous MP4 response from `POST`
  are updated to the async contract.

## Behavioral changes / breaking

- `POST /jobs` no longer returns the MP4. It returns 202 + `job_id`. The old
  synchronous behavior is removed entirely (no dual mode). Make scenarios and
  CLAUDE.md docs are updated accordingly.
