"""Passkeys, invites and magic links.

Three rules shape everything here:

1. Only hashes are stored. Invite tokens, magic-link tokens and session tokens exist in the
   clear exactly once, in the response or the email that carries them.
2. WebAuthn challenges live in the database, are bound to the user or invite they were issued
   for, and are consumed on first use — a `complete` call can never be replayed.
3. Nothing in the magic-link path reveals whether an address has an account.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.api.deps import hash_token
from app.config import get_settings
from app.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized
from app.models import (
    ChallengeKind,
    Credential,
    Invite,
    MagicLink,
    User,
    UserRole,
    WebAuthnChallenge,
)
from app.schemas.auth import EMAIL_PATTERN
from app.services.mail import send_mail
from app.services.settings import SettingsService
from app.utils.ids import new_id, new_token
from app.utils.time import in_minutes, is_expired, utcnow

log = logging.getLogger(__name__)

CHALLENGE_TTL_MINUTES = 5
MAGIC_LINK_TTL_MINUTES = 10
DEFAULT_INVITE_TTL_HOURS = 168

#: Avatar colours are assigned deterministically from the email so a user keeps the same
#: colour if their account is ever recreated.
AVATAR_COLORS = (
    "#6c8cff",
    "#f0708a",
    "#4bb98a",
    "#e0a24b",
    "#a97bd8",
    "#3fb5c9",
    "#d9705a",
    "#7aa93f",
)

#: Enough to label the common password managers; anything else falls back to a generic name.
_AAGUID_NAMES: dict[str, str] = {
    "adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome",
    "08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
    "9ddd1817-af5a-4672-a2b9-3e3dd95000a9": "Windows Hello",
    "6028b017-b1d4-4c02-b4b3-afcdafc96bb2": "Windows Hello",
    "dd4ec289-e01d-41c9-bb89-70fa845d4bf2": "iCloud Keychain",
    "fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "iCloud Keychain",
    "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager",
    "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
    "d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
    "cb69481e-8ff7-4039-93ec-0a2729a154a8": "YubiKey 5",
    "ee882879-721c-4913-9775-3dfcce97072a": "YubiKey 5",
    "2fc0579f-8113-47ea-b116-bb5a8db9202a": "YubiKey 5 NFC",
    "531126d6-e717-415c-9320-3d9aa6981239": "Dashlane",
    "b84e4048-15dc-4dd0-8640-f4f60813c8af": "NordPass",
    "891494da-2c90-4d31-a9cd-4eab0aed1309": "Proton Pass",
}


@dataclass(slots=True)
class CreatedInvite:
    """The raw token exists only inside this object; the DB keeps its sha256."""

    invite: Invite
    token: str
    url: str


# --- Small helpers ---------------------------------------------------------------------------


_EMAIL_RE = re.compile(EMAIL_PATTERN)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def avatar_color_for(email: str) -> str:
    digest = hashlib.sha256(normalize_email(email).encode("utf-8")).digest()
    return AVATAR_COLORS[digest[0] % len(AVATAR_COLORS)]


def display_name_from_email(email: str) -> str:
    local = normalize_email(email).split("@", 1)[0]
    cleaned = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in cleaned.split()) or "dramatarr"


def invite_url(token: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/invite/{token}"


def magic_link_url(token: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/login?token={token}"


def credential_nickname(aaguid: str | None, user_agent: str | None) -> str:
    if aaguid and aaguid in _AAGUID_NAMES:
        return _AAGUID_NAMES[aaguid]
    ua = (user_agent or "").lower()
    for needle, label in (
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("android", "Android"),
        ("macintosh", "Mac"),
        ("windows", "Windows"),
        ("linux", "Linux"),
    ):
        if needle in ua:
            return label
    return "Passkey"


def _credential_json(credential: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(credential, str):
        try:
            parsed = json.loads(credential)
        except json.JSONDecodeError as exc:
            raise AppError("INVALID_CREDENTIAL", "Credential payload is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise AppError("INVALID_CREDENTIAL", "Credential payload must be an object")
        return parsed
    if not isinstance(credential, dict):
        raise AppError("INVALID_CREDENTIAL", "Credential payload must be an object")
    return credential


def _client_challenge(credential: dict[str, Any]) -> bytes:
    """Recover the challenge the authenticator signed, so we can find its stored row.

    Doing the lookup from clientDataJSON keeps the browser flow standard — the client never
    has to carry an opaque challenge id between the two requests.
    """
    response = credential.get("response")
    raw = response.get("clientDataJSON") if isinstance(response, dict) else None
    if not isinstance(raw, str):
        raise AppError("INVALID_CREDENTIAL", "Credential is missing response.clientDataJSON")
    try:
        return parse_client_data_json(base64url_to_bytes(raw)).challenge
    except Exception as exc:  # malformed base64url or JSON
        raise AppError("INVALID_CREDENTIAL", "clientDataJSON could not be parsed") from exc


def _transports(credential: dict[str, Any]) -> list[str]:
    response = credential.get("response")
    values = response.get("transports") if isinstance(response, dict) else None
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if isinstance(v, str)]


def _descriptors(credentials: list[Credential]) -> list[PublicKeyCredentialDescriptor]:
    known = {t.value for t in AuthenticatorTransport}
    out: list[PublicKeyCredentialDescriptor] = []
    for cred in credentials:
        transports = [AuthenticatorTransport(t) for t in cred.transports if t in known]
        out.append(
            PublicKeyCredentialDescriptor(id=cred.credential_id, transports=transports or None)
        )
    return out


# --- Challenge store -------------------------------------------------------------------------


async def _store_challenge(
    session: AsyncSession,
    challenge: bytes,
    kind: ChallengeKind,
    *,
    user_id: str | None = None,
    invite_id: str | None = None,
) -> WebAuthnChallenge:
    # `POST /auth/passkey/login/begin` is public and mints a row on every call, so the table
    # would grow without bound from anonymous traffic. Nothing else in the app reaps it, so
    # each new challenge pays for one cheap sweep of the dead ones.
    await purge_expired_challenges(session)
    row = WebAuthnChallenge(
        challenge=challenge,
        kind=kind,
        user_id=user_id,
        invite_id=invite_id,
        expires_at=in_minutes(CHALLENGE_TTL_MINUTES),
    )
    session.add(row)
    await session.flush()
    return row


async def _consume_challenge(
    session: AsyncSession,
    credential: dict[str, Any],
    kind: ChallengeKind,
    *,
    user_id: str | None = None,
    invite_id: str | None = None,
) -> WebAuthnChallenge:
    """Find, validate and burn the challenge this credential answers."""
    challenge = _client_challenge(credential)
    row = (
        await session.execute(
            select(WebAuthnChallenge).where(
                WebAuthnChallenge.challenge == challenge,
                WebAuthnChallenge.kind == kind,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        raise AppError("CHALLENGE_INVALID", "Unknown or already-cleared challenge")
    if row.consumed_at is not None:
        raise AppError("CHALLENGE_USED", "This challenge has already been used")
    if is_expired(row.expires_at):
        raise AppError("CHALLENGE_EXPIRED", "This challenge has expired")
    # A challenge issued for one account must never authorize another.
    if user_id is not None and row.user_id != user_id:
        raise AppError("CHALLENGE_MISMATCH", "Challenge was issued for a different account")
    if invite_id is not None and row.invite_id != invite_id:
        raise AppError("CHALLENGE_MISMATCH", "Challenge was issued for a different invite")

    # Burn it with a conditional UPDATE rather than an attribute assignment. The SELECT above
    # runs outside any write transaction, so two requests replaying one assertion can both
    # see `consumed_at IS NULL`; only the statement that actually matches a row wins, and the
    # loser is told the challenge is spent instead of being signed in a second time.
    if not await burn_single_use(session, WebAuthnChallenge, row.id):
        raise AppError("CHALLENGE_USED", "This challenge has already been used")
    return row


async def burn_single_use(session: AsyncSession, model: type[Any], row_id: str) -> bool:
    """Atomically stamp `consumed_at` on a single-use row. False means someone else got it.

    Shared with the OAuth state store, which has the identical race for the identical reason.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(model)
            .where(model.id == row_id, model.consumed_at.is_(None))
            .values(consumed_at=utcnow())
            .execution_options(synchronize_session="fetch")
        ),
    )
    return bool(result.rowcount)


