"""Sign in with Google, from the attacker's side.

`test_google_auth.py` proves the feature works and refuses the obvious. This file is the
adversarial half: every test here is an *attempt to obtain something the deployment never
granted* — a session, someone else's account, a token, a redirect off-origin — and every one
of them asserts the attempt fails closed and leaves nothing behind.

The invariant under attack throughout:

    dramatarr is invite-only. No one without an existing account, or a valid invitation
    addressed to their own verified Google address, may ever obtain a session.

Three habits keep these tests honest:

* **Nothing behind.** A refusal is only a refusal if it also created no `User`, no
  `OAuthIdentity` and no `AuthSession`, and set no cookie. `assert_nothing_granted` checks all
  four, so a "refusal" that quietly enrolled someone cannot pass.
* **One knob at a time.** Each forged token differs from a *passing* token in exactly the
  claim under attack, so a test that passes proves the check it names rather than some
  unrelated malformation.
* **Two browsers.** Several attacks only exist across browsers — a callback replayed into
  someone else's session. Those use a second cookie jar rather than a second override, because
  the cookie jar is the thing the real attack manipulates.

Google is never contacted: the fake from `test_google_auth` replaces the module's entire
outbound HTTP surface, and any request it does not recognize is an explicit failure.
"""

# ruff: noqa: F811
# The fixtures come from `test_google_auth` by import, which is the supported way to share
# them between modules; every test that then names one as a parameter looks to ruff like a
# redefinition of the imported name. There are ~150 of those and none is a real shadowing.

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# The harness is shared on purpose: an attack proved against a *different* fake Google would
# prove nothing about the code the other file exercises.
from test_google_auth import (  # noqa: F401  (fixtures are used by name)
    _KEY,
    _OTHER_KEY,
    CLIENT_ID,
    CLIENT_SECRET,
    KID,
    FakeGoogle,
    admin,
    b64u,
    begin,
    callback,
    client,
    error_of,
    google,
    google_on,
    link_identity,
    make_id_token,
    make_invite,
    query_of,
    state_row,
    user_count,
    viewer,
    viewer_client,
)

from app.errors import AppError
from app.models import AuthSession, Credential, OAuthIdentity, OAuthStateKind, User
from app.services import oauth as oauth_service
from app.services.settings import SettingsService
from app.utils.time import utcnow

pytestmark = pytest.mark.anyio

STRANGER = "mallory@evil.example"
ACCESS_TOKEN = "ya29.super-secret-access-token"


# --- Shared assertions ---------------------------------------------------------------------


async def assert_nothing_granted(
    db: AsyncSession,
    response: Any,
    *,
    users_before: int,
    code: str | None = None,
) -> None:
    """A refusal that left a trace is not a refusal.

    Checks the four things an attacker actually wants: a cookie, a session row, an account,
    and a federated identity bound to one.
    """
    assert response.status_code == 302, response.text
    if code is not None:
        assert error_of(response) == code
    assert "set-cookie" not in response.headers, "a refused callback handed out a session cookie"
    assert not response.cookies

    sessions = int((await db.execute(select(func.count()).select_from(AuthSession))).scalar_one())
    assert sessions == 0, "a refused callback minted a session row"
    assert await user_count(db) == users_before, "a refused callback created an account"


def assert_origin_relative(location: str) -> None:
    """The `Location` of any redirect this feature emits must stay inside this origin.

    `//evil.example` and `/\\evil.example` are read by browsers as protocol-relative URLs, and
    a bare CR or LF in a header value splits the response, so all three are checked on the raw
    string rather than on anything a parser has already normalized away.
    """
    assert not any(ch in location for ch in "\r\n\x00"), location
    assert location.startswith("/"), location
    assert not location.startswith(("//", "/\\")), location
    split = urlsplit(location)
    assert split.scheme == "", location
    assert split.netloc == "", location


def unsigned_token(payload: dict[str, Any], *, alg: str = "none", signature: bytes = b"") -> str:
    header = {"alg": alg, "kid": KID, "typ": "JWT"}
    body = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(payload).encode())}"
    return f"{body}.{b64u(signature)}"


async def refusal_code(nonce: str, token: str) -> str:
    with pytest.raises(AppError) as excinfo:
        await oauth_service.verify_id_token(token, client_id=CLIENT_ID, nonce=nonce)
    return str(excinfo.value.code)


