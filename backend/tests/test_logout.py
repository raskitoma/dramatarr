"""Signing out, end to end through a real cookie jar.

There were **no logout tests at all**, which is how a report of "al cerrar sesión se queda en la
última pantalla" arrives with nothing to check it against. Every other auth test overrides the
`CurrentUser` dependency, so none of them ever exercises the cookie — and the cookie is the
whole mechanism.

Two properties are defended:

**The session really ends.** Revoking the row is not enough on its own and neither is clearing
the cookie; the endpoint the client asks after signing out has to answer "nobody".

**The `Set-Cookie` that clears it matches the one that set it.** A deletion is matched by name,
domain and path, and `Response.delete_cookie` defaults `secure` to `False` while `create_session`
sets it from `use_secure_cookie` — which is `True` on this deployment, because the public URL is
https. Asserting the attributes keeps that pair from drifting apart silently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AuthSession, User

pytestmark = pytest.mark.anyio


@pytest.fixture
async def http(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    """A client with a real cookie jar and no dependency overrides."""
    from app.api.deps import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def sign_in(db: AsyncSession, http: AsyncClient, user: User) -> str:
    """Mint a real session the way a login does, and put its cookie in the jar."""
    from starlette.requests import Request
    from starlette.responses import Response

    from app.api.deps import create_session

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"user-agent", b"pytest")],
        "client": ("127.0.0.1", 1234),
    }
    response = Response()
    await create_session(db, user, request=Request(scope), response=response)
    await db.commit()

    raw = response.headers["set-cookie"]
    name = get_settings().session_cookie_name
    value = raw.split(";", 1)[0].split("=", 1)[1]
    http.cookies.set(name, value)
    return value


async def test_signing_out_ends_the_session(
    db: AsyncSession, http: AsyncClient, admin_user: User
) -> None:
    await sign_in(db, http, admin_user)

    before = await http.get("/api/auth/session")
    assert before.status_code == 200
    assert (before.json().get("user") or {}).get("id") == admin_user.id

    out = await http.post("/api/auth/logout")
    assert out.status_code == 200

    # The client asks this immediately after signing out, and everything downstream — the
    # redirect to /login included — hangs off the answer.
    after = await http.get("/api/auth/session")
    assert after.status_code == 200
    assert after.json().get("user") is None


async def test_the_row_is_revoked_not_merely_forgotten(
    db: AsyncSession, http: AsyncClient, admin_user: User
) -> None:
    """A cookie a browser kept a copy of must not still work."""
    raw_value = await sign_in(db, http, admin_user)
    await http.post("/api/auth/logout")

    rows = (await db.execute(select(AuthSession))).scalars().all()
    assert rows and all(row.revoked_at is not None for row in rows)

    # Replaying the stolen cookie gets nothing.
    http.cookies.set(get_settings().session_cookie_name, raw_value)
    replay = await http.get("/api/auth/session")
    assert replay.json().get("user") is None


async def test_the_clearing_cookie_matches_the_one_that_was_set(
    db: AsyncSession, http: AsyncClient, admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attribute pair that has to stay in step.

    `create_session` sets the cookie `Secure` whenever the public URL is https, and
    `delete_cookie` defaults to `secure=False`. A browser matches a deletion on name, domain and
    path, so this is not the bug on its own — but a mismatched pair is a trap for whoever next
    adds a `domain` or moves the path, and the failure mode is a session that looks ended and is
    not.
    """
    settings = get_settings()
    monkeypatch.setattr(type(settings), "use_secure_cookie", property(lambda _self: True))

    await sign_in(db, http, admin_user)
    out = await http.post("/api/auth/logout")

    cleared = out.headers.get("set-cookie", "")
    assert settings.session_cookie_name in cleared
    assert "Path=/" in cleared
    assert "Secure" in cleared, f"the clearing cookie dropped Secure: {cleared}"
    assert "HttpOnly" in cleared, f"the clearing cookie dropped HttpOnly: {cleared}"
