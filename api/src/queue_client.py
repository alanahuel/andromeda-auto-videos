from __future__ import annotations

from functools import lru_cache

from redis import Redis
from rq import Queue

from .settings import get_settings

QUEUE_NAME = "renders"
# The worker imports this function path. Keep it stable; the API never
# imports the function itself — it only enqueues by string reference so
# the worker's codebase is the only one that needs to have it on disk.
WORKER_JOB_FUNCTION = "src.job_runner.run_job"


@lru_cache(maxsize=1)
def _redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


@lru_cache(maxsize=1)
def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=_redis())


def ping_redis() -> bool:
    try:
        return bool(_redis().ping())
    except Exception:
        return False
