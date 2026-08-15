"""Sign in with Google — OAuth 2.0 authorization code + OpenID Connect.

dramatarr is invite-only, and this module does not change that. Google is a way *into an
account that already exists*, or a way to accept an invitation addressed to the very same
address. It can never mint an account for an arbitrary Google user; §"account resolution"
below is the whole of the policy and every path that does not match one of its three cases
ends in `GOOGLE_NO_ACCOUNT` with nothing written.

Why matching a verified Google email to an existing account is allowed at all: the app
already emails single-use sign-in links, which hands access to whoever controls the mailbox.
A Google `id_token` carrying `"email_verified": true` is at least as strong a statement about
the same mailbox, so honouring it is consistent with a decision the deployment has already
made. An `email_verified` that is anything other than exactly `true` says nothing and is
refused outright.

Three implementation rules follow from that:

1. The `id_token` is verified, never merely decoded — RSA signature against Google's JWKS
   under the key id in the header, then `iss`, `aud`, `exp`, `iat` and the `nonce` we stored.
   Any failure is `GOOGLE_TOKEN_INVALID`; there is no lenient branch.
2. `state` is a server-side, single-use row *bound to one browser*. It carries the PKCE
   verifier, the nonce, the flow it belongs to and where to land afterwards, and it is burned
   with a conditional UPDATE whose rowcount decides the winner — the same race, and the same
   fix, as magic links. The browser binding is what stops `state` from being a bearer token;
   see `BROWSER_COOKIE_NAME` below.
3. No token, no client secret and no code verifier is ever logged or returned. `exchange_code`
   deliberately returns only the `id_token`, so the access token cannot escape this module.
   Anything a *caller* supplies and this module logs goes through `loggable` first, because a
   query parameter with a newline in it can otherwise write its own line in the log file.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import delete, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import hash_token
from app.config import get_settings
from app.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized
from app.models import (
    Invite,
    OAuthIdentity,
    OAuthProvider,
    OAuthState,
    OAuthStateKind,
    User,
)
from app.services.auth import (
    burn_single_use,
    consume_invite,
    create_user_from_invite,
    normalize_email,
    validate_invite_token,
)
from app.services.settings import SettingsService
from app.utils.ids import new_token
from app.utils.time import in_minutes, is_expired, utcnow

log = logging.getLogger(__name__)

DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

#: Google has served these for years and documents them; discovery being briefly unreachable
#: must not take sign-in down with it.
FALLBACK_ENDPOINTS: dict[str, str] = {
    "issuer": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
}

#: Google has issued tokens under both spellings; both are the same issuer.
VALID_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

SCOPE = "openid email profile"
STATE_TTL_MINUTES = 10
DISCOVERY_TTL_SECONDS = 3600
JWKS_TTL_SECONDS = 3600
#: Floor between forced JWKS refetches, so a stream of tokens bearing made-up key ids cannot
#: turn this server into a load generator pointed at Google.
JWKS_MIN_REFRESH_SECONDS = 60
#: Clocks drift. One minute either way is the usual allowance and is small enough that an
#: expired token stays expired for any practical purpose.
CLOCK_SKEW_SECONDS = 60

#: Where the SPA shows a localized message for a failed callback.
LOGIN_PATH = "/login"

#: Ties an authorization round trip to the browser that began it. Set by both `start` routes,
#: sent back by the browser on Google's top-level redirect (which is why it must be `Lax` and
#: not `Strict`), and compared against `OAuthState.browser_hash` before the state is burned.
#: Scoped to the callback's own path so it travels with nothing else.
BROWSER_COOKIE_NAME = "dramatarr_oauth"
BROWSER_COOKIE_PATH = "/api/auth/google"
#: Deliberately longer-lived than the state it unlocks. If the cookie went first, a user who
#: dawdled at Google would be told their attempt came from another browser instead of that it
#: took too long — the state's own expiry is what should decide that, and it still can.
BROWSER_COOKIE_MAX_AGE = 2 * STATE_TTL_MINUTES * 60

_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=10.0)

#: An OAuth error code is one token: `invalid_grant`, `access_denied`. Whitespace is dropped
#: along with everything else outside this set, so that whatever survives is visibly a mangled
#: single token rather than a sentence someone composed to be read as log prose.
_UNLOGGABLE = re.compile(r"[^A-Za-z0-9_.:/=+-]")


def loggable(value: object, *, limit: int = 96) -> str:
    """Make a caller-supplied string safe to hand to `%s` in a log call.

    `GET /google/callback?error=…` is public and lands in `log.info`. A value containing CR or
    LF forges an entire log line — "CRITICAL app.api.auth admin promoted mallory" reads exactly
    like the real thing in a log tail — and an ANSI escape can rewrite a terminal that reads
    one. Substituting rather than escaping keeps the surviving text obviously literal.
    """
    return _UNLOGGABLE.sub("?", str(value))[:limit]


# --- Configuration ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GoogleConfig:
    enabled: bool
    client_id: str
    client_secret: str
    allow_invites: bool

    @property
    def usable(self) -> bool:
        """Enabled *and* addressable. A client id is what makes the button work at all, so an
        install that flipped the switch without pasting one is reported as disabled rather
        than sending people to an authorization URL Google will reject."""
        return self.enabled and bool(self.client_id)


async def google_config(session: AsyncSession) -> GoogleConfig:
    svc = SettingsService(session)
    return GoogleConfig(
        enabled=await svc.get_bool("oauth.google.enabled", False),
        client_id=(await svc.get_str("oauth.google.client_id", "")).strip(),
        client_secret=await svc.get_str("oauth.google.client_secret", ""),
        allow_invites=await svc.get_bool("oauth.google.allow_invites", True),
    )


async def require_google(session: AsyncSession) -> GoogleConfig:
    config = await google_config(session)
    if not config.usable:
        raise Forbidden("GOOGLE_DISABLED", "Sign in with Google is not enabled here")
    return config


def redirect_uri() -> str:
    """The one URI registered with Google. Both flows come back through it."""
    return f"{get_settings().public_url.rstrip('/')}/api/auth/google/callback"


def validate_next_path(value: str | None) -> str | None:
    """A landing path, never a URL.

    `//evil.example` and `/\\evil.example` are read by browsers as protocol-relative URLs, so
    a naive "starts with /" check is an open redirect. Newlines are refused too: this string
    ends up in a `Location` header.
    """
    if not value:
        return None
    if len(value) > 512 or any(ch in value for ch in "\r\n\x00"):
        raise AppError("INVALID_NEXT_PATH", "That redirect target is not a usable path")
    if not value.startswith("/") or value.startswith(("//", "/\\")):
        raise AppError("INVALID_NEXT_PATH", "That redirect target is not a usable path")
    return value


def safe_next_path(value: str | None) -> str:
    """Same rule, applied again just before the redirect. A stored path went through
    `validate_next_path` on the way in; re-checking costs nothing and means no future edit to
    the state table can turn into an open redirect."""
    try:
        return validate_next_path(value) or "/"
    except AppError:
        return "/"


# --- The entire outbound HTTP surface ---------------------------------------------------------


async def _fetch_json(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One function, so tests replace one thing and nothing can quietly reach Google.

    The body is returned to the caller and never logged: the token endpoint answers with the
    `id_token` and the access token in it.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(
                method, url, data=data, headers={"Accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        # The class name only. httpx puts the request URL in its messages, and for the token
        # endpoint that request also carried the client secret in its body.
        raise AppError(
            "GOOGLE_UNAVAILABLE", f"Could not reach Google ({type(exc).__name__})", 503
        ) from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload if isinstance(payload, dict) else {}


# --- Discovery and signing keys ---------------------------------------------------------------

_discovery: dict[str, Any] | None = None
_discovery_at: float = 0.0
_discovery_lock = asyncio.Lock()

_jwks: dict[str, dict[str, Any]] = {}
_jwks_at: float = 0.0
_jwks_lock = asyncio.Lock()


def reset_caches() -> None:
    """Drop the discovery and JWKS caches. Used by tests and after a settings change."""
    global _discovery, _discovery_at, _jwks, _jwks_at
    _discovery = None
    _discovery_at = 0.0
    _jwks = {}
    _jwks_at = 0.0


async def discovery_document() -> dict[str, Any]:
    """Google's OpenID configuration, cached for an hour, with the documented endpoints as a
    fallback so a discovery outage degrades instead of failing."""
    global _discovery, _discovery_at

    if _discovery is not None and time.monotonic() - _discovery_at < DISCOVERY_TTL_SECONDS:
        return _discovery

    async with _discovery_lock:
        if _discovery is not None and time.monotonic() - _discovery_at < DISCOVERY_TTL_SECONDS:
            return _discovery

        document: dict[str, Any] = dict(FALLBACK_ENDPOINTS)
        try:
            status, payload = await _fetch_json("GET", DISCOVERY_URL)
            if status == 200 and payload.get("token_endpoint") and payload.get("jwks_uri"):
                document = payload
            else:
                log.warning("Google discovery answered %s; using the well-known endpoints", status)
        except AppError as exc:
            log.warning("Google discovery unreachable (%s); using well-known endpoints", exc.code)

        _discovery = document
        _discovery_at = time.monotonic()
        return document


def _endpoint(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    return str(value) if isinstance(value, str) and value else FALLBACK_ENDPOINTS[name]


async def _load_jwks(*, force: bool = False) -> dict[str, dict[str, Any]]:
    global _jwks, _jwks_at

    fresh = bool(_jwks) and time.monotonic() - _jwks_at < JWKS_TTL_SECONDS
    if fresh and not force:
        return _jwks

    async with _jwks_lock:
        fresh = bool(_jwks) and time.monotonic() - _jwks_at < JWKS_TTL_SECONDS
        if fresh and not force:
            return _jwks

        document = await discovery_document()
        status, payload = await _fetch_json("GET", _endpoint(document, "jwks_uri"))
        keys = payload.get("keys") if status == 200 else None
        if not isinstance(keys, list) or not keys:
            raise AppError("GOOGLE_UNAVAILABLE", "Google's signing keys are unavailable", 503)

        _jwks = {
            str(key["kid"]): key
            for key in keys
            if isinstance(key, dict) and isinstance(key.get("kid"), str)
        }
        _jwks_at = time.monotonic()
        return _jwks


async def _signing_key(kid: str) -> dict[str, Any]:
    keys = await _load_jwks()
    jwk = keys.get(kid)
    if jwk is None and time.monotonic() - _jwks_at >= JWKS_MIN_REFRESH_SECONDS:
        # Google rotates keys without warning; one refetch on a miss is the documented way to
        # follow a rotation. The floor above keeps that from becoming a free amplifier.
        jwk = (await _load_jwks(force=True)).get(kid)
    if jwk is None:
        raise Unauthorized("GOOGLE_TOKEN_INVALID", "id_token was signed with an unknown key")
    return jwk


# --- PKCE and the authorization URL -----------------------------------------------------------


def new_code_verifier() -> str:
    """RFC 7636 wants 43 to 128 unreserved characters; this yields 86."""
    return new_token(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    document: dict[str, Any],
    *,
    client_id: str,
    state: str,
    nonce: str,
    verifier: str,
    login_hint: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "scope": SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        # dramatarr never acts on the user's behalf offline, so it must not be handed a
        # refresh token it would then have to store and protect.
        "access_type": "online",
        # Households share devices. Landing on whichever Google account the browser happens to
        # be signed into is how the wrong person ends up in someone else's library.
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{_endpoint(document, 'authorization_endpoint')}?{urlencode(params)}"


# --- State store ------------------------------------------------------------------------------


async def purge_expired_states(session: AsyncSession) -> int:
    """`GET /google/start` is public and mints a row per call, so each new row pays for one
    cheap sweep of the dead ones. Bulk DELETE — loading them first would make the sweep
    proportional to how long it had been neglected."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(OAuthState)
            .where(OAuthState.expires_at <= utcnow())
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount or 0