@pytest.fixture
async def second_browser(db: AsyncSession, client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """A different browser against the same server.

    `client` has already installed the dependency overrides (they are global) and will clear
    them; all this adds is a second, empty cookie jar — which is exactly what a cross-browser
    replay has and the attacker's own browser does not.
    """
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as other:
        yield other


# ==============================================================================================
# 1. A session for someone who was never invited
# ==============================================================================================


async def test_attack_a_stranger_with_no_account_and_no_invitation_gets_nothing(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """The headline attack: a perfectly valid Google account, verified email and all, that
    this deployment has never heard of."""
    before = await user_count(db)
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-stranger", email=STRANGER))

    response = await callback(client, code="c", state=row.state)

    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_NO_ACCOUNT")
    assert await oauth_service.identity_for_subject(db, "sub-stranger") is None


async def test_attack_a_stranger_cannot_ride_an_invitation_they_never_received(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """An invitation exists, is pending, and is for somebody else. Merely *existing* must not
    be enough — the flow has to have been started from that invitation's own link."""
    await make_invite(db, "alice@example.com")
    before = await user_count(db)

    row = await begin(client, db)  # no `invite=`, so the state references none
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-stranger", email=STRANGER))

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_NO_ACCOUNT")


async def test_attack_an_invitation_for_alice_cannot_be_accepted_by_mallory(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """A forwarded or intercepted invitation link is the realistic version of this: Mallory
    holds Alice's token and completes the flow with her own Google account."""
    from app.services.auth import create_invite

    created = await create_invite(db, email="alice@example.com")
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    assert row.invite_id == created.invite.id
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-mallory", email=STRANGER))

    response = await callback(client, code="c", state=row.state)

    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_NO_ACCOUNT")
    await db.refresh(created.invite)
    assert created.invite.accepted_at is None, "Alice's invitation was burned by Mallory"
    assert created.invite.accepted_user_id is None


@pytest.mark.parametrize(
    "impostor",
    [
        "alice@example.com.evil.example",  # suffix
        "evil.example.com@alice.example",  # the whole thing rearranged
        "alice@example.co",  # one character short
        "alicE@example.example",  # right local part, wrong domain
        "alice+admin@example.com",  # sub-addressing is a different mailbox
        "alice@examplecom",
    ],
)
async def test_attack_lookalike_addresses_do_not_satisfy_an_invitation(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    impostor: str,
) -> None:
    from app.services.auth import create_invite

    created = await create_invite(db, email="alice@example.com")
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    google.will_return(make_id_token(nonce=row.nonce, sub=f"sub-{impostor}", email=impostor))

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_NO_ACCOUNT")


async def test_attack_a_revoked_invitation_cannot_be_completed_after_the_flow_started(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """`/google/start` validated the invitation minutes ago. The admin revoking it in between
    has to be honoured at the callback too, or revocation is advisory."""
    from app.services.auth import create_invite

    created = await create_invite(db, email="alice@example.com")
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    created.invite.revoked_at = utcnow()
    await db.commit()

    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-alice", email="alice@example.com")
    )
    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_NO_ACCOUNT")


async def test_attack_an_expired_invitation_cannot_be_completed_after_the_flow_started(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    from app.services.auth import create_invite

    created = await create_invite(db, email="alice@example.com")
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    created.invite.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()

    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-alice", email="alice@example.com")
    )
    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_NO_ACCOUNT")


async def test_attack_one_invitation_cannot_enrol_two_accounts(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """Two flows started from the same link, both completed. The second must find the
    invitation already spent."""
    from app.services.auth import create_invite

    created = await create_invite(db, email="alice@example.com")
    await db.commit()
    before = await user_count(db)

    first = await begin(client, db, invite=created.token)
    second = await begin(client, db, invite=created.token)

    google.will_return(
        make_id_token(nonce=first.nonce, sub="sub-alice-1", email="alice@example.com")
    )
    assert (await callback(client, code="c", state=first.state)).headers["location"] == "/"
    assert await user_count(db) == before + 1

    google.will_return(
        make_id_token(nonce=second.nonce, sub="sub-alice-2", email="alice@example.com")
    )
    response = await callback(client, code="c", state=second.state)

    # The second attempt now matches the account the first one created, by verified email —
    # but that account already answers to a different Google identity, so it is refused
    # rather than silently repointed. Either way: no second account.
    assert error_of(response) == "GOOGLE_ACCOUNT_MISMATCH"
    assert await user_count(db) == before + 1
    assert await oauth_service.identity_for_subject(db, "sub-alice-2") is None


@pytest.mark.parametrize("linked", [True, False])
async def test_attack_a_deactivated_account_cannot_be_reached_by_any_resolution_path(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    linked: bool,
) -> None:
    """Path 1 (already linked) and path 2 (verified email matches) both have to check it."""
    if linked:
        await link_identity(db, viewer, sub="sub-viewer", email=viewer.email)
    viewer.is_active = False
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-viewer", email=viewer.email))

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="ACCOUNT_DISABLED")


async def test_attack_deactivating_mid_flow_is_honoured_at_the_callback(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """The admin pressed "deactivate" while the browser was at Google."""
    before = await user_count(db)
    row = await begin(client, db)
    viewer.is_active = False
    await db.commit()

    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))
    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="ACCOUNT_DISABLED")


@pytest.mark.parametrize("claimed", ["true", "TRUE", 1, "1", "yes", None, [], {}, "false"])
async def test_attack_an_unverified_google_email_never_matches_an_account(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    claimed: Any,
) -> None:
    """`email_verified` is the *entire* basis for matching a token to an account, so anything
    other than the boolean `true` has to be worth nothing — a truthy string most of all."""
    before = await user_count(db)
    row = await begin(client, db)
    google.will_return(
        make_id_token(nonce=row.nonce, email=viewer.email, email_verified=claimed)
    )

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(
        db, response, users_before=before, code="GOOGLE_EMAIL_UNVERIFIED"
    )


# ==============================================================================================
# 2. State: replay, and crossing the two flows
# ==============================================================================================


async def test_attack_a_state_cannot_be_replayed_even_when_the_first_attempt_failed(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """A failed attempt must still spend its state, or an attacker gets unlimited retries at
    one authorization code with different forged tokens."""
    row = await begin(client, db)
    google.will_return(make_id_token(nonce="not-the-nonce", email=viewer.email))
    assert error_of(await callback(client, code="c", state=row.state)) == "GOOGLE_TOKEN_INVALID"

    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))
    retry = await callback(client, code="c", state=row.state)
    assert error_of(retry) == "GOOGLE_STATE_USED"
    assert "set-cookie" not in retry.headers


async def test_attack_a_link_state_cannot_be_redeemed_as_a_login(
    viewer_client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    admin: User,
) -> None:
    """A link state carries an account and skips account resolution. Redeeming one as a login
    would hand out a session on rules the login flow never applied."""
    started = await viewer_client.post("/api/auth/google/link/start")
    row = await state_row(db, query_of(started.json()["authorizationUrl"])["state"])
    assert row.kind == OAuthStateKind.LINK

    before = await user_count(db)
    # A token for the *admin's* address, presented anonymously against the viewer's link state.
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-admin", email=admin.email))

    from app.api.deps import get_current_user_optional
    from app.main import app

    app.dependency_overrides[get_current_user_optional] = lambda: None
    response = await callback(viewer_client, code="c", state=row.state)

    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_STATE_INVALID")
    assert await oauth_service.identity_for_subject(db, "sub-admin") is None


async def test_attack_a_login_state_cannot_be_redeemed_as_a_link(
    viewer_client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
) -> None:
    """The mirror image: a login state completed inside a signed-in browser must run the login
    rules — it must not quietly attach a stranger's Google account to that session."""
    row = await begin(viewer_client, db)  # a login state, minted by a signed-in browser
    assert row.kind == OAuthStateKind.LOGIN
    assert row.user_id is None
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-stranger", email=STRANGER))

    response = await callback(viewer_client, code="c", state=row.state)

    assert error_of(response) == "GOOGLE_NO_ACCOUNT"
    assert "set-cookie" not in response.headers
    assert await oauth_service.identity_for_subject(db, "sub-stranger") is None
    await db.refresh(viewer, ["oauth_identities"])
    assert viewer.oauth_identities == []


async def test_attack_a_nonce_from_a_concurrent_attempt_cannot_be_replayed(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """Two flows in flight at once. A token minted for one must not complete the other, or the
    nonce is decoration."""
    first = await begin(client, db)
    second = await begin(client, db)
    assert first.nonce != second.nonce

    google.will_return(make_id_token(nonce=second.nonce, email=viewer.email))
    response = await callback(client, code="c", state=first.state)

    assert error_of(response) == "GOOGLE_TOKEN_INVALID"
    assert "set-cookie" not in response.headers


async def test_attack_a_callback_cannot_plant_a_session_in_another_browser(
    client: AsyncClient,
    second_browser: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
) -> None:
    """Login CSRF (RFC 6819 §4.4.1.8).

    The attacker completes their own Google authorization, stops before following the final
    redirect, and delivers the resulting `code`/`state` to a victim as an ordinary link. The
    callback is a GET and the victim's browser follows it. If `state` is only a server-side
    row, nothing distinguishes the victim's browser from the attacker's, and the victim is
    silently signed in *as the attacker* — after which everything they watch, resume or import
    lands in the attacker's account.

    So the state has to be bound to the browser that minted it, not merely to the server.
    """
    row = await begin(client, db)  # the attacker's browser starts the flow
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-attacker", email=viewer.email))

    # Same URL, delivered to a browser that never visited `/google/start`.
    stolen = await second_browser.get(
        "/api/auth/google/callback", params={"code": "c", "state": row.state}
    )

    assert "set-cookie" not in stolen.headers, (
        "a callback replayed into a foreign browser handed it a session"
    )
    assert error_of(stolen) == "GOOGLE_STATE_INVALID"


async def test_attack_a_link_callback_cannot_be_replayed_into_a_foreign_browser(
    viewer_client: AsyncClient,
    second_browser: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
) -> None:
    """The same replay aimed at the link flow, which would attach the attacker's Google
    account to whoever's browser followed the link."""
    started = await viewer_client.post("/api/auth/google/link/start")
    row = await state_row(db, query_of(started.json()["authorizationUrl"])["state"])
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-attacker", email=STRANGER))

    from app.api.deps import get_current_user_optional
    from app.main import app

    app.dependency_overrides[get_current_user_optional] = lambda: None
    replayed = await second_browser.get(
        "/api/auth/google/callback", params={"code": "c", "state": row.state}
    )

    assert error_of(replayed) == "GOOGLE_STATE_INVALID"
    assert await oauth_service.identity_for_subject(db, "sub-attacker") is None


async def test_attack_a_forged_browser_binding_does_not_unlock_a_state(
    client: AsyncClient,
    second_browser: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
) -> None:
    """Holding the state *and* guessing at the binding cookie is still not enough — the server
    compares against a hash, so the cookie has to be the real one."""
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    # Set without a domain on purpose: `http.cookiejar` silently refuses to *send* a cookie
    # pinned to a single-label domain like `test`, so pinning one here would have made this
    # test a re-run of "a browser with no cookie at all" rather than the forgery it names.
    second_browser.cookies.set(oauth_service.BROWSER_COOKIE_NAME, "x" * 43)
    forged = await second_browser.get(
        "/api/auth/google/callback", params={"code": "c", "state": row.state}
    )

    assert error_of(forged) == "GOOGLE_STATE_INVALID"
    assert "set-cookie" not in forged.headers


async def test_attack_a_refused_foreign_callback_does_not_burn_the_real_flow(
    client: AsyncClient,
    second_browser: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
) -> None:
    """The binding is checked *before* the state is burned, so someone who knows a state
    cannot spend a stranger's in-flight sign-in just by loading the URL once."""
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    spoiler = await second_browser.get(
        "/api/auth/google/callback", params={"code": "c", "state": row.state}
    )
    assert error_of(spoiler) == "GOOGLE_STATE_INVALID"
    assert (await state_row(db, row.state)).consumed_at is None

    # The browser that actually started it still completes normally.
    genuine = await callback(client, code="c", state=row.state)
    assert genuine.headers["location"] == "/"
    assert "set-cookie" in genuine.headers


async def test_the_binding_does_not_break_two_tabs_signing_in_at_once(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """Not an attack — the regression the binding could easily have caused.

    One browser, two tabs, two states in flight. The binding is per *browser*, not per state,
    so the second `/google/start` must reuse the cookie rather than invalidate the first tab.
    """
    first = await begin(client, db)
    second = await begin(client, db)
    assert first.state != second.state
    assert first.browser_hash == second.browser_hash

    google.will_return(make_id_token(nonce=second.nonce, email=viewer.email))
    assert (await callback(client, code="c", state=second.state)).headers["location"] == "/"

    google.will_return(make_id_token(nonce=first.nonce, email=viewer.email))
    assert (await callback(client, code="c", state=first.state)).headers["location"] == "/"


async def test_attack_the_binding_cookie_is_not_reachable_from_script_or_other_paths(
    client: AsyncClient, google: FakeGoogle, google_on: None
) -> None:
    started = await client.get("/api/auth/google/start")
    header = started.headers["set-cookie"]

    assert header.startswith(f"{oauth_service.BROWSER_COOKIE_NAME}=")
    assert "HttpOnly" in header
    # `Strict` would be dropped on Google's own redirect back, which is the one request the
    # cookie exists for; `Lax` is sent on a top-level GET navigation, which that redirect is.
    assert "SameSite=lax" in header
    assert f"Path={oauth_service.BROWSER_COOKIE_PATH}" in header
    # It must outlive the state it unlocks, so that a slow user is told their attempt expired
    # rather than that it came from another browser.
    assert oauth_service.BROWSER_COOKIE_MAX_AGE > oauth_service.STATE_TTL_MINUTES * 60


async def test_a_slow_user_is_told_the_attempt_expired_not_that_it_was_someone_else(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """The binding must not swallow the expiry message it sits in front of."""
    row = await begin(client, db)
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    assert error_of(await callback(client, code="c", state=row.state)) == "GOOGLE_STATE_EXPIRED"


async def test_attack_a_refused_start_leaves_no_binding_cookie_behind(
    client: AsyncClient, google: FakeGoogle
) -> None:
    refused = await client.get("/api/auth/google/start")
    assert refused.status_code == 403
    assert "set-cookie" not in refused.headers


# ==============================================================================================
# 3. Forged id_tokens
# ==============================================================================================


async def test_attack_algorithm_confusion_hs256_signed_with_googles_public_key(
    google: FakeGoogle,
) -> None:
    """The classic: take the public key everyone can fetch, use it as an HMAC secret, and
    claim `alg: HS256`. A verifier that picks its algorithm from the header falls for it."""
    numbers = _KEY.public_key().public_numbers()
    secret = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")

    header = {"alg": "HS256", "kid": KID, "typ": "JWT"}
    payload = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "sub-forged",
        "email": "viewer@example.com",
        "email_verified": True,
        "nonce": "the-nonce",
        "iat": int(utcnow().timestamp()),
        "exp": int(utcnow().timestamp()) + 3600,
    }
    body = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(payload).encode())}"
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()

    assert await refusal_code("the-nonce", f"{body}.{b64u(signature)}") == "GOOGLE_TOKEN_INVALID"


@pytest.mark.parametrize("alg", ["none", "None", "NONE", "RS256 ", "rs256", "HS256", "PS256"])
async def test_attack_only_rs256_is_accepted(google: FakeGoogle, alg: str) -> None:
    payload = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "s",
        "email": "viewer@example.com",
        "email_verified": True,
        "nonce": "the-nonce",
        "iat": int(utcnow().timestamp()),
        "exp": int(utcnow().timestamp()) + 3600,
    }
    token = unsigned_token(payload, alg=alg)
    assert await refusal_code("the-nonce", token) == "GOOGLE_TOKEN_INVALID"


