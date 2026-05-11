"""Send the final job-result callback to Make.com.

3 attempts with exponential backoff (5s, 15s, 45s). If all 3 fail we log an
error and abandon — the job stays marked `done`/`failed` in Redis regardless.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from shared.models import CallbackPayload

logger = logging.getLogger(__name__)

_BACKOFFS = (5, 15, 45)
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def send_callback(url: str, payload: CallbackPayload) -> bool:
    """Returns True if any attempt landed a 2xx, False otherwise."""
    body: dict[str, Any] = payload.model_dump(mode="json")
    for attempt, backoff in enumerate(_BACKOFFS, start=1):
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(url, json=body)
            if 200 <= resp.status_code < 300:
                logger.info(
                    "callback_ok",
                    extra={"job_id": payload.job_id, "attempt": attempt, "status_code": resp.status_code},
                )
                return True
            logger.warning(
                "callback_non_2xx",
                extra={
                    "job_id": payload.job_id,
                    "attempt": attempt,
                    "status_code": resp.status_code,
                    "body_tail": (resp.text or "")[:500],
                },
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "callback_http_error",
                extra={"job_id": payload.job_id, "attempt": attempt, "error": str(exc)},
            )
        if attempt < len(_BACKOFFS):
            time.sleep(backoff)
    logger.error("callback_giving_up", extra={"job_id": payload.job_id})
    return False
