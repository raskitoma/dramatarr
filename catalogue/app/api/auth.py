"""Static API key.

Enough for a personal self-hosted service, and it means the instance is safe to
expose beyond localhost. `/health` is exempt so container healthchecks and
uptime probes do not need the secret.

Two ways to present the key, because a browser cannot do what an API client
does: a header for programmatic callers, and a cookie for the admin console.
Browsers send no custom headers when you navigate to a page or when an <img>
loads, so a header-only guard makes the console unusable in the very place it
is meant to be used.

The key is deliberately NOT accepted as a query parameter: it would land in
browser history, server logs and Referer headers.
"""

from __future__ import annotations

import secrets

from fastapi import Cookie, Header, HTTPException, status

from app.config import settings

COOKIE_NAME = "dramadex_key"


def _valid(candidate: str | None) -> bool:
    if not candidate:
        return False
    # Constant-time compare: a timing oracle on a static key is cheap to avoid.
    return secrets.compare_digest(candidate, settings.api_key)


async def require_api_key(
    x_api_key: str | None = Header(default=None),
    dramadex_key: str | None = Cookie(default=None),
) -> None:
    if not settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DRAMADEX_API_KEY is not configured",
        )
    if _valid(x_api_key) or _valid(dramadex_key):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key"
    )
