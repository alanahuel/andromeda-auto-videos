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
        headers = {"X-Status-Code": exc.code}
        if exc.job_id:
            headers["X-Job-Id"] = exc.job_id
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorResponse(
                error=exc.message, code=exc.code, job_id=exc.job_id
            ).model_dump(),
            headers=headers,
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
