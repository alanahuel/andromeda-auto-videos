from __future__ import annotations

import logging
import sys

import structlog
from fastapi import Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from shared.models import SUCCESS_CODE, ErrorResponse, JobParams

from .auth import require_api_key
from .render_orchestrator import RenderError, render_sync
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

app = FastAPI(title="Andromeda render-service", version="0.2.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", dependencies=[Depends(require_api_key)])
async def create_job(
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

    # Any subset of hook/cuerpo/cta is allowed, in that order, as long as at
    # least 2 clips arrive — concatenating a single clip is a no-op.
    clips: list[tuple[str, UploadFile]] = [
        (role, upload)
        for role, upload in (
            ("hook", clip_hook),
            ("cuerpo", clip_cuerpo),
            ("cta", clip_cta),
        )
        if upload is not None
    ]
    if len(clips) < 2:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error=(
                    "Se requieren al menos 2 clips (hook/cuerpo/cta) para "
                    "concatenar; recibí "
                    f"{len(clips)}."
                ),
                code="invalid_params",
                job_id=None,
            ).model_dump(),
            headers={"X-Status-Code": "invalid_params"},
        )

    try:
        result = await render_sync(
            clips=clips,
            music=music,
            params=job_params,
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

    return StreamingResponse(
        result.output_stream,
        media_type="video/mp4",
        headers={
            "X-Status-Code": SUCCESS_CODE,
            "X-Job-Id": result.job_id,
            "X-Output-Duration-Seconds": str(result.duration_seconds),
            "X-Concat-Strategy": result.concat_strategy,
            "Content-Disposition": f'attachment; filename="{job_params.output_name}.mp4"',
        },
    )
