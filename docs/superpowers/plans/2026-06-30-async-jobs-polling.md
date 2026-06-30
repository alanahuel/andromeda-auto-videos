# Async Render Jobs (202 + Polling) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple render submission from completion so Make's 300 s HTTP cap stops racing the render: `POST /jobs` returns `202 + job_id`, the render runs in a background task, and Make polls `GET /jobs/{id}` and downloads `GET /jobs/{id}/result`.

**Architecture:** A process-local job registry (`dict` guarded by a `threading.Lock`) tracks each job's status; the MP4 lives on disk in the per-job workdir. `POST /jobs` persists uploads, registers a `queued` job, and schedules a FastAPI `BackgroundTask` that runs the existing `run_pipeline` behind the existing `Semaphore(1)`. A reaper task (started in the app `lifespan`) purges expired workdirs; a successful download shortens a job's TTL to a short grace window.

**Tech Stack:** FastAPI, Starlette `BackgroundTasks` + `FileResponse`, Pydantic v2, structlog, pytest (sync `TestClient` + `asyncio.run` for the async orchestrator units). No new third-party dependencies.

## Global Constraints

- **Python 3.12**, Pydantic v2 only (`model_validate_json`, `model_dump`).
- **Single uvicorn worker** stays mandatory — renders serialize on a process-local `asyncio.Semaphore(1)`. Do not add workers.
- **No new external dependencies** (no Redis/DB/queue lib). State is process-local; jobs are lost on container restart by design.
- **User-facing error messages in Spanish.** Branch on `code`, never message text.
- **`X-Status-Code` on every response** — `"ok"` on success, the stable `ErrorCode` on failure.
- **All MP4 outputs already carry `-movflags +faststart`** (pipeline unchanged).
- **`output_name`** is already regex-validated at the schema layer — no second pass.
- ffmpeg/ffprobe are NOT available in tests — `run_pipeline` is always mocked; subprocess is never invoked.
- Run tests from `api/`: `uv run pytest -q`.

---

## File Structure

- **Create** `api/src/job_registry.py` — the in-memory job store + `JobRecord`. No FastAPI/ffmpeg imports.
- **Create** `api/tests/test_job_registry.py` — unit tests for the registry.
- **Create** `api/tests/test_orchestrator.py` — async unit tests for `enqueue_job` / `execute_job` / `reap_once`.
- **Modify** `shared/models.py` — add `JobStatus`, `JobStatusResponse`, and 3 new `ErrorCode`s.
- **Modify** `api/src/settings.py` — add `job_retention_seconds`, `reaper_interval_seconds`, `max_pending_jobs`.
- **Modify** `api/src/render_orchestrator.py` — replace `render_sync` with `enqueue_job` + `execute_job`; add reaper helpers; extend `_CODE_TO_STATUS`.
- **Modify** `api/src/main.py` — three endpoints (`POST /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/result`) + `lifespan` reaper wiring; bump version to `0.3.0`.
- **Rewrite** `api/tests/test_endpoint.py` — exercise the async contract.
- **Modify** `render-service/CLAUDE.md` — document the async request lifecycle.
- **Delete** `api/src/__pycache__/queue_client.cpython-314.pyc` — orphaned artifact (not tracked; local only).

---

## Task 1: Wire-contract models for job status

**Files:**
- Modify: `shared/models.py`
- Test: `api/tests/test_models.py` (create)

**Interfaces:**
- Produces:
  - `JobStatus = Literal["queued", "processing", "done", "failed"]`
  - `ErrorCode` gains literals `"not_found"`, `"not_ready"`, `"too_busy"`.
  - `class JobStatusResponse(BaseModel)` with fields `job_id: str`, `status: JobStatus`, `code: ResultCode`, `duration_seconds: float | None = None`, `concat_strategy: str | None = None`, `error: str | None = None`.

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_models.py`:

```python
from __future__ import annotations

from shared.models import ErrorResponse, JobStatusResponse


def test_job_status_response_defaults_optional_fields_to_none():
    r = JobStatusResponse(job_id="j1", status="queued", code="ok")
    assert r.status == "queued"
    assert r.code == "ok"
    assert r.duration_seconds is None
    assert r.concat_strategy is None
    assert r.error is None


def test_job_status_response_carries_done_metadata():
    r = JobStatusResponse(
        job_id="j2", status="done", code="ok",
        duration_seconds=42.5, concat_strategy="fast",
    )
    assert r.duration_seconds == 42.5
    assert r.concat_strategy == "fast"


def test_error_response_accepts_new_codes():
    for code in ("not_found", "not_ready", "too_busy"):
        assert ErrorResponse(error="x", code=code, job_id=None).code == code
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'JobStatusResponse'`.

- [ ] **Step 3: Implement the models**

In `shared/models.py`, extend the `ErrorCode` literal (add the 3 codes with comments) and add the status types. Apply this edit to the `ErrorCode` block:

```python
ErrorCode = Literal[
    "invalid_params",   # 422 — params JSON failed JobParams validation
    "clip_unreadable",  # 422 — ffprobe could not read a clip (corrupt/format)
    "clip_no_video",    # 422 — a clip has no video stream
    "empty_clip",       # 422 — concatenated video has duration 0
    "probe_timeout",    # 504 — ffprobe hung reading a clip
    "ffmpeg_timeout",   # 504 — ffmpeg hung during concat/mix
    "render_failed",    # 500 — ffmpeg exited non-zero (processing failure)
    "internal_error",   # 500 — unexpected, details only in logs
    "not_found",        # 404 — job id unknown or expired/purged
    "not_ready",        # 409 — /result requested while still queued/processing
    "too_busy",         # 429 — pending queue full; resubmit later
]
```

Then, after the `ErrorResponse` class (end of file), add:

```python
# Lifecycle of an async render job, surfaced by GET /jobs/{id}.
JobStatus = Literal["queued", "processing", "done", "failed"]


