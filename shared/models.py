"""Pydantic models shared between API and tests.

Single source of truth for the wire contract. The API Docker image
COPYs this file in and imports via `from shared.models import ...`.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Orientation = Literal["vertical", "horizontal"]


class JobParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orientation: Orientation
    music_volume: float = Field(default=0.3, ge=0.0, le=1.0)
    fade_in: float = Field(default=2.0, ge=0.0, le=10.0)
    fade_out: float = Field(default=2.0, ge=0.0, le=10.0)
    output_name: str = Field(pattern=r"^[A-Za-z0-9._-]+$", max_length=200)


# Stable, machine-readable error taxonomy. The Spanish `error` string may be
# reworded freely; callers (Make/Airtable) must branch on `code`, never on the
# message text. Codes map to HTTP status in api/src/render_orchestrator.py.
ErrorCode = Literal[
    "invalid_params",   # 422 — params JSON failed JobParams validation
    "clip_unreadable",  # 422 — ffprobe could not read a clip (corrupt/format)
    "clip_no_video",    # 422 — a clip has no video stream
    "empty_clip",       # 422 — concatenated video has duration 0
    "probe_timeout",    # 504 — ffprobe hung reading a clip
    "ffmpeg_timeout",   # 504 — ffmpeg hung during concat/mix
    "render_failed",    # 500 — ffmpeg exited non-zero (processing failure)
    "internal_error",   # 500 — unexpected, details only in logs
]


# Success discriminator. Returned in the `X-Status-Code` header on a 2xx
# (the body is the binary MP4, so it can't carry a JSON `code`). Errors put
# their `ErrorCode` in the same header — so Make can branch on one header
# regardless of success/failure.
SUCCESS_CODE = "ok"

# Every value `X-Status-Code` can take: "ok" on success, an ErrorCode on failure.
ResultCode = Literal["ok"] | ErrorCode


class ErrorResponse(BaseModel):
    """Body returned for every non-2xx from POST /jobs.

    `error` is a Spanish message safe to surface; `code` is the stable
    discriminator; `job_id` correlates with the structlog `job_id` (null
    when the failure happens before a job is created, e.g. invalid params).
    """

    error: str
    code: ErrorCode
    job_id: str | None = None