async def create_state(
    session: AsyncSession,
    *,
    kind: OAuthStateKind,
    browser_token: str,
    user_id: str | None = None,
    invite_id: str | None = None,
    next_path: str | None = None,
) -> OAuthState:
    """`browser_token` is required rather than optional on purpose: a state row minted without
    one is unredeemable, and a caller that could omit it would be minting dead states."""
    await purge_expired_states(session)
    row = OAuthState(
        state=new_token(32),
        code_verifier=new_code_verifier(),
        nonce=new_token(16),
        kind=str(kind),
        # Only the hash, as everywhere else: a database leak must not hand over live flows.
        browser_hash=hash_token(browser_token),
        user_id=user_id,
        invite_id=invite_id,
        next_path=next_path,
        expires_at=in_minutes(STATE_TTL_MINUTES),
    )
    session.add(row)
    await session.flush()
    return row


async def peek_state(session: AsyncSession, state: str) -> OAuthState | None:
    """Read without burning — the callback needs the row's `kind` to know which flow it is
    completing before it can consume it under that kind."""
    return (
        await session.execute(select(OAuthState).where(OAuthState.state == state))
    ).scalar_one_or_none()


def browser_holds_state(row: OAuthState, browser_token: str | None) -> bool:
    """Whether the browser presenting this callback is the one that started the flow.

    A missing binding on the row is *not* a wildcard: rows predating the column, or written by
    some future path that forgot it, are refused rather than accepted by everyone.
    """
    if not row.browser_hash or not browser_token:
        return False
    return secrets.compare_digest(hash_token(browser_token), row.browser_hash)


