from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Redis / RQ
    redis_url: str = "redis://redis:6379/0"
    worker_concurrency: int = Field(1, ge=1, le=4)
    job_timeout_seconds: int = 600

    # Google Drive Service Account — exactly one of these two must be set.
    # Path option preferred when running with a mounted volume; b64 option
    # preferred on Easypanel where env vars are the easiest secret channel.
    google_service_account_json: str | None = None
    google_service_account_json_b64: str | None = None

    # FFmpeg
    ffmpeg_timeout_seconds: int = 600
    orientation_strategy: Literal["crop", "pad"] = "crop"

    # Filesystem
    workdir_base: str = "/tmp"

    # Logging
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
