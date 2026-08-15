"""The invite-only invariant, proved by attacking it.

    Nobody who was not invited can obtain a session, and nothing but `app/cli.py` and
    `create_user_from_invite` can bring a `User` row into existence.

There are exactly three sign-in methods — a passkey, an emailed magic link, and Sign in with
Google — plus the console CLI, which is the operator standing at the box. This file runs every
one of them from the attacker's side, on an empty household and on a populated one, and
asserts three things about every refusal:

* **No session.** No `Set-Cookie`, and no `AuthSession` row. A 4xx that quietly minted a
  session would still be a break-in.
* **No account.** The user count is asserted at the end of *every* test in this file, refusals
  and successes alike, so an attempt that enrolled someone on its way to a nice-looking error
  cannot pass.
* **Nothing spent.** A refused attempt must not burn the invitation, the challenge or the link
  that a legitimate member is still holding.

The positive cases are here for the same reason: a suite that proves nobody can get in is
worthless if it is passing because nobody can get in *at all*. A real invitation accepted by
passkey and by Google each produce exactly one account and a working session, and the account
they produce is exercised — signed in, read back, signed out.

Nothing here reaches the network. Google is the same fake `test_google_auth` uses, and the
passkey flows run against `SoftAuthenticator` below: a real ES256 key and real CBOR/COSE
structures, so `py_webauthn` runs the same verification path it runs in production. That is
what makes "the signature does not verify" a meaningful refusal rather than a parse error.
"""

# ruff: noqa: F811
# The Google harness is imported wholesale from `test_google_auth`, which is the supported way
# to share fixtures between modules; every test that then names one as a parameter looks to
# ruff like a redefinition of the imported name.

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import struct
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_google_auth import (  # noqa: F401  (fixtures are used by name)
    CLIENT_ID,
    FakeGoogle,
    admin,
    begin,
    callback,
    client,
    error_of,
    google,
    google_on,
    link_identity,
    make_id_token,
    query_of,
    state_row,
    user_count,
    viewer,
)

from app.api.deps import hash_token
from app.config import get_settings
from app.models import (
    AuthSession,
    Credential,
    Invite,
    MagicLink,
    User,
    UserRole,
    WebAuthnChallenge,
)
from app.services import auth as auth_service
from app.services.settings import SettingsService
from app.utils.ids import new_token
from app.utils.time import in_days, utcnow

pytestmark = pytest.mark.anyio

STRANGER = "mallory@evil.example"
INVITED = "alice@example.com"

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

#: The only two places in the backend that may bring a `User` into existence, as
#: `(module, enclosing function)`. `test_only_two_places_can_create_a_user` re-derives this
#: list from the source on every run, so a third one cannot appear unnoticed.
ALLOWED_USER_CONSTRUCTIONS = {
    ("app/cli.py", "cmd_create_admin"),
    ("app/services/auth.py", "create_user_from_invite"),
}


