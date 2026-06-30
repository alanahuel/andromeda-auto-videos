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

    try:
        structlog.contextvars.bind_contextvars(job_id=job_id, output_name=rec.output_name)
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
