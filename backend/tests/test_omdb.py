"""IMDb metadata by way of OMDb — the fallback for what TMDB does not carry.

This path exists because of a measurement, not a hunch: all four of this household's series
have an IMDb id that TMDB's `/find` returns nothing for. The "use this metadata" button was
shipped resolving through TMDB only, and worked for none of them.

Two things are tested harder than the happy path:

**OMDb's absent value is the string `N/A`, not null.** Writing `"N/A"` into a year or a
synopsis is worse than leaving the field empty, and it is the failure a naive parser produces
on the very first title that lacks a plot.

**Filling must not destroy what a human wrote.** OMDb's plot is English; this household writes
in Spanish. Someone who wrote a synopsis and then came back for the year must keep the synopsis.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import Series
from app.services import library as library_service
from app.services import omdb

pytestmark = pytest.mark.anyio


def payload(**overrides: Any) -> dict[str, Any]:
    """A realistic OMDb answer, including the `N/A` spellings it actually sends."""
    return {
        "Response": "True",
        "imdbID": "tt43685491",
        "Title": "Definitely Not the Dragon God",
        "Year": "2026",
        "Plot": "A short drama about a reluctant god.",
        "Genre": "Drama, Fantasy",
        "Poster": "https://m.media-amazon.com/images/M/poster.jpg",
        "Type": "series",
        **overrides,
    }


@pytest.fixture
def serve_omdb(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))

        async def shared() -> httpx.AsyncClient:
            return client

        monkeypatch.setattr(omdb, "_shared_client", shared)
        return seen

    return install


def ok(body: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json=body)


# --- Parsing -------------------------------------------------------------------------------


async def test_a_full_record_parses(serve_omdb: Callable[..., list[httpx.Request]]) -> None:
    seen = serve_omdb(ok(payload()))

    found = await omdb.OmdbClient("k").by_imdb_id("tt43685491")

    assert found is not None
    assert found.title == "Definitely Not the Dragon God"
    assert found.year == 2026
    assert found.genres == ("Drama", "Fantasy")
    assert found.poster_url is not None
    # The id is sent lowercased and the key rides along; `plot=full` is what makes the
    # synopsis worth having rather than a one-line teaser.
    assert seen[0].url.params["i"] == "tt43685491"
    assert seen[0].url.params["plot"] == "full"


@pytest.mark.parametrize("absent", ["N/A", "n/a", "", "   "])
async def test_omdbs_absent_value_is_absent_not_a_string(
    absent: str, serve_omdb: Callable[..., list[httpx.Request]]
) -> None:
    """`"N/A"` written into a synopsis is worse than an empty synopsis."""
    serve_omdb(ok(payload(Plot=absent, Poster=absent, Year=absent, Genre=absent)))

    found = await omdb.OmdbClient("k").by_imdb_id("tt43685491")

    assert found is not None
    assert found.plot is None
    assert found.poster_url is None
    assert found.year is None
    assert found.genres == ()


async def test_a_year_range_keeps_the_first_year(
    serve_omdb: Callable[..., list[httpx.Request]]
) -> None:
    serve_omdb(ok(payload(Year="2024–2025")))
    found = await omdb.OmdbClient("k").by_imdb_id("tt1")
    assert found is not None and found.year == 2024


async def test_a_miss_is_none_rather_than_an_error(
    serve_omdb: Callable[..., list[httpx.Request]]
) -> None:
    """An id OMDb has never seen belongs on that row, not in a red box over the screen."""
    serve_omdb(ok({"Response": "False", "Error": "Incorrect IMDb ID."}))
    assert await omdb.OmdbClient("k").by_imdb_id("tt99999999") is None


async def test_a_bad_key_is_reported_as_a_key_problem(
    serve_omdb: Callable[..., list[httpx.Request]]
) -> None:
    """OMDb reports this in the body, with HTTP 200.

    Without the check it looks exactly like "no result", and the admin goes hunting for the
    wrong problem — a missing title rather than a missing key.
    """
    serve_omdb(ok({"Response": "False", "Error": "Invalid API key!"}))

    with pytest.raises(AppError) as caught:
        await omdb.OmdbClient("bad").by_imdb_id("tt43685491")
    assert caught.value.code == "OMDB_UNAUTHORIZED"


async def test_no_key_configured_is_no_client(db: AsyncSession) -> None:
    """Absence is not an error: the feature is simply not there."""
    assert await omdb.try_client(db) is None


# --- Applying to a series ------------------------------------------------------------------
#
# This is the path that carried a NameError until a test walked it: `OverviewSource` was used
# and never imported, and importing the module did not catch it because the failure lives in a
# function body. The same class of bug this project has hit before.


async def make_series(db: AsyncSession, **fields: Any) -> Series:
    series = Series(
        title=fields.pop("title", "Definitely Not the Dragon God"),
        slug="dragon-god",
        sort_title="definitely not the dragon god",
        library_path="Definitely Not the Dragon God",
        **fields,
    )
    db.add(series)
    await db.commit()
    return series


async def test_applying_fills_the_empty_fields(db: AsyncSession) -> None:
    series = await make_series(db)
    found = omdb.OmdbTitle(
        imdb_id="tt43685491",
        title="Definitely Not the Dragon God",
        year=2026,
        plot="A short drama about a reluctant god.",
        genres=("Drama", "Fantasy"),
        poster_url="https://m.media-amazon.com/images/M/poster.jpg",
    )

    filled = await library_service.apply_omdb_title(db, series, found)

    assert series.year == 2026
    assert series.overview == "A short drama about a reluctant god."
    # Normalized on the way in, like every other writer of this column.
    assert series.genres == ["Drama", "Fantasy"]
    assert series.imdb_id == "tt43685491"
    assert "year" in filled and "overview" in filled
    # A human curated this row, so the nightly refresh must leave it alone.
    assert series.metadata_locked is True
    # Provenance points at the page the text came from.
    assert series.overview_source_url == "https://www.imdb.com/title/tt43685491/"


async def test_applying_does_not_destroy_a_synopsis_somebody_wrote(
    db: AsyncSession,
) -> None:
    """The reason the default is blanks-only.

    OMDb's plot is English and this household writes in Spanish. Coming back for the year must
    not cost them the paragraph they wrote.
    """
    series = await make_series(db, overview="Una sinopsis que escribí yo.", year=None)

    await library_service.apply_omdb_title(
        db, series, omdb.OmdbTitle(imdb_id="tt1", plot="An English plot.", year=2026)
    )

    assert series.overview == "Una sinopsis que escribí yo."
    assert series.year == 2026


async def test_overwrite_replaces_when_asked(db: AsyncSession) -> None:
    series = await make_series(db, overview="Vieja.")

    await library_service.apply_omdb_title(
        db, series, omdb.OmdbTitle(imdb_id="tt1", plot="Nueva."), overwrite=True
    )

    assert series.overview == "Nueva."


async def test_the_id_is_recorded_even_when_nothing_else_changes(
    db: AsyncSession,
) -> None:
    """It is what the record was fetched by, so storing it makes the fill reproducible."""
    series = await make_series(db, overview="Ya estaba.", year=2020)

    filled = await library_service.apply_omdb_title(
        db, series, omdb.OmdbTitle(imdb_id="tt43685491", plot="x", year=1999)
    )

    assert series.imdb_id == "tt43685491"
    assert "imdb_id" in filled
    assert series.overview == "Ya estaba."
    assert series.year == 2020
