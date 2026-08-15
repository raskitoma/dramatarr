"""The founding account, and why the admin screens may not touch it.

`LAST_ADMIN` answers "is anyone left?". It is not enough. Every admin can demote, deactivate
or delete every other admin, so a household with two admins has no protection at all against
the one that matters: the account it was bootstrapped with going away — by a misclick, or by
a second admin whose laptop was taken — leaving nobody who can get back in without a shell on
the box. `users.is_protected` marks that account and `/api/admin/users` refuses every mutation
of the row with `PROTECTED_ACCOUNT`.

What these tests pin down, in order:

* every mutating path on the admin surface refuses, including the ones that change nothing;
* an ordinary admin is still fully manageable, so the guard is narrow rather than a freeze;
* the protected person is not restricted — `/api/me` still edits their own profile, because
  this guards the *management* surface, not self-service;
* the flag is set where it should be (bootstrap, and the first account `create-admin` makes)
  and can be moved from a shell, which is the escape hatch that keeps it from being a trap;
* the column arrives on a database that already holds a household without disturbing it.

The client is a *second* admin throughout. Testing this as the protected user themselves
would prove nothing: the interesting caller is the other admin — or whoever is holding their
session — and the guard is deliberately unconditional so there is no "unless it is you" hole
for that caller to walk through.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserRole

pytestmark = pytest.mark.anyio

#: The revision that shipped before `is_protected`, i.e. what an existing install is at.
PREVIOUS_REVISION = "0003_oauth_state_browser"


# --- Helpers ---------------------------------------------------------------------------------


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def namespace(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


async def persist(session: AsyncSession, user: User) -> User:
    """Add, commit, and hand back an instance loaded the way a request would find it.

    The handlers share this session, so `session.get()` inside them answers from the identity
    map without emitting the query that would populate `User.credentials`. Re-selecting here
    runs the selectin loader once, so the fixture stands in for a row read from the database
    rather than one this test just built in memory.
    """
    session.add(user)
    await session.commit()
    return (await session.execute(select(User).where(User.id == user.id))).scalar_one()


async def reload(session: AsyncSession, user: User) -> User:
    """Re-read the row, never trusting what is already in memory.

    The handler under test shares this session, so a mutation it applied to the instance and
    then failed to guard would still read back as "unchanged" from the object itself. This
    goes to the database for every attribute instead.
    """
    await session.refresh(user)
    return user


# --- Fixtures --------------------------------------------------------------------------------


@pytest.fixture
async def founder(db: AsyncSession) -> User:
    """The bootstrapped account: an active admin, protected."""
    user = User(
        email="founder@example.com",
        display_name="Fundadora",
        role=UserRole.ADMIN,
        is_active=True,
        is_protected=True,
        locale="es",
    )
    return await persist(db, user)


@pytest.fixture
async def other_admin(db: AsyncSession) -> User:
    """A second admin — the caller in every API test here, and itself fully manageable."""
    user = User(
        email="second@example.com",
        display_name="Segundo",
        role=UserRole.ADMIN,
        is_active=True,
        locale="es",
    )
    return await persist(db, user)


@pytest.fixture
async def viewer(db: AsyncSession) -> User:
    user = User(
        email="viewer@example.com",
        display_name="Espectadora",
        role=UserRole.VIEWER,
        is_active=True,
        locale="es",
    )
    return await persist(db, user)


def _client(db: AsyncSession, user: User, *, admin: bool) -> AsyncClient:
    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: user
    if admin:
        app.dependency_overrides[require_admin] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(db: AsyncSession, other_admin: User) -> AsyncIterator[AsyncClient]:
    """Signed in as the *other* admin: the caller the guard exists to stop."""
    from app.main import app

    try:
        async with _client(db, other_admin, admin=True) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def founder_client(db: AsyncSession, founder: User) -> AsyncIterator[AsyncClient]:
    """Signed in as the protected user, for the `/api/me` self-service checks."""
    from app.main import app

    try:
        async with _client(db, founder, admin=True) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


# --- Every mutating admin path refuses --------------------------------------------------------


@pytest.mark.parametrize(
    ("patch", "what"),
    [
        ({"role": "viewer"}, "demote"),
        ({"role": "admin"}, "re-assert the role it already has"),
        ({"isActive": False}, "deactivate"),
        ({"isActive": True}, "re-activate an already active account"),
        ({"displayName": "Renamed"}, "rename"),
        ({"role": "viewer", "isActive": False}, "demote and deactivate at once"),
    ],
)
async def test_patch_refuses_every_managed_field(
    client: AsyncClient, db: AsyncSession, founder: User, patch: dict[str, Any], what: str
) -> None:
    """Naming a field is what is refused, not naming one whose value would differ.

    The no-op cases matter as much as the real ones: letting a PATCH through because it
    happened to change nothing is the kind of subtlety a later refactor turns into a hole,
    and the UI has no reason to send these fields for a protected row at all.
    """
    response = await client.patch(f"/api/admin/users/{founder.id}", json=patch)

    assert response.status_code == 409, f"{what}: {response.text}"
    assert error_code(response) == "PROTECTED_ACCOUNT"

    fresh = await reload(db, founder)
    assert fresh.role == UserRole.ADMIN
    assert fresh.is_active is True
    assert fresh.display_name == "Fundadora"
    assert fresh.is_protected is True


async def test_delete_refuses(client: AsyncClient, db: AsyncSession, founder: User) -> None:
    response = await client.delete(f"/api/admin/users/{founder.id}")

    assert response.status_code == 409, response.text
    assert error_code(response) == "PROTECTED_ACCOUNT"
    assert await db.get(User, founder.id) is not None


async def test_empty_patch_is_not_a_mutation(
    client: AsyncClient, founder: User
) -> None:
    """A PATCH that names no managed field only reads the row back, so it is allowed.

    This is what keeps the guard from turning into "the protected user is unreadable": the
    admin screen still renders the row, it just cannot change it.
    """
    response = await client.patch(f"/api/admin/users/{founder.id}", json={})

    assert response.status_code == 200, response.text
    assert response.json()["isProtected"] is True


async def test_protected_wins_over_last_admin(
    db: AsyncSession, other_admin: User, founder: User
) -> None:
    """When both guards apply, the caller is told the stricter, more specific reason.

    Demoting the only remaining admin is refused twice over. `LAST_ADMIN` would send the
    admin looking for a second administrator to appoint; `PROTECTED_ACCOUNT` tells them the
    thing they did not know, which is that this row is off limits wherever they stand.
    """
    from app.main import app

    other_admin.is_active = False  # the founder is now the last active admin, too
    await db.commit()

    try:
        async with _client(db, other_admin, admin=True) as http:
            response = await http.patch(
                f"/api/admin/users/{founder.id}", json={"role": "viewer"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert error_code(response) == "PROTECTED_ACCOUNT"


# --- An unprotected admin is still fully manageable --------------------------------------------


async def test_unprotected_admin_is_still_editable(
    client: AsyncClient, db: AsyncSession, founder: User, other_admin: User
) -> None:
    """The guard is narrow. With the founder protected and present, the second admin can
    still be renamed, demoted and deactivated exactly as before."""
    response = await client.patch(
        f"/api/admin/users/{other_admin.id}",
        json={"displayName": "Segundo Renombrado", "role": "viewer", "isActive": False},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["displayName"] == "Segundo Renombrado"
    assert body["role"] == "viewer"
    assert body["isActive"] is False
    assert body["isProtected"] is False

    fresh = await reload(db, other_admin)
    assert fresh.role == UserRole.VIEWER
    assert fresh.is_active is False


async def test_unprotected_user_is_still_deletable(
    client: AsyncClient, db: AsyncSession, founder: User, viewer: User
) -> None:
    viewer_id = viewer.id

    response = await client.delete(f"/api/admin/users/{viewer_id}")

    assert response.status_code == 200, response.text
    db.expire_all()
    assert await db.get(User, viewer_id) is None


async def test_last_admin_guard_still_fires_without_protection(
    db: AsyncSession, other_admin: User
) -> None:
    """The pre-existing guard is untouched: an unprotected sole admin is still undemotable."""
    from app.main import app

    try:
        async with _client(db, other_admin, admin=True) as http:
            response = await http.patch(
                f"/api/admin/users/{other_admin.id}", json={"role": "viewer"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert error_code(response) == "LAST_ADMIN"


async def test_list_users_exposes_the_flag(
    client: AsyncClient, founder: User, other_admin: User
) -> None:
    """The UI needs to know *why* a row's controls are dead before the admin clicks them."""
    response = await client.get("/api/admin/users")

    assert response.status_code == 200, response.text
    flags = {row["email"]: row["isProtected"] for row in response.json()}
    assert flags == {"founder@example.com": True, "second@example.com": False}


