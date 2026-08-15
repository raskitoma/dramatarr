"""Wire shapes for `GET /api/library/search`.

A hit wraps the same `SeriesSummary` / `EpisodeSummary` the rest of `/api/library` returns —
the search page renders the identical poster card as the browse grid, so handing it a
different series shape would mean two renderers for one thing.

Each hit also carries *why* it matched — the stored variant that won and the field it came
from — so the UI can show "matched 迷情" under an English title instead of leaving the user
guessing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from app.schemas.common import APIModel
from app.schemas.library import EpisodeSummary, SeriesSummary


class MatchField(StrEnum):
    """Which stored text the query hit. `TITLE` is the canonical display title, `ALTERNATE`
    any other registered title, `ROMANIZATION` a generated transliteration."""

    TITLE = "title"
    ALTERNATE = "alternate"
    ROMANIZATION = "romanization"
    OVERVIEW = "overview"
    EPISODE = "episode"


class EpisodeHit(APIModel):
    episode: EpisodeSummary
    series_id: str
    series_slug: str
    series_title: str
    score: float
    matched_title: str
    matched_field: MatchField = MatchField.EPISODE


class SeriesHit(APIModel):
    series: SeriesSummary
    score: float
    #: The stored variant that matched, in its display form ("迷情", "Infatuation", "mi qing").
    matched_title: str
    matched_language: str = ""
    matched_field: MatchField = MatchField.TITLE
    matched_title_kind: str = ""
    episodes: list[EpisodeHit] = Field(default_factory=list)


class SearchResults(APIModel):
    query: str
    normalized_query: str = ""
    total: int = 0
    took_ms: float = 0.0
    series: list[SeriesHit] = Field(default_factory=list)
    episodes: list[EpisodeHit] = Field(default_factory=list)


__all__ = ["EpisodeHit", "MatchField", "SearchResults", "SeriesHit"]
