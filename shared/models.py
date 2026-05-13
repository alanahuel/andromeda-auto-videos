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