async def consume_state(
    session: AsyncSession, state: str, *, kind: OAuthStateKind, browser_token: str | None
) -> OAuthState:
    """Validate and burn. A row survives exactly one call to this function."""
    row = await peek_state(session, state)
    if row is None:
        raise AppError("GOOGLE_STATE_INVALID", "Unknown or already-cleared sign-in state")
    # A login state carries no account and a link state carries one; redeeming either as the
    # other would skip the rules the other flow depends on.
    if row.kind != str(kind):
        raise AppError("GOOGLE_STATE_INVALID", "That sign-in state belongs to a different flow")
    # Before the burn, deliberately: a stranger presenting a state they should not have must
    # not be able to spend somebody else's in-flight sign-in just by loading a URL.
    if not browser_holds_state(row, browser_token):
        raise AppError("GOOGLE_STATE_INVALID", "That sign-in attempt began in another browser")
    if row.consumed_at is not None:
        raise AppError("GOOGLE_STATE_USED", "That sign-in attempt was already completed")
    if is_expired(row.expires_at):
        raise AppError("GOOGLE_STATE_EXPIRED", "That sign-in attempt took too long")

    # The SELECT above is not part of a write transaction, so two deliveries of one callback
    # can both see `consumed_at IS NULL`. Only the UPDATE that matches a row wins.
    if not await burn_single_use(session, OAuthState, row.id):
        raise AppError("GOOGLE_STATE_USED", "That sign-in attempt was already completed")
    return row