# --- Self-service is untouched -----------------------------------------------------------------


async def test_protected_user_can_still_edit_their_own_profile(
    founder_client: AsyncClient, db: AsyncSession, founder: User
) -> None:
    """`/api/me` is the person, not the management surface, so the flag does not reach it."""
    response = await founder_client.patch(
        "/api/me", json={"displayName": "Fundadora Nueva", "theme": "dark", "locale": "en"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["displayName"] == "Fundadora Nueva"

    fresh = await reload(db, founder)
    assert fresh.display_name == "Fundadora Nueva"
    assert fresh.theme == "dark"
    assert fresh.is_protected is True  # editing themselves does not clear it


async def test_protected_user_cannot_rename_themselves_through_admin(
    db: AsyncSession, founder: User
) -> None:
    """Not even the protected admin gets an exception on the management surface.

    Whoever holds that session is not necessarily the person — that is the whole threat —
    so `/api/me` is the only door, and it is enough.
    """
    from app.main import app

    try:
        async with _client(db, founder, admin=True) as http:
            response = await http.patch(
                f"/api/admin/users/{founder.id}", json={"displayName": "Yo Mismo"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert error_code(response) == "PROTECTED_ACCOUNT"


# --- Where the flag is set ---------------------------------------------------------------------


async def test_bootstrap_marks_the_first_account(db: AsyncSession) -> None:
    """First boot mints an invite; the account appears later, when the passkey is enrolled.

    So the sequence under test is the real one: the household's only account exists, the
    container restarts, and `bootstrap` marks it on the way up.
    """
    from app.bootstrap import bootstrap

    user = User(
        email="first@example.com",
        display_name="Primera",
        role=UserRole.ADMIN,
        is_active=True,
        locale="es",
    )
    db.add(user)
    await db.commit()

    await bootstrap(db)

    assert (await reload(db, user)).is_protected is True


async def test_bootstrap_leaves_an_established_household_alone(
    db: AsyncSession, other_admin: User, viewer: User
) -> None:
    """Two accounts means the window has closed for good — this is what stops `unprotect`
    from being silently undone by the next restart."""
    from app.bootstrap import ensure_founder_protected

    assert await ensure_founder_protected(db) is None
    assert (await reload(db, other_admin)).is_protected is False
    assert (await reload(db, viewer)).is_protected is False


@pytest.mark.parametrize(
    ("role", "is_active"),
    [(UserRole.VIEWER, True), (UserRole.ADMIN, False)],
)
async def test_bootstrap_skips_a_sole_account_that_is_no_recovery_path(
    db: AsyncSession, role: UserRole, is_active: bool
) -> None:
    """Protecting a demoted or disabled account would freeze it that way: promoting and
    reactivating both go through the surface the flag blocks."""
    from app.bootstrap import ensure_founder_protected

    user = User(
        email="solo@example.com",
        display_name="Solo",
        role=role,
        is_active=is_active,
        locale="es",
    )
    db.add(user)
    await db.commit()

    assert await ensure_founder_protected(db) is None
    assert (await reload(db, user)).is_protected is False


async def test_create_admin_protects_only_the_founding_account(db: AsyncSession) -> None:
    """`create-admin` on an empty database is the other way a household starts."""
    from app.cli import cmd_create_admin

    for email in ("one@example.com", "two@example.com"):
        args = namespace(email=email, name=None, locale=None)
        assert await cmd_create_admin(db, args) == 0

    users = {user.email: user for user in (await db.execute(select(User))).scalars().all()}
    assert users["one@example.com"].is_protected is True
    assert users["two@example.com"].is_protected is False


# --- The escape hatch ---------------------------------------------------------------------------


async def test_cli_toggles_the_flag(db: AsyncSession, other_admin: User) -> None:
    """A shell is the correct authority here, exactly as it is for `login-link`."""
    from app.cli import cmd_protect, cmd_unprotect

    assert await cmd_protect(db, namespace(email="second@example.com")) == 0
    assert (await reload(db, other_admin)).is_protected is True

    assert await cmd_unprotect(db, namespace(email="second@example.com")) == 0
    assert (await reload(db, other_admin)).is_protected is False


async def test_cli_unprotect_reopens_the_admin_surface(
    db: AsyncSession, founder: User, other_admin: User
) -> None:
    """End to end: the hatch has to actually let the web UI back in, or it is decoration."""
    from app.cli import cmd_unprotect
    from app.main import app

    assert await cmd_unprotect(db, namespace(email="founder@example.com")) == 0

    try:
        async with _client(db, other_admin, admin=True) as http:
            response = await http.patch(
                f"/api/admin/users/{founder.id}", json={"displayName": "Ahora Sí"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert (await reload(db, founder)).display_name == "Ahora Sí"


async def test_cli_is_idempotent_and_reports_a_missing_account(
    db: AsyncSession, founder: User
) -> None:
    from app.cli import cmd_protect, cmd_unprotect

    assert await cmd_protect(db, namespace(email="founder@example.com")) == 0  # already set
    assert (await reload(db, founder)).is_protected is True

    assert await cmd_unprotect(db, namespace(email="nobody@example.com")) == 1
    assert await cmd_protect(db, namespace(email="nobody@example.com")) == 1


async def test_cli_refuses_to_strand_a_viewer(db: AsyncSession, viewer: User) -> None:
    """Protecting a viewer would make them unpromotable from the web UI, so it is refused
    rather than quietly creating the trap."""
    from app.cli import cmd_protect

    assert await cmd_protect(db, namespace(email="viewer@example.com")) == 1
    assert (await reload(db, viewer)).is_protected is False


async def test_cli_commands_are_wired_into_the_parser() -> None:
    from app.cli import COMMANDS, build_parser, cmd_protect, cmd_unprotect

    for name, expected in (("protect", cmd_protect), ("unprotect", cmd_unprotect)):
        args = build_parser().parse_args([name, "--email", "x@example.com"])
        assert args.command == name
        assert args.email == "x@example.com"
        assert COMMANDS[name] is expected


# --- The migration, against a database that already has a household -----------------------------


def _seed_pre_migration_users(path: Path) -> None:
    """Rows as `0003` left them: no `is_protected` column anywhere.

    Written with the stdlib driver on purpose — the ORM would insist on the new column and
    prove nothing about a database created before it existed.
    """
    rows = [
        # (id, email, display_name, role, is_active, created_at) — out of order on purpose,
        # so the backfill is shown to sort rather than to take whatever comes first.
        ("u3", "second@example.com", "Segundo", "admin", 1, "2024-06-01 00:00:00"),
        ("u1", "founder@example.com", "Fundadora", "admin", 1, "2024-01-01 00:00:00"),
        ("u4", "viewer@example.com", "Espectadora", "viewer", 1, "2024-03-01 00:00:00"),
        # Older than the founder, but disabled: not a recovery path, must not be picked.
        ("u0", "retired@example.com", "Retirada", "admin", 0, "2023-01-01 00:00:00"),
    ]
    connection = sqlite3.connect(path)
    try:
        connection.executemany(
            "INSERT INTO users (id, email, display_name, role, is_active, locale, theme, "
            "autoplay_next, avatar_color, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'es', 'auto', 1, '#6c8cff', ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _protection(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            email: value
            for email, value in connection.execute("SELECT email, is_protected FROM users")
        }
    finally:
        connection.close()


async def _upgrade(revision: str) -> None:
    """Alembic is synchronous and its env.py calls `asyncio.run`, so it needs a thread."""
    from alembic import command
    from app.lifespan import alembic_config

    await asyncio.to_thread(command.upgrade, alembic_config(), revision)


async def test_migration_adds_the_flag_to_an_existing_household(tmp_env: Path) -> None:
    """The column has to land on a live database without disturbing what is already there,
    and hand the existing household the same protection a fresh install gets."""
    from app.db import dispose_engine

    database = tmp_env / "test.db"
    await dispose_engine()  # drop any engine a previous test bound to another URL
    try:
        await _upgrade(PREVIOUS_REVISION)
        _seed_pre_migration_users(database)

        await _upgrade("head")

        # Nothing lost, nothing rewritten: same four accounts, same roles.
        assert _protection(database) == {
            "founder@example.com": 1,  # oldest *active* admin
            "second@example.com": 0,
            "viewer@example.com": 0,
            "retired@example.com": 0,  # older, but disabled
        }
    finally:
        await dispose_engine()


async def test_migrated_database_round_trips_through_the_orm(tmp_env: Path) -> None:
    """The migrated DDL and the mapped column have to be the same column — a mismatch here
    would only surface as a 500 in production."""
    from app.db import dispose_engine, get_sessionmaker

    database = tmp_env / "test.db"
    await dispose_engine()
    try:
        await _upgrade(PREVIOUS_REVISION)
        _seed_pre_migration_users(database)
        await _upgrade("head")

        async with get_sessionmaker()() as session:
            founder = await session.get(User, "u1")
            second = await session.get(User, "u3")
            assert founder is not None and second is not None
            assert founder.is_protected is True
            assert second.is_protected is False

            # And the flag set through the ORM survives a re-run of the migrations, which is
            # what every container restart does before the app serves a single request.
            second.is_protected = True
            await session.commit()

        await _upgrade("head")
        assert _protection(database)["second@example.com"] == 1
    finally:
        await dispose_engine()