@pytest.mark.parametrize(
    "issuer",
    [
        "https://accounts.google.com.evil.example",
        "https://accounts.google.com@evil.example",
        "https://accounts.google.com/",
        "http://accounts.google.com",
        "https://ACCOUNTS.GOOGLE.COM",
        "https://accounts.google.com.",
        "evil.example",
        "",
    ],
)
async def test_attack_issuer_lookalikes_are_refused(google: FakeGoogle, issuer: str) -> None:
    token = make_id_token(nonce="the-nonce", iss=issuer)
    assert await refusal_code("the-nonce", token) == "GOOGLE_TOKEN_INVALID"


@pytest.mark.parametrize(
    "aud",
    [
        "other-client.apps.googleusercontent.com",
        ["other-client.apps.googleusercontent.com"],
        [],
        None,
        "",
        f"{CLIENT_ID}.evil.example",
        f"prefix-{CLIENT_ID}",
        {"aud": CLIENT_ID},
    ],
)
async def test_attack_a_token_minted_for_another_client_is_refused(
    google: FakeGoogle, aud: Any
) -> None:
    """The realistic shape of this: the attacker owns a Google client of their own, signs a
    user in there, and presents the resulting id_token here."""
    token = make_id_token(nonce="the-nonce", aud=aud)
    assert await refusal_code("the-nonce", token) == "GOOGLE_TOKEN_INVALID"


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"exp_delta": -3600}, "long expired"),
        ({"exp_delta": -(oauth_service.CLOCK_SKEW_SECONDS + 5)}, "just outside the skew"),
        ({"iat_delta": 3600}, "issued an hour in the future"),
    ],
)
async def test_attack_stale_and_future_tokens_are_refused(
    google: FakeGoogle, kwargs: dict[str, int], why: str
) -> None:
    token = make_id_token(nonce="the-nonce", **kwargs)
    assert await refusal_code("the-nonce", token) == "GOOGLE_TOKEN_INVALID", why


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": None},
        {"exp": "not-a-number"},
        {"exp": True},
        {"iat": None},
        {"sub": 12345},
        {"sub": ""},
        {"sub": None},
        {"email": None},
        {"email": ["viewer@example.com"]},
        {"nonce": 12345},
        {"nonce": None},
    ],
)
async def test_attack_claims_of_the_wrong_type_are_refused_not_coerced(
    google: FakeGoogle, claims: dict[str, Any]
) -> None:
    """A verifier that stringifies whatever it finds turns `sub: 12345` into the identifier
    `"12345"`, which is a perfectly good account key for whoever can mint one."""
    now = int(utcnow().timestamp())
    payload: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "sub-ok",
        "email": "viewer@example.com",
        "email_verified": True,
        "nonce": "the-nonce",
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(claims)
    # Signed properly: only the claim under test is wrong.
    header = {"alg": "RS256", "kid": KID, "typ": "JWT"}
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    body = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(payload).encode())}"
    signature = _KEY.sign(body.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())

    code = await refusal_code("the-nonce", f"{body}.{b64u(signature)}")
    assert code in {"GOOGLE_TOKEN_INVALID", "GOOGLE_EMAIL_UNVERIFIED"}