async def purge_expired_challenges(session: AsyncSession) -> int:
    """One bulk DELETE — loading every stale row into the identity map first would make a
    routine sweep proportional to how long it had been neglected."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(WebAuthnChallenge)
            .where(WebAuthnChallenge.expires_at <= utcnow())
            # Nothing in a request-scoped session holds an expired challenge, and letting the
            # ORM synchronize would re-SELECT every row the DELETE is there to avoid touching.
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount or 0


# --- Passkey registration --------------------------------------------------------------------


async def begin_passkey_registration(
    session: AsyncSession,
    user: User | None = None,
    invite: Invite | None = None,
) -> dict[str, Any]:
    """Registration options for either an existing account or a pending invite.

    The invite's id doubles as the WebAuthn user handle and becomes the new `User.id` at
    completion, so the authenticator sees one stable identity across enrollment and any
    later passkey the user adds.
    """
    settings = get_settings()
    if user is not None and invite is None:
        handle, email, display_name = user.id, user.email, user.display_name
        exclude = _descriptors(list(user.credentials))
    elif invite is not None and user is None:
        handle, email = invite.id, invite.email
        display_name = display_name_from_email(invite.email)
        exclude = []
    else:
        raise AppError("INVALID_REGISTRATION_TARGET", "Pass exactly one of user or invite", 500)

    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=handle.encode("utf-8"),
        user_name=email,
        user_display_name=display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
    )
    await _store_challenge(
        session,
        options.challenge,
        ChallengeKind.REGISTRATION,
        user_id=user.id if user else None,
        invite_id=invite.id if invite else None,
    )
    return json.loads(options_to_json(options))


async def complete_passkey_registration(
    session: AsyncSession,
    credential: dict[str, Any] | str,
    *,
    user: User | None = None,
    invite: Invite | None = None,
    nickname: str | None = None,
    user_agent: str | None = None,
    display_name: str | None = None,
    locale: str | None = None,
) -> Credential:
    """Verify a registration response and persist the passkey.

    Exactly one of `user` (adding a passkey to a signed-in account) or `invite` (enrolling a
    brand-new account) must be given. In the invite case the `User` is created here with the
    invite's role and the invite is consumed; the new account is `Credential.user_id`.
    """
    if (user is None) == (invite is None):
        raise AppError("INVALID_REGISTRATION_TARGET", "Pass exactly one of user or invite", 500)

    payload = _credential_json(credential)
    settings = get_settings()
    challenge = await _consume_challenge(
        session,
        payload,
        ChallengeKind.REGISTRATION,
        user_id=user.id if user else None,
        invite_id=invite.id if invite else None,
    )

    try:
        verified = verify_registration_response(
            credential=payload,
            expected_challenge=challenge.challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origins,
            require_user_verification=False,
        )
    except WebAuthnException as exc:
        raise AppError(
            "PASSKEY_REGISTRATION_FAILED", f"Passkey could not be verified: {exc}"
        ) from exc

    existing = (
        await session.execute(
            select(Credential).where(Credential.credential_id == verified.credential_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("PASSKEY_ALREADY_REGISTERED", "This passkey is already registered")

    if invite is not None:
        user = await create_user_from_invite(
            session, invite, display_name=display_name, locale=locale
        )
        await consume_invite(session, invite, user)
    if user is None:  # unreachable; keeps the type narrow for the assignment below
        raise AppError("INVALID_REGISTRATION_TARGET", "No account to attach the passkey to", 500)

    record = Credential(
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=_transports(payload),
        aaguid=verified.aaguid,
        nickname=(nickname or "").strip() or credential_nickname(verified.aaguid, user_agent),
        backed_up=verified.credential_backed_up,
    )
    session.add(record)
    await session.flush()
    return record


async def create_user_from_invite(
    session: AsyncSession,
    invite: Invite,
    *,
    display_name: str | None,
    locale: str | None,
) -> User:
    """The one place an invite turns into an account.

    Shared with the Google flow so that "accept an invitation" means exactly the same thing —
    same id reuse, same role, same defaults — whichever credential the user arrives with.
    """
    email = normalize_email(invite.email)
    clash = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if clash is not None:
        raise Conflict("EMAIL_IN_USE", "An account already exists for this address")

    svc = SettingsService(session)
    default_locale = await svc.get_str("site.default_locale", get_settings().default_locale)
    user = User(
        # Reusing the invite id keeps the WebAuthn user handle minted at `begin` valid.
        id=invite.id,
        email=email,
        display_name=(display_name or "").strip() or display_name_from_email(email),
        role=invite.role or UserRole.VIEWER,
        is_active=True,
        locale=(locale or "").strip() or default_locale,
        avatar_color=avatar_color_for(email),
    )
    session.add(user)
    await session.flush()
    return user


# --- Passkey login ---------------------------------------------------------------------------


async def begin_passkey_login(session: AsyncSession) -> dict[str, Any]:
    """Options for a discoverable-credential assertion: no email, no allowCredentials."""
    settings = get_settings()
    options = generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    await _store_challenge(session, options.challenge, ChallengeKind.AUTHENTICATION)
    return json.loads(options_to_json(options))


async def complete_passkey_login(
    session: AsyncSession,
    credential_json: dict[str, Any] | str,
) -> User:
    payload = _credential_json(credential_json)
    settings = get_settings()
    challenge = await _consume_challenge(session, payload, ChallengeKind.AUTHENTICATION)

    raw_id = payload.get("rawId") or payload.get("id")
    if not isinstance(raw_id, str):
        raise AppError("INVALID_CREDENTIAL", "Credential is missing rawId")
    try:
        credential_id = base64url_to_bytes(raw_id)
    except Exception as exc:
        raise AppError("INVALID_CREDENTIAL", "Credential rawId is not base64url") from exc

    record = (
        await session.execute(select(Credential).where(Credential.credential_id == credential_id))
    ).scalar_one_or_none()
    if record is None:
        raise Unauthorized("PASSKEY_UNKNOWN", "This passkey is not registered")

    try:
        verified = verify_authentication_response(
            credential=payload,
            expected_challenge=challenge.challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origins,
            credential_public_key=record.public_key,
            credential_current_sign_count=record.sign_count,
            require_user_verification=False,
        )
    except WebAuthnException as exc:
        raise Unauthorized("PASSKEY_VERIFICATION_FAILED", f"Assertion rejected: {exc}") from exc

    # py_webauthn already rejects a stale counter; repeat the check so a future bump in its
    # leniency cannot silently weaken clone detection here.
    if verified.new_sign_count and verified.new_sign_count <= record.sign_count:
        raise Unauthorized("PASSKEY_SIGN_COUNT_REGRESSION", "Authenticator counter went backwards")

    user = await session.get(User, record.user_id)
    if user is None:
        raise Unauthorized("PASSKEY_UNKNOWN", "This passkey is not registered")
    if not user.is_active:
        raise Forbidden("ACCOUNT_DISABLED", "This account has been deactivated")

    record.sign_count = verified.new_sign_count
    record.backed_up = verified.credential_backed_up
    record.last_used_at = utcnow()
    await session.flush()
    return user


async def delete_passkey(session: AsyncSession, user: User, credential_id: str) -> None:
    """Refuse to strip the account of its last way in."""
    record = await session.get(Credential, credential_id)
    if record is None or record.user_id != user.id:
        raise NotFound("Passkey", "PASSKEY_NOT_FOUND")

    magic_link_enabled = await SettingsService(session).get_bool("auth.magic_link_enabled", True)
    remaining = [c for c in user.credentials if c.id != record.id]
    # A linked Google account is a way in too, so it counts here exactly as it does in the
    # mirror-image guard on unlinking. Without this the two guards would disagree and a user
    # with Google plus one passkey could not remove the passkey.
    if not remaining and not magic_link_enabled and not user.oauth_identities:
        raise Conflict(
            "LAST_CREDENTIAL",
            "Cannot remove the only passkey while no other sign-in method is available",
        )

    await session.delete(record)
    await session.flush()


# --- Invites ---------------------------------------------------------------------------------


def invite_status(invite: Invite) -> str:
    if invite.accepted_at is not None:
        return "accepted"
    if invite.revoked_at is not None:
        return "revoked"
    if is_expired(invite.expires_at):
        return "expired"
    return "pending"


async def create_invite(
    session: AsyncSession,
    *,
    email: str,
    role: str = UserRole.VIEWER,
    created_by: User | None = None,
    ttl_hours: int | None = None,
) -> CreatedInvite:
    normalized = normalize_email(email)
    # The HTTP surfaces validate first, but `bootstrap.py` feeds this an env var straight
    # from the operator, and an address with a CR/LF in it is a mail-header injection.
    if not _EMAIL_RE.match(normalized) or len(normalized) > 320:
        raise AppError("INVALID_EMAIL", f"Not a usable email address: {email!r}")
    # Every role, derived from the enum. Spelled out as a pair, this rejected `viewer_r`
    # while `POST /api/admin/invites` accepted it — the two paths disagreeing about what a
    # valid role is, which whoever hit it first would have had to debug from a 400.
    if role not in set(UserRole):
        raise AppError("INVALID_ROLE", f"Unknown role '{role}'")

    clash = (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict("EMAIL_IN_USE", "An account already exists for this address")

    hours = ttl_hours or await SettingsService(session).get_int(
        "auth.invite_ttl_hours", DEFAULT_INVITE_TTL_HOURS
    )
    token = new_token(32)
    invite = Invite(
        id=new_id(),
        email=normalized,
        role=role,
        token_hash=hash_token(token),
        created_by_id=created_by.id if created_by else None,
        expires_at=in_minutes(hours * 60),
    )
    session.add(invite)
    await session.flush()
    return CreatedInvite(invite=invite, token=token, url=invite_url(token))


async def rotate_invite_token(session: AsyncSession, invite: Invite) -> CreatedInvite:
    """Resend needs a fresh link: only the hash was kept, so the old token is unrecoverable."""
    if invite.accepted_at is not None:
        raise Conflict("INVITE_ALREADY_USED", "This invite has already been accepted")

    hours = await SettingsService(session).get_int(
        "auth.invite_ttl_hours", DEFAULT_INVITE_TTL_HOURS
    )
    token = new_token(32)
    invite.token_hash = hash_token(token)
    invite.expires_at = in_minutes(hours * 60)
    invite.revoked_at = None
    await session.flush()
    return CreatedInvite(invite=invite, token=token, url=invite_url(token))


async def validate_invite_token(session: AsyncSession, token: str) -> Invite:
    invite = (
        await session.execute(select(Invite).where(Invite.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if invite is None:
        raise NotFound("Invite", "INVITE_INVALID")
    if invite.accepted_at is not None:
        raise AppError("INVITE_ALREADY_USED", "This invite has already been accepted")
    if invite.revoked_at is not None:
        raise AppError("INVITE_REVOKED", "This invite was revoked")
    if is_expired(invite.expires_at):
        raise AppError("INVITE_EXPIRED", "This invite has expired")
    return invite


async def consume_invite(session: AsyncSession, invite: Invite, user: User) -> None:
    invite.accepted_at = utcnow()
    invite.accepted_user_id = user.id
    await session.flush()


async def send_invite_email(session: AsyncSession, created: CreatedInvite) -> bool:
    """Best-effort. Returns whether the mail actually left — the URL is shown either way."""
    svc = SettingsService(session)
    locale = await svc.get_str("site.default_locale", get_settings().default_locale)
    inviter = created.invite.created_by
    try:
        await send_mail(
            session,
            created.invite.email,
            "invite",
            locale,
            {
                "invite_url": created.url,
                "email": created.invite.email,
                "role": created.invite.role,
                "inviter_name": inviter.display_name if inviter else None,
                "expires_at": created.invite.expires_at,
            },
        )
    except Exception as exc:
        # Deliberately total: `POST /admin/invites` promises the enrollment URL whatever
        # happens to the mail, so anything short of a crash — MailNotConfigured, a dead
        # relay, a template bug, an unaddressable recipient — has to come back as False.
        log.warning("Invite mail to %s not sent: %s", created.invite.email, exc)
        return False
    return True


# --- Magic links -------------------------------------------------------------------------------


async def request_magic_link(session: AsyncSession, email: str) -> None:
    """Never reveals whether the address has an account.

    An unknown address, an inactive account and a dead SMTP server all look identical from
    the outside: the call simply returns. Only the feature being switched off is reported,
    because that is a property of the deployment, not of any address.
    """
    svc = SettingsService(session)
    if not await svc.get_bool("auth.magic_link_enabled", True):
        raise Forbidden("MAGIC_LINK_DISABLED", "Magic-link sign-in is disabled")

    normalized = normalize_email(email)
    user = (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        log.info("Magic link requested for unknown or inactive address")
        return

    token = new_token(32)
    session.add(
        MagicLink(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=in_minutes(MAGIC_LINK_TTL_MINUTES),
        )
    )
    await session.flush()

    try:
        await send_mail(
            session,
            user.email,
            "magic_link",
            user.locale,
            {
                "magic_url": magic_link_url(token),
                "display_name": user.display_name,
                "expires_minutes": MAGIC_LINK_TTL_MINUTES,
            },
        )
    except Exception as exc:
        # Swallowed on purpose, and for *any* failure rather than just AppError: an unknown
        # address returns 204 without ever reaching this line, so letting one class of send
        # failure escape as a 500 would turn the endpoint into an account-existence oracle.
        log.warning("Magic-link mail could not be sent: %s", exc)


async def consume_magic_link(session: AsyncSession, token: str) -> User:
    link = (
        await session.execute(select(MagicLink).where(MagicLink.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if link is None:
        raise Unauthorized("MAGIC_LINK_INVALID", "This sign-in link is not valid")
    if link.consumed_at is not None:
        raise Unauthorized("MAGIC_LINK_USED", "This sign-in link has already been used")
    if is_expired(link.expires_at):
        raise Unauthorized("MAGIC_LINK_EXPIRED", "This sign-in link has expired")

    user = await session.get(User, link.user_id)
    if user is None:
        raise Unauthorized("MAGIC_LINK_INVALID", "This sign-in link is not valid")
    if not user.is_active:
        raise Forbidden("ACCOUNT_DISABLED", "This account has been deactivated")

    # Same race as the WebAuthn challenge: the read above is not part of a write transaction,
    # so two clicks on one emailed link must be settled by the UPDATE, not by the check.
    if not await burn_single_use(session, MagicLink, link.id):
        raise Unauthorized("MAGIC_LINK_USED", "This sign-in link has already been used")

    now = utcnow()
    # Signing in retires every other outstanding link for the account, so a stale email in
    # an inbox cannot be redeemed later.
    others = (
        (
            await session.execute(
                select(MagicLink).where(
                    MagicLink.user_id == user.id,
                    MagicLink.consumed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for other in others:
        other.consumed_at = now
    await session.flush()
    return user


__all__ = [
    "AVATAR_COLORS",
    "CHALLENGE_TTL_MINUTES",
    "MAGIC_LINK_TTL_MINUTES",
    "CreatedInvite",
    "avatar_color_for",
    "begin_passkey_login",
    "begin_passkey_registration",
    "burn_single_use",
    "complete_passkey_login",
    "complete_passkey_registration",
    "consume_invite",
    "consume_magic_link",
    "create_invite",
    "create_user_from_invite",
    "credential_nickname",
    "delete_passkey",
    "display_name_from_email",
    "invite_status",
    "invite_url",
    "magic_link_url",
    "normalize_email",
    "purge_expired_challenges",
    "request_magic_link",
    "rotate_invite_token",
    "send_invite_email",
    "validate_invite_token",
]