# ==============================================================================================
# Harness
# ==============================================================================================


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SoftAuthenticator:
    """A passkey in software: one ES256 key pair, one credential id, one counter.

    Real structures throughout — a CBOR attestation object with `fmt: "none"`, authenticator
    data with the proper flags and a COSE-encoded public key, and ECDSA-over-SHA256
    assertions — because the point of these tests is that the server's verification is doing
    the work. A stub that returned "yes" would prove nothing about the server at all.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.rp_id = settings.webauthn_rp_id
        self.origin = settings.webauthn_origins[0]
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0

    # -- internals ----------------------------------------------------------------------
    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, kind: str, challenge: str, *, origin: str | None = None) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": origin or self.origin,
                "crossOrigin": False,
            }
        ).encode()

    def _authenticator_data(self, *, attested: bool) -> bytes:
        self.sign_count += 1
        flags = 0x01 | 0x04 | (0x40 if attested else 0x00)  # UP | UV | AT
        data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([flags])
            + struct.pack(">I", self.sign_count)
        )
        if attested:
            data += (
                b"\x00" * 16  # aaguid
                + struct.pack(">H", len(self.credential_id))
                + self.credential_id
                + self._cose_key()
            )
        return data

    # -- the two ceremonies -------------------------------------------------------------
    def register(self, challenge: str) -> dict[str, Any]:
        attestation = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": self._authenticator_data(attested=True)}
        )
        return {
            "id": b64u(self.credential_id),
            "rawId": b64u(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64u(self._client_data("webauthn.create", challenge)),
                "attestationObject": b64u(attestation),
                "transports": ["internal", "hybrid"],
            },
        }

    def authenticate(
        self, challenge: str, *, credential_id: bytes | None = None
    ) -> dict[str, Any]:
        """Sign an assertion. `credential_id` lets an attacker claim to be somebody else's
        passkey while signing with their own key, which is the realistic forgery."""
        claimed = self.credential_id if credential_id is None else credential_id
        auth_data = self._authenticator_data(attested=False)
        client_data = self._client_data("webauthn.get", challenge)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64u(claimed),
            "rawId": b64u(claimed),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64u(client_data),
                "authenticatorData": b64u(auth_data),
                "signature": b64u(signature),
                "userHandle": b64u(claimed),
            },
        }


@pytest.fixture
async def anon(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    """A browser with a cookie jar and no help.

    Only `get_session` is overridden, so the session cookie, `get_current_user_optional` and
    `require_admin` all run for real — which is what makes "signed in" and "not signed in"
    mean in these tests what they mean in production.
    """
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_magic_link_limiter() -> Iterator[None]:
    """The per-address rate limiter is a module-level singleton and outlives a test."""
    from app.api.auth import _limiter

    _limiter._hits.clear()
    yield
    _limiter._hits.clear()


# --- Shared assertions -------------------------------------------------------------------


async def assert_users(db: AsyncSession, expected: int) -> None:
    assert await user_count(db) == expected, "the household is not the size this test expects"


async def assert_no_session(db: AsyncSession, response: Any) -> None:
    """A refusal that handed out a session is not a refusal."""
    assert "set-cookie" not in response.headers, "a refused attempt set a cookie"
    assert not response.cookies
    live = int((await db.execute(select(func.count()).select_from(AuthSession))).scalar_one())
    assert live == 0, "a refused attempt minted a session row"


async def assert_refused(
    db: AsyncSession, response: Any, *, status: int, code: str, users: int
) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code, response.text
    await assert_no_session(db, response)
    await assert_users(db, users)


async def magic_links(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count()).select_from(MagicLink))).scalar_one())


async def sessions_for(db: AsyncSession, user: User) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user.id)
            )
        ).scalar_one()
    )


async def make_invite(
    db: AsyncSession, email: str = INVITED, role: str = UserRole.VIEWER
) -> auth_service.CreatedInvite:
    created = await auth_service.create_invite(db, email=email, role=role)
    await db.commit()
    return created


async def issue_magic_link(db: AsyncSession, user: User, *, minutes: int = 10) -> str:
    """What `request_magic_link` and `python -m app.cli login-link` both produce."""
    token = new_token(32)
    db.add(
        MagicLink(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(minutes=minutes),
        )
    )
    await db.commit()
    return token


async def sign_in_with_cookie(db: AsyncSession, http: AsyncClient, user: User) -> str:
    """Give a client a genuine session for `user`, the way a completed sign-in would.

    The cookie is set without a domain on purpose: `http.cookiejar` refuses to send a cookie
    whose domain is a single label like `test`, so pinning one would silently produce an
    *anonymous* client and turn every "this is refused" assertion below into a tautology.
    """
    raw = new_token(32)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=in_days(get_settings().session_ttl_days),
        )
    )
    await db.commit()
    http.cookies.set(get_settings().session_cookie_name, raw)
    sanity = await http.get("/api/auth/session")
    assert sanity.json()["user"]["id"] == user.id, "the test client is not actually signed in"
    return raw


def options_of(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return dict(response.json()["publicKey"])


# ==============================================================================================
# 1. Provenance: where a User can come from at all
# ==============================================================================================


def _user_constructions() -> list[tuple[str, str | None, int]]:
    """Every `User(...)` call in `app/`, with the function that contains it.

    An AST walk rather than a grep: `class User(Base)` and `AuthUser(APIModel)` are not
    constructions, and a grep that tried to tell them apart by hand would be the thing that
    goes stale. Nested functions are attributed to themselves, so a helper defined inside a
    route cannot hide inside its parent's name.
    """
    found: list[tuple[str, str | None, int]] = []

    def walk(node: ast.AST, module: str, enclosing: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                walk(child, module, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "User"
            ):
                found.append((module, enclosing, child.lineno))
            walk(child, module, enclosing)

    for path in sorted(APP_ROOT.rglob("*.py")):
        module = path.relative_to(APP_ROOT.parent).as_posix()
        walk(ast.parse(path.read_text(encoding="utf-8")), module, None)
    return found


def test_only_two_places_in_the_backend_can_create_a_user() -> None:
    """The invariant every other test in this file rests on.

    If a third construction site appears — a "quick fix" in a route, a self-registration
    endpoint, a fixture helper that leaked into `app/` — every refusal proved below stops
    meaning what it says, because there is now a door none of them are watching.
    """
    sites = {(module, function) for module, function, _line in _user_constructions()}
    assert sites == ALLOWED_USER_CONSTRUCTIONS, (
        "a new place constructs a User: "
        f"{sorted(sites - ALLOWED_USER_CONSTRUCTIONS)} appeared, "
        f"{sorted(ALLOWED_USER_CONSTRUCTIONS - sites)} vanished"
    )
    # And none of them sits at module scope, where import alone would run it.
    assert all(function is not None for _module, function, _line in _user_constructions())


def test_no_module_writes_the_users_table_behind_the_orm() -> None:
    """The AST check above sees ORM constructions. This one sees the way around them."""
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("insert into users", "insert(user)", "users.insert("):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders


def test_the_protected_flag_is_not_something_the_api_can_write() -> None:
    """`is_protected` is readable on the admin list and writable only from a shell."""
    from app.schemas.admin import UserPatchIn

    assert set(UserPatchIn.model_fields) == {"role", "is_active", "display_name"}


# ==============================================================================================
# 2. Magic links
# ==============================================================================================


async def test_a_magic_link_for_an_address_with_no_account_creates_nothing(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """The headline: asking for a sign-in link is not a way to acquire an account."""
    response = await anon.post("/api/auth/magic-link/request", json={"email": STRANGER})

    assert response.status_code == 204, response.text
    assert response.content == b""
    await assert_no_session(db, response)
    assert await magic_links(db) == 0, "a link was minted for an address with no account"
    await assert_users(db, 0)


async def test_the_magic_link_endpoint_answers_a_stranger_exactly_as_it_answers_a_member(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """An account-existence oracle would turn the login page into a household roster.

    Compared byte for byte — status, body and every header but `date` — because a difference
    of one header or one byte of body is all an enumeration script needs.
    """
    known = await anon.post("/api/auth/magic-link/request", json={"email": viewer.email})
    unknown = await anon.post("/api/auth/magic-link/request", json={"email": STRANGER})

    assert known.status_code == unknown.status_code == 204
    assert known.content == unknown.content == b""

    def comparable(response: Any) -> dict[str, str]:
        return {k: v for k, v in response.headers.items() if k.lower() != "date"}

    assert comparable(known) == comparable(unknown)
    # And the address that *does* exist is the only one that got a link.
    assert await magic_links(db) == 1
    await assert_users(db, 1)


async def test_a_magic_link_for_a_deactivated_account_is_never_minted(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    viewer.is_active = False
    await db.commit()

    response = await anon.post("/api/auth/magic-link/request", json={"email": viewer.email})

    assert response.status_code == 204
    await assert_no_session(db, response)
    assert await magic_links(db) == 0
    await assert_users(db, 1)


@pytest.mark.parametrize(
    "why",
    ["random", "the-stored-hash", "one-character-off", "uppercased", "padded", "empty-ish"],
)
async def test_a_forged_or_guessed_magic_link_token_is_refused(
    anon: AsyncClient, db: AsyncSession, viewer: User, why: str
) -> None:
    """Every shape of "I do not have the token but I have something".

    `the-stored-hash` is the database-leak case and the one that matters most: the table keeps
    only a sha256, so presenting that sha256 must be worth nothing.
    """
    real = await issue_magic_link(db, viewer)
    forged = {
        "random": new_token(32),
        "the-stored-hash": hash_token(real),
        "one-character-off": real[:-1] + ("a" if real[-1] != "a" else "b"),
        "uppercased": real.upper(),
        "padded": f" {real} ",
        "empty-ish": "--------",
    }[why]
    if forged == real:  # `uppercased` on an all-digit token, say
        pytest.skip("the mutation happened to be the real token")

    response = await anon.post("/api/auth/magic-link/consume", json={"token": forged})

    await assert_refused(db, response, status=401, code="MAGIC_LINK_INVALID", users=1)
    # The real link is untouched and still redeemable by the person holding it.
    link = (await db.execute(select(MagicLink))).scalar_one()
    assert link.consumed_at is None


async def test_a_magic_link_is_single_use(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    token = await issue_magic_link(db, viewer)

    first = await anon.post("/api/auth/magic-link/consume", json={"token": token})
    assert first.status_code == 200, first.text

    anon.cookies.clear()
    replay = await anon.post("/api/auth/magic-link/consume", json={"token": token})

    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "MAGIC_LINK_USED"
    assert "set-cookie" not in replay.headers
    assert await sessions_for(db, viewer) == 1, "a replayed link minted a second session"
    await assert_users(db, 1)


async def test_an_expired_magic_link_is_refused(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    token = await issue_magic_link(db, viewer, minutes=-1)

    response = await anon.post("/api/auth/magic-link/consume", json={"token": token})

    await assert_refused(db, response, status=401, code="MAGIC_LINK_EXPIRED", users=1)


async def test_a_link_issued_before_a_deactivation_stops_working(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """The admin pressed "deactivate" while the email was still in the inbox."""
    token = await issue_magic_link(db, viewer)
    viewer.is_active = False
    await db.commit()

    response = await anon.post("/api/auth/magic-link/consume", json={"token": token})

    await assert_refused(db, response, status=403, code="ACCOUNT_DISABLED", users=1)


async def test_signing_in_retires_every_other_outstanding_link(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """A stale email in an inbox must not stay redeemable after a newer one was used."""
    stale = await issue_magic_link(db, viewer)
    fresh = await issue_magic_link(db, viewer)

    used = await anon.post("/api/auth/magic-link/consume", json={"token": fresh})
    assert used.status_code == 200, used.text

    anon.cookies.clear()
    response = await anon.post("/api/auth/magic-link/consume", json={"token": stale})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MAGIC_LINK_USED"
    await assert_users(db, 1)


async def test_a_pending_invitation_is_not_an_account_the_magic_link_path_can_reach(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """An invitation is a promise, not an account.

    Someone holding a forwarded invitation *email* — the address, not the token — must not be
    able to convert it into a sign-in link by asking the login page for one. The account only
    begins to exist at the moment a credential is enrolled against the token.
    """
    await make_invite(db, INVITED)

    response = await anon.post("/api/auth/magic-link/request", json={"email": INVITED})

    assert response.status_code == 204
    await assert_no_session(db, response)
    assert await magic_links(db) == 0
    await assert_users(db, 0)


async def test_a_real_magic_link_signs_in_the_account_it_was_issued_for(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """The positive control: the refusals above are not passing by breaking sign-in."""
    token = await issue_magic_link(db, viewer)

    response = await anon.post("/api/auth/magic-link/consume", json={"token": token})

    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == viewer.email
    assert response.cookies.get(get_settings().session_cookie_name)

    # The cookie is a real session, not just a header.
    session = await anon.get("/api/auth/session")
    assert session.json()["user"]["id"] == viewer.id
    assert await sessions_for(db, viewer) == 1
    await assert_users(db, 1)


# ==============================================================================================
# 3. Passkeys
# ==============================================================================================


async def test_a_passkey_that_is_not_in_the_database_cannot_sign_in(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """A perfectly well-formed assertion from an authenticator this install has never seen.

    Discoverable-credential login sends no email and no allowCredentials, so the credential id
    is the only thing tying an assertion to an account — an unknown one has to be the end of
    the road, not a lookup miss that falls through to something lenient.
    """
    device = SoftAuthenticator()
    options = options_of(await anon.post("/api/auth/passkey/login/begin"))

    response = await anon.post(
        "/api/auth/passkey/login/complete",
        json={"credential": device.authenticate(options["challenge"])},
    )

    await assert_refused(db, response, status=401, code="PASSKEY_UNKNOWN", users=1)
    assert int((await db.execute(select(func.count()).select_from(Credential))).scalar_one()) == 0


async def test_a_stranger_signing_a_registered_credential_id_with_their_own_key_is_refused(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """The credential id is not a secret — it travels in `allowCredentials` and in every
    assertion. Holding one must be worth nothing without the private key behind it."""
    real = SoftAuthenticator()
    viewer.credentials.append(
        Credential(credential_id=real.credential_id, public_key=b"not-the-real-key")
    )
    await db.commit()

    impostor = SoftAuthenticator()  # a different key pair
    options = options_of(await anon.post("/api/auth/passkey/login/begin"))

    response = await anon.post(
        "/api/auth/passkey/login/complete",
        json={
            "credential": impostor.authenticate(
                options["challenge"], credential_id=real.credential_id
            )
        },
    )

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "PASSKEY_VERIFICATION_FAILED"
    await assert_no_session(db, response)
    await assert_users(db, 1)


async def test_an_assertion_answering_a_challenge_the_server_never_issued_is_refused(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """The challenge store is what makes an assertion unreplayable, so a self-chosen
    challenge must not be accepted merely because the signature over it is valid."""
    device = SoftAuthenticator()

    response = await anon.post(
        "/api/auth/passkey/login/complete",
        json={"credential": device.authenticate(b64u(os.urandom(32)))},
    )

    await assert_refused(db, response, status=400, code="CHALLENGE_INVALID", users=1)


async def test_a_login_challenge_is_burned_on_first_use(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    device = SoftAuthenticator()
    options = options_of(await anon.post("/api/auth/passkey/login/begin"))
    assertion = device.authenticate(options["challenge"])

    first = await anon.post("/api/auth/passkey/login/complete", json={"credential": assertion})
    assert first.json()["error"]["code"] == "PASSKEY_UNKNOWN"

    replay = await anon.post("/api/auth/passkey/login/complete", json={"credential": assertion})
    assert replay.json()["error"]["code"] == "CHALLENGE_USED"
    await assert_no_session(db, replay)
    await assert_users(db, 1)


@pytest.mark.parametrize("token", ["not-a-real-token", "", "../admin", "x" * 200])
async def test_passkey_enrollment_without_a_valid_invite_token_is_refused(
    anon: AsyncClient, db: AsyncSession, token: str
) -> None:
    """`/invite/{token}/begin` and `/complete` are the only public routes that can create an
    account. Neither may do anything at all without a token that resolves to a live invite."""
    device = SoftAuthenticator()

    read = await anon.get(f"/api/auth/invite/{token}")
    begin_ = await anon.post(f"/api/auth/invite/{token}/begin")
    complete = await anon.post(
        f"/api/auth/invite/{token}/complete",
        json={"credential": device.register(b64u(os.urandom(32)))},
    )

    for response in (read, begin_, complete):
        # An empty token is a different URL entirely; anything else is an unknown invite.
        assert response.status_code in (404, 405), response.text
        if response.status_code == 404 and "error" in response.text:
            assert response.json()["error"]["code"] in {"INVITE_INVALID", "NOT_FOUND"}
        await assert_no_session(db, response)

    assert (
        int((await db.execute(select(func.count()).select_from(WebAuthnChallenge))).scalar_one())
        == 0
    )
    await assert_users(db, 0)


async def test_adding_a_passkey_to_an_account_requires_being_signed_into_it(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """The other registration route. Without a session it must not even issue a challenge."""
    device = SoftAuthenticator()

    begin_ = await anon.post("/api/auth/passkey/register/begin")
    complete = await anon.post(
        "/api/auth/passkey/register/complete",
        json={"credential": device.register(b64u(os.urandom(32)))},
    )

    for response in (begin_, complete):
        await assert_refused(db, response, status=401, code="UNAUTHORIZED", users=1)


async def test_a_challenge_issued_for_one_invitation_cannot_complete_another(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """Two invitations in flight. The challenge is bound to the invite it was minted for, so
    the holder of a challenge for Alice cannot spend it against Bruno's invitation."""
    alice = await make_invite(db, INVITED)
    bruno = await make_invite(db, "bruno@example.com")
    device = SoftAuthenticator()

    options = options_of(await anon.post(f"/api/auth/invite/{alice.token}/begin"))
    response = await anon.post(
        f"/api/auth/invite/{bruno.token}/complete",
        json={"credential": device.register(options["challenge"])},
    )

    await assert_refused(db, response, status=400, code="CHALLENGE_MISMATCH", users=0)
    for created in (alice, bruno):
        await db.refresh(created.invite)
        assert created.invite.accepted_at is None, "a refused attempt burned an invitation"


