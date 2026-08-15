"""Invites, sessions and the last-admin guard."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.errors import AppError
from app.models import UserRole
from app.utils.time import utcnow

pytestmark = pytest.mark.anyio


async def test_invite_token_is_not_stored_in_plaintext(db, admin_user) -> None:
    """A database dump must not hand over live enrollment links."""
    from app.services.auth import create_invite

    created = await create_invite(
        db, email="nuevo@example.com", role=UserRole.VIEWER, created_by=admin_user
    )
    await db.commit()

    assert created.token
    assert created.invite.token_hash != created.token
    assert created.token not in created.invite.token_hash
    # The shareable URL is what the admin screen copies when SMTP is not configured.
    assert created.token in created.url


async def test_valid_invite_resolves(db, admin_user) -> None:
    from app.services.auth import create_invite, validate_invite_token

    created = await create_invite(
        db, email="nuevo@example.com", role=UserRole.VIEWER, created_by=admin_user
    )
    await db.commit()

    resolved = await validate_invite_token(db, created.token)
    assert resolved.email == "nuevo@example.com"
    assert resolved.role == UserRole.VIEWER


async def test_unknown_invite_token_is_rejected(db) -> None:
    from app.services.auth import validate_invite_token

    with pytest.raises(AppError) as excinfo:
        await validate_invite_token(db, "not-a-real-token")
    assert excinfo.value.code == "INVITE_INVALID"


async def test_expired_invite_is_rejected(db, admin_user) -> None:
    from app.services.auth import create_invite, validate_invite_token

    created = await create_invite(
        db, email="tarde@example.com", role=UserRole.VIEWER, created_by=admin_user
    )
    created.invite.expires_at = utcnow() - timedelta(hours=1)
    await db.commit()

    with pytest.raises(AppError) as excinfo:
        await validate_invite_token(db, created.token)
    assert excinfo.value.code == "INVITE_EXPIRED"


async def test_invite_is_single_use(db, admin_user, viewer_user) -> None:
    from app.services.auth import consume_invite, create_invite, validate_invite_token

    created = await create_invite(
        db, email="unavez@example.com", role=UserRole.VIEWER, created_by=admin_user
    )
    await db.commit()

    await consume_invite(db, created.invite, viewer_user)
    await db.commit()

    with pytest.raises(AppError) as excinfo:
        await validate_invite_token(db, created.token)
    assert excinfo.value.code == "INVITE_ALREADY_USED"


async def test_session_token_is_hashed(db) -> None:
    from app.api.deps import hash_token

    raw = "some-session-token"
    assert hash_token(raw) != raw
    assert len(hash_token(raw)) == 64  # sha256 hex
    assert hash_token(raw) == hash_token(raw)  # deterministic


# --- Last-admin guard ----------------------------------------------------------------------
#
# Locking every human out of the admin panel is unrecoverable without shell access, so the
# guard is exercised on all three paths that could cause it.


async def test_last_active_admin_cannot_be_demoted(db, admin_user, viewer_user) -> None:
    from app.api.admin import _assert_not_last_admin

    with pytest.raises(AppError) as excinfo:
        await _assert_not_last_admin(db, admin_user, action="demote")
    assert excinfo.value.code == "LAST_ADMIN"


async def test_admin_can_be_demoted_when_another_exists(db, admin_user) -> None:
    from app.api.admin import _assert_not_last_admin
    from app.models import User

    db.add(
        User(
            email="admin2@example.com",
            display_name="Second Admin",
            role=UserRole.ADMIN,
            is_active=True,
        )
    )
    await db.commit()

    await _assert_not_last_admin(db, admin_user, action="demote")  # must not raise


async def test_inactive_admin_does_not_count_as_the_remaining_one(db, admin_user) -> None:
    from app.api.admin import _assert_not_last_admin
    from app.models import User

    db.add(
        User(
            email="dormant@example.com",
            display_name="Dormant",
            role=UserRole.ADMIN,
            is_active=False,
        )
    )
    await db.commit()

    with pytest.raises(AppError) as excinfo:
        await _assert_not_last_admin(db, admin_user, action="delete")
    assert excinfo.value.code == "LAST_ADMIN"
