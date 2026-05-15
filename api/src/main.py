from __future__ import annotations

import logging
import sys

import structlog
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from shared.models import JobParams

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
    clip_hook: UploadFile = File(...),
    clip_cuerpo: UploadFile = File(...),
    clip_cta: UploadFile = File(...),
    music: UploadFile = File(...),
    params: str = Form(...),
):
    try:
        job_params = JobParams.model_validate_json(params)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"params inválidos: {exc.errors()}",
        )

    try:
        result = await render_sync(
            clip_hook=clip_hook,
            clip_cuerpo=clip_cuerpo,
            clip_cta=clip_cta,
            music=music,
            params=job_params,
        )
    except RenderError as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(exc)},
        )

    return StreamingResponse(
        result.output_stream,
        media_type="video/mp4",
        headers={
            "X-Job-Id": result.job_id,
            "X-Output-Duration-Seconds": str(result.duration_seconds),
            "X-Concat-Strategy": result.concat_strategy,
            "Content-Disposition": f'attachment; filename="{job_params.output_name}.mp4"',
        },
    )