# --- id_token verification --------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GoogleClaims:
    """What a *verified* id_token asserted. `email_verified` is not a field because a token
    that did not assert exactly `true` never becomes one of these."""

    sub: str
    email: str
    name: str | None = None


def _invalid(message: str) -> Unauthorized:
    return Unauthorized("GOOGLE_TOKEN_INVALID", message)


def _b64url_decode(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _invalid("id_token is not valid base64url") from exc


def _json_segment(segment: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_b64url_decode(segment))
    except ValueError as exc:
        raise _invalid("id_token segment is not JSON") from exc
    if not isinstance(parsed, dict):
        raise _invalid("id_token segment is not an object")
    return parsed


def _rsa_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    if jwk.get("kty") != "RSA":
        raise _invalid("id_token was signed with an unsupported key type")
    modulus = jwk.get("n")
    exponent = jwk.get("e")
    if not isinstance(modulus, str) or not isinstance(exponent, str):
        raise _invalid("Google's signing key is malformed")
    numbers = rsa.RSAPublicNumbers(
        e=int.from_bytes(_b64url_decode(exponent), "big"),
        n=int.from_bytes(_b64url_decode(modulus), "big"),
    )
    return numbers.public_key()


def _as_epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def verify_id_token(id_token: str, *, client_id: str, nonce: str) -> GoogleClaims:
    """Signature first, then every claim that matters. There is no path through this function
    that returns claims from a token whose signature did not verify."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise _invalid("id_token is not a three-part JWS")
    header_segment, payload_segment, signature_segment = parts

    header = _json_segment(header_segment)
    # "none" and any HMAC algorithm are the classic JWT confusion attacks; Google signs with
    # RS256 and nothing else is accepted here.
    if header.get("alg") != "RS256":
        raise _invalid("id_token is not signed with RS256")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise _invalid("id_token header carries no key id")

    key = _rsa_key(await _signing_key(kid))
    try:
        key.verify(
            _b64url_decode(signature_segment),
            f"{header_segment}.{payload_segment}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise _invalid("id_token signature does not verify") from exc

    claims = _json_segment(payload_segment)

    issuer = str(claims.get("iss") or "")
    if issuer not in VALID_ISSUERS:
        raise _invalid("id_token was not issued by Google")

    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if client_id not in [str(value) for value in audiences]:
        raise _invalid("id_token was issued for a different client")
    # OIDC Core §3.1.3.7: a token naming several audiences must also name the party it was
    # issued to, and that party must be us. Google only ever sends a single `aud`, so this
    # cannot fire on a real token — it exists so that a multi-audience token, which by
    # definition was minted for somebody else as well, cannot be presented here.
    if len(audiences) > 1 and str(claims.get("azp") or "") != client_id:
        raise _invalid("id_token names several audiences and was not authorized for this one")

    now = utcnow().timestamp()
    expires = _as_epoch(claims.get("exp"))
    if expires is None or now > expires + CLOCK_SKEW_SECONDS:
        raise _invalid("id_token has expired")
    issued = _as_epoch(claims.get("iat"))
    if issued is None or issued > now + CLOCK_SKEW_SECONDS:
        raise _invalid("id_token was issued in the future")

    # Binds this token to the authorization request we started, so one captured elsewhere
    # cannot be replayed into this flow. Compared as bytes: `compare_digest` rejects non-ASCII
    # str operands with a TypeError, and a claim is not ours to assume anything about.
    claimed_nonce = str(claims.get("nonce") or "").encode("utf-8")
    if not secrets.compare_digest(claimed_nonce, nonce.encode("utf-8")):
        raise _invalid("id_token answers a different sign-in attempt")

    subject = claims.get("sub")
    email = claims.get("email")
    if not isinstance(subject, str) or not subject:
        raise _invalid("id_token carries no subject")
    if not isinstance(email, str) or not email:
        raise _invalid("id_token carries no email address")

    # Exactly `true`. A missing, absent or stringly-typed value asserts nothing about the
    # mailbox, and the mailbox is the entire basis for matching this token to an account.
    if claims.get("email_verified") is not True:
        raise Forbidden(
            "GOOGLE_EMAIL_UNVERIFIED", "That Google account has not verified its email address"
        )

    name = claims.get("name")
    return GoogleClaims(
        sub=subject,
        email=normalize_email(email),
        name=name.strip() if isinstance(name, str) and name.strip() else None,
    )


# --- Token exchange ----------------------------------------------------------------------------


async def exchange_code(
    document: dict[str, Any],
    *,
    code: str,
    client_id: str,
    client_secret: str,
    verifier: str,
) -> str:
    """Trade the authorization code for an id_token, and return *only* that.

    The access token is discarded here on purpose: nothing downstream needs it, and a value
    that never leaves this function cannot be logged, serialized or returned by mistake.
    """
    status, payload = await _fetch_json(
        "POST",
        _endpoint(document, "token_endpoint"),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(),
            "code_verifier": verifier,
        },
    )
    if status != 200:
        # The OAuth error slug only — the body of a *successful* exchange holds tokens, and
        # this branch must not become the one place a future edit prints the whole payload.
        log.warning(
            "Google refused the token exchange: %s", loggable(payload.get("error") or status)
        )
        raise AppError("GOOGLE_EXCHANGE_FAILED", "Google refused the authorization code", 502)

    id_token = payload.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise AppError("GOOGLE_EXCHANGE_FAILED", "Google's response carried no id_token", 502)
    return id_token


# --- Identities ---------------------------------------------------------------------------------


def google_identity_of(user: User) -> OAuthIdentity | None:
    for identity in user.oauth_identities:
        if identity.provider == OAuthProvider.GOOGLE:
            return identity
    return None


async def identity_for_subject(session: AsyncSession, subject: str) -> OAuthIdentity | None:
    return (
        await session.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == OAuthProvider.GOOGLE,
                OAuthIdentity.provider_sub == subject,
            )
        )
    ).scalar_one_or_none()


async def _link_identity(session: AsyncSession, user: User, claims: GoogleClaims) -> OAuthIdentity:
    identity = OAuthIdentity(
        user_id=user.id,
        provider=OAuthProvider.GOOGLE,
        provider_sub=claims.sub,
        email=claims.email,
        last_used_at=utcnow(),
    )
    if "oauth_identities" in sa_inspect(user).unloaded:
        # An account this request just created from an invitation: nothing ever loaded the
        # collection, and reaching for it here would fire a lazy load, which an async session
        # refuses outright. Nothing reads it before the response either, so the plain insert
        # is both correct and the only thing that works.
        session.add(identity)
    else:
        # Otherwise go through the relationship, so a caller that reads the collection later
        # in the same request sees what was actually written.
        user.oauth_identities.append(identity)
    await session.flush()
    return identity


# --- Account resolution --------------------------------------------------------------------------


async def _usable_invite(session: AsyncSession, invite_id: str | None) -> Invite | None:
    if not invite_id:
        return None
    invite = await session.get(Invite, invite_id)
    if invite is None or invite.accepted_at is not None or invite.revoked_at is not None:
        return None
    if is_expired(invite.expires_at):
        return None
    return invite


async def resolve_account(
    session: AsyncSession,
    claims: GoogleClaims,
    state: OAuthState,
    *,
    allow_invites: bool,
) -> User:
    """Turn a verified Google assertion into a local account, or refuse.

    Exactly three cases produce a session, in this order:

    1. This Google subject is already linked → that account.
    2. No link yet, but an account exists at the verified address → link it and sign in. This
       is the trust-model step: it grants no more than an emailed sign-in link already does.
    3. No link and no account, but this flow started from an unaccepted invitation addressed
       to that same verified address → accept it, with the invite's role.

    Anything else refuses. In particular an invitation for a *different* address than the
    Google account falls through to the refusal, which is the case that would otherwise let
    one forwarded invite link enrol any Google user at all.
    """
    identity = await identity_for_subject(session, claims.sub)
    if identity is not None:
        user = await session.get(User, identity.user_id)
        if user is None:  # FK is ON DELETE CASCADE, so this cannot happen
            raise Forbidden("GOOGLE_NO_ACCOUNT", "That Google account is not linked to anyone")
        if not user.is_active:
            raise Forbidden("ACCOUNT_DISABLED", "This account has been deactivated")
        identity.last_used_at = utcnow()
        await session.flush()
        return user

    existing = (
        await session.execute(select(User).where(User.email == claims.email))
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.is_active:
            raise Forbidden("ACCOUNT_DISABLED", "This account has been deactivated")
        # The account already answers to a different Google account. Silently re-pointing it
        # would break "one Google account per user" and would be a surprising way to lose
        # access to the identity you deliberately linked.
        if google_identity_of(existing) is not None:
            raise Conflict(
                "GOOGLE_ACCOUNT_MISMATCH",
                "This account is already linked to a different Google account",
            )
        await _link_identity(session, existing, claims)
        return existing

    invite = await _usable_invite(session, state.invite_id)
    if allow_invites and invite is not None and normalize_email(invite.email) == claims.email:
        user = await create_user_from_invite(
            session, invite, display_name=claims.name, locale=None
        )
        await consume_invite(session, invite, user)
        await _link_identity(session, user, claims)
        return user

    raise Forbidden(
        "GOOGLE_NO_ACCOUNT",
        "No dramatarr account matches that Google address, and no invitation covers it",
    )


# --- Flows --------------------------------------------------------------------------------------


async def start_login(
    session: AsyncSession,
    *,
    browser_token: str,
    next_path: str | None = None,
    invite_token: str | None = None,
) -> str:
    """Mint a state row and return the URL to send the browser to."""
    config = await require_google(session)
    landing = validate_next_path(next_path)

    invite: Invite | None = None
    if invite_token:
        # Fails loudly here rather than after the round trip, so an expired or revoked invite
        # is reported on the page the user is already looking at.
        invite = await validate_invite_token(session, invite_token)

    state = await create_state(
        session,
        kind=OAuthStateKind.LOGIN,
        browser_token=browser_token,
        invite_id=invite.id if invite else None,
        next_path=landing,
    )
    return build_authorization_url(
        await discovery_document(),
        client_id=config.client_id,
        state=state.state,
        nonce=state.nonce,
        verifier=state.code_verifier,
        login_hint=invite.email if invite else None,
    )


async def start_link(
    session: AsyncSession, user: User, *, browser_token: str, next_path: str | None = None
) -> str:
    config = await require_google(session)
    if google_identity_of(user) is not None:
        raise Conflict("GOOGLE_ALREADY_LINKED", "This account already has a Google account linked")

    landing = validate_next_path(next_path)
    state = await create_state(
        session,
        kind=OAuthStateKind.LINK,
        browser_token=browser_token,
        user_id=user.id,
        next_path=landing,
    )
    return build_authorization_url(
        await discovery_document(),
        client_id=config.client_id,
        state=state.state,
        nonce=state.nonce,
        verifier=state.code_verifier,
        login_hint=user.email,
    )


@dataclass(slots=True)
class CallbackResult:
    kind: str
    #: Set for a completed login, `None` for a link — a link already has a session.
    user: User | None
    next_path: str


async def complete_callback(
    session: AsyncSession,
    *,
    code: str | None,
    state: str | None,
    browser_token: str | None,
    error: str | None = None,
    current_user: User | None = None,
) -> CallbackResult:
    """Everything between Google's redirect and "you are signed in"."""
    if error:
        # `access_denied` is the user pressing cancel on Google's own screen. Everything else
        # Google reports here is a misconfiguration, but neither is worth a scary message.
        # Sanitized: this whole string is whatever the query said it was.
        log.info("Google returned an authorization error: %s", loggable(error))
        raise AppError("GOOGLE_CANCELLED", "The Google sign-in was cancelled")
    if not state or not code:
        raise AppError("GOOGLE_STATE_INVALID", "The Google callback was missing state or code")

    # An install with the feature switched off refuses the callback too, not just the start —
    # otherwise a state minted before the admin turned it off would still complete.
    config = await require_google(session)

    # The kind decides which rules apply, so it is read before anything is burned.
    pending = await peek_state(session, state)
    if pending is None:
        raise AppError("GOOGLE_STATE_INVALID", "Unknown or already-cleared sign-in state")
    kind = OAuthStateKind.LINK if pending.kind == OAuthStateKind.LINK else OAuthStateKind.LOGIN
    row = await consume_state(session, state, kind=kind, browser_token=browser_token)

    id_token = await exchange_code(
        await discovery_document(),
        code=code,
        client_id=config.client_id,
        client_secret=config.client_secret,
        verifier=row.code_verifier,
    )
    claims = await verify_id_token(id_token, client_id=config.client_id, nonce=row.nonce)

    if kind is OAuthStateKind.LINK:
        await _complete_link(session, row, claims, current_user=current_user)
        # No new session: a link runs inside one that already exists.
        return CallbackResult(kind=str(kind), user=None, next_path=safe_next_path(row.next_path))

    user = await resolve_account(session, claims, row, allow_invites=config.allow_invites)
    return CallbackResult(kind=str(kind), user=user, next_path=safe_next_path(row.next_path))


async def _complete_link(
    session: AsyncSession,
    row: OAuthState,
    claims: GoogleClaims,
    *,
    current_user: User | None,
) -> User:
    # The state is already unguessable and single-use, which is the CSRF defence. Requiring
    # the browser to still hold the session that minted it costs nothing — it was minted by
    # an authenticated request minutes ago — and closes the case where a link callback
    # replayed in someone else's browser would attach a Google account to *their* library.
    if current_user is None or row.user_id is None or current_user.id != row.user_id:
        raise AppError("GOOGLE_STATE_INVALID", "That link attempt belongs to another session")

    existing = await identity_for_subject(session, claims.sub)
    if existing is not None:
        if existing.user_id == current_user.id:
            existing.last_used_at = utcnow()  # already linked; nothing to do
            await session.flush()
            return current_user
        raise Conflict(
            "GOOGLE_ACCOUNT_IN_USE", "That Google account is already linked to another user"
        )

    if google_identity_of(current_user) is not None:
        raise Conflict("GOOGLE_ALREADY_LINKED", "This account already has a Google account linked")

    await _link_identity(session, current_user, claims)
    return current_user


# --- Account page ---------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SignInMethods:
    passkeys: int
    google_linked: bool
    google_email: str | None
    magic_link_enabled: bool


async def sign_in_methods(session: AsyncSession, user: User) -> SignInMethods:
    identity = google_identity_of(user)
    return SignInMethods(
        passkeys=len(user.credentials),
        google_linked=identity is not None,
        google_email=identity.email if identity else None,
        magic_link_enabled=await SettingsService(session).get_bool("auth.magic_link_enabled", True),
    )


async def unlink_google(session: AsyncSession, user: User) -> None:
    """Refuse to strip the account of its last way in — the mirror of the passkey guard."""
    identity = google_identity_of(user)
    if identity is None:
        raise NotFound("Google account", "GOOGLE_NOT_LINKED")

    magic_link_enabled = await SettingsService(session).get_bool("auth.magic_link_enabled", True)
    if not user.credentials and not magic_link_enabled:
        raise Conflict(
            "LAST_SIGN_IN_METHOD",
            "Cannot unlink Google while it is the only way into this account",
        )

    # `delete-orphan` on the relationship turns the removal into the DELETE, and keeps the
    # user object consistent for anything that reads the collection later in this request.
    user.oauth_identities.remove(identity)
    await session.flush()


__all__ = [
    "BROWSER_COOKIE_MAX_AGE",
    "BROWSER_COOKIE_NAME",
    "BROWSER_COOKIE_PATH",
    "CLOCK_SKEW_SECONDS",
    "DISCOVERY_URL",
    "FALLBACK_ENDPOINTS",
    "LOGIN_PATH",
    "SCOPE",
    "STATE_TTL_MINUTES",
    "VALID_ISSUERS",
    "CallbackResult",
    "GoogleClaims",
    "GoogleConfig",
    "SignInMethods",
    "browser_holds_state",
    "build_authorization_url",
    "code_challenge",
    "complete_callback",
    "consume_state",
    "create_state",
    "discovery_document",
    "exchange_code",
    "google_config",
    "google_identity_of",
    "identity_for_subject",
    "loggable",
    "new_code_verifier",
    "peek_state",
    "purge_expired_states",
    "redirect_uri",
    "require_google",
    "reset_caches",
    "resolve_account",
    "safe_next_path",
    "sign_in_methods",
    "start_link",
    "start_login",
    "unlink_google",
    "validate_next_path",
    "verify_id_token",
]
