"""`/api/auth` — sessions, passkeys, invites, magic links and Sign in with Google.

`GET /session` is the one route that must never fail: the SPA calls it before it knows
whether anyone is signed in, and a 401 there would turn a cold load into an error screen.

`GET /google/callback` is the one route that must never answer with JSON: Google redirects
the *browser* to it, so every outcome — including every failure — is a 302 back into the SPA
carrying a stable error code the UI can localize.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import (
    CurrentUser,
    OptionalUser,
    SessionDep,
    create_session,
    revoke_current_session,
)
from app.config import get_settings
from app.errors import AppError, NotFound
from app.models import Credential, User
from app.schemas.auth import (
    AuthUser,
    GoogleAuthUrlOut,
    InviteCompleteIn,
    InviteInfoOut,
    MagicLinkConsumeIn,
    MagicLinkRequestIn,
    PasskeyLoginCompleteIn,
    PasskeyOut,
    PasskeyRegisterCompleteIn,
    PasskeyRenameIn,
    PublicKeyOptionsOut,
    SessionOut,
    SignInMethodsOut,
)
from app.schemas.common import Ok
from app.services import auth as auth_service
from app.services import oauth as oauth_service
from app.services.settings import SettingsService
from app.utils.ids import new_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

NextQuery = Annotated[str | None, Query(alias="next", max_length=512)]

MAGIC_LINK_WINDOW_SECONDS = 15 * 60
MAGIC_LINK_MAX_REQUESTS = 3


class _MagicLinkLimiter:
    """Per-email sliding window, in memory.

    dramatarr is one process by design (see the topology section of the architecture doc), so
    a dict is the whole implementation — no Redis, no cross-process coordination to get wrong.
    """

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        async with self._lock:
            # Opportunistic sweep: without it the dict grows once per address, forever.
            for existing in list(self._hits):
                fresh = [t for t in self._hits[existing] if t > cutoff]
                if fresh:
                    self._hits[existing] = fresh
                else:
                    del self._hits[existing]

            hits = self._hits.setdefault(key, [])
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True


_limiter = _MagicLinkLimiter(MAGIC_LINK_MAX_REQUESTS, MAGIC_LINK_WINDOW_SECONDS)


# --- Session -------------------------------------------------------------------------------


@router.get("/session", response_model=SessionOut)
async def read_session(user: OptionalUser) -> SessionOut:
    return SessionOut(user=AuthUser.model_validate(user) if user else None)


@router.post("/logout", response_model=Ok)
async def logout(
    session: SessionDep,
    request: Request,
    response: Response,
    user: CurrentUser,
) -> Ok:
    await revoke_current_session(session, request, response)
    return Ok()


# --- Passkey login -------------------------------------------------------------------------


@router.post("/passkey/login/begin", response_model=PublicKeyOptionsOut)
async def passkey_login_begin(session: SessionDep) -> PublicKeyOptionsOut:
    options = await auth_service.begin_passkey_login(session)
    return PublicKeyOptionsOut(public_key=options)


@router.post("/passkey/login/complete", response_model=SessionOut)
async def passkey_login_complete(
    payload: PasskeyLoginCompleteIn,
    session: SessionDep,
    request: Request,
    response: Response,
) -> SessionOut:
    user = await auth_service.complete_passkey_login(session, payload.credential)
    await create_session(session, user, request=request, response=response)
    return SessionOut(user=AuthUser.model_validate(user))


# --- Passkey management --------------------------------------------------------------------


@router.get("/passkeys", response_model=list[PasskeyOut])
async def list_passkeys(user: CurrentUser) -> list[PasskeyOut]:
    ordered = sorted(user.credentials, key=lambda c: c.created_at)
    return [PasskeyOut.model_validate(cred) for cred in ordered]


@router.post("/passkey/register/begin", response_model=PublicKeyOptionsOut)
async def passkey_register_begin(session: SessionDep, user: CurrentUser) -> PublicKeyOptionsOut:
    options = await auth_service.begin_passkey_registration(session, user=user)
    return PublicKeyOptionsOut(public_key=options)


@router.post("/passkey/register/complete", response_model=PasskeyOut)
async def passkey_register_complete(
    payload: PasskeyRegisterCompleteIn,
    session: SessionDep,
    request: Request,
    user: CurrentUser,
) -> PasskeyOut:
    credential = await auth_service.complete_passkey_registration(
        session,
        payload.credential,
        user=user,
        nickname=payload.nickname,
        user_agent=request.headers.get("user-agent"),
    )
    return PasskeyOut.model_validate(credential)


@router.patch("/passkeys/{credential_id}", response_model=PasskeyOut)
async def rename_passkey(
    credential_id: str,
    payload: PasskeyRenameIn,
    session: SessionDep,
    user: CurrentUser,
) -> PasskeyOut:
    credential = await session.get(Credential, credential_id)
    if credential is None or credential.user_id != user.id:
        raise NotFound("Passkey", "PASSKEY_NOT_FOUND")
    credential.nickname = payload.nickname.strip()
    await session.flush()
    return PasskeyOut.model_validate(credential)


@router.delete("/passkeys/{credential_id}", response_model=Ok)
async def delete_passkey(credential_id: str, session: SessionDep, user: CurrentUser) -> Ok:
    await auth_service.delete_passkey(session, user, credential_id)
    return Ok()


# --- Invites -------------------------------------------------------------------------------


@router.get("/invite/{token}", response_model=InviteInfoOut)
async def read_invite(token: str, session: SessionDep) -> InviteInfoOut:
    invite = await auth_service.validate_invite_token(session, token)
    site_name = await SettingsService(session).get_str("site.name", "dramatarr")
    return InviteInfoOut(
        email=invite.email,
        role=invite.role,
        site_name=site_name,
        expires_at=invite.expires_at,
    )


@router.post("/invite/{token}/begin", response_model=PublicKeyOptionsOut)
async def invite_register_begin(token: str, session: SessionDep) -> PublicKeyOptionsOut:
    invite = await auth_service.validate_invite_token(session, token)
    options = await auth_service.begin_passkey_registration(session, invite=invite)
    return PublicKeyOptionsOut(public_key=options)


@router.post("/invite/{token}/complete", response_model=SessionOut)
async def invite_register_complete(
    token: str,
    payload: InviteCompleteIn,
    session: SessionDep,
    request: Request,
    response: Response,
) -> SessionOut:
    invite = await auth_service.validate_invite_token(session, token)
    credential = await auth_service.complete_passkey_registration(
        session,
        payload.credential,
        invite=invite,
        nickname=payload.nickname,
        user_agent=request.headers.get("user-agent"),
        display_name=payload.display_name,
        locale=payload.locale,
    )
    user = await session.get(User, credential.user_id)
    if user is None:  # the account was created two statements ago; this cannot happen
        raise AppError("INVITE_COMPLETE_FAILED", "Account creation did not persist", 500)

    await create_session(session, user, request=request, response=response)
    return SessionOut(user=AuthUser.model_validate(user))


# --- Magic links ---------------------------------------------------------------------------


@router.post("/magic-link/request", status_code=status.HTTP_204_NO_CONTENT)
async def magic_link_request(payload: MagicLinkRequestIn, session: SessionDep) -> None:
    email = auth_service.normalize_email(payload.email)
    if not await _limiter.check(email):
        raise AppError(
            "RATE_LIMITED",
            "Too many sign-in link requests; try again shortly",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
    await auth_service.request_magic_link(session, email)


@router.post("/magic-link/consume", response_model=SessionOut)
async def magic_link_consume(
    payload: MagicLinkConsumeIn,
    session: SessionDep,
    request: Request,
    response: Response,
) -> SessionOut:
    user = await auth_service.consume_magic_link(session, payload.token)
    await create_session(session, user, request=request, response=response)
    return SessionOut(user=AuthUser.model_validate(user))


# --- Sign in with Google ---------------------------------------------------------------------


def _browser_binding(request: Request) -> str:
    """The value that ties an authorization round trip to *this* browser.

    Reused when the browser already carries one, so that two tabs starting sign-in at the
    same time do not invalidate each other's state; minted otherwise. The bounds are a sanity
    check on a client-supplied string, not a security property — the security property is that
    the server compares this against the hash it stored.
    """
    existing = request.cookies.get(oauth_service.BROWSER_COOKIE_NAME)
    if existing and 32 <= len(existing) <= 128:
        return existing
    return new_token(32)


def _remember_browser(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=oauth_service.BROWSER_COOKIE_NAME,
        value=token,
        max_age=oauth_service.BROWSER_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.use_secure_cookie,
        # `Strict` would be dropped on Google's redirect back here, which is the one request
        # this cookie exists for. `Lax` is sent on a top-level GET navigation, which is
        # exactly what that redirect is.
        samesite="lax",
        path=oauth_service.BROWSER_COOKIE_PATH,
    )


def _google_failure(code: str) -> RedirectResponse:
    """Every failed callback lands on the SPA's login screen with a stable code.

    The browser is the client here, so there is nobody to read a JSON error body. The code is
    all the UI needs: it maps it to a localized message exactly as it does for every other
    `AppError`.
    """
    return RedirectResponse(
        f"{oauth_service.LOGIN_PATH}?error={quote(code, safe='')}",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/google/start", response_model=GoogleAuthUrlOut)
async def google_start(
    session: SessionDep,
    request: Request,
    response: Response,
    next_path: NextQuery = None,
    invite: Annotated[str | None, Query(max_length=512)] = None,
) -> GoogleAuthUrlOut:
    binding = _browser_binding(request)
    url = await oauth_service.start_login(
        session, browser_token=binding, next_path=next_path, invite_token=invite
    )
    # Only once the state actually exists: a refused start must not leave a cookie behind.
    _remember_browser(response, binding)
    return GoogleAuthUrlOut(authorization_url=url)


@router.get("/google/callback")
async def google_callback(
    session: SessionDep,
    request: Request,
    current: OptionalUser,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=128)] = None,
    error: Annotated[str | None, Query(max_length=128)] = None,
) -> RedirectResponse:
    """Google's redirect target. Always a 302, never a body, never a token in the URL."""
    try:
        result = await oauth_service.complete_callback(
            session,
            code=code,
            state=state,
            error=error,
            current_user=current,
            # Present only in the browser that ran `/google/start`, which is what stops this
            # URL from working as a link an attacker can hand to somebody else.
            browser_token=request.cookies.get(oauth_service.BROWSER_COOKIE_NAME),
        )
    except AppError as exc:
        # Deliberately *not* rolled back: whatever the outcome, the state row this callback
        # burned must stay burned, or a replay would get a second attempt at it.
        return _google_failure(exc.code)
    except Exception:
        log.exception("Google callback failed unexpectedly")
        await session.rollback()
        return _google_failure("INTERNAL_ERROR")

    response = RedirectResponse(result.next_path, status_code=status.HTTP_302_FOUND)
    if result.user is not None:
        await create_session(session, result.user, request=request, response=response)
    return response


@router.post("/google/link/start", response_model=GoogleAuthUrlOut)
async def google_link_start(
    session: SessionDep,
    request: Request,
    response: Response,
    user: CurrentUser,
    next_path: NextQuery = None,
) -> GoogleAuthUrlOut:
    binding = _browser_binding(request)
    url = await oauth_service.start_link(
        session, user, browser_token=binding, next_path=next_path
    )
    _remember_browser(response, binding)
    return GoogleAuthUrlOut(authorization_url=url)


@router.delete("/google/link", response_model=Ok)
async def google_unlink(session: SessionDep, user: CurrentUser) -> Ok:
    await oauth_service.unlink_google(session, user)
    return Ok()


@router.get("/methods", response_model=SignInMethodsOut)
async def sign_in_methods(session: SessionDep, user: CurrentUser) -> SignInMethodsOut:
    methods = await oauth_service.sign_in_methods(session, user)
    return SignInMethodsOut(
        passkeys=methods.passkeys,
        google_linked=methods.google_linked,
        google_email=methods.google_email,
        magic_link_enabled=methods.magic_link_enabled,
    )


__all__ = ["router"]