async def test_attack_a_token_signed_by_a_key_google_never_published_is_refused(
    google: FakeGoogle,
) -> None:
    """Right `kid`, right shape, wrong key — the attacker's own RSA key pair."""
    token = make_id_token(nonce="the-nonce", key=_OTHER_KEY)
    assert await refusal_code("the-nonce", token) == "GOOGLE_TOKEN_INVALID"


async def test_attack_a_forged_token_never_reaches_account_resolution(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """End to end, not just the verifier: a token forged for an account that really exists
    must be refused before anything looks that account up."""
    before = await user_count(db)
    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email, key=_OTHER_KEY))

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_TOKEN_INVALID")


@pytest.mark.parametrize(
    "token",
    ["", ".", "..", "a.b", "a.b.c.d", "not-a-jwt", "$$$.$$$.$$$", "e30.e30.e30"],
)
async def test_attack_malformed_tokens_are_refused_rather_than_crashing(
    google: FakeGoogle, token: str
) -> None:
    assert await refusal_code("the-nonce", token) == "GOOGLE_TOKEN_INVALID"


async def test_attack_a_json_array_body_is_refused(google: FakeGoogle) -> None:
    """A payload segment that decodes to something other than an object."""
    header = b64u(json.dumps({"alg": "RS256", "kid": KID}).encode())
    body = b64u(json.dumps(["not", "an", "object"]).encode())
    assert await refusal_code("the-nonce", f"{header}.{body}.{b64u(b'sig')}") == (
        "GOOGLE_TOKEN_INVALID"
    )


