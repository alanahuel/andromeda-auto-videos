from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .settings import get_settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject requests missing or carrying the wrong X-API-Key header.

    `hmac.compare_digest` is used so the comparison is constant-time and does
    not leak the key length / prefix via timing.
    """
    settings = get_settings()
    if x_api_key is None or not hmac.compare_digest(x_api_key, settings.render_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
        )