class JobStatusResponse(BaseModel):
    """Body of GET /jobs/{id}. `code` is "ok" while healthy, the render's
    stable ErrorCode once `status == "failed"`. The metadata fields are
    populated only once the render is done."""

    job_id: str
    status: JobStatus
    code: ResultCode
    duration_seconds: float | None = None
    concat_strategy: str | None = None
    error: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_models.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add shared/models.py api/tests/test_models.py
git commit -m "feat(models): job status response + not_found/not_ready/too_busy codes"
```

---

## Task 2: In-memory job registry

**Files:**
- Create: `api/src/job_registry.py`
- Test: `api/tests/test_job_registry.py`

**Interfaces:**
- Consumes: `JobParams`, `JobStatus`, `ResultCode`, `ErrorCode` from `shared.models`.
- Produces (all module-level functions on `src.job_registry`):
  - `@dataclass JobRecord` with: `job_id: str`, `output_name: str`, `workdir: Path`, `output_path: Path`, `clip_paths: list[Path]`, `music_path: Path | None`, `params: JobParams`, `status: JobStatus = "queued"`, `code: ResultCode = "ok"`, `error: str | None = None`, `duration_seconds: float | None = None`, `concat_strategy: str | None = None`, `expires_at: float = 0.0`.
  - `create(job_id, *, output_name, workdir, output_path, clip_paths, music_path, params, retention_seconds, now=None) -> JobRecord`
  - `get(job_id) -> JobRecord | None`
  - `mark_processing(job_id) -> None`
  - `mark_done(job_id, *, duration_seconds, concat_strategy) -> None`
  - `mark_failed(job_id, *, code, error) -> None`
  - `pending_count() -> int` (counts `queued` + `processing`)
  - `mark_downloaded(job_id, *, grace_seconds, now=None) -> None` (shortens `expires_at`)
  - `sweep_expired(now=None) -> list[Path]` (drops expired records, returns their workdirs)
  - `reset() -> None` (test helper; clears the store)

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_job_registry.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from shared.models import JobParams
from src import job_registry


def _params() -> JobParams:
    return JobParams(orientation="vertical", output_name="ad_test")


def _create(job_id="j1", *, retention=1800.0, now=0.0) -> None:
    wd = Path(f"/tmp/render_{job_id}")
    job_registry.create(
        job_id,
        output_name="ad_test",
        workdir=wd,
        output_path=wd / "ad_test.mp4",
        clip_paths=[wd / "hook.mp4"],
        music_path=None,
        params=_params(),
        retention_seconds=retention,
        now=now,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    job_registry.reset()
    yield
    job_registry.reset()


def test_create_registers_a_queued_job():
    _create()
    rec = job_registry.get("j1")
    assert rec is not None
    assert rec.status == "queued"
    assert rec.code == "ok"
    assert rec.expires_at == 1800.0  # now(0) + retention(1800)


def test_get_unknown_returns_none():
    assert job_registry.get("nope") is None


def test_status_transitions():
    _create()
    job_registry.mark_processing("j1")
    assert job_registry.get("j1").status == "processing"
    job_registry.mark_done("j1", duration_seconds=42.5, concat_strategy="fast")
    rec = job_registry.get("j1")
    assert rec.status == "done"
    assert rec.code == "ok"
    assert rec.duration_seconds == 42.5
    assert rec.concat_strategy == "fast"


def test_mark_failed_records_code_and_message():
    _create()
    job_registry.mark_failed("j1", code="render_failed", error="petó")
    rec = job_registry.get("j1")
    assert rec.status == "failed"
    assert rec.code == "render_failed"
    assert rec.error == "petó"


def test_pending_count_counts_queued_and_processing_only():
    _create("a")
    _create("b")
    _create("c")
    job_registry.mark_processing("b")
    job_registry.mark_done("c", duration_seconds=1.0, concat_strategy="fast")
    assert job_registry.pending_count() == 2  # a (queued) + b (processing)


def test_sweep_expired_drops_and_returns_expired_workdirs():
    _create("old", retention=100.0, now=0.0)   # expires_at = 100
    _create("new", retention=100.0, now=0.0)
    job_registry.mark_processing("new")
    job_registry.mark_done("new", duration_seconds=1.0, concat_strategy="fast")
    # bump 'new' far into the future so only 'old' expires
    job_registry.get("new").expires_at = 10_000.0

    dropped = job_registry.sweep_expired(now=150.0)

    assert [p.name for p in dropped] == ["render_old"]
    assert job_registry.get("old") is None
    assert job_registry.get("new") is not None


def test_mark_downloaded_shortens_ttl_to_grace_window():
    _create(retention=1800.0, now=0.0)         # expires_at = 1800
    job_registry.mark_downloaded("j1", grace_seconds=60.0, now=100.0)
    assert job_registry.get("j1").expires_at == 160.0  # now(100) + grace(60)


def test_mark_downloaded_never_extends_ttl():
    _create(retention=50.0, now=0.0)           # expires_at = 50
    job_registry.mark_downloaded("j1", grace_seconds=600.0, now=0.0)
    assert job_registry.get("j1").expires_at == 50.0   # min(50, 600) keeps 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_job_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.job_registry'`.

