"""IMDb metadata, by way of OMDb.

This exists because of a measured dead end. The household's library is vertical short-form
drama; **IMDb catalogues it and TMDB does not**. The discovery search finds the IMDb page and
reads `tt43685491` out of its URL, but the only bridge to a structured record was TMDB's
`/find`, and for these titles that bridge returns nothing — measured on all four of their
series, every one a miss.

OMDb is a public API over IMDb's data, keyed and rate-limited, built to be consumed exactly
this way. It answers the question TMDB cannot for this library: given an IMDb id, what is the
title, the year, the plot and the poster.

Three properties are deliberate:

* **Second, never first.** TMDB is asked first wherever a TMDB id exists, because it carries
  more (episodes, translations, alternative titles, artwork galleries) and is already wired
  into the refresh job. OMDb is the fallback for the id TMDB has never heard of.
* **Optional.** No key means the feature is simply absent — `try_client` returns None and the
  caller degrades to what it had before. A household that never sets one loses nothing it had.
* **The poster is a URL, not bytes.** Storing it goes through the same guarded artwork path
  every other remote image does; nothing here downloads.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

API_BASE = "https://www.omdbapi.com/"

#: Setting key. Secret, so it is redacted on read like every other credential.
API_KEY_SETTING = "omdb.api_key"

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()


async def _shared_client() -> httpx.AsyncClient:
    global _client
    async with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
        return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass(slots=True, frozen=True)
class OmdbTitle:
    """One record, reduced to the fields the editor actually fills.

    Everything is optional because OMDb returns the literal string `"N/A"` for anything it
    does not hold, and a title with `"N/A"` written into its year is worse than one with no
    year at all.
    """

    imdb_id: str
    title: str = ""
    year: int | None = None
    plot: str | None = None
    genres: tuple[str, ...] = ()
    poster_url: str | None = None
    #: `series` | `movie` | `episode`, as OMDb reports it.
    kind: str = ""


def _text(value: Any) -> str | None:
    """OMDb's absent value is the string `N/A`, not null. Treat it as absent."""
    text = str(value or "").strip()
    return None if not text or text.upper() == "N/A" else text


def _year(value: Any) -> int | None:
    """`2026`, and a run like `2024-2025` (OMDb uses an en dash). The first four digits win."""
    text = _text(value) or ""
    head = text[:4]
    return int(head) if head.isdigit() else None


def _parse(payload: dict[str, Any]) -> OmdbTitle | None:
    if str(payload.get("Response", "")).lower() != "true":
        return None
    imdb_id = _text(payload.get("imdbID"))
    if not imdb_id:
        return None
    genres = _text(payload.get("Genre")) or ""
    return OmdbTitle(
        imdb_id=imdb_id,
        title=_text(payload.get("Title")) or "",
        year=_year(payload.get("Year")),
        plot=_text(payload.get("Plot")),
        genres=tuple(part.strip() for part in genres.split(",") if part.strip()),
        poster_url=_text(payload.get("Poster")),
        kind=(_text(payload.get("Type")) or "").lower(),
    )


class OmdbClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def by_imdb_id(self, imdb_id: str) -> OmdbTitle | None:
        """One record, or None when OMDb does not have that id.

        `None` rather than an error for a miss: an id that OMDb has never seen is an ordinary
        outcome and belongs on the row that asked, not in a red box over the screen. A *key*
        problem is different and does raise — that is configuration, and the admin needs to be
        told which of the two happened.
        """
        ident = (imdb_id or "").strip().lower()
        if not ident:
            return None

        client = await _shared_client()
        try:
            response = await client.get(
                API_BASE, params={"i": ident, "apikey": self.api_key, "plot": "full"}
            )
        except httpx.HTTPError as exc:
            raise AppError("OMDB_UNAVAILABLE", f"Could not reach OMDb: {exc}"[:200], 503) from exc

        if response.status_code == 401:
            raise AppError("OMDB_UNAUTHORIZED", "OMDb rejected the API key", 502)
        if response.status_code >= 400:
            raise AppError(
                "OMDB_ERROR", f"OMDb returned HTTP {response.status_code}", 502
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AppError("OMDB_ERROR", "OMDb returned a non-JSON body", 502) from exc
        if not isinstance(payload, dict):
            raise AppError("OMDB_ERROR", "OMDb returned JSON that is not an object", 502)

        # OMDb reports a bad key in the *body*, with HTTP 200. Without this the admin would
        # see "no result" for every lookup and go hunting for the wrong problem.
        error = _text(payload.get("Error")) or ""
        if "api key" in error.lower():
            raise AppError("OMDB_UNAUTHORIZED", f"OMDb rejected the API key: {error}", 502)

        return _parse(payload)

    async def validate_key(self) -> bool:
        """A known id, to confirm the key works before an admin discovers it in the editor."""
        try:
            return await self.by_imdb_id("tt0903747") is not None
        except AppError as exc:
            if exc.code == "OMDB_UNAUTHORIZED":
                return False
            raise


async def try_client(session: AsyncSession) -> OmdbClient | None:
    """The configured client, or None when no key is set. Absence is not an error."""
    key = (await SettingsService(session).get_str(API_KEY_SETTING)).strip()
    return OmdbClient(key) if key else None


__all__ = [
    "API_BASE",
    "API_KEY_SETTING",
    "OmdbClient",
    "OmdbTitle",
    "close_client",
    "try_client",
]
