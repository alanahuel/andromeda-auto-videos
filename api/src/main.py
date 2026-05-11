from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Response, status
from redis.exceptions import RedisError
from rq.exceptions import NoSuchJobError
from rq.job import Job

from shared.models import (
    HealthDegraded,
    HealthOk,
    JobAccepted,
    JobRequest,
    JobStatus,
)

from .auth import require_api_key
from .queue_client import WORKER_JOB_FUNCTION, get_queue, ping_redis
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

app = FastAPI(title="Andromeda render-service", version="0.1.0")


@app.get("/health", response_model=None)
def health(response: Response) -> dict[str, str]:
    if ping_redis():
        return HealthOk().model_dump()
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthDegraded().model_dump()


@app.post(
    "/jobs",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def create_job(req: JobRequest) -> JobAccepted:
    job_id = str(uuid.uuid4())
    settings = get_settings()

    # Pydantic v2: by_alias=False, mode="json" turns HttpUrl/etc into JSON-safe primitives.
    payload: dict[str, Any] = req.model_dump(mode="json")

    try:
        queue = get_queue()
        queue.enqueue(
            WORKER_JOB_FUNCTION,
            job_id,
            payload,
            job_id=job_id,
            job_timeout=settings.job_timeout_seconds,
            result_ttl=86_400,
            failure_ttl=86_400,
            description=f"render {req.output.name}",
        )
    except RedisError as exc:
        log.error("redis_enqueue_failed", job_id=job_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="queue unavailable",
        )

    log.info(
        "job_enqueued",
        job_id=job_id,
        output_name=req.output.name,
        orientation=req.output.orientation,
    )
    return JobAccepted(job_id=job_id, status="queued")


_RQ_STATUS_MAP = {
    "queued": "queued",
    "deferred": "queued",
    "scheduled": "queued",
    "started": "processing",
    "finished": "done",
    "failed": "failed",
    "stopped": "failed",
    "canceled": "failed",
}


@app.get(
    "/jobs/{job_id}",
    response_model=JobStatus,
    dependencies=[Depends(require_api_key)],
)
def get_job(job_id: str) -> JobStatus:
    try:
        queue = get_queue()
        job = Job.fetch(job_id, connection=queue.connection)
    except NoSuchJobError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    except RedisError as exc:
        log.error("redis_fetch_failed", job_id=job_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="queue unavailable",
        )

    rq_status = job.get_status(refresh=True)
    mapped = _RQ_STATUS_MAP.get(rq_status, "queued")
    error: str | None = None
    if mapped == "failed":
        # exc_info is a stack trace — surface the latest line only.
        info = (job.exc_info or "").strip()
        error = info.splitlines()[-1] if info else "job failed"
    return JobStatus(job_id=job_id, status=mapped, error=error)
