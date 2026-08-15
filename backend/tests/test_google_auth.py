"""Sign in with Google.

The property under test throughout is the one the whole deployment rests on: dramatarr is
invite-only, and adding a third sign-in method must not open a fourth way in. So the file is
organized around the two places that could break it —

* **id_token verification.** Every claim gets its own refusal test, and each one is written so
  that the *only* difference from the passing case is the claim under test. A verifier that
  silently accepted an unsigned token, a token for another audience, or a token minted by
  someone else's issuer would still pass a naive "it signs in" test; it cannot pass these.
* **Account resolution.** All four branches, including the one that must refuse, plus the near
  misses that a plausible implementation gets wrong: an invitation for a *different* address
  than the Google account, an invitation when invites are switched off, and an account that
  already answers to a different Google account.

Google is never contacted. `oauth._fetch_json` is the module's entire outbound HTTP surface
and it is replaced wholesale, so a test that somehow reached the network would fail with an
explicit assertion rather than quietly succeeding on someone's laptop and failing in CI.

The fake Google signs with a real RSA key generated once for the module, which is what makes
the "tampered signature" and "unknown key id" tests meaningful — the verifier runs the same
code path it will run in production.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import Invite, OAuthIdentity, OAuthState, OAuthStateKind, User, UserRole
from app.services import oauth as oauth_service
from app.services.settings import SettingsService
from app.utils.time import utcnow

pytestmark = pytest.mark.anyio

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-never-logged"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"

KID = "test-kid-1"

#: One key for the whole module: RSA generation is slow and nothing here needs a second one
#: except the "signed by someone else" case, which gets its own below.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


# --- A fake Google -------------------------------------------------------------------------


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": b64u(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64u(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def make_id_token(
    *,
    nonce: str,
    sub: str = "google-subject-1",
    email: str = "viewer@example.com",
    email_verified: Any = True,
    aud: Any = CLIENT_ID,
    iss: str = "https://accounts.google.com",
    exp_delta: int = 3600,
    iat_delta: int = 0,
    kid: str = KID,
    alg: str = "RS256",
    name: str | None = None,
    key: rsa.RSAPrivateKey | None = None,
    tamper: bool = False,
) -> str:
    """A signed JWS in Google's shape. Every knob exists so one test can turn exactly one."""
    now = int(utcnow().timestamp())
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    payload: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "nonce": nonce,
        "iat": now + iat_delta,
        "exp": now + exp_delta,
    }
    if name is not None:
        payload["name"] = name

    signing_input = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(payload).encode())}"
    signature = (key or _KEY).sign(
        signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    if tamper:
        # Flip the last bit rather than mangling the structure: the token still parses, still
        # names a real key, and only the mathematics disagrees.
        signature = signature[:-1] + bytes([signature[-1] ^ 0x01])
    return f"{signing_input}.{b64u(signature)}"


class FakeGoogle:
    """Stands in for `oauth._fetch_json` — the module's whole outbound HTTP surface."""

    def __init__(self) -> None:
        self.token_requests: list[dict[str, str]] = []
        self.token_payload: dict[str, Any] = {}
        self.token_status = 200
        self.discovery_calls = 0
        self.jwks_calls = 0
        self.discovery_fails = False
        self.extra_keys: list[dict[str, str]] = []

    async def fetch(
        self, method: str, url: str, *, data: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        if url == oauth_service.DISCOVERY_URL:
            self.discovery_calls += 1
            if self.discovery_fails:
                raise AppError("GOOGLE_UNAVAILABLE", "Could not reach Google (ConnectError)", 503)
            return 200, {
                "issuer": "https://accounts.google.com",
                "authorization_endpoint": AUTH_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
                "jwks_uri": JWKS_URI,
            }
        if url == JWKS_URI:
            self.jwks_calls += 1
            return 200, {"keys": [_jwk(_KEY, KID), *self.extra_keys]}
        if url == TOKEN_ENDPOINT:
            assert method == "POST", "the token endpoint must be called with POST"
            self.token_requests.append(dict(data or {}))
            return self.token_status, self.token_payload
        raise AssertionError(f"unexpected outbound request: {method} {url}")

    def will_return(self, id_token: str) -> None:
        self.token_payload = {
            "id_token": id_token,
            # Present precisely so the tests can prove it never escapes the exchange.
            "access_token": "ya29.super-secret-access-token",
            "token_type": "Bearer",
            "expires_in": 3599,
        }


# --- Fixtures ------------------------------------------------------------------------------


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeGoogle]:
    fake = FakeGoogle()
    monkeypatch.setattr(oauth_service, "_fetch_json", fake.fetch)
    oauth_service.reset_caches()  # the discovery/JWKS caches outlive a single test
    yield fake
    oauth_service.reset_caches()


async def hydrated(db: AsyncSession, user: User) -> User:
    """Re-read the user through a query, the way a request does.

    In production every request loads its user with a `select`, so the eager relationships —
    `credentials`, `oauth_identities` — arrive already populated. The shared fixtures build
    the object in Python instead, which leaves those collections unloaded, and a lazy load on
    an async session raises rather than quietly emitting a query. This is a property of the
    fixture, not of the code under test, so it is fixed here.
    """
    return (
        await db.execute(
            select(User).where(User.id == user.id).execution_options(populate_existing=True)
        )
    ).scalar_one()


@pytest.fixture
async def viewer(db: AsyncSession, viewer_user: User) -> User:
    return await hydrated(db, viewer_user)


@pytest.fixture
async def admin(db: AsyncSession, admin_user: User) -> User:
    return await hydrated(db, admin_user)


@pytest.fixture
async def google_on(db: AsyncSession) -> None:
    svc = SettingsService(db)
    await svc.set("oauth.google.enabled", True)
    await svc.set("oauth.google.client_id", CLIENT_ID)
    await svc.set("oauth.google.client_secret", CLIENT_SECRET)
    await db.commit()


def _overrides(db: AsyncSession, user: User | None) -> AsyncClient:
    from app.api.deps import get_current_user, get_current_user_optional
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_user_optional] = lambda: user
    else:
        app.dependency_overrides[get_current_user_optional] = lambda: None
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Anonymous: the state that `/google/start` and `/google/callback` actually run in."""
    from app.main import app

    try:
        async with _overrides(db, None) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def viewer_client(db: AsyncSession, viewer: User) -> AsyncIterator[AsyncClient]:
    from app.main import app

    try:
        async with _overrides(db, viewer) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


# --- Helpers -------------------------------------------------------------------------------


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


async def start_login(client: AsyncClient, **params: str) -> str:
    response = await client.get("/api/auth/google/start", params=params)
    assert response.status_code == 200, response.text
    return str(response.json()["authorizationUrl"])


async def state_row(db: AsyncSession, state: str) -> OAuthState:
    row = await oauth_service.peek_state(db, state)
    assert row is not None
    return row


async def begin(client: AsyncClient, db: AsyncSession, **params: str) -> OAuthState:
    """Run `/google/start` and hand back the state row it created."""
    url = await start_login(client, **params)
    return await state_row(db, query_of(url)["state"])


async def callback(client: AsyncClient, **params: str) -> Any:
    return await client.get("/api/auth/google/callback", params=params)


def error_of(response: Any) -> str:
    """The stable code a failed callback redirected with."""
    assert response.status_code == 302, f"expected a redirect, got {response.status_code}"
    location = response.headers["location"]
    assert location.startswith("/login?"), location
    return query_of(location)["error"]


async def link_identity(db: AsyncSession, user: User, *, sub: str, email: str) -> OAuthIdentity:
    identity = OAuthIdentity(provider="google", provider_sub=sub, email=email)
    user.oauth_identities.append(identity)
    await db.commit()
    return identity


async def user_count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count()).select_from(User))).scalar_one())


async def make_invite(db: AsyncSession, email: str, role: str = UserRole.VIEWER) -> Invite:
    from app.services.auth import create_invite

    created = await create_invite(db, email=email, role=role)
    await db.commit()
    return created.invite


# --- The authorization request ----------------------------------------------------------------


async def test_start_asks_google_for_exactly_what_the_flow_needs(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    url = await start_login(client)
    assert url.startswith(f"{AUTH_ENDPOINT}?")

    params = query_of(url)
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["scope"] == "openid email profile"
    assert params["code_challenge_method"] == "S256"
    # No refresh token: dramatarr never acts for the user while they are away.
    assert params["access_type"] == "online"
    # Shared household devices — never silently reuse whoever the browser is signed in as.
    assert params["prompt"] == "select_account"
    assert params["redirect_uri"].endswith("/api/auth/google/callback")

    row = await state_row(db, params["state"])
    assert params["nonce"] == row.nonce
    # The challenge is derived from a verifier that never leaves the server.
    assert params["code_challenge"] == oauth_service.code_challenge(row.code_verifier)
    assert row.code_verifier not in url
    assert 43 <= len(row.code_verifier) <= 128  # RFC 7636


async def test_start_is_refused_when_google_is_switched_off(
    client: AsyncClient, google: FakeGoogle
) -> None:
    response = await client.get("/api/auth/google/start")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "GOOGLE_DISABLED"


async def test_start_is_refused_when_no_client_id_is_configured(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle
) -> None:
    """Enabled but unconfigured is still unusable, and says so with the same code."""
    await SettingsService(db).set("oauth.google.enabled", True)
    await db.commit()

    response = await client.get("/api/auth/google/start")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "GOOGLE_DISABLED"


async def test_next_must_be_a_path_and_not_a_url(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    """`//evil.example` is a protocol-relative URL to a browser, so "starts with /" is not
    enough on its own — that check alone is an open redirect."""
    for hostile in ("//evil.example", "/\\evil.example", "https://evil.example", "evil"):
        response = await client.get("/api/auth/google/start", params={"next": hostile})
        assert response.status_code == 400, hostile
        assert response.json()["error"]["code"] == "INVALID_NEXT_PATH", hostile


async def test_discovery_falls_back_to_the_documented_endpoints(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    google.discovery_fails = True
    url = await start_login(client)
    assert url.startswith(f"{oauth_service.FALLBACK_ENDPOINTS['authorization_endpoint']}?")


async def test_discovery_is_cached_across_calls(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    await start_login(client)
    await start_login(client)
    assert google.discovery_calls == 1


# --- State: single use, and bound to its flow --------------------------------------------------


async def test_state_is_single_use_and_a_replay_is_refused(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    first = await callback(client, code="auth-code", state=row.state)
    assert first.status_code == 302
    assert first.headers["location"] == "/"

    replay = await callback(client, code="auth-code", state=row.state)
    assert error_of(replay) == "GOOGLE_STATE_USED"


async def test_a_state_minted_for_another_flow_is_refused(
    db: AsyncSession, viewer: User
) -> None:
    """A link state carries an account; a login state does not. Redeeming either as the other
    would skip the rules the other flow depends on, so the kind is part of the check."""
    row = await oauth_service.create_state(
        db, kind=OAuthStateKind.LINK, browser_token="the-browser", user_id=viewer.id
    )
    await db.commit()

    with pytest.raises(AppError) as excinfo:
        await oauth_service.consume_state(
            db, row.state, kind=OAuthStateKind.LOGIN, browser_token="the-browser"
        )
    assert excinfo.value.code == "GOOGLE_STATE_INVALID"

    # And it is still unburned, so the flow it *does* belong to still works.
    assert (await state_row(db, row.state)).consumed_at is None


async def test_an_unknown_state_is_refused(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    assert error_of(await callback(client, code="c", state="never-issued")) == (
        "GOOGLE_STATE_INVALID"
    )


async def test_an_expired_state_is_refused(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    from datetime import timedelta

    row = await begin(client, db)
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    assert error_of(await callback(client, code="c", state=row.state)) == "GOOGLE_STATE_EXPIRED"


async def test_googles_own_cancellation_is_reported_as_cancelled(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    row = await begin(client, db)
    response = await callback(client, state=row.state, error="access_denied")
    assert error_of(response) == "GOOGLE_CANCELLED"


# --- PKCE --------------------------------------------------------------------------------------


async def test_the_pkce_verifier_travels_with_the_code(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """The verifier is what proves the exchange comes from whoever started the flow. It is
    stored server-side, never put in the authorization URL, and sent only here."""
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    await callback(client, code="the-auth-code", state=row.state)

    assert len(google.token_requests) == 1
    sent = google.token_requests[0]
    assert sent["code_verifier"] == row.code_verifier
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-auth-code"
    assert sent["client_id"] == CLIENT_ID
    assert sent["client_secret"] == CLIENT_SECRET
    assert sent["redirect_uri"].endswith("/api/auth/google/callback")


async def test_a_refused_exchange_does_not_sign_anyone_in(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    row = await begin(client, db)
    google.token_status = 400
    google.token_payload = {"error": "invalid_grant"}

    response = await callback(client, code="stale", state=row.state)
    assert error_of(response) == "GOOGLE_EXCHANGE_FAILED"
    assert "set-cookie" not in response.headers


# --- id_token verification ----------------------------------------------------------------------


async def _verify(google: FakeGoogle, token: str, *, nonce: str) -> oauth_service.GoogleClaims:
    return await oauth_service.verify_id_token(token, client_id=CLIENT_ID, nonce=nonce)


async def _refusal(google: FakeGoogle, token: str, *, nonce: str = "the-nonce") -> str:
    with pytest.raises(AppError) as excinfo:
        await _verify(google, token, nonce=nonce)
    return excinfo.value.code


async def test_a_well_formed_token_verifies(google: FakeGoogle) -> None:
    claims = await _verify(
        google,
        make_id_token(nonce="the-nonce", email="Someone@Example.COM", name="  Ana  "),
        nonce="the-nonce",
    )
    assert claims.sub == "google-subject-1"
    assert claims.email == "someone@example.com"  # normalized, as everywhere else in the app
    assert claims.name == "Ana"


async def test_a_nonce_from_another_attempt_is_refused(google: FakeGoogle) -> None:
    token = make_id_token(nonce="somebody-elses-nonce")
    assert await _refusal(google, token, nonce="the-nonce") == "GOOGLE_TOKEN_INVALID"


async def test_a_token_for_another_client_is_refused(google: FakeGoogle) -> None:
    token = make_id_token(nonce="the-nonce", aud="someone-elses-client.apps.googleusercontent.com")
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


async def test_a_token_from_another_issuer_is_refused(google: FakeGoogle) -> None:
    token = make_id_token(nonce="the-nonce", iss="https://accounts.evil.example")
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


async def test_both_spellings_of_googles_issuer_are_accepted(google: FakeGoogle) -> None:
    """Google has issued tokens under both, and refusing one would lock those users out."""
    for issuer in ("https://accounts.google.com", "accounts.google.com"):
        claims = await _verify(
            google, make_id_token(nonce="the-nonce", iss=issuer), nonce="the-nonce"
        )
        assert claims.sub


async def test_an_expired_token_is_refused(google: FakeGoogle) -> None:
    token = make_id_token(nonce="the-nonce", exp_delta=-3600, iat_delta=-7200)
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


async def test_expiry_allows_only_a_small_clock_skew(google: FakeGoogle) -> None:
    """Just-expired is tolerated, long-expired is not — otherwise a drifting server clock
    locks the household out, and a generous window makes expiry meaningless."""
    barely = make_id_token(nonce="the-nonce", exp_delta=-10)
    assert (await _verify(google, barely, nonce="the-nonce")).sub

    well_past = make_id_token(nonce="the-nonce", exp_delta=-oauth_service.CLOCK_SKEW_SECONDS * 5)
    assert await _refusal(google, well_past) == "GOOGLE_TOKEN_INVALID"


async def test_a_token_issued_in_the_future_is_refused(google: FakeGoogle) -> None:
    token = make_id_token(nonce="the-nonce", iat_delta=3600, exp_delta=7200)
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


async def test_a_tampered_signature_is_refused(google: FakeGoogle) -> None:
    """Same header, same claims, one flipped bit in the signature."""
    token = make_id_token(nonce="the-nonce", tamper=True)
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


async def test_a_token_signed_by_the_wrong_key_is_refused(google: FakeGoogle) -> None:
    """A real signature, a real key id — but not Google's key."""
    token = make_id_token(nonce="the-nonce", key=_OTHER_KEY)
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