- [ ] **Step 3: Implement the registry**

Create `api/src/job_registry.py`:

```python
"""Process-local registry of async render jobs.

A plain dict guarded by a threading.Lock (the operations are O(1) and never
block, so a thread lock is fine and avoids async-coloring the callers). Each
record carries everything `execute_job` needs to run the render plus the
status surfaced by GET /jobs/{id}. The MP4 itself lives on disk at
`output_path`; this module only tracks metadata and expiry.

State is per-process and lost on restart — acceptable: a poll for a lost job
returns 404 and Make resubmits.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from shared.models import ErrorCode, JobParams, JobStatus, ResultCode


@dataclass
class JobRecord:
    job_id: str
    output_name: str
    workdir: Path
    output_path: Path
    clip_paths: list[Path]
    music_path: Path | None
    params: JobParams
    status: JobStatus = "queued"
    code: ResultCode = "ok"
    error: str | None = None
    duration_seconds: float | None = None
    concat_strategy: str | None = None
    expires_at: float = 0.0


_lock = threading.Lock()
_jobs: dict[str, JobRecord] = {}


def create(
    job_id: str,
    *,
    output_name: str,
    workdir: Path,
    output_path: Path,
    clip_paths: list[Path],
    music_path: Path | None,
    params: JobParams,
    retention_seconds: float,
    now: float | None = None,
) -> JobRecord:
    ts = time.monotonic() if now is None else now
    rec = JobRecord(
        job_id=job_id,
        output_name=output_name,
        workdir=workdir,
        output_path=output_path,
        clip_paths=clip_paths,
        music_path=music_path,
        params=params,
        expires_at=ts + retention_seconds,
    )
    with _lock:
        _jobs[job_id] = rec
    return rec


def get(job_id: str) -> JobRecord | None:
    with _lock:
        return _jobs.get(job_id)


def mark_processing(job_id: str) -> None:
    with _lock:
        rec = _jobs.get(job_id)
        if rec is not None:
            rec.status = "processing"


def mark_done(job_id: str, *, duration_seconds: float, concat_strategy: str) -> None:
    with _lock:
        rec = _jobs.get(job_id)
        if rec is not None:
            rec.status = "done"
            rec.code = "ok"
            rec.duration_seconds = duration_seconds
            rec.concat_strategy = concat_strategy


def mark_failed(job_id: str, *, code: ErrorCode, error: str) -> None:
    with _lock:
        rec = _jobs.get(job_id)
        if rec is not None:
            rec.status = "failed"
            rec.code = code
            rec.error = error


def pending_count() -> int:
    with _lock:
        return sum(1 for r in _jobs.values() if r.status in ("queued", "processing"))


def mark_downloaded(job_id: str, *, grace_seconds: float, now: float | None = None) -> None:
    ts = time.monotonic() if now is None else now
    with _lock:
        rec = _jobs.get(job_id)
        if rec is not None:
            rec.expires_at = min(rec.expires_at, ts + grace_seconds)


def sweep_expired(now: float | None = None) -> list[Path]:
    ts = time.monotonic() if now is None else now
    with _lock:
        expired = [jid for jid, r in _jobs.items() if ts >= r.expires_at]
        workdirs = [_jobs[jid].workdir for jid in expired]
        for jid in expired:
            del _jobs[jid]
    return workdirs


def reset() -> None:
    """Test helper — clears all jobs. Not used by application code."""
    with _lock:
        _jobs.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_job_registry.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add api/src/job_registry.py api/tests/test_job_registry.py
git commit -m "feat(registry): in-memory job store with TTL expiry"
```

---

## Task 3: Settings for retention, reaper interval, queue bound

**Files:**
- Modify: `api/src/settings.py`
- Test: `api/tests/test_settings.py` (create)

**Interfaces:**
- Produces: `Settings` gains `job_retention_seconds: int = 1800`, `reaper_interval_seconds: int = 60`, `max_pending_jobs: int = 20`.

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_settings.py`:

```python
from __future__ import annotations

from src.settings import Settings


def test_async_job_settings_have_sane_defaults():
    s = Settings(render_api_key="test-key-test-key")
    assert s.job_retention_seconds == 1800
    assert s.reaper_interval_seconds == 60
    assert s.max_pending_jobs == 20


