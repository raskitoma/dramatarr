"""Profile, preferences, sessions and personal watch lists — `/api/me`.

The profile schemas live here rather than in `app/schemas/` because nothing else answers with
them; the library shapes these endpoints reuse come from `app.schemas.library`.

Everything here is scoped to the caller by construction: the routes take no user id, they read
`CurrentUser`. That is what makes the avatar routes safe for viewers as well as admins — there
is no path by which "upload my picture" could name somebody else's account.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from pydantic import Field, model_validator
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep, hash_token
from app.config import get_settings
from app.errors import NotFound
from app.models import AuthSession, User
from app.schemas.common import APIModel, Ok, Page
from app.schemas.library import ContinueWatchingItem, HistoryItem
from app.services import library as library_service
from app.services.images import (
    MAX_AVATAR_BYTES,
    avatar_url,
    delete_avatar,
    read_capped,
    store_avatar,
)
from app.utils.time import as_aware, is_expired, utcnow

router = APIRouter(prefix="/me", tags=["me"])


class MeOut(APIModel):
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    locale: str
    theme: str
    preferred_audio_lang: str | None = None
    preferred_subtitle_lang: str | None = None
    autoplay_next: bool = True
    avatar_color: str
    #: Null until the member uploads a photo — the UI then keeps drawing initials on
    #: `avatarColor`, which is why this is nullable rather than a generated placeholder URL.
    avatar_url: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None

    @model_validator(mode="after")
    def _resolve_avatar(self) -> Self:
        """Derived from the stored file, exactly as `AuthUser` does it — see the note there."""
        if self.avatar_url is None:
            self.avatar_url = avatar_url(self.id)
        return self


class MePatch(APIModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    locale: str | None = Field(default=None, min_length=2, max_length=16)
    theme: Literal["auto", "light", "dark"] | None = None
    preferred_audio_lang: str | None = Field(default=None, max_length=16)
    preferred_subtitle_lang: str | None = Field(default=None, max_length=16)
    autoplay_next: bool | None = None


class SessionOut(APIModel):
    id: str
    user_agent: str | None = None
    ip: str | None = None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool = False


def _me(user: User) -> MeOut:
    return MeOut.model_validate(user)


def _aware(value: datetime) -> datetime:
    """`as_aware` for a column that cannot be null, so the result cannot be either."""
    return as_aware(value) or value


@router.get("", response_model=MeOut)
@router.get("/", response_model=MeOut, include_in_schema=False)
async def get_me(user: CurrentUser) -> MeOut:
    return _me(user)


@router.patch("", response_model=MeOut)
@router.patch("/", response_model=MeOut, include_in_schema=False)
async def patch_me(session: SessionDep, user: CurrentUser, body: MePatch) -> MeOut:
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await session.flush()
    return _me(user)


# --- Profile picture -------------------------------------------------------------------------
#
# The stored file is not the uploaded file. Whatever arrives is checked by its magic bytes,
# capped, and then re-encoded from decoded pixels — which is what removes the EXIF block. That
# matters more here than anywhere else in the application: a photo taken on a phone carries the
# GPS coordinates of wherever it was taken, and this endpoint exists so that household members
# can hand each other pictures of themselves.


@router.post("/avatar", response_model=MeOut)
async def upload_avatar(
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
) -> MeOut:
    """Replace the caller's own avatar. Any signed-in member, admin or viewer."""
    data = await read_capped(file, MAX_AVATAR_BYTES, "AVATAR_TOO_LARGE")
    await store_avatar(user.id, data)
    return _me(user)


@router.delete("/avatar", response_model=MeOut)
async def remove_avatar(user: CurrentUser) -> MeOut:
    """Revert to the generated initials avatar.

    Idempotent, and not a 404 when there was nothing to delete: the caller asked to end up
    with no photo, and they do.
    """
    await delete_avatar(user.id)
    return _me(user)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    session: SessionDep, user: CurrentUser, request: Request
) -> list[SessionOut]:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    current_hash = hash_token(raw) if raw else None

    rows = (
        (
            await session.execute(
                select(AuthSession)
                .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
                .order_by(AuthSession.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        SessionOut(
            id=row.id,
            user_agent=row.user_agent,
            ip=row.ip,
            created_at=_aware(row.created_at),
            last_seen_at=_aware(row.last_seen_at),
            expires_at=_aware(row.expires_at),
            current=current_hash is not None and row.token_hash == current_hash,
        )
        for row in rows
        if not is_expired(row.expires_at)
    ]


@router.delete("/sessions/{session_id}", response_model=Ok)
async def revoke_session(
    session: SessionDep,
    user: CurrentUser,
    session_id: str,
    request: Request,
    response: Response,
) -> Ok:
    row = await session.get(AuthSession, session_id)
    if row is None or row.user_id != user.id:
        raise NotFound("Session", "SESSION_NOT_FOUND")
    row.revoked_at = utcnow()
    await session.flush()

    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if raw and hash_token(raw) == row.token_hash:
        # Signing yourself out from the session list should actually sign you out.
        response.delete_cookie(settings.session_cookie_name, path="/")
    return Ok()


@router.get("/continue-watching", response_model=list[ContinueWatchingItem])
async def continue_watching(
    session: SessionDep,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=30)] = 30,
) -> list[ContinueWatchingItem]:
    return await library_service.continue_watching(session, user.id, limit=limit)


@router.get("/history", response_model=Page[HistoryItem])
async def history(
    session: SessionDep,
    user: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 30,
) -> Page[HistoryItem]:
    return await library_service.watch_history(session, user.id, page=page, page_size=page_size)


__all__ = ["MeOut", "MePatch", "SessionOut", "router"]
