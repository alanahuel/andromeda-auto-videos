from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    render_api_key: str = Field(..., min_length=8)
    orientation_strategy: Literal["crop", "pad"] = "crop"
    log_level: str = "INFO"

    # Async job lifecycle.
    job_retention_seconds: int = 1800   # reaper purges jobs + workdirs older than this
    reaper_interval_seconds: int = 60   # how often the reaper sweeps
    max_pending_jobs: int = 20          # POST /jobs returns 429 too_busy beyond this


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