async def test_an_unknown_key_id_is_refused(google: FakeGoogle) -> None:
    token = make_id_token(nonce="the-nonce", kid="not-a-google-key")
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


async def test_an_unsigned_token_is_refused(google: FakeGoogle) -> None:
    """The classic JWT confusion attack: claim `alg: none` and hope the verifier obliges."""
    header = b64u(json.dumps({"alg": "none", "kid": KID, "typ": "JWT"}).encode())
    now = int(utcnow().timestamp())
    payload = b64u(
        json.dumps(
            {
                "iss": "https://accounts.google.com",
                "aud": CLIENT_ID,
                "sub": "google-subject-1",
                "email": "viewer@example.com",
                "email_verified": True,
                "nonce": "the-nonce",
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    assert await _refusal(google, f"{header}.{payload}.") == "GOOGLE_TOKEN_INVALID"


async def test_garbage_is_refused_rather_than_crashing(google: FakeGoogle) -> None:
    for junk in ("", "not-a-jwt", "a.b", "a.b.c", "...."):
        assert await _refusal(google, junk) == "GOOGLE_TOKEN_INVALID"


@pytest.mark.parametrize("value", [False, "true", "True", 1, None])
async def test_an_email_google_has_not_verified_is_refused(google: FakeGoogle, value: Any) -> None:
    """Only the boolean `true` counts. The whole justification for matching a Google account
    to a local one is that Google vouched for the mailbox; anything else vouches for nothing,
    including a *string* that merely looks like it does."""
    token = make_id_token(nonce="the-nonce", email_verified=value)
    assert await _refusal(google, token) == "GOOGLE_EMAIL_UNVERIFIED"


async def test_verification_precedes_the_email_check(google: FakeGoogle) -> None:
    """A forged token must be rejected as forged, not evaluated for its claims."""
    token = make_id_token(nonce="the-nonce", email_verified=False, tamper=True)
    assert await _refusal(google, token) == "GOOGLE_TOKEN_INVALID"


# --- Account resolution: the four paths ----------------------------------------------------------


async def test_path_one_a_linked_google_account_signs_in(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    identity = await link_identity(db, viewer, sub="sub-123", email="viewer@example.com")
    assert identity.last_used_at is None

    row = await begin(client, db)
    # A different address than the one recorded at link time: `sub` is the identifier.
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-123", email="moved@example.com"))

    response = await callback(client, code="c", state=row.state)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert "set-cookie" in response.headers

    await db.refresh(identity)
    assert identity.last_used_at is not None
    assert identity.user_id == viewer.id


async def test_path_two_a_verified_email_matches_an_existing_account(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """The trust-model step: this grants no more than the emailed sign-in link already does."""
    row = await begin(client, db)
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-new", email=viewer.email.upper())
    )

    response = await callback(client, code="c", state=row.state)
    assert response.status_code == 302
    assert "set-cookie" in response.headers

    identity = await oauth_service.identity_for_subject(db, "sub-new")
    assert identity is not None
    assert identity.user_id == viewer.id
    assert identity.email == viewer.email  # normalized on the way in


async def test_path_three_an_invitation_for_the_same_address_is_accepted(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    from app.services.auth import create_invite

    created = await create_invite(db, email="nueva@example.com", role=UserRole.ADMIN)
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    assert row.invite_id == created.invite.id
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-invited", email="nueva@example.com", name="Nueva")
    )

    response = await callback(client, code="c", state=row.state)
    assert response.status_code == 302
    assert "set-cookie" in response.headers

    assert await user_count(db) == before + 1
    created_user = (
        await db.execute(select(User).where(User.email == "nueva@example.com"))
    ).scalar_one()
    assert created_user.role == UserRole.ADMIN  # the invitation's role, not a default
    assert created_user.display_name == "Nueva"

    await db.refresh(created.invite)
    assert created.invite.accepted_at is not None
    assert created.invite.accepted_user_id == created_user.id


async def test_path_four_a_stranger_gets_nothing(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """The rule the whole feature is built around. A perfectly valid Google account, a
    perfectly valid token, and no account here — so no session, and nothing written."""
    before = await user_count(db)
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-stranger", email="who@example.com"))

    response = await callback(client, code="c", state=row.state)
    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    assert "set-cookie" not in response.headers
    assert await user_count(db) == before
    assert await oauth_service.identity_for_subject(db, "sub-stranger") is None


async def test_an_invitation_for_a_different_address_does_not_enrol_anyone(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """Otherwise one forwarded invitation link would enrol any Google account at all — the
    invite would stop being an invitation and become a public registration form."""
    from app.services.auth import create_invite

    created = await create_invite(db, email="invitada@example.com", role=UserRole.VIEWER)
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-other", email="alguien.mas@example.com")
    )

    response = await callback(client, code="c", state=row.state)
    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    assert await user_count(db) == before

    await db.refresh(created.invite)
    assert created.invite.accepted_at is None  # still redeemable by the person it was for


async def test_invitations_can_be_switched_off_for_google(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    from app.services.auth import create_invite

    await SettingsService(db).set("oauth.google.allow_invites", False)
    created = await create_invite(db, email="nueva@example.com", role=UserRole.VIEWER)
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-x", email="nueva@example.com"))

    assert error_of(await callback(client, code="c", state=row.state)) == "GOOGLE_NO_ACCOUNT"
    assert await user_count(db) == before


async def test_a_deactivated_account_is_refused(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    viewer.is_active = False
    await db.commit()

    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-1", email=viewer.email))

    response = await callback(client, code="c", state=row.state)
    assert error_of(response) == "ACCOUNT_DISABLED"
    assert "set-cookie" not in response.headers


async def test_a_deactivated_account_is_refused_even_when_already_linked(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    await link_identity(db, viewer, sub="sub-123", email=viewer.email)
    viewer.is_active = False
    await db.commit()

    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-123", email=viewer.email))

    assert error_of(await callback(client, code="c", state=row.state)) == "ACCOUNT_DISABLED"


async def test_an_account_bound_to_another_google_account_is_not_silently_repointed(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """The email matches, but this account already answers to a different Google account.
    Re-pointing it would break "one Google account per user" and would be a silent way to
    lose the identity you deliberately linked."""
    await link_identity(db, viewer, sub="sub-original", email=viewer.email)

    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-different", email=viewer.email))

    response = await callback(client, code="c", state=row.state)
    assert error_of(response) == "GOOGLE_ACCOUNT_MISMATCH"
    assert await oauth_service.identity_for_subject(db, "sub-different") is None


# --- The callback contract -----------------------------------------------------------------------


async def test_the_callback_is_disabled_with_the_rest_of_the_feature(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """A state minted before the admin turned Google off must not still complete."""
    row = await begin(client, db)
    await SettingsService(db).set("oauth.google.enabled", False)
    await db.commit()
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    assert error_of(await callback(client, code="c", state=row.state)) == "GOOGLE_DISABLED"


async def test_the_callback_always_redirects_and_never_carries_a_token(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """Google drives a *browser* here, so JSON would be a dead end — and whatever comes back
    from the token endpoint must not reach the URL bar, the body, or a header."""
    row = await begin(client, db)
    id_token = make_id_token(nonce=row.nonce, email=viewer.email)
    google.will_return(id_token)

    success = await callback(client, code="c", state=row.state)
    failure = await callback(client, code="c", state="never-issued")
    cancelled = await callback(client, state=row.state, error="access_denied")
    missing = await callback(client)

    secrets = (id_token, "ya29.super-secret-access-token", CLIENT_SECRET, row.code_verifier)
    for response in (success, failure, cancelled, missing):
        assert response.status_code == 302, response.text
        location = response.headers["location"]
        haystack = f"{location} {response.text} {response.headers!r}"
        for secret in secrets:
            assert secret not in haystack
        for leaky in ("id_token", "access_token", "code_verifier", "client_secret"):
            assert leaky not in location

    assert success.headers["location"] == "/"
    assert "set-cookie" in success.headers


async def test_a_successful_login_lands_on_the_requested_path(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    row = await begin(client, db, next="/series/ruta-de-cristal")
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    response = await callback(client, code="c", state=row.state)
    assert response.headers["location"] == "/series/ruta-de-cristal"


async def test_the_session_cookie_is_a_real_session(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    from app.api.deps import hash_token
    from app.models import AuthSession

    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))
    response = await callback(client, code="c", state=row.state)

    raw = response.cookies.get("dramatarr_session")
    assert raw
    stored = (
        await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_token(raw)))
    ).scalar_one()
    assert stored.user_id == viewer.id
    # Only the hash is kept, exactly as for every other session.
    assert raw not in stored.token_hash


# --- Linking from the account page -----------------------------------------------------


async def test_link_start_returns_a_link_state_for_the_current_account(
    viewer_client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None,
    viewer: User,
) -> None:
    response = await viewer_client.post("/api/auth/google/link/start", params={"next": "/account"})
    assert response.status_code == 200

    row = await state_row(db, query_of(response.json()["authorizationUrl"])["state"])
    assert row.kind == OAuthStateKind.LINK
    assert row.user_id == viewer.id
    assert row.next_path == "/account"


async def test_link_start_refuses_when_a_google_account_is_already_linked(
    viewer_client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None,
    viewer: User,
) -> None:
    await link_identity(db, viewer, sub="sub-123", email=viewer.email)

    response = await viewer_client.post("/api/auth/google/link/start")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GOOGLE_ALREADY_LINKED"


async def test_linking_attaches_the_google_account_without_a_new_session(
    viewer_client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None,
    viewer: User,
) -> None:
    response = await viewer_client.post("/api/auth/google/link/start", params={"next": "/account"})
    row = await state_row(db, query_of(response.json()["authorizationUrl"])["state"])
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-linked", email="personal@gmail.example")
    )

    done = await viewer_client.get(
        "/api/auth/google/callback", params={"code": "c", "state": row.state}
    )
    assert done.status_code == 302
    assert done.headers["location"] == "/account"
    # The user was already signed in; a link must not mint a second session.
    assert "set-cookie" not in done.headers

    identity = await oauth_service.identity_for_subject(db, "sub-linked")
    assert identity is not None
    assert identity.user_id == viewer.id
    # A Google address may differ from the dramatarr one — that is the point of linking.
    assert identity.email == "personal@gmail.example"


async def test_a_link_cannot_be_completed_by_a_different_session(
    viewer_client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None,
    viewer: User, admin: User,
) -> None:
    """The state is the CSRF defence; requiring the session that minted it closes the case
    where a replayed link callback attaches a Google account inside someone else's browser.

    The two states are minted up front because the callback burns one whichever way it goes —
    the second attempt must fail on *whose session it is*, not on a spent state.
    """
    from app.api.deps import get_current_user_optional
    from app.main import app

    async def link_state() -> OAuthState:
        started = await viewer_client.post("/api/auth/google/link/start")
        assert started.status_code == 200, started.text
        return await state_row(db, query_of(started.json()["authorizationUrl"])["state"])

    for arriving_as, label in ((lambda: admin, "another account"), (lambda: None, "nobody")):
        row = await link_state()
        google.will_return(make_id_token(nonce=row.nonce, sub="sub-linked", email="a@x.example"))

        # Same callback URL, same valid state — a different browser behind it.
        app.dependency_overrides[get_current_user_optional] = arriving_as
        response = await callback(viewer_client, code="c", state=row.state)
        assert error_of(response) == "GOOGLE_STATE_INVALID", label
        assert await oauth_service.identity_for_subject(db, "sub-linked") is None, label


async def test_a_google_account_cannot_be_linked_to_two_users(
    viewer_client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None,
    admin: User, viewer: User,
) -> None:
    await link_identity(db, admin, sub="sub-shared", email="shared@example.com")

    started = await viewer_client.post("/api/auth/google/link/start")
    row = await state_row(db, query_of(started.json()["authorizationUrl"])["state"])
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-shared", email="shared@example.com"))

    response = await viewer_client.get(
        "/api/auth/google/callback", params={"code": "c", "state": row.state}
    )
    assert error_of(response) == "GOOGLE_ACCOUNT_IN_USE"

    identity = await oauth_service.identity_for_subject(db, "sub-shared")
    assert identity is not None
    assert identity.user_id == admin.id  # untouched


# --- Unlinking, and the last-way-in guard ----------------------------------------------


async def test_unlink_is_refused_when_it_is_the_only_way_in(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """No passkeys, magic link switched off — removing Google would lock the account out for
    good, and only an admin with shell access could undo it."""
    await SettingsService(db).set("auth.magic_link_enabled", False)
    await link_identity(db, viewer, sub="sub-only", email=viewer.email)

    response = await viewer_client.delete("/api/auth/google/link")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_SIGN_IN_METHOD"
    assert await oauth_service.identity_for_subject(db, "sub-only") is not None


async def test_unlink_is_allowed_when_a_passkey_remains(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    from app.models import Credential

    await SettingsService(db).set("auth.magic_link_enabled", False)
    viewer.credentials.append(
        Credential(credential_id=b"cred-bytes", public_key=b"key-bytes", nickname="Telefono")
    )
    await link_identity(db, viewer, sub="sub-extra", email=viewer.email)

    response = await viewer_client.delete("/api/auth/google/link")
    assert response.status_code == 200
    assert await oauth_service.identity_for_subject(db, "sub-extra") is None


async def test_unlink_is_allowed_when_the_magic_link_remains(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    await link_identity(db, viewer, sub="sub-extra", email=viewer.email)

    assert (await viewer_client.delete("/api/auth/google/link")).status_code == 200
    assert await oauth_service.identity_for_subject(db, "sub-extra") is None


async def test_unlink_with_nothing_linked_is_a_not_found(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    response = await viewer_client.delete("/api/auth/google/link")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GOOGLE_NOT_LINKED"


async def test_the_last_passkey_may_go_once_google_can_take_over(
    db: AsyncSession, viewer: User
) -> None:
    """The mirror image of the guard above: with Google linked, the passkey is no longer the
    only way in, so the two guards have to agree about that."""
    from app.models import Credential
    from app.services.auth import delete_passkey

    await SettingsService(db).set("auth.magic_link_enabled", False)
    credential = Credential(credential_id=b"cred-bytes", public_key=b"key-bytes")
    viewer.credentials.append(credential)
    await db.commit()

    with pytest.raises(AppError) as excinfo:
        await delete_passkey(db, viewer, credential.id)
    assert excinfo.value.code == "LAST_CREDENTIAL"

    await link_identity(db, viewer, sub="sub-rescue", email=viewer.email)
    await delete_passkey(db, viewer, credential.id)  # must not raise


# --- What the account page reads -------------------------------------------------------


async def test_methods_reports_what_the_account_can_sign_in_with(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    from app.models import Credential

    empty = (await viewer_client.get("/api/auth/methods")).json()
    assert empty == {
        "passkeys": 0,
        "googleLinked": False,
        "googleEmail": None,
        "magicLinkEnabled": True,
    }

    viewer.credentials.append(
        Credential(credential_id=b"c1", public_key=b"k1", nickname="Telefono")
    )
    await link_identity(db, viewer, sub="sub-1", email="personal@gmail.example")
    await SettingsService(db).set("auth.magic_link_enabled", False)
    await db.commit()

    assert (await viewer_client.get("/api/auth/methods")).json() == {
        "passkeys": 1,
        "googleLinked": True,
        "googleEmail": "personal@gmail.example",
        "magicLinkEnabled": False,
    }


async def test_methods_requires_a_session(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/methods")).status_code == 401


async def test_public_config_tells_the_login_page_whether_to_offer_google(
    client: AsyncClient, db: AsyncSession
) -> None:
    assert (await client.get("/api/config")).json()["googleEnabled"] is False

    # Switched on but not configured is still unusable, and must not be advertised.
    await SettingsService(db).set("oauth.google.enabled", True)
    await db.commit()
    assert (await client.get("/api/config")).json()["googleEnabled"] is False

    await SettingsService(db).set("oauth.google.client_id", CLIENT_ID)
    await db.commit()
    assert (await client.get("/api/config")).json()["googleEnabled"] is True


async def test_the_client_secret_is_never_returned(db: AsyncSession) -> None:
    """It round-trips through the settings form as the redaction sentinel, like every other
    secret, so saving the form cannot blank it and reading it cannot leak it."""
    from app.services.settings import REDACTED, SECRET_KEYS

    assert "oauth.google.client_secret" in SECRET_KEYS

    svc = SettingsService(db)
    await svc.set("oauth.google.client_secret", CLIENT_SECRET)
    await db.commit()

    assert (await svc.all())["oauth.google.client_secret"] == REDACTED

    await svc.update_many({"oauth.google.client_secret": REDACTED})
    assert await svc.get_str("oauth.google.client_secret") == CLIENT_SECRET


async def test_signing_in_twice_reuses_the_identity_it_created(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """The second visit is path one, not a second link — `(provider, user_id)` is unique, so
    a resolver that took the email branch again would hit an integrity error instead."""
    for _ in range(2):
        row = await begin(client, db)
        google.will_return(make_id_token(nonce=row.nonce, sub="sub-1", email=viewer.email))
        assert (await callback(client, code="c", state=row.state)).status_code == 302

    identities = (
        (await db.execute(select(OAuthIdentity).where(OAuthIdentity.user_id == viewer.id)))
        .scalars()
        .all()
    )
    assert len(identities) == 1


async def test_dead_states_are_swept_as_new_ones_are_minted(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """`/google/start` is public, so without the sweep anonymous traffic would grow the table
    without bound — nothing else in the app reaps it."""
    from datetime import timedelta

    stale = await begin(client, db)
    stale.expires_at = utcnow() - timedelta(minutes=1)
    await db.commit()

    fresh = await begin(client, db)

    assert await oauth_service.peek_state(db, stale.state) is None
    assert await oauth_service.peek_state(db, fresh.state) is not None


async def test_deleting_a_user_takes_their_linked_identity_with_them(
    db: AsyncSession, viewer: User
) -> None:
    await link_identity(db, viewer, sub="sub-gone", email=viewer.email)

    await db.delete(viewer)
    await db.commit()

    assert await oauth_service.identity_for_subject(db, "sub-gone") is None


async def test_a_failed_callback_still_burns_its_state(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """A refused attempt must not leave a live state behind. Otherwise someone holding the
    callback URL could keep re-presenting it until a different id_token got through — the
    route commits on an `AppError` for exactly this reason."""
    row = await begin(client, db)
    google.will_return(make_id_token(nonce="a-different-nonce", email=viewer.email))

    first = await callback(client, code="c", state=row.state)
    assert error_of(first) == "GOOGLE_TOKEN_INVALID"
    assert (await state_row(db, row.state)).consumed_at is not None

    # Now with a token that *would* have verified. The state is spent, so it cannot.
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))
    retry = await callback(client, code="c", state=row.state)
    assert error_of(retry) == "GOOGLE_STATE_USED"
    assert "set-cookie" not in retry.headers
