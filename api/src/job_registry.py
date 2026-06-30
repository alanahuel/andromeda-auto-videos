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
