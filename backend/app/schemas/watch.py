"""Wire shapes for the watch-state API."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import APIModel
from app.schemas.library import SeriesProgress, WatchProgressOut


class ProgressIn(APIModel):
    """Sent every ~10s while playing, on pause, and via `sendBeacon` on unload."""

    position_seconds: float = Field(ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)


class SeriesWatchedIn(APIModel):
    watched: bool


class WatchStateOut(APIModel):
    episode_id: str
    progress: WatchProgressOut


class SeriesWatchStateOut(APIModel):
    series_id: str
    progress: SeriesProgress


__all__ = [
    "ProgressIn",
    "SeriesWatchStateOut",
    "SeriesWatchedIn",
    "WatchStateOut",
]
