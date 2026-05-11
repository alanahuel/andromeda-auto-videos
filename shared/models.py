"""Pydantic models shared between API and worker.

Single source of truth for the wire contract. Both /app/api and /app/worker
Docker images COPY this file in and import via `from shared.models import ...`.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


ClipRole = Literal["hook", "cuerpo", "cta"]
Orientation = Literal["vertical", "horizontal"]
JobStatusLiteral = Literal["queued", "processing", "done", "failed"]


class ClipInput(BaseModel):
    drive_id: str = Field(..., min_length=1)
    role: ClipRole


class MusicInput(BaseModel):
    drive_id: str = Field(..., min_length=1)
    volume: float = Field(0.3, ge=0.0, le=1.0)
    fade_in: float = Field(2.0, ge=0.0)
    fade_out: float = Field(2.0, ge=0.0)


class OutputSpec(BaseModel):
    name: str = Field(..., min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    folder_drive_id: str = Field(..., min_length=1)
    orientation: Orientation


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clips: list[ClipInput]
    music: MusicInput
    output: OutputSpec
    callback_url: HttpUrl
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("clips")
    @classmethod
    def _exactly_three_distinct_roles(cls, clips: list[ClipInput]) -> list[ClipInput]:
        if len(clips) != 3:
            raise ValueError("clips must contain exactly 3 entries")
        roles = {c.role for c in clips}
        expected = {"hook", "cuerpo", "cta"}
        if roles != expected:
            missing = expected - roles
            extra = roles - expected
            parts = []
            if missing:
                parts.append(f"missing roles: {sorted(missing)}")
            if extra:
                parts.append(f"unexpected roles: {sorted(extra)}")
            raise ValueError("; ".join(parts) or "clips roles must be hook, cuerpo, cta")
        return clips


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatusLiteral = "queued"


class JobStatus(BaseModel):
    job_id: str
    status: JobStatusLiteral
    error: str | None = None


class HealthOk(BaseModel):
    status: Literal["ok"] = "ok"
    redis: Literal["ok"] = "ok"


class HealthDegraded(BaseModel):
    status: Literal["degraded"] = "degraded"
    redis: Literal["down"] = "down"


class CallbackPayload(BaseModel):
    """Body sent by the worker to the caller-supplied callback_url."""

    job_id: str
    status: Literal["done", "failed"]
    output_drive_id: str | None = None
    output_url: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
