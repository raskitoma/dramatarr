"""How long a session lasts, and what it takes to lose one.

The owner's ask: "que no caduque… por razones de seguridad se podría poner un límite de 365
días." So `session_ttl_days` became what its name always claimed — how long a session survives
*unused* — and `session_max_days` is the line nothing crosses.

Before this the window was counted from signing in and never moved, so somebody using the app
every day was signed out on a schedule they could not see, and somebody offline for three weeks
came back to a login form guarding a library already on their device.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.api.deps import _refresh_expiry
from app.config import get_settings
from app.models import AuthSession
from app.utils.time import utcnow

pytestmark = pytest.mark.anyio


def a_session(*, created_days_ago: float, expires_in_days: float) -> AuthSession:
    now = utcnow()
    return AuthSession(
        user_id="u1",
        token_hash="x",
        created_at=now - timedelta(days=created_days_ago),
        expires_at=now + timedelta(days=expires_in_days),
        last_seen_at=now,
    )


def days_from_now(value) -> float:
    return (value - utcnow()).total_seconds() / 86400


def test_using_it_pushes_the_expiry_forward() -> None:
    auth = a_session(created_days_ago=20, expires_in_days=10)

    _refresh_expiry(auth, utcnow())

    assert days_from_now(auth.expires_at) == pytest.approx(get_settings().session_ttl_days, abs=0.01)


def test_it_never_rolls_past_the_ceiling() -> None:
    """The security half. A session in daily use for a year still ends."""
    settings = get_settings()
    auth = a_session(created_days_ago=settings.session_max_days - 3, expires_in_days=1)

    _refresh_expiry(auth, utcnow())

    assert days_from_now(auth.expires_at) == pytest.approx(3, abs=0.01)


def test_a_session_at_the_ceiling_is_not_extended() -> None:
    settings = get_settings()
    auth = a_session(created_days_ago=settings.session_max_days + 5, expires_in_days=0.5)
    before = auth.expires_at

    _refresh_expiry(auth, utcnow())

    # The ceiling is behind us, so the target is in the past and must not shorten what is left.
    assert auth.expires_at == before


def test_it_only_ever_extends() -> None:
    """A clock that jumped, or a longer expiry already granted, must not be clawed back from
    somebody in the middle of using the app."""
    auth = a_session(created_days_ago=0, expires_in_days=400)
    before = auth.expires_at

    _refresh_expiry(auth, utcnow())

    assert auth.expires_at == before


def test_three_weeks_offline_does_not_cost_the_session() -> None:
    """The case that prompted this: the NAS is off, the member is travelling, and nothing is
    reaching the server to refresh anything."""
    settings = get_settings()
    # A three-week absence with nothing reaching the server, plus room either side. Thirty left
    # nine days of margin, which is a number to be nervous about rather than to rely on.
    assert settings.session_ttl_days >= 60, (
        "the idle window has to outlast a trip by a comfortable margin, or a downloaded library "
        "ends up guarded by a login form that cannot submit"
    )
    assert settings.session_ttl_days <= settings.session_max_days


def test_the_ceiling_is_the_number_the_owner_asked_for() -> None:
    assert get_settings().session_max_days == 365
