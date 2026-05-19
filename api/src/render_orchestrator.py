"""Sync render orchestrator.

Wraps the FFmpeg pipeline in an asyncio.Semaphore so only one render runs
at a time (FFmpeg is CPU-bound and the VPS has 2 vCPU). The pipeline
itself is synchronous; we await it via asyncio.to_thread so the event
loop stays responsive for /health while a render is in flight.

UploadFiles are streamed to a per-job tmp workdir, the pipeline writes
the output MP4 inside that workdir, we read it into memory, and the
workdir is removed in `finally`. The caller receives an async iterator
that yields the bytes once.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import structlog
from fastapi import UploadFile

from shared.models import ErrorCode, JobParams

from .ffmpeg_pipeline import _FriendlyError, run_pipeline


log = structlog.get_logger("render-api")

# Serialise renders: FFmpeg is CPU-bound; one job at a time matches WORKER_CONCURRENCY=1.
_render_lock = asyncio.Semaphore(1)

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
}


def status_for_code(code: str) -> int:
    return _CODE_TO_STATUS.get(code, 500)


class RenderError(Exception):
    """Public error surfaced to the caller. Message is safe (Spanish).

    Carries the stable `code`, the mapped HTTP `http_status`, and the
    `job_id` so the caller can correlate the failure with the logs.
    """

    def __init__(self, message: str, *, code: ErrorCode, job_id: str | None) -> None:
        super().__init__(message)
        self.message = message
        self.code: ErrorCode = code
        self.job_id = job_id
        self.http_status = status_for_code(code)


@dataclass(frozen=True)
class RenderResult:
    output_stream: AsyncIterator[bytes]
    duration_seconds: float
    concat_strategy: str
    job_id: str


async def _persist(upload: UploadFile, dest: Path) -> None:
    with open(dest, "wb") as fh:
        while chunk := await upload.read(1024 * 1024):
            fh.write(chunk)


async def render_sync(
    *,
    clip_hook: UploadFile,
    clip_cuerpo: UploadFile,
    clip_cta: UploadFile,
    music: UploadFile | None,
    params: JobParams,
) -> RenderResult:
    job_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(job_id=job_id, output_name=params.output_name)
    workdir = Path(tempfile.mkdtemp(prefix=f"render_{job_id}_"))

    try:
        async with _render_lock:
            log.info(
                "job_started",
                orientation=params.orientation,
                has_music=music is not None,
            )
            started = time.monotonic()

            hook_path = workdir / "hook.mp4"
            cuerpo_path = workdir / "cuerpo.mp4"
            cta_path = workdir / "cta.mp4"
            await _persist(clip_hook, hook_path)
            await _persist(clip_cuerpo, cuerpo_path)
            await _persist(clip_cta, cta_path)

            music_path: Path | None = None
            if music is not None:
                music_path = workdir / "music"
                await _persist(music, music_path)

            output_path = workdir / f"{params.output_name}.mp4"

            pipeline_result = await asyncio.to_thread(
                run_pipeline,
                hook=hook_path,
                cuerpo=cuerpo_path,
                cta=cta_path,
                music=music_path,
                output=output_path,
                params=params,
            )

            elapsed = round(time.monotonic() - started, 2)
            log.info(
                "job_done",
                duration_seconds=pipeline_result.duration_seconds,
                concat_strategy=pipeline_result.concat_strategy,
                elapsed_seconds=elapsed,
            )

            # Clips < 200 MB per ops note — loading into memory is acceptable
            # and lets us clean the workdir before returning.
            output_bytes = output_path.read_bytes()

            async def stream() -> AsyncIterator[bytes]:
                yield output_bytes

            return RenderResult(
                output_stream=stream(),
                duration_seconds=pipeline_result.duration_seconds,
                concat_strategy=pipeline_result.concat_strategy,
                job_id=job_id,
            )
    except _FriendlyError as exc:
        log.error("job_failed", error=str(exc), error_code=exc.code)
        raise RenderError(str(exc), code=exc.code, job_id=job_id) from exc
    except Exception as exc:
        log.exception("job_failed_unexpected", error_type=type(exc).__name__)
        raise RenderError(
            "Error inesperado en el render — revisa los logs del servicio.",
            code="internal_error",
            job_id=job_id,
        ) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        structlog.contextvars.clear_contextvars()
