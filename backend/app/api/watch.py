"""Watch-state routes — `/api/watch`.

Thin by design: every rule lives in `app.services.watch`, these endpoints only translate.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.schemas.common import Ok
from app.schemas.watch import (
    ProgressIn,
    SeriesWatchedIn,
    SeriesWatchStateOut,
    WatchStateOut,
)
from app.services import library as library_service
from app.services import watch as watch_service

router = APIRouter(prefix="/watch", tags=["watch"])


@router.post("/{episode_id}/progress", response_model=WatchStateOut)
async def post_progress(
    session: SessionDep, user: CurrentUser, episode_id: str, body: ProgressIn
) -> WatchStateOut:
    progress = await watch_service.record_progress(
        session, user.id, episode_id, body.position_seconds, body.duration_seconds
    )
    return WatchStateOut(episode_id=episode_id, progress=library_service.progress_out(progress))


@router.post("/{episode_id}/complete", response_model=WatchStateOut)
async def post_complete(session: SessionDep, user: CurrentUser, episode_id: str) -> WatchStateOut:
    progress = await watch_service.mark_completed(session, user.id, episode_id)
    return WatchStateOut(episode_id=episode_id, progress=library_service.progress_out(progress))


@router.delete("/{episode_id}/progress", response_model=Ok)
async def delete_progress(session: SessionDep, user: CurrentUser, episode_id: str) -> Ok:
    await library_service.get_episode_or_404(session, episode_id, viewer=user)
    await watch_service.clear_progress(session, user.id, episode_id)
    return Ok()


@router.post("/series/{series_id}/watched", response_model=SeriesWatchStateOut)
async def post_series_watched(
    session: SessionDep, user: CurrentUser, series_id: str, body: SeriesWatchedIn
) -> SeriesWatchStateOut:
    series = await library_service.get_series_or_404(session, series_id, viewer=user)
    await watch_service.set_series_watched(session, user.id, series.id, body.watched)
    progress = await library_service.series_progress(session, series, user.id)
    return SeriesWatchStateOut(series_id=series.id, progress=progress)


__all__ = ["router"]