# ==============================================================================================
# 4. Open redirect
# ==============================================================================================


HOSTILE_NEXT = [
    "//evil.example",
    "///evil.example",
    "//evil.example/path",
    "/\\evil.example",
    "/\\/evil.example",
    "https://evil.example",
    "http://evil.example",
    "HTTPS://evil.example",
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "\\\\evil.example",
    "evil.example",
    "",
    "/path\r\nSet-Cookie: dramatarr_session=stolen",
    "/path\nLocation: https://evil.example",
    "/path\x00",
]


@pytest.mark.parametrize("hostile", HOSTILE_NEXT)
async def test_attack_next_cannot_leave_the_origin_on_login_start(
    client: AsyncClient, google: FakeGoogle, google_on: None, hostile: str
) -> None:
    response = await client.get("/api/auth/google/start", params={"next": hostile})
    # An empty `next` is simply absent, not hostile; everything else must be refused outright.
    if hostile == "":
        assert response.status_code == 200
        return
    assert response.status_code == 400, hostile
    assert response.json()["error"]["code"] == "INVALID_NEXT_PATH", hostile


@pytest.mark.parametrize("hostile", HOSTILE_NEXT)
async def test_attack_next_cannot_leave_the_origin_on_link_start(
    viewer_client: AsyncClient, google: FakeGoogle, google_on: None, viewer: User, hostile: str
) -> None:
    response = await viewer_client.post("/api/auth/google/link/start", params={"next": hostile})
    if hostile == "":
        assert response.status_code == 200
        return
    assert response.status_code == 400, hostile
    assert response.json()["error"]["code"] == "INVALID_NEXT_PATH", hostile


