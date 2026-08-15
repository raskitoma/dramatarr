"""Sign in with Google, driven end to end against a real OpenID provider.

`test_google_auth.py` proves the *logic* by replacing `oauth._fetch_json`, which is the right
tool for asserting that one malformed claim is refused. This file proves the *integration*: a
real OpenID Connect issuer listens on a real socket, and every outbound call the app makes —
discovery, JWKS, the token exchange — leaves the process over HTTP and comes back parsed.
Nothing in `app.services.oauth` is stubbed. `DISCOVERY_URL` is repointed and the caches are
dropped; from there the app finds the authorization endpoint, the signing keys and the token
endpoint entirely on its own, exactly as it would against Google.

The fake issuer is strict on purpose. It refuses a token request whose `code_verifier` does
not hash to the `code_challenge` it was given, whose `client_secret` is wrong, whose
`redirect_uri` does not match the authorization request, or whose code it has already
redeemed. So a PKCE or client-authentication regression fails here as a refused exchange
rather than passing unnoticed the way it would against a lenient double.

The browser leg is simulated the way a browser behaves: an unredirected GET to the
authorization URL, then the `code` and `state` are read out of the `Location` header and
handed to the callback. Session cookies are carried by hand, so what is under test is the
real cookie the app set and the real dependency that reads it — `get_current_user` is never
overridden here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import hash_token
from app.config import get_settings
from app.models import AuthSession, Invite, OAuthIdentity, OAuthState, User, UserRole
from app.services import auth as auth_service
from app.services import oauth as oauth_service
from app.services.settings import SettingsService
from app.utils.ids import new_token
from app.utils.time import in_days, utcnow

pytestmark = pytest.mark.anyio

CLIENT_ID = "integration-client.apps.googleusercontent.com"
CLIENT_SECRET = "integration-client-secret-never-logged"
ACCESS_TOKEN = "ya29.integration-access-token-must-never-escape"
KID = "integration-kid-1"

#: The `iss` a token must carry. The fake issuer answers on 127.0.0.1 but speaks as Google,
#: because `VALID_ISSUERS` is a hard-coded allowlist and repointing it would weaken the test.
ISSUER = "https://accounts.google.com"

#: Generated once. RSA keygen dominates the runtime of this file otherwise.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# --- A real OpenID provider on a real socket -------------------------------------------------


class FakeGoogle:
    """State and policy for the issuer. The HTTP handler below is a thin shell over this."""

    def __init__(self) -> None:
        self.origin = ""
        #: What the next consent screen asserts. Tests rewrite it between round trips.
        self.sub = "google-sub-integration"
        self.email = "viewer@example.com"
        self.name: str | None = "Google Viewer"
        self.email_verified: Any = True
        #: When set, the id_token is bound to this instead of the nonce the app sent. Nothing
        #: else about the token changes, so it is a real, correctly-signed token answering a
        #: different sign-in attempt — the shape a replayed token would have.
        self.forge_nonce: str | None = None
        #: code -> the authorization request it answers. Popped on redemption, so a code is
        #: usable exactly once.
        self.codes: dict[str, dict[str, Any]] = {}
        #: Every request the app made, so a test can assert discovery and JWKS really happened.
        self.hits: list[str] = []
        #: The authorization requests seen, for asserting on PKCE and prompt parameters.
        self.authorizations: list[dict[str, str]] = []
        #: Token requests that were refused, with the reason.
        self.refusals: list[str] = []

    # --- Documents ---------------------------------------------------------------------

    def discovery(self) -> dict[str, str]:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": f"{self.origin}/o/oauth2/v2/auth",
            "token_endpoint": f"{self.origin}/token",
            "jwks_uri": f"{self.origin}/oauth2/v3/certs",
            "userinfo_endpoint": f"{self.origin}/userinfo",
        }

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        numbers = _KEY.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "alg": "RS256",
                    "use": "sig",
                    "kid": KID,
                    "n": b64u(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                    "e": b64u(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
                }
            ]
        }

    def id_token(self, *, nonce: str) -> str:
        now = int(utcnow().timestamp())
        header = {"alg": "RS256", "kid": KID, "typ": "JWT"}
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": self.sub,
            "email": self.email,
            "email_verified": self.email_verified,
            "nonce": nonce,
            "iat": now,
            "exp": now + 3600,
        }
        if self.name is not None:
            payload["name"] = self.name
        signing_input = (
            f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(payload).encode())}"
        )
        signature = _KEY.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{b64u(signature)}"

    # --- Endpoints ---------------------------------------------------------------------

    def authorize(self, query: dict[str, str]) -> tuple[int, dict[str, str], str]:
        """The consent screen, minus the human. Returns (status, headers, body)."""
        self.authorizations.append(query)
        redirect = query.get("redirect_uri", "")
        state = query.get("state", "")

        def bounce(error: str) -> tuple[int, dict[str, str], str]:
            if not redirect:
                return 400, {"Content-Type": "application/json"}, json.dumps({"error": error})
            params = {"error": error}
            if state:
                params["state"] = state
            return 302, {"Location": f"{redirect}?{urlencode(params)}"}, ""

        if query.get("client_id") != CLIENT_ID:
            return bounce("unauthorized_client")
        if query.get("response_type") != "code":
            return bounce("unsupported_response_type")
        if "openid" not in query.get("scope", "").split():
            return bounce("invalid_scope")
        challenge = query.get("code_challenge")
        if not challenge or query.get("code_challenge_method") != "S256":
            return bounce("invalid_request")

        code = f"code-{new_token(12)}"
        self.codes[code] = {
            "nonce": query.get("nonce", ""),
            "challenge": challenge,
            "redirect_uri": redirect,
            "client_id": query["client_id"],
        }
        return 302, {"Location": f"{redirect}?{urlencode({'code': code, 'state': state})}"}, ""

    def token(self, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        def refuse(error: str) -> tuple[int, dict[str, Any]]:
            self.refusals.append(error)
            return 400, {"error": error}

        if form.get("grant_type") != "authorization_code":
            return refuse("unsupported_grant_type")
        # Client authentication. A regression that stopped sending the secret would otherwise
        # look identical to a working exchange.
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return refuse("invalid_client")

        record = self.codes.pop(form.get("code", ""), None)
        if record is None:
            return refuse("invalid_grant")
        if form.get("redirect_uri") != record["redirect_uri"]:
            return refuse("invalid_grant")

        verifier = form.get("code_verifier", "")
        expected = b64u(hashlib.sha256(verifier.encode("ascii")).digest())
        if not verifier or expected != record["challenge"]:
            return refuse("invalid_grant")

        return 200, {
            "access_token": ACCESS_TOKEN,
            "expires_in": 3599,
            "scope": "openid email profile",
            "token_type": "Bearer",
            "id_token": self.id_token(nonce=self.forge_nonce or record["nonce"]),
        }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def _google(self) -> FakeGoogle:
        return self.server.google  # type: ignore[attr-defined]

    def log_message(self, *_args: Any) -> None:  # keep pytest output readable
        return

    def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, {"Content-Type": "application/json"}, json.dumps(payload).encode())

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's own spelling
        parsed = urlsplit(self.path)
        query = dict(parse_qsl(parsed.query))
        google = self._google

        if parsed.path == "/.well-known/openid-configuration":
            google.hits.append("discovery")
            return self._json(200, google.discovery())
        if parsed.path == "/oauth2/v3/certs":
            google.hits.append("jwks")
            return self._json(200, google.jwks())
        if parsed.path == "/o/oauth2/v2/auth":
            google.hits.append("authorize")
            status, headers, body = google.authorize(query)
            return self._send(status, headers, body.encode())
        return self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = dict(parse_qsl(self.rfile.read(length).decode("utf-8")))

        if parsed.path == "/token":
            self._google.hits.append("token")
            status, payload = self._google.token(form)
            return self._json(status, payload)
        return self._json(404, {"error": "not_found"})


@pytest.fixture(scope="module")
def issuer() -> Iterator[FakeGoogle]:
    google = FakeGoogle()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.google = google  # type: ignore[attr-defined]
    google.origin = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield google
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def google(issuer: FakeGoogle, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    """Repoint discovery and drop the caches. Nothing else about the module is touched."""
    monkeypatch.setattr(
        oauth_service, "DISCOVERY_URL", f"{issuer.origin}/.well-known/openid-configuration"
    )
    oauth_service.reset_caches()
    issuer.hits.clear()
    issuer.authorizations.clear()
    issuer.refusals.clear()
    issuer.codes.clear()
    issuer.sub = "google-sub-integration"
    issuer.email = "viewer@example.com"
    issuer.name = "Google Viewer"
    issuer.email_verified = True
    issuer.forge_nonce = None
    yield issuer
    oauth_service.reset_caches()


# --- The app ---------------------------------------------------------------------------------


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real app, with only the database session shared with the test.

    `get_current_user` is deliberately *not* overridden: the session cookie the callback sets
    is the thing under test, so it has to travel through the real dependency.
    """
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        # Mirrors the production dependency, so a burned state row is really committed.
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def second_browser(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """A different browser against the same app: its own cookie jar, nothing shared.

    Depends on `client` so the dependency override is already installed and both talk to the
    same database — the two really are two browsers, not two deployments.
    """
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
async def google_on(db: AsyncSession) -> None:
    svc = SettingsService(db)
    await svc.set("oauth.google.enabled", True)
    await svc.set("oauth.google.client_id", CLIENT_ID)
    await svc.set("oauth.google.client_secret", CLIENT_SECRET)
    await db.commit()


COOKIE = get_settings().session_cookie_name


async def hydrated(db: AsyncSession, user_id: str) -> User:
    """Re-read a user the way a request does, with the eager collections populated."""
    return (
        await db.execute(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def sign_in_directly(db: AsyncSession, user: User) -> str:
    """A real session row and its raw cookie value, minted without going through Google.

    Linking needs a browser that is *already* signed in by some other method; this is that
    browser. It uses the same table and the same hash the login routes use.
    """
    raw = new_token(32)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=in_days(30),
        )
    )
    await db.commit()
    return raw


def auth(client: AsyncClient, session: str | None = None) -> dict[str, str]:
    """A `Cookie` header carrying the whole browser, not just the session.

    httpx fills a request's cookies in from its jar only when the caller did *not* set a
    `Cookie` header of their own — and `/google/start` sets a browser-binding cookie that
    lives in exactly that jar. Merging by hand is what lets one request present both, which
    is what a real browser does and what the binding check now requires.
    """
    cookies = {c.name: c.value or "" for c in client.cookies.jar}
    if session:
        cookies[COOKIE] = session
    return {"cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}


# --- Browser leg -------------------------------------------------------------------------------


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


async def consent(authorization_url: str) -> dict[str, str]:
    """Follow the authorization URL the way a browser would, and stop at the redirect back.

    A real HTTP request to the real issuer, unredirected — what comes back is the query the
    browser would then deliver to `/api/auth/google/callback`.
    """
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as browser:
        response = await browser.get(authorization_url)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(oauth_service.redirect_uri()), location
    return query_of(location)


async def start(client: AsyncClient, **params: str) -> str:
    response = await client.get("/api/auth/google/start", params=params)
    assert response.status_code == 200, response.text
    return str(response.json()["authorizationUrl"])


async def callback(client: AsyncClient, params: dict[str, str], cookie: str | None = None) -> Any:
    return await client.get(
        "/api/auth/google/callback", params=params, headers=auth(client, cookie)
    )


async def round_trip(
    client: AsyncClient, cookie: str | None = None, **params: str
) -> Any:
    """start -> authorization URL -> consent -> callback. The whole login leg."""
    return await callback(client, await consent(await start(client, **params)), cookie)


def redirected_to(response: Any) -> str:
    assert response.status_code == 302, f"expected 302, got {response.status_code}"
    return str(response.headers["location"])


def error_of(response: Any) -> str:
    location = redirected_to(response)
    assert location.startswith("/login?"), location
    return query_of(location)["error"]


def session_cookie(response: Any) -> str:
    raw = response.headers.get("set-cookie", "")
    assert raw.startswith(f"{COOKIE}="), raw
    return raw.split(";", 1)[0].split("=", 1)[1]


async def count(db: AsyncSession, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def invite_for(db: AsyncSession, email: str, role: str = UserRole.VIEWER) -> str:
    created = await auth_service.create_invite(db, email=email, role=role)
    await db.commit()
    return created.token


# --- 1. The whole flow, over real HTTP -----------------------------------------------------------


async def test_discovery_jwks_and_token_all_travel_over_http(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    """The headline: nothing is stubbed, and every leg is observed at the issuer."""
    response = await round_trip(client)

    assert redirected_to(response) == "/"
    assert google.hits == ["discovery", "authorize", "jwks", "token"] or google.hits == [
        "discovery",
        "authorize",
        "token",
        "jwks",
    ], google.hits
    assert google.refusals == []


async def test_authorization_url_is_built_from_the_discovered_endpoint(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    url = await start(client, next="/library")

    assert url.startswith(f"{google.origin}/o/oauth2/v2/auth?")
    params = query_of(url)
    assert params["client_id"] == CLIENT_ID
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    assert params["access_type"] == "online"
    assert params["prompt"] == "select_account"
    assert "openid" in params["scope"].split()
    assert params["redirect_uri"] == oauth_service.redirect_uri()
    # PKCE: the challenge on the wire is the hash, never the verifier.
    assert len(params["code_challenge"]) >= 43


async def test_pkce_verifier_is_checked_by_the_issuer(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    """Proves the exchange really carries a verifier that hashes to the challenge.

    The issuer refuses otherwise, so this test would fail as `GOOGLE_EXCHANGE_FAILED` if the
    app ever stopped sending the right one.
    """
    url = await start(client)
    challenge = query_of(url)["code_challenge"]
    state_row = await oauth_service.peek_state(db, query_of(url)["state"])
    assert state_row is not None
    assert oauth_service.code_challenge(state_row.code_verifier) == challenge

    response = await callback(client, await consent(url))
    assert redirected_to(response) == "/"
    assert google.refusals == []


async def test_session_cookie_signs_the_browser_in_for_real(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    response = await round_trip(client, next="/library")
    assert redirected_to(response) == "/library"

    raw = session_cookie(response)
    assert "httponly" in response.headers["set-cookie"].lower()

    who = await client.get("/api/auth/session", headers=auth(client, raw))
    assert who.status_code == 200
    assert who.json()["user"]["email"] == "viewer@example.com"

    stored = (
        await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    assert stored is not None and stored.user_id == viewer_user.id
    # The raw token is never what is stored.
    assert (
        await db.execute(select(AuthSession).where(AuthSession.token_hash == raw))
    ).scalar_one_or_none() is None


async def test_start_binds_the_round_trip_to_this_browser(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    response = await client.get("/api/auth/google/start")
    assert response.status_code == 200

    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{oauth_service.BROWSER_COOKIE_NAME}="), raw
    lowered = raw.lower()
    assert "httponly" in lowered, raw
    # `Strict` would be dropped on Google's top-level redirect back here, which is the one
    # request this cookie exists for.
    assert "samesite=lax" in lowered, raw
    assert f"path={oauth_service.BROWSER_COOKIE_PATH}" in lowered, raw


async def test_login_csrf_a_callback_delivered_to_another_browser_is_refused(
    client: AsyncClient,
    second_browser: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer_user: User,
) -> None:
    """RFC 6819 §4.4.1.8, over real HTTP and real cookie jars.

    The attacker authorizes at Google in their own browser, stops before following the last
    redirect, and mails the victim the resulting callback link. If `state` were only a
    server-side row, the victim's browser would be signed in *as the attacker* and everything
    they went on to watch, resume or import would land in the attacker's account.
    """
    params = await consent(await start(client))  # the attacker's browser starts the flow

    stolen = await second_browser.get(
        "/api/auth/google/callback", params=params, headers=auth(second_browser)
    )

    assert "set-cookie" not in stolen.headers, "a foreign browser was handed a session"
    assert error_of(stolen) == "GOOGLE_STATE_INVALID"
    assert await count(db, OAuthIdentity) == 0

    # The refusal is targeted, not a burn: the binding is checked before the row is consumed,
    # so a stranger presenting a state cannot also destroy the legitimate attempt behind it.
    # The browser that actually started the flow still completes normally.
    legitimate = await callback(client, params)
    assert redirected_to(legitimate) == "/"
    assert "set-cookie" in legitimate.headers


# --- 2. Account resolution, all four outcomes ---------------------------------------------------


async def test_path_two_links_an_existing_account_by_verified_email(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    before = await count(db, User)
    google.email = "viewer@example.com"
    google.sub = "sub-path-two"

    response = await round_trip(client)

    assert redirected_to(response) == "/"
    assert await count(db, User) == before  # matched, never created
    identity = (
        await db.execute(select(OAuthIdentity).where(OAuthIdentity.provider_sub == "sub-path-two"))
    ).scalar_one()
    assert identity.user_id == viewer_user.id
    assert identity.email == "viewer@example.com"


async def test_path_one_reuses_the_existing_link_on_the_second_sign_in(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    google.sub = "sub-returning"
    await round_trip(client)
    assert await count(db, OAuthIdentity) == 1

    # The address changed at Google; `sub` did not. Resolution keys off `sub`, so this is the
    # same person and must not create a second identity or a second account.
    google.email = "renamed@example.com"
    response = await round_trip(client)

    assert redirected_to(response) == "/"
    assert await count(db, OAuthIdentity) == 1
    assert await count(db, User) == 1
    identity = (await db.execute(select(OAuthIdentity))).scalar_one()
    assert identity.user_id == viewer_user.id
    assert identity.last_used_at is not None


async def test_path_three_accepts_an_invitation_for_the_same_verified_address(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    token = await invite_for(db, "newcomer@example.com", role=UserRole.ADMIN)
    google.email = "newcomer@example.com"
    google.sub = "sub-invited"
    google.name = "New Comer"

    response = await round_trip(client, invite=token)

    assert redirected_to(response) == "/"
    created = (
        await db.execute(select(User).where(User.email == "newcomer@example.com"))
    ).scalar_one()
    assert created.role == UserRole.ADMIN  # the invitation's role, not a default
    assert created.display_name == "New Comer"

    invite = (await db.execute(select(Invite))).scalar_one()
    assert invite.accepted_at is not None and invite.accepted_user_id == created.id

    identity = (await db.execute(select(OAuthIdentity))).scalar_one()
    assert identity.user_id == created.id

    # And the browser really is signed in as the brand-new account.
    who = await client.get("/api/auth/session", headers=auth(client, session_cookie(response)))
    assert who.json()["user"]["email"] == "newcomer@example.com"


async def test_path_four_refuses_and_writes_nothing(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    google.email = "stranger@example.com"
    google.sub = "sub-stranger"

    response = await round_trip(client)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    assert "set-cookie" not in response.headers
    assert await count(db, User) == 0
    assert await count(db, OAuthIdentity) == 0


async def test_an_invitation_for_a_different_address_does_not_enrol_anyone(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """The case that would turn one forwarded invite link into an open door."""
    token = await invite_for(db, "intended@example.com")
    google.email = "someone.else@example.com"
    google.sub = "sub-forwarded"

    response = await round_trip(client, invite=token)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    assert await count(db, User) == 0
    invite = (await db.execute(select(Invite))).scalar_one()
    assert invite.accepted_at is None  # still usable by the person it was addressed to


async def test_google_account_mismatch_when_the_user_already_answers_to_another_google(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    user = await hydrated(db, viewer_user.id)
    user.oauth_identities.append(
        OAuthIdentity(provider="google", provider_sub="sub-original", email="viewer@example.com")
    )
    await db.commit()

    google.sub = "sub-second-google"
    google.email = "viewer@example.com"
    response = await round_trip(client)

    assert error_of(response) == "GOOGLE_ACCOUNT_MISMATCH"
    assert await count(db, OAuthIdentity) == 1
    assert (await db.execute(select(OAuthIdentity))).scalar_one().provider_sub == "sub-original"


async def test_unverified_email_is_refused(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    google.email_verified = "true"  # a string is not an assertion
    response = await round_trip(client)

    assert error_of(response) == "GOOGLE_EMAIL_UNVERIFIED"
    assert await count(db, OAuthIdentity) == 0


async def test_a_correctly_signed_token_for_a_different_attempt_is_refused(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    """The nonce binding, exercised where it could actually be missed.

    Everything else about this token is genuine — real issuer, real signature, real audience,
    unexpired. Only the `nonce` answers a different authorization request, which is exactly
    the shape of a token captured from another flow and replayed into this one.
    """
    google.forge_nonce = "a-nonce-from-somewhere-else"

    response = await round_trip(client)

    assert error_of(response) == "GOOGLE_TOKEN_INVALID"
    assert "set-cookie" not in response.headers
    assert await count(db, OAuthIdentity) == 0


async def test_a_deactivated_account_cannot_sign_in_with_google(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    viewer_user.is_active = False
    await db.commit()

    response = await round_trip(client)
    assert error_of(response) == "ACCOUNT_DISABLED"
    assert "set-cookie" not in response.headers


async def test_invites_switched_off_falls_through_to_the_refusal(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    await SettingsService(db).set("oauth.google.allow_invites", False)
    token = await invite_for(db, "newcomer@example.com")
    google.email = "newcomer@example.com"
    google.sub = "sub-invited-but-off"

    response = await round_trip(client, invite=token)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    assert await count(db, User) == 0


# --- 3. Linking and unlinking from a signed-in session ------------------------------------------


async def test_link_flow_attaches_google_to_the_signed_in_account(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, admin_user: User
) -> None:
    cookie = await sign_in_directly(db, admin_user)

    started = await client.post(
        "/api/auth/google/link/start", params={"next": "/account"}, headers=auth(client, cookie)
    )
    assert started.status_code == 200, started.text
    google.email = "admin@example.com"
    google.sub = "sub-admin-link"

    response = await callback(
        client, await consent(started.json()["authorizationUrl"]), cookie
    )

    assert redirected_to(response) == "/account"
    # A link runs inside an existing session, so it must not mint a second one.
    assert "set-cookie" not in response.headers

    identity = (await db.execute(select(OAuthIdentity))).scalar_one()
    assert identity.user_id == admin_user.id

    methods = await client.get("/api/auth/methods", headers=auth(client, cookie))
    assert methods.status_code == 200
    body = methods.json()
    assert body["googleLinked"] is True
    assert body["googleEmail"] == "admin@example.com"


async def test_a_link_callback_replayed_in_another_browser_is_refused(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, admin_user: User
) -> None:
    """Without this the callback would attach the admin's Google account to whoever opened it."""
    cookie = await sign_in_directly(db, admin_user)
    started = await client.post(
        "/api/auth/google/link/start", headers=auth(client, cookie)
    )
    google.email = "admin@example.com"
    google.sub = "sub-admin-hijack"

    # Same code and state, delivered by a browser holding no session at all.
    response = await callback(client, await consent(started.json()["authorizationUrl"]))

    assert error_of(response) == "GOOGLE_STATE_INVALID"
    assert await count(db, OAuthIdentity) == 0


async def test_a_login_state_cannot_be_redeemed_as_a_link(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, admin_user: User
) -> None:
    """A login state carries no account; completing it while signed in must still be a login."""
    cookie = await sign_in_directly(db, admin_user)
    google.email = "admin@example.com"
    google.sub = "sub-login-not-link"

    response = await round_trip(client, cookie)

    # It resolved as a login for the same person, which is fine — what matters is that the
    # link-only rules were not applied to a login state.
    assert redirected_to(response) == "/"
    assert "set-cookie" in response.headers


async def test_linking_a_google_account_already_used_by_someone_else(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    admin_user: User,
    viewer_user: User,
) -> None:
    viewer = await hydrated(db, viewer_user.id)
    viewer.oauth_identities.append(
        OAuthIdentity(provider="google", provider_sub="sub-taken", email="viewer@example.com")
    )
    await db.commit()

    cookie = await sign_in_directly(db, admin_user)
    started = await client.post("/api/auth/google/link/start", headers=auth(client, cookie))
    google.sub = "sub-taken"
    google.email = "viewer@example.com"

    response = await callback(client, await consent(started.json()["authorizationUrl"]), cookie)

    assert error_of(response) == "GOOGLE_ACCOUNT_IN_USE"
    assert await count(db, OAuthIdentity) == 1


async def test_unlink_removes_the_identity(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    google.sub = "sub-to-unlink"
    await round_trip(client)
    assert await count(db, OAuthIdentity) == 1

    cookie = await sign_in_directly(db, viewer_user)
    response = await client.delete("/api/auth/google/link", headers=auth(client, cookie))

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert await count(db, OAuthIdentity) == 0

    methods = await client.get("/api/auth/methods", headers=auth(client, cookie))
    assert methods.json()["googleLinked"] is False


async def test_unlink_refuses_when_google_is_the_only_way_in(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    await SettingsService(db).set("auth.magic_link_enabled", False)
    await db.commit()
    google.sub = "sub-only-way-in"
    await round_trip(client)

    cookie = await sign_in_directly(db, viewer_user)
    response = await client.delete("/api/auth/google/link", headers=auth(client, cookie))

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "LAST_SIGN_IN_METHOD"
    assert await count(db, OAuthIdentity) == 1


async def test_unlink_when_nothing_is_linked(
    client: AsyncClient, db: AsyncSession, viewer_user: User
) -> None:
    cookie = await sign_in_directly(db, viewer_user)
    response = await client.delete("/api/auth/google/link", headers=auth(client, cookie))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GOOGLE_NOT_LINKED"


async def test_link_start_requires_a_session(client: AsyncClient, google_on: None) -> None:
    assert (await client.post("/api/auth/google/link/start")).status_code == 401
    assert (await client.get("/api/auth/methods")).status_code == 401
    assert (await client.delete("/api/auth/google/link")).status_code == 401


# --- 4. The public config switch ------------------------------------------------------------------


async def test_config_google_enabled_flips_with_the_setting(
    client: AsyncClient, db: AsyncSession
) -> None:
    svc = SettingsService(db)

    assert (await client.get("/api/config")).json()["googleEnabled"] is False

    # Switched on but unaddressable: still reported off, or the button dead-ends at Google.
    await svc.set("oauth.google.enabled", True)
    await db.commit()
    assert (await client.get("/api/config")).json()["googleEnabled"] is False

    await svc.set("oauth.google.client_id", CLIENT_ID)
    await db.commit()
    assert (await client.get("/api/config")).json()["googleEnabled"] is True

    await svc.set("oauth.google.enabled", False)
    await db.commit()
    assert (await client.get("/api/config")).json()["googleEnabled"] is False


async def test_start_and_callback_refuse_while_disabled(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    """A state minted before the admin switched Google off must not still complete."""
    url = await start(client)
    params = await consent(url)

    await SettingsService(db).set("oauth.google.enabled", False)
    await db.commit()

    assert error_of(await callback(client, params)) == "GOOGLE_DISABLED"
    started = await client.get("/api/auth/google/start")
    assert started.status_code == 403
    assert started.json()["error"]["code"] == "GOOGLE_DISABLED"


async def test_admin_settings_report_the_redirect_uri_the_server_will_actually_send(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, admin_user: User
) -> None:
    """What the admin pastes into Google Cloud Console has to be what this server sends.

    Only the server knows it — it comes from `public_url`. A settings screen that derived it
    from the browser's own origin would be right on a laptop and wrong on every install
    reached through a proxy or by LAN address, and the symptom is a `redirect_uri_mismatch`
    at Google that the UI gives no way to diagnose.
    """
    cookie = await sign_in_directly(db, admin_user)

    response = await client.get("/api/admin/settings", headers=auth(client, cookie))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["derived.google.redirect_uri"] == oauth_service.redirect_uri()
    assert body["derived.google.js_origin"] == get_settings().public_url.rstrip("/")

    # And it is genuinely the one on the wire, not a second construction that could drift.
    url = await start(client)
    assert query_of(url)["redirect_uri"] == body["derived.google.redirect_uri"]

    # Derived, so a save must neither persist it nor reject it.
    saved = await client.put(
        "/api/admin/settings",
        json={"site.name": "Casa", "derived.google.redirect_uri": "https://evil.example/cb"},
        headers=auth(client, cookie),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["derived.google.redirect_uri"] == oauth_service.redirect_uri()
    assert saved.json()["site.name"] == "Casa"


# --- 5. The callback never leaks and never answers with anything but a redirect -----------------


SECRETS = (ACCESS_TOKEN, CLIENT_SECRET, "id_token", "access_token", "code_verifier")


def assert_no_secret_anywhere(response: Any) -> None:
    """Body, headers and `Location` together — a token may appear in none of them."""
    haystack = "\n".join(
        [
            response.text or "",
            "\n".join(f"{k}: {v}" for k, v in response.headers.items()),
        ]
    )
    for needle in SECRETS:
        assert needle not in haystack, f"{needle!r} leaked into the callback response"
    # And nothing that looks like a JWS either.
    assert ".eyJ" not in haystack


async def test_every_callback_outcome_is_a_bare_302(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    good = await consent(await start(client))
    cases: dict[str, dict[str, str]] = {
        "success": good,
        "cancelled": {"error": "access_denied", "state": good["state"]},
        "unknown-state": {"code": "code-nonexistent", "state": "not-a-real-state"},
        "no-parameters": {},
        "state-without-code": {"state": good["state"]},
        "code-google-never-issued": {"code": "code-invented", "state": good["state"]},
    }

    for label, params in cases.items():
        response = await callback(client, params)
        assert response.status_code == 302, f"{label}: got {response.status_code}"
        assert response.text == "", f"{label}: answered with a body"
        assert response.headers["location"], label
        assert_no_secret_anywhere(response)

    # The successful one really did sign someone in, so this is not vacuously true.
    assert await count(db, OAuthIdentity) == 1


async def test_the_state_is_single_use(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    params = await consent(await start(client))

    first = await callback(client, params)
    assert redirected_to(first) == "/"

    second = await callback(client, params)
    assert error_of(second) == "GOOGLE_STATE_USED"
    assert "set-cookie" not in second.headers
    # A replay must not reach the issuer at all.
    assert google.refusals == []


async def test_a_failed_callback_still_burns_the_state(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """The refusal path is not rolled back, or a replay would get a second attempt."""
    google.email = "stranger@example.com"
    google.sub = "sub-burned"
    url = await start(client)
    state = query_of(url)["state"]

    assert error_of(await callback(client, await consent(url))) == "GOOGLE_NO_ACCOUNT"

    row = (
        await db.execute(select(OAuthState).where(OAuthState.state == state))
    ).scalar_one()
    assert row.consumed_at is not None


async def test_an_expired_state_is_refused(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    url = await start(client)
    params = await consent(url)
    row = (
        await db.execute(select(OAuthState).where(OAuthState.state == params["state"]))
    ).scalar_one()
    row.expires_at = utcnow().replace(year=utcnow().year - 1)
    await db.commit()

    assert error_of(await callback(client, params)) == "GOOGLE_STATE_EXPIRED"


async def test_open_redirect_attempts_are_refused_at_the_start(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    for hostile in ("//evil.example.com", "/\\evil.example.com", "https://evil.example.com"):
        response = await client.get("/api/auth/google/start", params={"next": hostile})
        assert response.status_code == 400, hostile
        assert response.json()["error"]["code"] == "INVALID_NEXT_PATH", hostile


async def test_a_hostile_next_path_smuggled_into_the_state_row_still_cannot_redirect(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    """Belt and braces: even if a row somehow held one, the redirect falls back to `/`."""
    url = await start(client)
    row = (
        await db.execute(select(OAuthState).where(OAuthState.state == query_of(url)["state"]))
    ).scalar_one()
    row.next_path = "//evil.example.com"
    await db.commit()

    response = await callback(client, await consent(url))
    assert redirected_to(response) == "/"


async def test_the_exchange_is_refused_without_the_client_secret(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer_user: User
) -> None:
    """Confirms the issuer's client authentication is really enforced, so the passing tests
    above are evidence the secret was sent rather than evidence the double ignored it."""
    await SettingsService(db).set("oauth.google.client_secret", "wrong-secret")
    await db.commit()

    response = await callback(client, await consent(await start(client)))

    assert error_of(response) == "GOOGLE_EXCHANGE_FAILED"
    assert google.refusals == ["invalid_client"]
    assert await count(db, OAuthIdentity) == 0


async def test_discovery_outage_falls_back_to_the_documented_endpoints(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch, google_on: None
) -> None:
    """Nothing here reaches Google: the fallback URLs are asserted, not fetched."""
    oauth_service.reset_caches()
    monkeypatch.setattr(oauth_service, "DISCOVERY_URL", "http://127.0.0.1:1/nowhere")
    try:
        url = await start(client)
        assert url.startswith(oauth_service.FALLBACK_ENDPOINTS["authorization_endpoint"])
    finally:
        oauth_service.reset_caches()
