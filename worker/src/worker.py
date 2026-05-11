"""RQ worker entrypoint. Run with: `uv run python -m src.worker`.

`WORKER_CONCURRENCY` controls how many jobs run in parallel inside this
container. Default 1 — anything beyond 2 will saturate the 2 vCPU VPS.

For concurrency > 1 we use `rq.worker_pool.WorkerPool`, which forks N child
workers. For concurrency == 1 we run a plain Worker (cleaner shutdown). Both
honour SIGTERM so `docker stop` / Easypanel restart let the current ffmpeg
invocation finish before tearing the process down.
"""
from __future__ import annotations

import os
import signal
import sys

import structlog
from redis import Redis
from rq import Queue, Worker

from .logging_setup import configure_logging
from .settings import get_settings

QUEUE_NAME = "renders"


def main() -> int:
    configure_logging()
    log = structlog.get_logger("render-worker")
    settings = get_settings()

    # Fail loudly on misconfiguration so the container restart-loop is visible
    # in Easypanel rather than silently never picking up jobs.
    if not (settings.google_service_account_json or settings.google_service_account_json_b64):
        log.error(
            "missing_service_account",
            hint="Set GOOGLE_SERVICE_ACCOUNT_JSON (path) or GOOGLE_SERVICE_ACCOUNT_JSON_B64.",
        )
        return 2

    conn = Redis.from_url(settings.redis_url)
    try:
        conn.ping()
    except Exception as exc:  # noqa: BLE001
        log.error("redis_ping_failed", error=str(exc))
        return 3

    queue = Queue(QUEUE_NAME, connection=conn)
    log.info(
        "worker_starting",
        queue=QUEUE_NAME,
        concurrency=settings.worker_concurrency,
        pid=os.getpid(),
    )

    if settings.worker_concurrency > 1:
        # Import here so that envs running with default concurrency=1 don't pay
        # for the import at all (WorkerPool drags in some forking machinery).
        from rq.worker_pool import WorkerPool

        pool = WorkerPool(
            queues=[queue],
            connection=conn,
            num_workers=settings.worker_concurrency,
        )
        pool.start(logging_level=settings.log_level)
        return 0

    worker = Worker([queue], connection=conn)
    signal.signal(signal.SIGTERM, lambda *_: worker.request_stop(signal.SIGTERM, None))
    worker.work(with_scheduler=False, logging_level=settings.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
