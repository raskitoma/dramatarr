"""Shared FastAPI dependencies: session cookie handling, current user, role gates.

Session tokens are opaque random strings. Only their sha256 is stored, so a database leak
does not hand over live sessions.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db import get_session
from app.errors import Forbidden, Unauthorized
from app.models import AuthSession, User, UserRole
from app.utils.ids import new_token
from app.utils.time import in_days, is_expired, utcnow

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Session lifecycle ---------------------------------------------------------------------


async def create_session(
    session: AsyncSession,
    user: User,
    *,
    request: Request,
    response: Response,
) -> AuthSession:
    """Mint a session, set the cookie, and stamp the user's last login."""
    settings = get_settings()
    raw = new_token(32)
    auth = AuthSession(
        user_id=user.id,
        token_hash=hash_token(raw),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        ip=request.client.host if request.client else None,
        expires_at=in_days(settings.session_ttl_days),
    )
    session.add(auth)
    user.last_login_at = utcnow()
    await session.flush()

    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.use_secure_cookie,
        samesite="lax",
        path="/",
    )
    return auth


async def revoke_current_session(session: AsyncSession, request: Request, response: Response) -> None:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if raw:
        row = (
            await session.execute(
                select(AuthSession).where(AuthSession.token_hash == hash_token(raw))
            )
        ).scalar_one_or_none()
        if row is not None:
            row.revoked_at = utcnow()
    response.delete_cookie(settings.session_cookie_name, path="/")


# --- Current user --------------------------------------------------------------------------


async def get_current_user_optional(request: Request, session: SessionDep) -> User | None:
    """Never raises. Used by `GET /api/auth/session`, which must answer for anonymous callers."""
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None

    auth = (
        await session.execute(
            select(AuthSession)
            .where(AuthSession.token_hash == hash_token(raw))
            .options(selectinload(AuthSession.user))
        )
    ).scalar_one_or_none()

    if auth is None or auth.revoked_at is not None or is_expired(auth.expires_at):
        return None
    if auth.user is None or not auth.user.is_active:
        return None

    # Cheap liveness stamp; avoid a write on every single request.
    now = utcnow()
    if (now - auth.last_seen_at).total_seconds() > 300:
        auth.last_seen_at = now

    return auth.user


async def get_current_user(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    if user is None:
        raise Unauthorized()
    return user


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != UserRole.ADMIN:
        raise Forbidden("ADMIN_REQUIRED", "This action requires an administrator")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user_optional)]
AdminUser = Annotated[User, Depends(require_admin)]


__all__ = [
    "AdminUser",
    "CurrentUser",
    "OptionalUser",
    "SessionDep",
    "SettingsDep",
    "create_session",
    "get_current_user",
    "get_current_user_optional",
    "hash_token",
    "require_admin",
    "revoke_current_session",
]