def test_async_job_settings_are_overridable(monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_SECONDS", "300")
    monkeypatch.setenv("MAX_PENDING_JOBS", "5")
    s = Settings(render_api_key="test-key-test-key")
    assert s.job_retention_seconds == 300
    assert s.max_pending_jobs == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_settings.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'job_retention_seconds'`.

- [ ] **Step 3: Implement**

In `api/src/settings.py`, add the three fields to `Settings` (after `log_level`):

```python
    log_level: str = "INFO"

    # Async job lifecycle.
    job_retention_seconds: int = 1800   # reaper purges jobs + workdirs older than this
    reaper_interval_seconds: int = 60   # how often the reaper sweeps
    max_pending_jobs: int = 20          # POST /jobs returns 429 too_busy beyond this
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_settings.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add api/src/settings.py api/tests/test_settings.py
git commit -m "feat(settings): job retention, reaper interval, pending bound"
```

---

## Task 4: Orchestrator — enqueue, execute, reap

**Files:**
- Modify: `api/src/render_orchestrator.py`
- Test: `api/tests/test_orchestrator.py` (create)

**Interfaces:**
- Consumes: `src.job_registry` (Task 2), `JobParams`, `run_pipeline`, `_FriendlyError`, `PipelineResult`.
- Produces:
  - `DOWNLOAD_GRACE_SECONDS = 60`
  - `class RenderError(Exception)` — unchanged signature: `(message, *, code: ErrorCode, job_id: str | None)`; exposes `.message`, `.code`, `.job_id`, `.http_status`.
  - `status_for_code(code: str) -> int` — unchanged.
  - `async def enqueue_job(*, clips: list[tuple[str, UploadFile]], music: UploadFile | None, params: JobParams, retention_seconds: float, max_pending: int) -> str` — raises `RenderError(code="too_busy", job_id=None)` when the queue is full; otherwise persists uploads, registers a `queued` job, returns `job_id`.
  - `async def execute_job(job_id: str) -> None` — runs the pipeline behind `Semaphore(1)`, updates the registry to `done`/`failed`. Never raises.
  - `def reap_once(*, now: float | None = None) -> int` — sweeps the registry and `rmtree`s each expired workdir; returns count.
  - `async def run_reaper(*, interval_seconds: float, stop: asyncio.Event) -> None` — periodic loop calling `reap_once`.

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_orchestrator.py`:

```python
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import UploadFile

from shared.models import JobParams
from src import job_registry, render_orchestrator
from src.ffmpeg_pipeline import PipelineResult, _FriendlyError


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _params(output_name="ad_test") -> JobParams:
    return JobParams(orientation="vertical", output_name=output_name)


@pytest.fixture(autouse=True)
def _clean_registry():
    job_registry.reset()
    yield
    job_registry.reset()


def test_enqueue_persists_uploads_and_registers_queued(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / prefix))
    Path(str(tmp_path / "render_")).mkdir(parents=True, exist_ok=True)

    job_id = asyncio.run(
        render_orchestrator.enqueue_job(
            clips=[("hook", _upload("hook.mp4", b"hook-bytes"))],
            music=_upload("music.mp3", b"music-bytes"),
            params=_params(),
            retention_seconds=1800,
            max_pending=20,
        )
    )

    rec = job_registry.get(job_id)
    assert rec is not None
    assert rec.status == "queued"
    assert rec.clip_paths[0].read_bytes() == b"hook-bytes"
    assert rec.music_path.read_bytes() == b"music-bytes"
    assert rec.output_path.name == "ad_test.mp4"


def test_enqueue_raises_too_busy_when_queue_full(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / prefix))
    Path(str(tmp_path / "render_")).mkdir(parents=True, exist_ok=True)
    # Fill the registry with 2 queued jobs, bound = 2.
    for jid in ("a", "b"):
        wd = tmp_path / jid
        job_registry.create(
            jid, output_name="x", workdir=wd, output_path=wd / "x.mp4",
            clip_paths=[], music_path=None, params=_params(), retention_seconds=1800,
        )

    with pytest.raises(render_orchestrator.RenderError) as ei:
        asyncio.run(
            render_orchestrator.enqueue_job(
                clips=[("hook", _upload("hook.mp4", b"x"))],
                music=None, params=_params(), retention_seconds=1800, max_pending=2,
            )
        )
    assert ei.value.code == "too_busy"
    assert ei.value.http_status == 429
    assert ei.value.job_id is None


def _fake_pipeline(*, output: Path, **_kw) -> PipelineResult:
    output.write_bytes(b"MP4")
    return PipelineResult(duration_seconds=42.5, concat_strategy="fast")


def test_execute_marks_done_and_writes_output(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    job_registry.create(
        "j1", output_name="ad", workdir=wd, output_path=wd / "ad.mp4",
        clip_paths=[wd / "hook.mp4"], music_path=None, params=_params(),
        retention_seconds=1800,
    )
    with patch("src.render_orchestrator.run_pipeline", side_effect=_fake_pipeline):
        asyncio.run(render_orchestrator.execute_job("j1"))

    rec = job_registry.get("j1")
    assert rec.status == "done"
    assert rec.duration_seconds == 42.5
    assert rec.concat_strategy == "fast"
    assert rec.output_path.read_bytes() == b"MP4"


def test_execute_marks_failed_with_friendly_code(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    job_registry.create(
        "j1", output_name="ad", workdir=wd, output_path=wd / "ad.mp4",
        clip_paths=[], music_path=None, params=_params(), retention_seconds=1800,
    )

    def _boom(**_kw):
        raise _FriendlyError("clip corrupto", code="clip_unreadable")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        asyncio.run(render_orchestrator.execute_job("j1"))

    rec = job_registry.get("j1")
    assert rec.status == "failed"
    assert rec.code == "clip_unreadable"
    assert "clip corrupto" in rec.error


def test_execute_hides_unexpected_error_detail(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    job_registry.create(
        "j1", output_name="ad", workdir=wd, output_path=wd / "ad.mp4",
        clip_paths=[], music_path=None, params=_params(), retention_seconds=1800,
    )

    def _boom(**_kw):
        raise RuntimeError("disk full, internal detail")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        asyncio.run(render_orchestrator.execute_job("j1"))

    rec = job_registry.get("j1")
    assert rec.status == "failed"
    assert rec.code == "internal_error"
    assert "disk full" not in rec.error


def test_reap_once_deletes_expired_workdirs(tmp_path):
    wd = tmp_path / "render_old"
    wd.mkdir()
    job_registry.create(
        "old", output_name="x", workdir=wd, output_path=wd / "x.mp4",
        clip_paths=[], music_path=None, params=_params(),
        retention_seconds=100.0, now=0.0,
    )
    count = render_orchestrator.reap_once(now=200.0)
    assert count == 1
    assert not wd.exists()
    assert job_registry.get("old") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_orchestrator.py -q`
Expected: FAIL — `AttributeError: module 'src.render_orchestrator' has no attribute 'enqueue_job'`.

- [ ] **Step 3: Replace the orchestrator**

Overwrite `api/src/render_orchestrator.py` with:

```python
"""Async render orchestrator.

`POST /jobs` calls `enqueue_job`: it persists the uploaded clips + optional
music into a per-job tmp workdir, registers a `queued` job, and returns the
job_id immediately. The endpoint then schedules `execute_job` as a background
task. `execute_job` acquires the module-level Semaphore(1) (FFmpeg is
CPU-bound; the VPS has 2 vCPU), runs the synchronous pipeline via
asyncio.to_thread so the event loop stays responsive for /health and polling,
and records the outcome on the job. The MP4 stays on disk at `output_path` and
is served by GET /jobs/{id}/result; the reaper purges workdirs after the TTL.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import structlog
from fastapi import UploadFile

from shared.models import ErrorCode, JobParams

from . import job_registry
from .ffmpeg_pipeline import _FriendlyError, run_pipeline


log = structlog.get_logger("render-api")

# Serialise renders: FFmpeg is CPU-bound; one job at a time matches WORKER=1.
_render_lock = asyncio.Semaphore(1)

# After a successful download, keep the workdir alive this long for Make retries.
DOWNLOAD_GRACE_SECONDS = 60

# Stable error code → HTTP status. Anything not listed is a server fault (500).
_CODE_TO_STATUS: dict[str, int] = {
    "invalid_params": 422,
    "clip_unreadable": 422,
    "clip_no_video": 422,
    "empty_clip": 422,
    "probe_timeout": 504,
    "ffmpeg_timeout": 504,
    "render_failed": 500,
    "internal_error": 500,
    "not_found": 404,
    "not_ready": 409,
    "too_busy": 429,
}


def status_for_code(code: str) -> int:
    return _CODE_TO_STATUS.get(code, 500)


class RenderError(Exception):
    """Public error surfaced to the caller. Message is safe (Spanish)."""

    def __init__(self, message: str, *, code: ErrorCode, job_id: str | None) -> None:
        super().__init__(message)
        self.message = message
        self.code: ErrorCode = code
        self.job_id = job_id
        self.http_status = status_for_code(code)


async def _persist(upload: UploadFile, dest: Path) -> None:
    with open(dest, "wb") as fh:
        while chunk := await upload.read(1024 * 1024):
            fh.write(chunk)


async def enqueue_job(
    *,
    clips: list[tuple[str, UploadFile]],
    music: UploadFile | None,
    params: JobParams,
    retention_seconds: float,
    max_pending: int,
) -> str:
    """Persist uploads, register a queued job, return its id.

    Raises RenderError(too_busy) if too many jobs are already in flight.
    """
    if job_registry.pending_count() >= max_pending:
        raise RenderError(
            "El servicio está saturado de renders en curso. Reintenta en unos minutos.",
            code="too_busy",
            job_id=None,
        )

    job_id = str(uuid.uuid4())
    workdir = Path(tempfile.mkdtemp(prefix=f"render_{job_id}_"))

    clip_paths: list[Path] = []
    for role, upload in clips:
        dest = workdir / f"{role}.mp4"
        await _persist(upload, dest)
        clip_paths.append(dest)

    music_path: Path | None = None
    if music is not None:
        music_path = workdir / "music"
        await _persist(music, music_path)

    output_path = workdir / f"{params.output_name}.mp4"

    job_registry.create(
        job_id,
        output_name=params.output_name,
        workdir=workdir,
        output_path=output_path,
        clip_paths=clip_paths,
        music_path=music_path,
        params=params,
        retention_seconds=retention_seconds,
    )
    return job_id


async def execute_job(job_id: str) -> None:
    """Run the render for a queued job. Never raises — records outcome on the job."""
    rec = job_registry.get(job_id)
    if rec is None:
        return

    structlog.contextvars.bind_contextvars(job_id=job_id, output_name=rec.output_name)
    try:
        async with _render_lock:
            job_registry.mark_processing(job_id)
            log.info(
                "job_started",
                orientation=rec.params.orientation,
                has_music=rec.music_path is not None,
                clip_roles=[p.stem for p in rec.clip_paths],
            )
            started = time.monotonic()

            result = await asyncio.to_thread(
                run_pipeline,
                clips=rec.clip_paths,
                music=rec.music_path,
                output=rec.output_path,
                params=rec.params,
            )

            elapsed = round(time.monotonic() - started, 2)
            job_registry.mark_done(
                job_id,
                duration_seconds=result.duration_seconds,
                concat_strategy=result.concat_strategy,
            )
            log.info(
                "job_done",
                duration_seconds=result.duration_seconds,
                concat_strategy=result.concat_strategy,
                elapsed_seconds=elapsed,
            )
    except _FriendlyError as exc:
        log.error("job_failed", error=str(exc), error_code=exc.code)
        job_registry.mark_failed(job_id, code=exc.code, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — detail goes to logs, not the caller
        log.exception("job_failed_unexpected", error_type=type(exc).__name__)
        job_registry.mark_failed(
            job_id,
            code="internal_error",
            error="Error inesperado en el render — revisa los logs del servicio.",
        )
    finally:
        structlog.contextvars.clear_contextvars()


def reap_once(*, now: float | None = None) -> int:
    """Delete the workdirs of all expired jobs. Returns how many were reaped."""
    workdirs = job_registry.sweep_expired(now=now)
    for wd in workdirs:
        shutil.rmtree(wd, ignore_errors=True)
    return len(workdirs)


async def run_reaper(*, interval_seconds: float, stop: asyncio.Event) -> None:
    """Sweep expired jobs every `interval_seconds` until `stop` is set."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            reap_once()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_orchestrator.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add api/src/render_orchestrator.py api/tests/test_orchestrator.py
git commit -m "feat(orchestrator): enqueue/execute/reap async job lifecycle"
```

---

## Task 5: Endpoints + reaper lifespan

**Files:**
- Modify: `api/src/main.py`
- Test: rewrite `api/tests/test_endpoint.py`

**Interfaces:**
- Consumes: `enqueue_job`, `execute_job`, `run_reaper`, `RenderError`, `status_for_code`, `DOWNLOAD_GRACE_SECONDS` (Task 4); `job_registry` (Task 2); `JobStatusResponse`, `ErrorResponse`, `SUCCESS_CODE` (Task 1).
- Produces: `POST /jobs` (202), `GET /jobs/{job_id}` (200 JSON status / 404), `GET /jobs/{job_id}/result` (200 MP4 / 409 / 404 / failed-error), `GET /health` (unchanged).

- [ ] **Step 1: Write the failing test**

Overwrite `api/tests/test_endpoint.py`:

```python
"""Integration tests for the async job contract (run_pipeline mocked).

POST /jobs schedules a FastAPI BackgroundTask; under the sync TestClient that
task runs to completion before the POST call returns, so after POST the job is
already `done` (or `failed`) and observable via GET. Transient states
(`queued`/`processing`, `not_ready`, `too_busy`) are driven by seeding the
registry directly, which needs no background timing.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RENDER_API_KEY", "test-key-test-key-test-key")

from shared.models import JobParams  # noqa: E402
from src import job_registry  # noqa: E402
from src.ffmpeg_pipeline import PipelineResult, _FriendlyError  # noqa: E402
from src.main import app  # noqa: E402


API_KEY = os.environ["RENDER_API_KEY"]
client = TestClient(app)
_FAKE_MP4 = b"\x00\x00\x00\x20ftypmp42" + b"\xde\xad\xbe\xef" * 32


@pytest.fixture(autouse=True)
def _clean_registry():
    job_registry.reset()
    yield
    job_registry.reset()


def _multipart_files() -> dict:
    return {
        "clip_hook": ("hook.mp4", b"hook-bytes", "video/mp4"),
        "clip_cuerpo": ("cuerpo.mp4", b"cuerpo-bytes", "video/mp4"),
        "clip_cta": ("cta.mp4", b"cta-bytes", "video/mp4"),
        "music": ("music.mp3", b"music-bytes", "audio/mpeg"),
    }


def _params(**overrides) -> str:
    base = {
        "orientation": "vertical", "music_volume": 0.3, "fade_in": 2.0,
        "fade_out": 2.0, "output_name": "ad_2026_05_test",
    }
    base.update(overrides)
    return json.dumps(base)


def _fake_pipeline(*, output: Path, **_kw) -> PipelineResult:
    output.write_bytes(_FAKE_MP4)
    return PipelineResult(duration_seconds=42.5, concat_strategy="fast")


def _post(files=None, params=None):
    return client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": params or _params()},
        files=_multipart_files() if files is None else files,
    )


# ---- POST /jobs ----------------------------------------------------------

def test_post_returns_202_with_job_id():
    with patch("src.render_orchestrator.run_pipeline", side_effect=_fake_pipeline):
        resp = _post()
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    uuid.UUID(body["job_id"])
    assert resp.headers["x-status-code"] == "ok"
    assert resp.headers["x-job-id"] == body["job_id"]


def test_post_422_when_no_clips_uploaded():
    resp = client.post("/jobs", headers={"X-API-Key": API_KEY}, data={"params": _params()})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_params"


def test_post_422_on_invalid_params_json():
    resp = _post(params="{not json")
    assert resp.status_code == 422
    assert resp.headers["x-status-code"] == "invalid_params"
    assert resp.json()["job_id"] is None


def test_post_rejects_missing_api_key():
    resp = client.post("/jobs", data={"params": _params()}, files=_multipart_files())
    assert resp.status_code == 401


def test_post_429_when_queue_full():
    # Seed max_pending queued jobs so the next POST is rejected.
    from src.settings import get_settings
    for i in range(get_settings().max_pending_jobs):
        wd = Path(f"/tmp/seed_{i}")
        job_registry.create(
            f"seed{i}", output_name="x", workdir=wd, output_path=wd / "x.mp4",
            clip_paths=[], music_path=None,
            params=JobParams(orientation="vertical", output_name="x"),
            retention_seconds=1800,
        )
    resp = _post()
    assert resp.status_code == 429
    assert resp.json()["code"] == "too_busy"
    assert resp.headers["x-status-code"] == "too_busy"


# ---- GET /jobs/{id} ------------------------------------------------------

def test_status_then_result_happy_path():
    with patch("src.render_orchestrator.run_pipeline", side_effect=_fake_pipeline):
        job_id = _post().json()["job_id"]

    s = client.get(f"/jobs/{job_id}", headers={"X-API-Key": API_KEY})
    assert s.status_code == 200
    body = s.json()
    assert body["status"] == "done"
    assert body["code"] == "ok"
    assert body["duration_seconds"] == 42.5
    assert body["concat_strategy"] == "fast"

    r = client.get(f"/jobs/{job_id}/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["x-status-code"] == "ok"
    assert r.headers["x-concat-strategy"] == "fast"
    assert 'filename="ad_2026_05_test.mp4"' in r.headers["content-disposition"]
    assert r.content == _FAKE_MP4


def test_status_unknown_job_404():
    s = client.get("/jobs/does-not-exist", headers={"X-API-Key": API_KEY})
    assert s.status_code == 404
    assert s.json()["code"] == "not_found"


def test_status_requires_api_key():
    assert client.get("/jobs/whatever").status_code == 401


def test_failed_render_status_and_result():
    def _boom(**_kw):
        raise _FriendlyError("clip corrupto", code="clip_unreadable")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        job_id = _post().json()["job_id"]

    s = client.get(f"/jobs/{job_id}", headers={"X-API-Key": API_KEY})
    assert s.status_code == 200
    assert s.json()["status"] == "failed"
    assert s.json()["code"] == "clip_unreadable"
    assert s.headers["x-status-code"] == "clip_unreadable"

    r = client.get(f"/jobs/{job_id}/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 422  # clip_unreadable → 422
    assert r.json()["code"] == "clip_unreadable"


# ---- GET /jobs/{id}/result transient states -----------------------------

def test_result_409_while_processing():
    # Seed a job that never runs: still queued → /result not ready.
    wd = Path("/tmp/seed_pending")
    job_registry.create(
        "pending1", output_name="x", workdir=wd, output_path=wd / "x.mp4",
        clip_paths=[], music_path=None,
        params=JobParams(orientation="vertical", output_name="x"),
        retention_seconds=1800,
    )
    r = client.get("/jobs/pending1/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 409
    assert r.json()["code"] == "not_ready"


def test_result_unknown_job_404():
    r = client.get("/jobs/nope/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_health_ok_no_auth_required():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_endpoint.py -q`
Expected: FAIL — POST returns 200 (old contract) not 202, and `/jobs/{id}` routes don't exist (404 from no route / KeyError on headers).

- [ ] **Step 3: Rewrite `main.py`**

Overwrite `api/src/main.py` with:

```python
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from shared.models import SUCCESS_CODE, ErrorResponse, JobParams, JobStatusResponse

from . import job_registry
from .auth import require_api_key
from .render_orchestrator import (
    DOWNLOAD_GRACE_SECONDS,
    RenderError,
    enqueue_job,
    execute_job,
    run_reaper,
    status_for_code,
)
from .settings import get_settings


def _configure_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


_configure_logging()
log = structlog.get_logger("render-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    stop = asyncio.Event()
    reaper = asyncio.create_task(
        run_reaper(interval_seconds=settings.reaper_interval_seconds, stop=stop)
    )
    try:
        yield
    finally:
        stop.set()
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Andromeda render-service", version="0.3.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", status_code=202, dependencies=[Depends(require_api_key)])
async def create_job(
    background_tasks: BackgroundTasks,
    clip_hook: UploadFile | None = File(None),
    clip_cuerpo: UploadFile | None = File(None),
    clip_cta: UploadFile | None = File(None),
    music: UploadFile | None = File(None),
    params: str = Form(...),
):
    try:
        job_params = JobParams.model_validate_json(params)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error=f"params inválidos: {exc.errors()}",
                code="invalid_params",
                job_id=None,
            ).model_dump(),
            headers={"X-Status-Code": "invalid_params"},
        )

    clips: list[tuple[str, UploadFile]] = [
        (role, upload)
        for role, upload in (
            ("hook", clip_hook),
            ("cuerpo", clip_cuerpo),
            ("cta", clip_cta),
        )
        if upload is not None
    ]
    if len(clips) < 1:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="Se requiere al menos 1 clip (hook/cuerpo/cta); no recibí ninguno.",
                code="invalid_params",
                job_id=None,
            ).model_dump(),
            headers={"X-Status-Code": "invalid_params"},
        )

    settings = get_settings()
    try:
        job_id = await enqueue_job(
            clips=clips,
            music=music,
            params=job_params,
            retention_seconds=settings.job_retention_seconds,
            max_pending=settings.max_pending_jobs,
        )
    except RenderError as exc:
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorResponse(
                error=exc.message, code=exc.code, job_id=exc.job_id
            ).model_dump(),
            headers={"X-Status-Code": exc.code},
        )

    background_tasks.add_task(execute_job, job_id)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "queued"},
        headers={"X-Status-Code": SUCCESS_CODE, "X-Job-Id": job_id},
    )


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def job_status(job_id: str):
    rec = job_registry.get(job_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                error="Job no encontrado o expirado.", code="not_found", job_id=job_id
            ).model_dump(),
            headers={"X-Status-Code": "not_found", "X-Job-Id": job_id},
        )
    body = JobStatusResponse(
        job_id=job_id,
        status=rec.status,
        code=rec.code,
        duration_seconds=rec.duration_seconds,
        concat_strategy=rec.concat_strategy,
        error=rec.error,
    )
    return JSONResponse(
        status_code=200,
        content=body.model_dump(),
        headers={"X-Status-Code": rec.code, "X-Job-Id": job_id},
    )


@app.get("/jobs/{job_id}/result", dependencies=[Depends(require_api_key)])
async def job_result(job_id: str):
    rec = job_registry.get(job_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                error="Job no encontrado o expirado.", code="not_found", job_id=job_id
            ).model_dump(),
            headers={"X-Status-Code": "not_found", "X-Job-Id": job_id},
        )
    if rec.status == "failed":
        return JSONResponse(
            status_code=status_for_code(rec.code),
            content=ErrorResponse(
                error=rec.error or "El render falló.", code=rec.code, job_id=job_id
            ).model_dump(),
            headers={"X-Status-Code": rec.code, "X-Job-Id": job_id},
        )
    if rec.status != "done":
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                error="El render aún no ha terminado.", code="not_ready", job_id=job_id
            ).model_dump(),
            headers={"X-Status-Code": "not_ready", "X-Job-Id": job_id},
        )

    job_registry.mark_downloaded(job_id, grace_seconds=DOWNLOAD_GRACE_SECONDS)
    return FileResponse(
        path=str(rec.output_path),
        media_type="video/mp4",
        filename=f"{rec.output_name}.mp4",
        headers={
            "X-Status-Code": SUCCESS_CODE,
            "X-Job-Id": job_id,
            "X-Output-Duration-Seconds": str(rec.duration_seconds),
            "X-Concat-Strategy": rec.concat_strategy or "",
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_endpoint.py -q`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Run the full suite**

Run: `cd api && uv run pytest -q`
Expected: PASS — all files green (`test_models`, `test_job_registry`, `test_settings`, `test_orchestrator`, `test_endpoint`, `test_ffmpeg_pipeline`).

- [ ] **Step 6: Commit**

```bash
git add api/src/main.py api/tests/test_endpoint.py
git commit -m "feat(api): async 202 + polling endpoints with reaper lifespan"
```

---

## Task 6: Docs + cleanup

**Files:**
- Modify: `render-service/CLAUDE.md`
- Delete: `api/src/__pycache__/queue_client.cpython-314.pyc`

- [ ] **Step 1: Update the request-lifecycle docs**

In `render-service/CLAUDE.md`, replace the "Request lifecycle" section (steps 1–4) and the relevant smoke-test/curl notes so they describe the async contract:
- `POST /jobs` → **202** `{job_id, status:"queued"}`; render runs in a background task behind `Semaphore(1)`.
- `GET /jobs/{id}` → JSON status `{status, code, duration_seconds?, concat_strategy?, error?}`.
- `GET /jobs/{id}/result` → MP4 when `done`; **409** `not_ready` while running; **404** `not_found`; failed-render error JSON otherwise.
- Note the in-memory registry + on-disk MP4, the reaper (TTL `job_retention_seconds`, default 1800 s; 60 s post-download grace), and the `max_pending_jobs` → **429** `too_busy` bound.
- Add the new codes to the `code → HTTP status` table: `not_found` 404, `not_ready` 409, `too_busy` 429.
- Update the `curl` smoke-test block to: POST (expect 202 + job_id), then `GET /jobs/<id>`, then `GET /jobs/<id>/result -o out.mp4`.

Add the recommended Make flow note: `POST → store job_id → Sleep 3–5 s → GET /jobs/{id} (loop: queued/processing → wait; done → GET /result; failed → branch on code)`.

- [ ] **Step 2: Remove the orphaned bytecode**

Run:

```bash
rm -f api/src/__pycache__/queue_client.cpython-314.pyc
```

(It is not tracked by git — no commit needed for the deletion itself.)

- [ ] **Step 3: Commit the docs**

```bash
git add render-service/CLAUDE.md
git commit -m "docs: describe async 202 + polling lifecycle"
```

---

## Task 7: Final verification

- [ ] **Step 1: Full test suite**

Run: `cd api && uv run pytest -q`
Expected: PASS, no warnings about unraised/swallowed exceptions.

- [ ] **Step 2: Smoke-build the image (optional, if Docker available)**

Run: `docker compose build`
Expected: builds clean (single worker CMD unchanged; `shared/` copied in).

- [ ] **Step 3: Push the branch**

```bash
git push -u origin feat/async-jobs-polling
```

Then open a PR from `feat/async-jobs-polling` for review.

---

## Self-Review Notes (verified during planning)

- **Spec coverage:** HTTP contract (Task 5), status/result models (Task 1), job registry + on-disk MP4 (Task 2), enqueue/execute/`Semaphore(1)` (Task 4), reaper + TTL + post-download grace (Tasks 2/4/5), bounded queue → 429 (Tasks 4/5), new settings (Task 3), new error codes + `_CODE_TO_STATUS` (Tasks 1/4), docs + orphan cleanup (Task 6). All spec sections map to a task.
- **Type consistency:** `enqueue_job`/`execute_job`/`run_reaper`/`reap_once`/`DOWNLOAD_GRACE_SECONDS` names are identical across Tasks 4 and 5; `JobRecord` field names used in `main.py` (`status`, `code`, `output_path`, `output_name`, `duration_seconds`, `concat_strategy`, `error`) match Task 2; `JobStatusResponse` fields match Task 1.
- **No placeholders:** every code/test step contains complete content.
```