async def test_a_real_invitation_accepted_with_a_passkey_creates_exactly_one_user(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """The positive control for the whole passkey path, end to end on an empty household.

    Enrol, be signed in, sign out, sign back in with the passkey. If any refusal above were
    passing because passkeys are broken, this is what would fail.
    """
    created = await make_invite(db, INVITED, role=UserRole.ADMIN)
    device = SoftAuthenticator()

    info = await anon.get(f"/api/auth/invite/{created.token}")
    assert info.status_code == 200
    assert info.json()["email"] == INVITED

    options = options_of(await anon.post(f"/api/auth/invite/{created.token}/begin"))
    enrolled = await anon.post(
        f"/api/auth/invite/{created.token}/complete",
        json={
            "credential": device.register(options["challenge"]),
            "displayName": "Alicia",
            "locale": "es",
        },
    )

    assert enrolled.status_code == 200, enrolled.text
    body = enrolled.json()["user"]
    assert body["email"] == INVITED
    assert body["role"] == UserRole.ADMIN  # the invitation's role, not a default
    assert body["displayName"] == "Alicia"
    assert enrolled.cookies.get(get_settings().session_cookie_name)

    await assert_users(db, 1)
    user = (await db.execute(select(User))).scalar_one()
    await db.refresh(created.invite)
    assert created.invite.accepted_at is not None
    assert created.invite.accepted_user_id == user.id

    # The session is real, and the passkey is really registered.
    assert (await anon.get("/api/auth/session")).json()["user"]["id"] == user.id
    assert (await anon.get("/api/auth/passkeys")).json()[0]["nickname"]

    assert (await anon.post("/api/auth/logout")).status_code == 200
    anon.cookies.clear()
    assert (await anon.get("/api/auth/session")).json()["user"] is None

    options = options_of(await anon.post("/api/auth/passkey/login/begin"))
    signed_in = await anon.post(
        "/api/auth/passkey/login/complete",
        json={"credential": device.authenticate(options["challenge"])},
    )
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["user"]["id"] == user.id
    await assert_users(db, 1)


async def test_the_enrollment_payload_cannot_smuggle_a_role_or_the_protected_flag(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """The invitee chooses their display name and language, and nothing else.

    Role comes from the invitation the admin issued; `isProtected` comes from a shell. Both
    are absent from `InviteCompleteIn`, so extra keys are ignored — this pins that down, since
    "ignored" and "quietly honoured" look identical until someone tries it.
    """
    created = await make_invite(db, INVITED, role=UserRole.VIEWER)
    device = SoftAuthenticator()
    options = options_of(await anon.post(f"/api/auth/invite/{created.token}/begin"))

    response = await anon.post(
        f"/api/auth/invite/{created.token}/complete",
        json={
            "credential": device.register(options["challenge"]),
            "displayName": "Alicia",
            "role": "admin",
            "isProtected": True,
            "isActive": True,
            "email": "someone.else@example.com",
        },
    )

    assert response.status_code == 200, response.text
    await assert_users(db, 1)
    user = (await db.execute(select(User))).scalar_one()
    assert user.role == UserRole.VIEWER, "the enrollment payload chose its own role"
    assert user.is_protected is False
    assert user.email == INVITED, "the enrollment payload chose its own address"


async def test_an_invitation_cannot_mint_a_second_account_for_an_address_that_now_has_one(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """The invitation was issued, and the operator created the account from a shell in the
    meantime. Redeeming the link must not produce a second row for the same address — the
    unique index would stop it, but it has to be refused before anything is written."""
    from app.cli import cmd_create_admin

    created = await make_invite(db, INVITED, role=UserRole.VIEWER)
    await cmd_create_admin(db, argparse.Namespace(email=INVITED, name=None, locale=None))
    await assert_users(db, 1)

    device = SoftAuthenticator()
    options = options_of(await anon.post(f"/api/auth/invite/{created.token}/begin"))
    response = await anon.post(
        f"/api/auth/invite/{created.token}/complete",
        json={"credential": device.register(options["challenge"])},
    )

    await assert_refused(db, response, status=409, code="EMAIL_IN_USE", users=1)
    survivor = (await db.execute(select(User))).scalar_one()
    assert survivor.role == UserRole.ADMIN  # the shell's account, untouched


# ==============================================================================================
# 4. Invitations that are no longer live, by every method that accepts one
# ==============================================================================================


async def _spend(db: AsyncSession, created: auth_service.CreatedInvite, how: str) -> None:
    if how == "accepted":
        # Consumed the way the enrollment flow consumes it, by an account of its own.
        placeholder = await auth_service.create_user_from_invite(
            db, created.invite, display_name=None, locale=None
        )
        await auth_service.consume_invite(db, created.invite, placeholder)
    elif how == "revoked":
        created.invite.revoked_at = utcnow()
    elif how == "expired":
        created.invite.expires_at = utcnow() - timedelta(seconds=1)
    else:  # pragma: no cover - guard against a typo in the parametrization
        raise AssertionError(how)
    await db.commit()


DEAD_INVITE_CODES = {
    "accepted": "INVITE_ALREADY_USED",
    "revoked": "INVITE_REVOKED",
    "expired": "INVITE_EXPIRED",
}


@pytest.mark.parametrize("how", ["accepted", "revoked", "expired"])
async def test_a_dead_invitation_is_refused_by_every_route_that_takes_one(
    anon: AsyncClient, db: AsyncSession, how: str
) -> None:
    """Reading it, starting enrollment, completing enrollment — one shared validator, checked
    at all three doors, because a link that is dead in the UI but live at `complete` is a link
    that never really died."""
    created = await make_invite(db, INVITED)
    await _spend(db, created, how)
    expected_users = 1 if how == "accepted" else 0
    device = SoftAuthenticator()

    responses = {
        "read": await anon.get(f"/api/auth/invite/{created.token}"),
        "begin": await anon.post(f"/api/auth/invite/{created.token}/begin"),
        "complete": await anon.post(
            f"/api/auth/invite/{created.token}/complete",
            json={"credential": device.register(b64u(os.urandom(32)))},
        ),
    }

    for where, response in responses.items():
        assert response.status_code == 400, f"{where}: {response.text}"
        assert response.json()["error"]["code"] == DEAD_INVITE_CODES[how], where
        await assert_no_session(db, response)

    await assert_users(db, expected_users)


@pytest.mark.parametrize("how", ["accepted", "revoked", "expired"])
async def test_a_dead_invitation_cannot_start_a_google_flow(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, how: str
) -> None:
    """`/google/start?invite=…` validates through the same door, so the refusal lands on the
    page the person is already looking at rather than after a round trip to Google."""
    created = await make_invite(db, INVITED)
    await _spend(db, created, how)

    response = await client.get("/api/auth/google/start", params={"invite": created.token})

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == DEAD_INVITE_CODES[how]
    assert "set-cookie" not in response.headers, "a refused start left a browser binding behind"
    await assert_users(db, 1 if how == "accepted" else 0)


@pytest.mark.parametrize("how", ["accepted", "revoked", "expired"])
async def test_an_invitation_that_dies_mid_flow_cannot_be_completed_at_the_callback(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, how: str
) -> None:
    """The flow started while the invitation was live and the admin revoked it — or it
    expired, or somebody else used it — while the browser was at Google. Revocation that is
    only enforced at `start` is advisory."""
    created = await make_invite(db, INVITED)
    row = await begin(client, db, invite=created.token)
    assert row.invite_id == created.invite.id

    await _spend(db, created, how)
    before = await user_count(db)

    google.will_return(make_id_token(nonce=row.nonce, sub="sub-alice", email=INVITED))
    response = await callback(client, code="c", state=row.state)

    if how == "accepted":
        # The address now belongs to the account that already accepted it, so this is the
        # ordinary "an account exists at that verified address" path, not a second enrolment.
        assert response.status_code == 302
        assert await user_count(db) == before == 1
    else:
        assert error_of(response) == "GOOGLE_NO_ACCOUNT"
        assert "set-cookie" not in response.headers
        await assert_users(db, 0)


# ==============================================================================================
# 5. Sign in with Google
# ==============================================================================================


async def test_google_cannot_enrol_an_account_that_was_never_invited(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """A real Google account, a verified address, a token that verifies — and no invitation.
    This is the one that decides whether the deployment is invite-only at all."""
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-stranger", email=STRANGER))

    response = await callback(client, code="c", state=row.state)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    await assert_no_session(db, response)
    await assert_users(db, 0)


async def test_google_cannot_ride_an_invitation_addressed_to_somebody_else(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """The forwarded-link case: Mallory holds Alice's enrollment URL and completes the flow
    with her own Google account. If this worked, one leaked invitation would turn the install
    into an open registration form."""
    created = await make_invite(db, INVITED)

    row = await begin(client, db, invite=created.token)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-mallory", email=STRANGER))

    response = await callback(client, code="c", state=row.state)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    await assert_no_session(db, response)
    await assert_users(db, 0)
    await db.refresh(created.invite)
    assert created.invite.accepted_at is None, "Alice's invitation was spent by Mallory"
    assert created.invite.accepted_user_id is None


async def test_google_cannot_use_an_invitation_the_flow_did_not_start_from(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """An invitation for this very address exists and is live — but the flow began at the
    plain Google button, so no invitation is attached to it.

    Merely *existing* must not be enough, or the invitation stops being a token and becomes a
    list of addresses that may register: anyone who learns that Alice was invited could then
    sign up as Alice with a Google account of their own making.
    """
    created = await make_invite(db, INVITED)

    row = await begin(client, db)  # no `invite=`, so the state references none
    assert row.invite_id is None
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-alice", email=INVITED))

    response = await callback(client, code="c", state=row.state)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    await assert_no_session(db, response)
    await assert_users(db, 0)
    await db.refresh(created.invite)
    assert created.invite.accepted_at is None


@pytest.mark.parametrize("claimed", [False, "true", "True", 1, "1", None, [], {}])
async def test_an_unverified_google_email_is_refused_even_when_the_account_exists(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    claimed: Any,
) -> None:
    """`email_verified` is the entire basis for matching a Google token to a local account.
    Anything but the boolean `true` asserts nothing about the mailbox — a truthy *string* most
    of all, because that is what a lenient verifier would swallow."""
    row = await begin(client, db)
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-x", email=viewer.email, email_verified=claimed)
    )

    response = await callback(client, code="c", state=row.state)

    assert error_of(response) == "GOOGLE_EMAIL_UNVERIFIED"
    await assert_no_session(db, response)
    await assert_users(db, 1)


async def test_google_invitations_can_be_switched_off_and_then_an_invite_alone_is_not_enough(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """With `oauth.google.allow_invites` false, Google is a way into an account that already
    exists and nothing more. The only thing that would have matched here is the invitation."""
    await SettingsService(db).set("oauth.google.allow_invites", False)
    created = await make_invite(db, INVITED)

    row = await begin(client, db, invite=created.token)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-alice", email=INVITED))

    response = await callback(client, code="c", state=row.state)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    await assert_no_session(db, response)
    await assert_users(db, 0)
    await db.refresh(created.invite)
    assert created.invite.accepted_at is None


async def test_a_real_invitation_accepted_with_google_creates_exactly_one_user(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """The positive control for the Google path: same invitation, same address, one account."""
    created = await make_invite(db, INVITED, role=UserRole.VIEWER)

    row = await begin(client, db, invite=created.token)
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-alice", email=INVITED.upper(), name="Alicia")
    )

    response = await callback(client, code="c", state=row.state)

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/"
    assert response.cookies.get(get_settings().session_cookie_name)

    await assert_users(db, 1)
    user = (await db.execute(select(User))).scalar_one()
    assert user.email == INVITED  # normalized, whatever case Google asserted
    assert user.role == UserRole.VIEWER
    assert user.display_name == "Alicia"
    assert await sessions_for(db, user) == 1

    await db.refresh(created.invite)
    assert created.invite.accepted_at is not None
    assert created.invite.accepted_user_id == user.id

    # Signing in again is the "already linked" path, not a second account.
    again = await begin(client, db)
    google.will_return(make_id_token(nonce=again.nonce, sub="sub-alice", email=INVITED))
    assert (await callback(client, code="c", state=again.state)).status_code == 302
    await assert_users(db, 1)


# ==============================================================================================
# 6. The same attempts against an account that exists but is deactivated
# ==============================================================================================


async def test_a_deactivated_account_cannot_get_in_by_passkey(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """The passkey is genuine and the assertion verifies. The account is switched off, and
    that has to be the last word — the credential outlives the deactivation."""
    device = SoftAuthenticator()
    enrollment = await auth_service.begin_passkey_registration(db, user=viewer)
    registered = await auth_service.complete_passkey_registration(
        db, device.register(enrollment["challenge"]), user=viewer
    )
    assert registered.user_id == viewer.id
    viewer.is_active = False
    await db.commit()

    options = options_of(await anon.post("/api/auth/passkey/login/begin"))
    response = await anon.post(
        "/api/auth/passkey/login/complete",
        json={"credential": device.authenticate(options["challenge"])},
    )

    await assert_refused(db, response, status=403, code="ACCOUNT_DISABLED", users=1)


@pytest.mark.parametrize("linked", [True, False])
async def test_a_deactivated_account_cannot_get_in_by_google(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    linked: bool,
) -> None:
    """Both resolution paths that can reach an existing account have to check the flag: the
    one that matches a linked Google subject, and the one that matches a verified address."""
    if linked:
        await link_identity(db, viewer, sub="sub-viewer", email=viewer.email)
    viewer.is_active = False
    await db.commit()

    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-viewer", email=viewer.email))

    response = await callback(client, code="c", state=row.state)

    assert error_of(response) == "ACCOUNT_DISABLED"
    await assert_no_session(db, response)
    await assert_users(db, 1)


async def test_deactivating_an_account_kills_the_session_it_already_had(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """Otherwise "deactivate" would mean "cannot sign in again", which is not what an admin
    reaches for when a device goes missing."""
    await sign_in_with_cookie(db, anon, viewer)
    assert (await anon.get("/api/auth/session")).json()["user"]["id"] == viewer.id

    viewer.is_active = False
    await db.commit()

    assert (await anon.get("/api/auth/session")).json()["user"] is None
    assert (await anon.get("/api/auth/passkeys")).status_code == 401
    assert (await anon.get("/api/admin/users")).status_code == 401
    await assert_users(db, 1)


async def test_an_empty_household_cannot_be_entered_by_any_method_at_all(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """No users, no invitations, no credentials: the state a fresh install is in before the
    operator runs the CLI. Every public door, one after another."""
    device = SoftAuthenticator()

    requested = await anon.post("/api/auth/magic-link/request", json={"email": STRANGER})
    assert requested.status_code == 204

    consumed = await anon.post(
        "/api/auth/magic-link/consume", json={"token": new_token(32)}
    )
    assert consumed.status_code == 401

    options = options_of(await anon.post("/api/auth/passkey/login/begin"))
    logged_in = await anon.post(
        "/api/auth/passkey/login/complete",
        json={"credential": device.authenticate(options["challenge"])},
    )
    assert logged_in.status_code == 401

    enrolled = await anon.post(
        f"/api/auth/invite/{new_token(32)}/complete",
        json={"credential": device.register(b64u(os.urandom(32)))},
    )
    assert enrolled.status_code == 404

    # Google is not even configured on a fresh install.
    assert (await anon.get("/api/auth/google/start")).status_code == 403

    for response in (requested, consumed, logged_in, enrolled):
        await assert_no_session(db, response)
    assert (await anon.get("/api/auth/session")).json()["user"] is None
    await assert_users(db, 0)


# ==============================================================================================
# 7. The founding account
# ==============================================================================================


@pytest.fixture
async def founder(db: AsyncSession) -> User:
    """The account the household was bootstrapped with: active admin, protected."""
    from app.cli import cmd_create_admin

    await cmd_create_admin(
        db, argparse.Namespace(email="founder@example.com", name="Fundadora", locale="es")
    )
    user = (
        await db.execute(select(User).where(User.email == "founder@example.com"))
    ).scalar_one()
    assert user.is_protected is True, "create-admin did not protect the founding account"
    return user


@pytest.fixture
async def second_admin(db: AsyncSession, founder: User) -> User:
    """A second admin — the caller the guard exists to stop, or whoever holds their session."""
    from app.cli import cmd_create_admin

    await cmd_create_admin(
        db, argparse.Namespace(email="second@example.com", name="Segundo", locale="es")
    )
    user = (
        await db.execute(select(User).where(User.email == "second@example.com"))
    ).scalar_one()
    assert user.is_protected is False
    return user


ATTEMPTS: dict[str, dict[str, Any]] = {
    "demote": {"role": "viewer"},
    "deactivate": {"isActive": False},
    "rename": {"displayName": "Renombrada"},
    "re-assert the role it has": {"role": "admin"},
}


@pytest.mark.parametrize("what", list(ATTEMPTS))
async def test_a_second_admin_cannot_change_the_founding_account(
    anon: AsyncClient, db: AsyncSession, founder: User, second_admin: User, what: str
) -> None:
    """Signed in as a *real* second admin — a genuine session cookie, `require_admin` running
    for real — because the interesting caller is the other admin, or whoever took their laptop.

    The no-op patches matter as much as the destructive ones: letting one through because it
    happened to change nothing is the subtlety a later refactor turns into a hole.
    """
    await sign_in_with_cookie(db, anon, second_admin)

    response = await anon.patch(f"/api/admin/users/{founder.id}", json=ATTEMPTS[what])

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROTECTED_ACCOUNT"

    await db.refresh(founder)
    assert founder.role == UserRole.ADMIN
    assert founder.is_active is True
    assert founder.display_name == "Fundadora"
    assert founder.is_protected is True
    await assert_users(db, 2)


async def test_a_second_admin_cannot_delete_the_founding_account(
    anon: AsyncClient, db: AsyncSession, founder: User, second_admin: User
) -> None:
    await sign_in_with_cookie(db, anon, second_admin)

    response = await anon.delete(f"/api/admin/users/{founder.id}")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROTECTED_ACCOUNT"
    assert await db.get(User, founder.id) is not None
    await assert_users(db, 2)


async def test_the_guard_does_not_spare_the_protected_admin_themselves(
    anon: AsyncClient, db: AsyncSession, founder: User, second_admin: User
) -> None:
    """No "unless it is you" hole: whoever holds that session is not necessarily the person,
    and that is the whole threat. `/api/me` remains the door for the person."""
    await sign_in_with_cookie(db, anon, founder)

    through_admin = await anon.patch(
        f"/api/admin/users/{founder.id}", json={"displayName": "Yo Misma"}
    )
    assert through_admin.status_code == 409
    assert through_admin.json()["error"]["code"] == "PROTECTED_ACCOUNT"

    themselves = await anon.patch("/api/me", json={"displayName": "Yo Misma"})
    assert themselves.status_code == 200, themselves.text

    await db.refresh(founder)
    assert founder.display_name == "Yo Misma"
    assert founder.is_protected is True
    await assert_users(db, 2)


async def test_the_shell_is_the_authority_the_web_ui_is_not(
    anon: AsyncClient, db: AsyncSession, founder: User, second_admin: User
) -> None:
    """The escape hatch, end to end.

    `python -m app.cli unprotect` is what moves the flag — the admin API cannot, which is why
    the guard is not a trap when the founding address has to change hands. Once the shell has
    spoken, the same three operations the API just refused all go through.
    """
    from app.cli import cmd_protect, cmd_unprotect

    await sign_in_with_cookie(db, anon, second_admin)
    assert await cmd_unprotect(db, argparse.Namespace(email="founder@example.com")) == 0
    await db.refresh(founder)
    assert founder.is_protected is False

    demoted = await anon.patch(f"/api/admin/users/{founder.id}", json={"role": "viewer"})
    assert demoted.status_code == 200, demoted.text
    deactivated = await anon.patch(f"/api/admin/users/{founder.id}", json={"isActive": False})
    assert deactivated.status_code == 200, deactivated.text

    founder_id = founder.id
    deleted = await anon.delete(f"/api/admin/users/{founder_id}")
    assert deleted.status_code == 200, deleted.text
    db.expire_all()
    assert await db.get(User, founder_id) is None
    await assert_users(db, 1)

    # And the flag can be re-armed on whoever is now the way back in.
    assert await cmd_protect(db, argparse.Namespace(email="second@example.com")) == 0
    await db.refresh(second_admin)
    assert second_admin.is_protected is True

    blocked = await anon.patch(f"/api/admin/users/{second_admin.id}", json={"role": "viewer"})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PROTECTED_ACCOUNT"
    await assert_users(db, 1)


async def test_self_service_cannot_promote_anyone_including_the_person_using_it(
    anon: AsyncClient, db: AsyncSession, founder: User, second_admin: User
) -> None:
    """`/api/me` is the one surface a non-admin can write to, so what it accepts is the whole
    of what a viewer can change about themselves. Role, address and the protection flag are
    not among its fields; extra keys are ignored rather than honoured."""
    await sign_in_with_cookie(db, anon, second_admin)

    response = await anon.patch(
        "/api/me",
        json={
            "displayName": "Segunda",
            "role": "admin",
            "isProtected": True,
            "isActive": True,
            "email": "someone.else@example.com",
        },
    )

    assert response.status_code == 200, response.text
    await db.refresh(second_admin)
    assert second_admin.display_name == "Segunda"
    assert second_admin.email == "second@example.com"
    assert second_admin.is_protected is False
    await assert_users(db, 2)


async def test_a_viewer_cannot_reach_the_admin_surface_at_all(
    anon: AsyncClient, db: AsyncSession, founder: User
) -> None:
    """Before any of the above matters, the surface has to be admin-only."""
    from app.cli import cmd_invite

    await cmd_invite(
        db, argparse.Namespace(email="viewer@example.com", admin=False, url=None)
    )
    invite = (
        await db.execute(select(Invite).where(Invite.email == "viewer@example.com"))
    ).scalar_one()
    viewer = await auth_service.create_user_from_invite(
        db, invite, display_name="Espectadora", locale="es"
    )
    await auth_service.consume_invite(db, invite, viewer)
    await db.commit()

    await sign_in_with_cookie(db, anon, viewer)

    assert (await anon.get("/api/admin/users")).status_code == 403
    patched = await anon.patch(f"/api/admin/users/{founder.id}", json={"role": "viewer"})
    assert patched.status_code == 403
    assert patched.json()["error"]["code"] == "ADMIN_REQUIRED"
    invited = await anon.post("/api/admin/invites", json={"email": "x@example.com"})
    assert invited.status_code == 403

    await db.refresh(founder)
    assert founder.role == UserRole.ADMIN
    await assert_users(db, 2)


# ==============================================================================================
# 8. The CLI, which is the operator standing at the box
# ==============================================================================================


async def test_the_cli_is_the_only_thing_that_can_start_a_household(
    anon: AsyncClient, db: AsyncSession
) -> None:
    """`create-admin` on an empty database is the one account nobody invited — and it is
    reachable only from a shell, which already has the database and the secret key."""
    from app.cli import cmd_create_admin

    await assert_users(db, 0)
    args = argparse.Namespace(email="op@example.com", name=None, locale=None)
    assert await cmd_create_admin(db, args) == 0
    await assert_users(db, 1)

    user = (await db.execute(select(User))).scalar_one()
    assert user.role == UserRole.ADMIN
    assert user.is_protected is True
    # No passkey, no Google, no session: the account exists and still cannot be entered until
    # somebody enrols a credential for it.
    assert user.credentials == []
    assert await sessions_for(db, user) == 0

    # `login-link` is how the operator gets in the first time, and it is a normal magic link.
    from app.cli import cmd_login_link

    assert await cmd_login_link(
        db, argparse.Namespace(email="op@example.com", ttl=60, url=None)
    ) == 0
    link = (await db.execute(select(MagicLink))).scalar_one()
    assert link.user_id == user.id
    await assert_users(db, 1)


async def test_a_cli_sign_in_link_still_works_when_magic_links_are_switched_off(
    anon: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """Deliberate, and worth pinning down: `auth.magic_link_enabled` governs whether the login
    page will *mail* a link, not whether a link the operator printed from a shell can be
    redeemed. Enforcing the switch at consume time would break the documented recovery path
    for a headless box — and it would grant nothing to an attacker either way, since a link
    only ever names an account that already exists.
    """
    await SettingsService(db).set("auth.magic_link_enabled", False)
    await db.commit()

    refused = await anon.post("/api/auth/magic-link/request", json={"email": viewer.email})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "MAGIC_LINK_DISABLED"
    assert await magic_links(db) == 0

    token = await issue_magic_link(db, viewer)  # what `cmd_login_link` writes
    signed_in = await anon.post("/api/auth/magic-link/consume", json={"token": token})
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["user"]["id"] == viewer.id
    await assert_users(db, 1)
