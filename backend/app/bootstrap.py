"""First-boot setup: default settings rows and the initial admin enrollment link.

Enrollment is invite-only and `complete_passkey_registration` creates the account itself —
`create_user_from_invite` refuses an email that already has a `User`. So first boot mints an
*invite* for `DRAMATARR_BOOTSTRAP_ADMIN_EMAIL` rather than a `User` row; the admin account
materializes with role `admin`, active, the moment the operator enrolls a passkey. Creating
the row up front would make its own enrollment link fail with `EMAIL_IN_USE`.

That indirection is also why `ensure_founder_protected` is a separate step that runs on every
start rather than a field set at creation: see its docstring.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Invite, Setting, User, UserRole
from app.services.auth import (
    CreatedInvite,
    create_invite,
    normalize_email,
    rotate_invite_token,
)
from app.services.settings import DEFAULTS, SettingsService

log = logging.getLogger(__name__)


async def bootstrap(session: AsyncSession) -> None:
    """Idempotent. Safe to call on every start; does real work only while the DB is empty."""
    await seed_settings(session)
    created = await ensure_bootstrap_admin(session)
    # After `ensure_bootstrap_admin`, never before: that call rolls the session back when the
    # configured address turns out to be unusable, and a flag set first would go down with it.
    await ensure_founder_protected(session)
    await session.commit()
    if created is not None:
        announce_enrollment(created.invite.email, created.url)


async def seed_settings(session: AsyncSession) -> None:
    """Write a row for every default that has none.

    Only missing keys are inserted, so a release that adds a default picks it up on the next
    start without ever overwriting a value an admin has changed.
    """
    existing = set((await session.execute(select(Setting.key))).scalars().all())
    missing = [key for key in DEFAULTS if key not in existing]
    if not missing:
        return
    session.add_all([Setting(key=key, value={"v": DEFAULTS[key]}) for key in missing])
    # Committed here rather than left to `bootstrap`: `ensure_bootstrap_admin` rolls the
    # session back when the configured address turns out to be unusable, and a merely
    # flushed seed would go down with it — leaving `settings` empty on every single boot
    # for as long as the address stays bad. Invalidate only once the rows are durable.
    await session.commit()
    SettingsService.invalidate()
    log.info("seeded %d default setting(s)", len(missing))


async def ensure_bootstrap_admin(session: AsyncSession) -> CreatedInvite | None:
    settings = get_settings()

    if await session.scalar(select(func.count()).select_from(User)):
        return None  # the household exists; first boot is over for good

    email = (settings.bootstrap_admin_email or "").strip()
    if not email:
        log.warning(
            "There are no users and DRAMATARR_BOOTSTRAP_ADMIN_EMAIL is unset, so nobody can "
            "sign in. Set it and restart to get an enrollment link."
        )
        return None

    try:
        created = await _bootstrap_invite(session, normalize_email(email))
    except Exception:
        # A bad address must not crash-loop the container: the operator needs the API up to
        # read the log line that explains what went wrong.
        log.exception("could not create the bootstrap admin invite for %r", email)
        await session.rollback()
        return None

    return created


async def ensure_founder_protected(session: AsyncSession) -> User | None:
    """Mark the account the household was bootstrapped with as protected.

    Set on the account this module is responsible for creating — but it cannot be set at the
    moment of creation, because what first boot creates is an *invite*, not a `User`. The row
    only materializes when the operator enrolls their passkey, minutes and one HTTP request
    later, inside `create_user_from_invite`. So the flag is applied on the next start, to the
    account that is by then the household's only one, which is exactly the account that
    invite minted.

    "Only one" is the whole rule, and it is what keeps this from fighting the escape hatch:
    the moment a second person is invited this function does nothing, ever again, so an
    operator who ran `python -m app.cli unprotect` on a real household never finds the flag
    quietly back after a restart. And while the household really is one admin, re-asserting
    it costs almost nothing: the last-admin guard already refuses to demote, deactivate or
    delete that account, so all the flag adds is that their display name has to be changed
    from Account rather than from the admin screen.

    Installs that predate the flag are handled once, by migration `0004_user_is_protected`,
    which marks their oldest active admin.
    """
    users = (await session.execute(select(User).limit(2))).scalars().all()
    if len(users) != 1:
        return None

    founder = users[0]
    # An inactive or demoted sole account is not a recovery path, and protecting it would
    # freeze it that way — the web UI refuses to reactivate or promote a protected row.
    if founder.is_protected or not founder.is_admin or not founder.is_active:
        return None

    founder.is_protected = True
    log.info(
        "%s is the founding account; protecting it from the admin UI "
        "(move the flag with `python -m app.cli unprotect --email %s`)",
        founder.email,
        founder.email,
    )
    return founder


async def _bootstrap_invite(session: AsyncSession, email: str) -> CreatedInvite:
    pending = (
        (
            await session.execute(
                select(Invite)
                .where(Invite.email == email, Invite.accepted_at.is_(None))
                .order_by(Invite.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if pending is None:
        return await create_invite(session, email=email, role=UserRole.ADMIN)

    # Only the token's sha256 was stored, so an earlier link can never be reprinted. Rotating
    # reuses the one invite row and still hands the operator a working URL after a restart.
    pending.role = UserRole.ADMIN
    return await rotate_invite_token(session, pending)


def announce_enrollment(email: str, url: str) -> None:
    """Logged at WARNING on purpose: this is the only time the raw token is ever visible, and
    it must not be lost among INFO chatter."""
    rule = "=" * 78
    log.warning(
        "\n%s\n  dramatarr first-run enrollment\n"
        "  Admin account : %s\n"
        "  Open this URL to register your passkey (single use):\n\n"
        "    %s\n\n%s",
        rule,
        email,
        url,
        rule,
    )


__all__ = [
    "announce_enrollment",
    "bootstrap",
    "ensure_bootstrap_admin",
    "ensure_founder_protected",
    "seed_settings",
]
