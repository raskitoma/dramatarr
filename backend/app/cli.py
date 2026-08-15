"""Console administration for dramatarr.

Passkeys are the only way in through the web UI, and a passkey has to be created on the
device that will use it. That is fine on a laptop and impossible on a headless NAS — nobody
is standing at the box with a fingerprint reader, and screen-sharing into it does not forward
biometrics either. These commands give the operator a way in from a shell:

    docker compose exec api python -m app.cli create-admin --email you@example.com
    docker compose exec api python -m app.cli login-link  --email you@example.com

`login-link` prints a sign-in URL directly instead of mailing it, so first access never
depends on SMTP being configured yet. Anyone who can run this already has the database and
the secret key, so it grants nothing they did not already have.

`protect` / `unprotect` live here for the same reason, from the other direction: a protected
account is one the web UI may not touch, so the flag has to be movable from somewhere the web
UI is not — otherwise the guard that saves the household from a bad admin becomes a trap of
its own the first time the founding address needs to change hands.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import hash_token
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.models import AuthSession, Credential, Invite, MagicLink, User, UserRole
from app.services.auth import create_invite, normalize_email
from app.utils.ids import new_token
from app.utils.time import in_days, in_minutes, utcnow

DEFAULT_LINK_TTL_MINUTES = 60


def _base_url(override: str | None) -> str:
    """Where the printed links should point.

    `DRAMATARR_PUBLIC_URL` is right for mailed links, but the operator running this may be on
    a different machine than the one the app calls itself — reaching the box by LAN address,
    for instance — so it can be overridden per invocation.
    """
    return (override or get_settings().public_url).rstrip("/")


def _banner(title: str, lines: list[str]) -> None:
    width = 78
    print()
    print("=" * width)
    print(f"  {title}")
    print("-" * width)
    for line in lines:
        print(f"  {line}")
    print("=" * width)
    print()


async def _find_user(session: AsyncSession, email: str) -> User | None:
    normalized = normalize_email(email)
    return (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()


# --- Commands -------------------------------------------------------------------------------


async def cmd_create_admin(session: AsyncSession, args: argparse.Namespace) -> int:
    email = normalize_email(args.email)
    existing = await _find_user(session, email)
    if existing is not None:
        if existing.role == UserRole.ADMIN and existing.is_active:
            print(f"{email} is already an active admin.")
            return 0
        existing.role = UserRole.ADMIN
        existing.is_active = True
        await session.commit()
        print(f"Promoted {email} to an active admin.")
        return 0

    # The very first account of an empty database is the founding one: it is the household's
    # way back in, so it is protected from the admin UI from the moment it exists. Later
    # accounts are not — `protect` is there for moving the flag deliberately.
    founding = not await session.scalar(select(func.count()).select_from(User))

    user = User(
        email=email,
        display_name=args.name or email.split("@")[0],
        role=UserRole.ADMIN,
        is_active=True,
        locale=args.locale or get_settings().default_locale,
        is_protected=founding,
    )
    session.add(user)
    await session.commit()

    lines = [
        f"Email : {user.email}",
        f"Name  : {user.display_name}",
    ]
    if founding:
        lines += [
            "",
            "Marked as the founding account: no admin can demote, deactivate, rename",
            "or delete it from the web UI. Undo that from a shell with:",
            f"  python -m app.cli unprotect --email {user.email}",
        ]
    lines += [
        "",
        "This account has no passkey yet. Get a sign-in link with:",
        f"  python -m app.cli login-link --email {user.email}",
        "",
        "Then add a passkey from Account once you are signed in on the device",
        "you actually want to use.",
    ]
    _banner("Admin created", lines)
    return 0


#: What the flag means, printed by `protect`/`unprotect` so nobody has to guess from the name.
PROTECTION_EXPLAINER = [
    "A protected account cannot be changed from the web UI at all: no role change,",
    "no deactivation, no rename, no deletion — the admin screens answer PROTECTED_ACCOUNT.",
    "It is meant for the account the household was bootstrapped with, so a slip or a",
    "compromised second admin cannot take away the last way back in.",
    "",
    "The person themselves is not restricted: they change their own name, language and",
    "preferences from Account as usual. Only the admin *management* surface is blocked.",
    "",
    "This flag is reachable from a shell and nowhere else, exactly like `login-link`.",
]


async def cmd_protect(session: AsyncSession, args: argparse.Namespace) -> int:
    user = await _find_user(session, args.email)
    if user is None:
        print(f"No account for {args.email}.", file=sys.stderr)
        return 1

    if user.is_protected:
        print(f"{user.email} is already protected.")
        return 0
    if not (user.role == UserRole.ADMIN and user.is_active):
        # Protecting a viewer or a disabled account would freeze it that way: promoting and
        # reactivating both go through the admin surface this flag blocks.
        print(
            f"{user.email} is not an active admin, and protecting it would leave it stuck "
            "that way — the web UI could no longer promote or reactivate it.\n"
            f"Fix that first:  python -m app.cli create-admin --email {user.email}",
            file=sys.stderr,
        )
        return 1

    user.is_protected = True
    await session.commit()
    _banner("Account protected", [f"Account : {user.email}", "", *PROTECTION_EXPLAINER])
    return 0


async def cmd_unprotect(session: AsyncSession, args: argparse.Namespace) -> int:
    user = await _find_user(session, args.email)
    if user is None:
        print(f"No account for {args.email}.", file=sys.stderr)
        return 1

    if not user.is_protected:
        print(f"{user.email} is not protected.")
        return 0

    user.is_protected = False
    await session.commit()
    _banner(
        "Protection removed",
        [
            f"Account : {user.email}",
            "",
            "Any admin can now demote, deactivate, rename or delete this account from",
            "the web UI. Only the last-admin guard is left, so if this is the account",
            "you would use to get back in, protect another one first:",
            "  python -m app.cli protect --email someone@example.com",
            "",
            *PROTECTION_EXPLAINER,
        ],
    )
    return 0


async def cmd_set_email(session: AsyncSession, args: argparse.Namespace) -> int:
    """Change an account's address.

    The web UI deliberately refuses this — an address is an identity, not a preference, and
    letting people edit their own would let one household member impersonate an invitation
    meant for another. Passkeys survive the change: a credential is bound to the user's id,
    not their address, and `user_name` only labels the entry in the authenticator's list.
    """
    user = await _find_user(session, args.email)
    if user is None:
        print(f"No account for {args.email}.", file=sys.stderr)
        return 1

    new_email = normalize_email(args.new_email)
    if new_email == user.email:
        print(f"{user.email} already has that address.")
        return 0

    clash = await _find_user(session, new_email)
    if clash is not None:
        print(f"{new_email} is already used by another account.", file=sys.stderr)
        return 1

    old = user.email
    user.email = new_email
    await session.commit()

    passkeys = await session.scalar(
        select(func.count()).select_from(Credential).where(Credential.user_id == user.id)
    )
    lines = [f"{old}  ->  {new_email}"]
    if passkeys:
        lines += [
            "",
            f"{passkeys} passkey(s) still work — they are bound to the account, not the address.",
            "Your authenticator may keep showing the old address as the entry's label.",
        ]
    _banner("Address changed", lines)
    return 0


async def cmd_login_link(session: AsyncSession, args: argparse.Namespace) -> int:
    user = await _find_user(session, args.email)
    if user is None:
        print(f"No account for {args.email}. Create one with: create-admin --email …", file=sys.stderr)
        return 1
    if not user.is_active:
        print(f"{user.email} is deactivated. Reactivate it first.", file=sys.stderr)
        return 1

    token = new_token(32)
    session.add(
        MagicLink(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=in_minutes(args.ttl),
        )
    )
    await session.commit()

    _banner(
        "Sign-in link",
        [
            f"Account : {user.email}",
            f"Valid   : {args.ttl} minutes, single use",
            "",
            f"{_base_url(args.url)}/login?token={token}",
            "",
            "Open it in the browser you want to be signed in on.",
        ],
    )
    return 0


async def cmd_invite(session: AsyncSession, args: argparse.Namespace) -> int:
    role = UserRole.ADMIN if args.admin else UserRole.VIEWER
    created = await create_invite(session, email=args.email, role=role)
    await session.commit()

    url = f"{_base_url(args.url)}/invite/{created.token}"
    _banner(
        "Invitation created",
        [
            f"Email : {created.invite.email}",
            f"Role  : {created.invite.role}",
            f"Expires: {created.invite.expires_at:%Y-%m-%d %H:%M} UTC",
            "",
            url,
            "",
            "Single use. The invitee creates their passkey on their own device.",
        ],
    )
    return 0


async def cmd_list_users(session: AsyncSession, _: argparse.Namespace) -> int:
    users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
    if not users:
        print("No users yet.")
        return 0

    counts = dict(
        (
            await session.execute(
                select(Credential.user_id, func.count(Credential.id)).group_by(Credential.user_id)
            )
        ).all()
    )
    # PROTECTED is listed because a shell is the only place the flag can be read *or* changed.
    print(f"{'EMAIL':38} {'ROLE':8} {'ACTIVE':7} {'PROTECTED':10} {'PASSKEYS':9} LAST LOGIN")
    for user in users:
        last = f"{user.last_login_at:%Y-%m-%d %H:%M}" if user.last_login_at else "never"
        print(
            f"{user.email:38} {user.role:8} {'yes' if user.is_active else 'no':7} "
            f"{'yes' if user.is_protected else 'no':10} {counts.get(user.id, 0):<9} {last}"
        )
    return 0


async def cmd_revoke_sessions(session: AsyncSession, args: argparse.Namespace) -> int:
    """Sign a person out everywhere — the thing you want after a lost device."""
    user = await _find_user(session, args.email)
    if user is None:
        print(f"No account for {args.email}.", file=sys.stderr)
        return 1

    rows = (
        await session.execute(
            select(AuthSession).where(
                AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    now = utcnow()
    for row in rows:
        row.revoked_at = now
    await session.commit()
    print(f"Revoked {len(rows)} active session(s) for {user.email}.")
    return 0


async def cmd_prune(session: AsyncSession, _: argparse.Namespace) -> int:
    """Drop expired sessions, magic links and invites."""
    now = utcnow()
    removed = 0
    for model, column in (
        (AuthSession, AuthSession.expires_at),
        (MagicLink, MagicLink.expires_at),
        (Invite, Invite.expires_at),
    ):
        rows = (await session.execute(select(model).where(column < now))).scalars().all()
        for row in rows:
            await session.delete(row)
        removed += len(rows)
    await session.commit()
    print(f"Removed {removed} expired row(s).")
    return 0


COMMANDS: dict[str, Callable[[AsyncSession, argparse.Namespace], Awaitable[int]]] = {
    "create-admin": cmd_create_admin,
    "set-email": cmd_set_email,
    "login-link": cmd_login_link,
    "invite": cmd_invite,
    "list-users": cmd_list_users,
    "protect": cmd_protect,
    "unprotect": cmd_unprotect,
    "revoke-sessions": cmd_revoke_sessions,
    "prune": cmd_prune,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="dramatarr console administration",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-admin", help="create (or promote) an active admin account")
    p.add_argument("--email", required=True)
    p.add_argument("--name", help="display name; defaults to the address' local part")
    p.add_argument("--locale", help="es or en; defaults to DRAMATARR_DEFAULT_LOCALE")

    p = sub.add_parser("set-email", help="change an account's address (passkeys survive)")
    p.add_argument("--email", required=True, help="current address")
    p.add_argument("--new-email", required=True, help="address to move the account to")

    p = sub.add_parser("login-link", help="print a single-use sign-in link, bypassing email")
    p.add_argument("--email", required=True)
    p.add_argument("--ttl", type=int, default=DEFAULT_LINK_TTL_MINUTES, help="minutes (default 60)")
    p.add_argument("--url", help="base URL for the link, e.g. http://192.168.1.20:8080")

    p = sub.add_parser("invite", help="create an invitation and print its enrollment URL")
    p.add_argument("--email", required=True)
    p.add_argument("--admin", action="store_true", help="invite as admin instead of viewer")
    p.add_argument("--url", help="base URL for the link")

    sub.add_parser("list-users", help="list accounts, their passkey counts and protection")

    p = sub.add_parser("protect", help="shield an account from every change made in the web UI")
    p.add_argument("--email", required=True)

    p = sub.add_parser("unprotect", help="let the web UI manage an account again")
    p.add_argument("--email", required=True)

    p = sub.add_parser("revoke-sessions", help="sign an account out of every device")
    p.add_argument("--email", required=True)

    sub.add_parser("prune", help="delete expired sessions, magic links and invitations")

    return parser


async def _run(args: argparse.Namespace) -> int:
    get_settings().ensure_dirs()
    async with get_sessionmaker()() as session:
        try:
            return await COMMANDS[args.command](session, args)
        finally:
            await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