@pytest.mark.parametrize(
    "sneaky",
    [
        "/\t//evil.example",  # browsers strip raw tabs from URLs before parsing
        "/\t/\\evil.example",
        "/ //evil.example",
        "/%2f%2fevil.example",
        "/%09//evil.example",
        "/..//evil.example",
        "/.//evil.example",
    ],
)
async def test_attack_a_sneaky_next_still_lands_inside_the_origin(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    sneaky: str,
) -> None:
    """Whatever `/google/start` chooses to accept, the `Location` a successful callback emits
    must not be readable as another origin."""
    started = await client.get("/api/auth/google/start", params={"next": sneaky})
    if started.status_code == 400:
        assert started.json()["error"]["code"] == "INVALID_NEXT_PATH"
        return

    row = await state_row(db, query_of(started.json()["authorizationUrl"])["state"])
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))
    response = await callback(client, code="c", state=row.state)

    assert response.status_code == 302
    assert_origin_relative(response.headers["location"])


async def test_attack_a_hostile_next_written_straight_into_the_state_is_neutralised(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    """Defence in depth: suppose a future edit lets a bad path into the table anyway. The
    redirect itself has to re-check, so the table can never become an open redirect."""
    row = await begin(client, db)
    row.next_path = "//evil.example/take-over"
    await db.commit()
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    response = await callback(client, code="c", state=row.state)
    assert response.headers["location"] == "/"
    assert_origin_relative(response.headers["location"])


async def test_attack_every_failure_redirect_stays_inside_the_origin(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    for params in (
        {"code": "c", "state": "never-issued"},
        {"state": "x", "error": "access_denied"},
        {"error": "//evil.example"},
        {"code": "c"},
        {},
    ):
        response = await callback(client, **params)
        assert_origin_relative(response.headers["location"])


# ==============================================================================================
# 5. Linking, unlinking, and locking an account out
# ==============================================================================================


async def test_attack_cannot_link_a_google_account_that_belongs_to_another_user(
    viewer_client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    admin: User,
) -> None:
    await link_identity(db, admin, sub="sub-admin", email="admin@gmail.example")

    started = await viewer_client.post("/api/auth/google/link/start")
    row = await state_row(db, query_of(started.json()["authorizationUrl"])["state"])
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-admin", email="admin@gmail.example")
    )

    response = await callback(viewer_client, code="c", state=row.state)
    assert error_of(response) == "GOOGLE_ACCOUNT_IN_USE"

    identity = await oauth_service.identity_for_subject(db, "sub-admin")
    assert identity is not None
    assert identity.user_id == admin.id


async def test_attack_signing_in_cannot_steal_an_account_already_bound_to_another_google_user(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
) -> None:
    """Path 2 is the one that trusts a verified address. It must not fire when the account has
    already deliberately bound itself to a *different* Google account."""
    await link_identity(db, viewer, sub="sub-real", email=viewer.email)
    before = await user_count(db)

    row = await begin(client, db)
    google.will_return(make_id_token(nonce=row.nonce, sub="sub-impostor", email=viewer.email))

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(
        db, response, users_before=before, code="GOOGLE_ACCOUNT_MISMATCH"
    )
    assert await oauth_service.identity_for_subject(db, "sub-impostor") is None
    still = await oauth_service.identity_for_subject(db, "sub-real")
    assert still is not None and still.user_id == viewer.id


async def test_attack_unlinking_cannot_remove_the_last_way_in(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    await SettingsService(db).set("auth.magic_link_enabled", False)
    await link_identity(db, viewer, sub="sub-only", email=viewer.email)

    response = await viewer_client.delete("/api/auth/google/link")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_SIGN_IN_METHOD"

    identities = (
        (await db.execute(select(OAuthIdentity).where(OAuthIdentity.user_id == viewer.id)))
        .scalars()
        .all()
    )
    assert len(identities) == 1


async def test_attack_the_two_last_way_in_guards_cannot_be_played_against_each_other(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User
) -> None:
    """Magic link off, one passkey, one Google account. Removing either is fine; removing both
    is not, whichever order they are removed in — and neither guard may be tricked into
    believing the other method still exists."""
    await SettingsService(db).set("auth.magic_link_enabled", False)
    credential = Credential(credential_id=b"cred-bytes", public_key=b"key-bytes")
    viewer.credentials.append(credential)
    await link_identity(db, viewer, sub="sub-both", email=viewer.email)
    await db.commit()

    # Drop the passkey: allowed, because Google can still get in.
    dropped = await viewer_client.delete(f"/api/auth/passkeys/{credential.id}")
    assert dropped.status_code == 200

    await db.refresh(viewer, ["credentials", "oauth_identities"])

    # Now Google is the only way in, and unlinking it must be refused.
    response = await viewer_client.delete("/api/auth/google/link")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_SIGN_IN_METHOD"


async def test_attack_unlinking_only_ever_touches_the_callers_own_identity(
    viewer_client: AsyncClient, db: AsyncSession, viewer: User, admin: User
) -> None:
    await link_identity(db, viewer, sub="sub-viewer", email=viewer.email)
    await link_identity(db, admin, sub="sub-admin", email=admin.email)

    assert (await viewer_client.delete("/api/auth/google/link")).status_code == 200

    assert await oauth_service.identity_for_subject(db, "sub-viewer") is None
    survivor = await oauth_service.identity_for_subject(db, "sub-admin")
    assert survivor is not None and survivor.user_id == admin.id


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/google/link/start"),
        ("DELETE", "/api/auth/google/link"),
        ("GET", "/api/auth/methods"),
    ],
)
async def test_attack_account_routes_require_a_session(
    client: AsyncClient, google: FakeGoogle, google_on: None, method: str, path: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# ==============================================================================================
# 6. The feature switch
# ==============================================================================================


@pytest.mark.parametrize("configured", ["off", "no-client-id"])
async def test_attack_no_google_route_works_while_the_feature_is_off(
    viewer_client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    viewer: User,
    configured: str,
) -> None:
    svc = SettingsService(db)
    if configured == "no-client-id":
        # Enabled but unaddressable is still unusable, and must be refused identically.
        await svc.set("oauth.google.enabled", True)
        await svc.set("oauth.google.client_id", "")
    await svc.set("oauth.google.client_secret", CLIENT_SECRET)
    await db.commit()

    start = await viewer_client.get("/api/auth/google/start")
    assert start.status_code == 403
    assert start.json()["error"]["code"] == "GOOGLE_DISABLED"

    link = await viewer_client.post("/api/auth/google/link/start")
    assert link.status_code == 403
    assert link.json()["error"]["code"] == "GOOGLE_DISABLED"

    done = await callback(viewer_client, code="c", state="anything")
    assert error_of(done) == "GOOGLE_DISABLED"
    assert "set-cookie" not in done.headers

    # And nothing reached Google at all.
    assert google.token_requests == []


async def test_attack_a_state_minted_before_the_switch_flipped_cannot_complete(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None, viewer: User
) -> None:
    before = await user_count(db)
    row = await begin(client, db)
    await SettingsService(db).set("oauth.google.enabled", False)
    await db.commit()
    google.will_return(make_id_token(nonce=row.nonce, email=viewer.email))

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_DISABLED")
    assert google.token_requests == [], "the code was exchanged even though Google was off"


async def test_attack_invitations_through_google_can_be_switched_off_independently(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    from app.services.auth import create_invite

    await SettingsService(db).set("oauth.google.allow_invites", False)
    created = await create_invite(db, email="alice@example.com")
    await db.commit()
    before = await user_count(db)

    row = await begin(client, db, invite=created.token)
    google.will_return(
        make_id_token(nonce=row.nonce, sub="sub-alice", email="alice@example.com")
    )

    response = await callback(client, code="c", state=row.state)
    await assert_nothing_granted(db, response, users_before=before, code="GOOGLE_NO_ACCOUNT")


# ==============================================================================================
# 7. Secrets: responses, redirects and logs
# ==============================================================================================


def _haystack(response: Any) -> str:
    return " ".join(
        [
            response.headers.get("location", ""),
            repr(dict(response.headers)),
            response.text,
            repr(dict(response.cookies)),
        ]
    )


async def test_attack_no_secret_escapes_through_any_response_of_any_flow(
    viewer_client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
) -> None:
    """Every surface the feature exposes, across success and every failure shape."""
    row = await begin(viewer_client, db)
    id_token = make_id_token(nonce=row.nonce, email=viewer.email)
    google.will_return(id_token)
    verifier = row.code_verifier

    responses = [
        await viewer_client.get("/api/config"),
        await viewer_client.get("/api/auth/google/start"),
        await viewer_client.post("/api/auth/google/link/start"),
        await viewer_client.get("/api/auth/methods"),
        await callback(viewer_client, code="c", state=row.state),
        await callback(viewer_client, code="c", state=row.state),  # replayed
        await callback(viewer_client, code="c", state="never-issued"),
        await callback(viewer_client, state="x", error="access_denied"),
        await callback(viewer_client),
    ]

    for response in responses:
        haystack = _haystack(response)
        for secret in (id_token, ACCESS_TOKEN, CLIENT_SECRET, verifier):
            assert secret not in haystack, response.request.url
        for name in ("id_token", "access_token", "client_secret", "code_verifier"):
            assert name not in haystack, response.request.url


async def test_attack_no_secret_reaches_the_logs(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    viewer: User,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every branch that logs anything, run at DEBUG so nothing is filtered out."""
    caplog.set_level(logging.DEBUG)

    ok = await begin(client, db)
    id_token = make_id_token(nonce=ok.nonce, email=viewer.email)
    google.will_return(id_token)
    await callback(client, code="the-real-code", state=ok.state)

    refused = await begin(client, db)
    google.token_status = 400
    google.token_payload = {"error": "invalid_grant"}
    await callback(client, code="the-real-code", state=refused.state)

    cancelled = await begin(client, db)
    await callback(client, state=cancelled.state, error="access_denied")

    from_app = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    everywhere = "\n".join(r.getMessage() for r in caplog.records)

    # The app's own logging carries none of the flow's secret material — not the tokens, not
    # the PKCE verifier, not the state or nonce that would let someone else finish the flow.
    for secret in (
        ACCESS_TOKEN,
        CLIENT_SECRET,
        id_token,
        ok.code_verifier,
        refused.code_verifier,
        ok.nonce,
        ok.state,
    ):
        assert secret not in from_app

    # And these three appear *nowhere*, including any library's debug output: nothing outside
    # `exchange_code` ever holds them, and nothing writes them to a row a query could log.
    for secret in (ACCESS_TOKEN, CLIENT_SECRET, id_token):
        assert secret not in everywhere


async def test_attack_raising_the_log_level_does_not_dump_bound_sql_parameters(
    tmp_env: Any,
) -> None:
    """An operator debugging the app must not thereby write the database's secrets to disk.

    `aiosqlite` logs every statement together with its bound parameters at DEBUG, and those
    parameters include the stored Google client secret on the way in, every token hash, and
    live PKCE verifiers. Following the app's own DEBUG output must not cost that.
    """
    from app.main import configure_logging

    configure_logging("debug")

    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    for noisy in ("aiosqlite", "httpcore", "httpx", "asyncio"):
        assert logging.getLogger(noisy).getEffectiveLevel() >= logging.INFO, noisy
    # SQLAlchemy logs statements *and bound parameters* at INFO, so INFO is not far enough.
    for engine in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.engine.Engine"):
        assert logging.getLogger(engine).getEffectiveLevel() >= logging.WARNING, engine

    configure_logging("info")


async def test_attack_a_hostile_error_parameter_cannot_forge_a_log_line(
    client: AsyncClient,
    db: AsyncSession,
    google: FakeGoogle,
    google_on: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`error` is whatever the query string says, and it is logged.

    So it must not be able to open a second line and impersonate one — "CRITICAL … admin
    promoted mallory" reads exactly like the real thing in a log tail — nor smuggle an ANSI
    escape into whatever terminal reads that tail. Surviving as a visibly mangled single
    token is fine; surviving as prose on a line of its own is not.
    """
    caplog.set_level(logging.DEBUG)
    row = await begin(client, db)

    forged = "access_denied\nCRITICAL app.api.auth admin promoted mallory\r\n\x1b[2J"
    response = await callback(client, state=row.state, error=forged)
    assert error_of(response) == "GOOGLE_CANCELLED"

    from_app = [r.getMessage() for r in caplog.records if r.name.startswith("app.")]
    assert from_app, "the branch under test logged nothing at all"

    for message in from_app:
        for control in ("\n", "\r", "\x1b", "\x00"):
            assert control not in message, repr(message)
        # Nothing that could be mistaken for prose an operator wrote or a level name.
        assert " " not in message.split(": ", 1)[-1], repr(message)
        assert "CRITICAL app.api.auth admin promoted mallory" not in message


async def test_attack_the_public_config_reveals_nothing_but_the_switch(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    response = await client.get("/api/config")
    body = response.json()
    assert body["googleEnabled"] is True
    assert CLIENT_ID not in response.text
    assert CLIENT_SECRET not in response.text


async def test_attack_the_authorization_url_carries_no_server_side_secret(
    client: AsyncClient, db: AsyncSession, google: FakeGoogle, google_on: None
) -> None:
    """PKCE only works if the verifier stays behind; the URL may carry its hash and nothing
    more."""
    started = await client.get("/api/auth/google/start")
    url = started.json()["authorizationUrl"]
    row = await state_row(db, query_of(url)["state"])

    assert row.code_verifier not in url
    assert CLIENT_SECRET not in url
    assert query_of(url)["code_challenge"] == oauth_service.code_challenge(row.code_verifier)
    assert (
        base64.urlsafe_b64decode(query_of(url)["code_challenge"] + "==")
        == hashlib.sha256(row.code_verifier.encode()).digest()
    )
