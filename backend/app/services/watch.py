"""Per-user watch state.

The completion / rewatch / resume rules of the architecture contract live here and nowhere
else — the API layer only calls into this module, so the player, the series view and the
"mark watched" buttons can never drift apart.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import NotFound
from app.models import Episode, MediaStatus, PlayEvent, WatchProgress
from app.utils.time import as_aware, utcnow

#: An episode counts as watched at 90%…
COMPLETION_PERCENT = 90.0
#: …or when this little is left. Guarded by `MIN_TAIL_DURATION` below so that a clip shorter
#: than the tail window is not "complete" the instant it starts.
COMPLETION_TAIL_SECONDS = 120.0
MIN_TAIL_DURATION = 120.0
#: Below this the UI must not offer Resume — restarting is cheaper than a 3-second skip.
RESUME_MIN_SECONDS = 15.0
#: `play_events` exists only to order continue-watching; one row a minute is plenty.
PLAY_EVENT_THROTTLE_SECONDS = 60.0


def compute_percent(position_seconds: float, duration_seconds: float) -> float:
    if duration_seconds <= 0:
        return 0.0
    return max(0.0, min(100.0, position_seconds / duration_seconds * 100.0))


def is_complete(position_seconds: float, duration_seconds: float) -> bool:
    if duration_seconds <= 0:
        return False
    if compute_percent(position_seconds, duration_seconds) >= COMPLETION_PERCENT:
        return True
    remaining = duration_seconds - position_seconds
    return duration_seconds > MIN_TAIL_DURATION and remaining < COMPLETION_TAIL_SECONDS


def resume_position(progress: WatchProgress | None) -> float | None:
    """Where the player should offer to resume, or None when it must not offer at all."""
    if progress is None or progress.completed:
        return None
    if progress.position_seconds < RESUME_MIN_SECONDS:
        return None
    return progress.position_seconds


# --- Internals ------------------------------------------------------------------------------


async def _load_progress(
    session: AsyncSession, user_id: str, episode_id: str
) -> WatchProgress | None:
    stmt = select(WatchProgress).where(
        WatchProgress.user_id == user_id, WatchProgress.episode_id == episode_id
    )
    return (await session.execute(stmt)).scalars().first()


async def _get_episode(session: AsyncSession, episode_id: str) -> Episode:
    # `episode_duration` reads `media_files` from a sync context, so the relationship has to
    # be populated before it is touched. A plain `session.get` would hand back an
    # identity-map instance whose collection was never loaded — for example one built by the
    # caller earlier in the same session — and the lazy load would then raise MissingGreenlet.
    episode = await session.get(
        Episode,
        episode_id,
        options=[selectinload(Episode.media_files)],
        populate_existing=True,
    )
    if episode is None:
        raise NotFound("Episode", "EPISODE_NOT_FOUND")
    return episode


def _new_progress(user_id: str, episode_id: str, now: datetime) -> WatchProgress:
    """Column defaults only land at flush time, and every caller here reads the counters
    before flushing, so they are spelled out."""
    return WatchProgress(
        user_id=user_id,
        episode_id=episode_id,
        position_seconds=0.0,
        duration_seconds=0.0,
        percent=0.0,
        completed=False,
        play_count=0,
        first_played_at=now,
        last_played_at=now,
    )


def episode_duration(episode: Episode) -> float:
    """Best known runtime: the probed file wins over the declared metadata value."""
    for media in episode.media_files:
        if media.status == MediaStatus.PRESENT and media.duration_seconds:
            return float(media.duration_seconds)
    return float(episode.runtime_seconds or 0)


async def _append_play_event(
    session: AsyncSession,
    *,
    user_id: str,
    episode: Episode,
    position_seconds: float,
    now: datetime,
) -> None:
    last_at = (
        await session.execute(
            select(func.max(PlayEvent.at)).where(
                PlayEvent.user_id == user_id, PlayEvent.episode_id == episode.id
            )
        )
    ).scalar_one_or_none()
    previous = as_aware(last_at)
    if previous is not None and (now - previous).total_seconds() < PLAY_EVENT_THROTTLE_SECONDS:
        return
    session.add(
        PlayEvent(
            user_id=user_id,
            episode_id=episode.id,
            series_id=episode.series_id,
            position_seconds=position_seconds,
            at=now,
        )
    )


# --- Public API -----------------------------------------------------------------------------


async def record_progress(
    session: AsyncSession,
    user_id: str,
    episode_id: str,
    position_seconds: float,
    duration_seconds: float,
) -> WatchProgress:
    """Idempotent progress ping. Applies the completion and rewatch rules."""
    episode = await _get_episode(session, episode_id)
    progress = await _load_progress(session, user_id, episode_id)

    duration = float(duration_seconds or 0.0)
    if duration <= 0:
        # A beacon on unload often carries no duration; fall back to what we already know.
        duration = float(progress.duration_seconds) if progress else 0.0
    if duration <= 0:
        duration = episode_duration(episode)

    position = max(0.0, float(position_seconds))
    if duration > 0:
        position = min(position, duration)

    now = utcnow()
    complete_now = is_complete(position, duration)

    if progress is None:
        progress = _new_progress(user_id, episode_id, now)
        session.add(progress)
    elif progress.completed and not complete_now:
        # Playing a finished episode again is a rewatch: the state starts over, the count
        # of past viewings does not.
        progress.completed = False
        progress.position_seconds = 0.0
        progress.percent = 0.0

    was_completed = progress.completed
    progress.position_seconds = position
    progress.duration_seconds = duration
    progress.percent = compute_percent(position, duration)
    progress.last_played_at = now
    if complete_now and not was_completed:
        progress.completed = True
        progress.play_count += 1

    await session.flush()
    await _append_play_event(
        session, user_id=user_id, episode=episode, position_seconds=position, now=now
    )
    await session.flush()
    return progress


async def mark_completed(session: AsyncSession, user_id: str, episode_id: str) -> WatchProgress:
    """Force-mark watched, without having played it."""
    episode = await _get_episode(session, episode_id)
    progress = await _load_progress(session, user_id, episode_id)
    now = utcnow()

    if progress is None:
        progress = _new_progress(user_id, episode_id, now)
        session.add(progress)

    duration = float(progress.duration_seconds or 0.0) or episode_duration(episode)
    if not progress.completed:
        progress.play_count += 1
    progress.completed = True
    progress.duration_seconds = duration
    progress.position_seconds = duration
    progress.percent = 100.0
    progress.last_played_at = now
    await session.flush()
    return progress


async def clear_progress(session: AsyncSession, user_id: str, episode_id: str) -> None:
    """Erase this user's state for one episode, play trail included, so continue-watching
    does not keep resurrecting it."""
    await session.execute(
        delete(WatchProgress).where(
            WatchProgress.user_id == user_id, WatchProgress.episode_id == episode_id
        )
    )
    await session.execute(
        delete(PlayEvent).where(PlayEvent.user_id == user_id, PlayEvent.episode_id == episode_id)
    )
    await session.flush()


async def set_series_watched(
    session: AsyncSession, user_id: str, series_id: str, watched: bool
) -> None:
    """Mark every available episode of a series watched, or clear the whole series."""
    if not watched:
        episode_ids = select(Episode.id).where(Episode.series_id == series_id).scalar_subquery()
        await session.execute(
            delete(WatchProgress).where(
                WatchProgress.user_id == user_id, WatchProgress.episode_id.in_(episode_ids)
            )
        )
        await session.execute(
            delete(PlayEvent).where(PlayEvent.user_id == user_id, PlayEvent.series_id == series_id)
        )
        await session.flush()
        return

    episodes = (
        (
            await session.execute(
                select(Episode)
                .where(Episode.series_id == series_id)
                .order_by(Episode.season_number, Episode.episode_number)
            )
        )
        .scalars()
        .all()
    )
    existing = {
        row.episode_id: row
        for row in (
            await session.execute(
                select(WatchProgress).where(
                    WatchProgress.user_id == user_id,
                    WatchProgress.episode_id.in_([e.id for e in episodes]),
                )
            )
        )
        .scalars()
        .all()
    }

    now = utcnow()
    for episode in episodes:
        # Episodes with no file are left alone: they are "declared but absent", and marking
        # them would make watched exceed available.
        if not episode.is_available:
            continue
        progress = existing.get(episode.id)
        if progress is None:
            progress = _new_progress(user_id, episode.id, now)
            session.add(progress)
        duration = float(progress.duration_seconds or 0.0) or episode_duration(episode)
        if not progress.completed:
            progress.play_count += 1
        progress.completed = True
        progress.duration_seconds = duration
        progress.position_seconds = duration
        progress.percent = 100.0
        progress.last_played_at = now
    await session.flush()


async def get_progress(
    session: AsyncSession, user_id: str, episode_id: str
) -> WatchProgress | None:
    return await _load_progress(session, user_id, episode_id)


async def progress_for_episodes(
    session: AsyncSession, user_id: str, episode_ids: list[str]
) -> dict[str, WatchProgress]:
    """Bulk fetch, so callers never do a query per episode."""
    if not episode_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(WatchProgress).where(
                    and_(
                        WatchProgress.user_id == user_id,
                        WatchProgress.episode_id.in_(episode_ids),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.episode_id: row for row in rows}


__all__ = [
    "COMPLETION_PERCENT",
    "COMPLETION_TAIL_SECONDS",
    "RESUME_MIN_SECONDS",
    "clear_progress",
    "compute_percent",
    "episode_duration",
    "get_progress",
    "is_complete",
    "mark_completed",
    "progress_for_episodes",
    "record_progress",
    "resume_position",
    "set_series_watched",
]
