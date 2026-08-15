"""Watch-state rules from the architecture contract.

These encode product behaviour, not implementation detail: getting completion or rewatch
wrong is immediately visible to every household member.
"""

from __future__ import annotations

import pytest

from app.services.watch import (
    clear_progress,
    get_progress,
    mark_completed,
    record_progress,
    resume_position,
)

pytestmark = pytest.mark.anyio


async def resume_for(session, user_id: str, episode_id: str) -> float | None:
    """`resume_position` is a pure function over a loaded row; this is the DB-backed form
    the UI effectively sees."""
    return resume_position(await get_progress(session, user_id, episode_id))


async def test_progress_below_threshold_is_not_completed(db, viewer_user, sample_series) -> None:
    _, episodes = sample_series
    progress = await record_progress(db, viewer_user.id, episodes[0].id, 300.0, 1800.0)

    assert progress.percent == pytest.approx(16.67, abs=0.01)
    assert progress.completed is False
    assert progress.play_count == 0


async def test_completed_at_ninety_percent(db, viewer_user, sample_series) -> None:
    _, episodes = sample_series
    progress = await record_progress(db, viewer_user.id, episodes[0].id, 1620.0, 1800.0)

    assert progress.completed is True
    assert progress.play_count == 1


async def test_completed_when_under_two_minutes_remain(db, viewer_user, sample_series) -> None:
    """An episode can finish under 90% — the credits are not worth sitting through.

    600s duration keeps 100s remaining below the 120s tail while percent is only ~83%.
    """
    _, episodes = sample_series
    progress = await record_progress(db, viewer_user.id, episodes[0].id, 500.0, 600.0)

    assert progress.percent < 90
    assert progress.completed is True


async def test_short_clip_is_not_complete_at_position_zero(db, viewer_user, sample_series) -> None:
    """The tail rule is guarded by a minimum duration; without that, any clip shorter than
    the tail would count as watched the instant it opened."""
    _, episodes = sample_series
    progress = await record_progress(db, viewer_user.id, episodes[0].id, 1.0, 60.0)

    assert progress.completed is False


async def test_play_count_increments_once_per_viewing(db, viewer_user, sample_series) -> None:
    """Progress pings arrive every 10s; only the transition to completed may count."""
    _, episodes = sample_series
    await record_progress(db, viewer_user.id, episodes[0].id, 1620.0, 1800.0)
    await record_progress(db, viewer_user.id, episodes[0].id, 1700.0, 1800.0)
    progress = await record_progress(db, viewer_user.id, episodes[0].id, 1790.0, 1800.0)

    assert progress.play_count == 1


async def test_rewatch_resets_completion_but_keeps_count(db, viewer_user, sample_series) -> None:
    _, episodes = sample_series
    await record_progress(db, viewer_user.id, episodes[0].id, 1750.0, 1800.0)

    # Starting the episode again from the top is a rewatch, not a correction.
    progress = await record_progress(db, viewer_user.id, episodes[0].id, 5.0, 1800.0)

    assert progress.completed is False
    assert progress.position_seconds == pytest.approx(5.0)
    assert progress.play_count == 1


async def test_resume_suppressed_in_first_fifteen_seconds(db, viewer_user, sample_series) -> None:
    _, episodes = sample_series
    await record_progress(db, viewer_user.id, episodes[0].id, 8.0, 1800.0)

    assert await resume_for(db, viewer_user.id, episodes[0].id) is None


async def test_resume_offered_mid_episode(db, viewer_user, sample_series) -> None:
    _, episodes = sample_series
    await record_progress(db, viewer_user.id, episodes[0].id, 640.0, 1800.0)

    assert await resume_for(db, viewer_user.id, episodes[0].id) == pytest.approx(640.0)


async def test_resume_suppressed_when_completed(db, viewer_user, sample_series) -> None:
    _, episodes = sample_series
    await record_progress(db, viewer_user.id, episodes[0].id, 1790.0, 1800.0)

    assert await resume_for(db, viewer_user.id, episodes[0].id) is None


async def test_progress_is_isolated_between_users(db, viewer_user, admin_user, sample_series) -> None:
    """The whole point of the household model: two people never share a position."""
    _, episodes = sample_series
    await record_progress(db, viewer_user.id, episodes[0].id, 600.0, 1800.0)
    await record_progress(db, admin_user.id, episodes[0].id, 1200.0, 1800.0)

    assert await resume_for(db, viewer_user.id, episodes[0].id) == pytest.approx(600.0)
    assert await resume_for(db, admin_user.id, episodes[0].id) == pytest.approx(1200.0)


async def test_mark_completed_and_clear(db, viewer_user, sample_series) -> None:
    _, episodes = sample_series
    progress = await mark_completed(db, viewer_user.id, episodes[0].id)
    assert progress.completed is True

    await clear_progress(db, viewer_user.id, episodes[0].id)
    assert await resume_for(db, viewer_user.id, episodes[0].id) is None
