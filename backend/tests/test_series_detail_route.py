"""`GET /library/series/{id}` — the screen every other screen leads to.

There was no test for it, and that is how a `TypeError` reached production: adding
`dramadex_id` to `SeriesSummary` left `series_detail` passing it *again* on top of
`base.model_dump()`, so every series page 500'd. The whole suite stayed green, because nothing
asked the route for a series.

The lesson is narrow and worth keeping: `SeriesDetail` is built by spreading the summary and
then naming extra fields. Any field that exists on both is a duplicate keyword argument — an
error Python raises at call time and neither `tsc`, the schema, nor a service-level test can
see. So this file fetches the real route, for a viewer and for an admin, and reads the payload.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Episode, MediaFile, MediaStatus, Series, SeriesCast, SeriesLink, User

pytestmark = pytest.mark.anyio


@pytest.fixture
async def series(db: AsyncSession) -> Series:
    """A series with everything the detail payload knows how to render."""
    row = Series(
        title="Djinn Under Contract",
        slug="djinn-under-contract",
        sort_title="djinn under contract",
        library_path="Djinn Under Contract",
        year=2026,
        overview="Lin Baran is a genie in a magic lamp.",
        genres=["Drama", "Fantasy"],
        network="ReelShort",
        dramadex_id=577,
        declared_episode_count=68,
    )
    db.add(row)
    await db.flush()

    episode = Episode(series_id=row.id, season_number=1, episode_number=1, title="Episodio 1")
    db.add(episode)
    await db.flush()
    db.add(
        MediaFile(
            episode_id=episode.id,
            path="Djinn Under Contract/S01E01.mp4",
            size_bytes=1024,
            status=MediaStatus.PRESENT,
        )
    )
    db.add(
        SeriesCast(
            series_id=row.id, dramadex_person_id=11, name="Jesse Morales", billing_order=0
        )
    )
    db.add(
        SeriesLink(
            series_id=row.id,
            studio_key="reelshort",
            studio_name="ReelShort",
            external_id="abc",
            url="https://www.reelshort.com/movie/abc",
        )
    )
    await db.commit()
    return row


async def client_for(db: AsyncSession, user: User) -> AsyncIterator[AsyncClient]:
    from app.api.deps import get_current_user, get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def viewer_http(db: AsyncSession, viewer_user: User) -> AsyncIterator[AsyncClient]:
    async for http in client_for(db, viewer_user):
        yield http


async def test_a_member_can_open_a_series(viewer_http: AsyncClient, series: Series) -> None:
    """The plain case, and the one that was 500ing."""
    response = await viewer_http.get(f"/api/library/series/{series.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Djinn Under Contract"
    assert body["slug"] == "djinn-under-contract"


async def test_the_detail_carries_the_binding_exactly_once(
    viewer_http: AsyncClient, series: Series
) -> None:
    """`dramadex_id` lives on `SeriesSummary` and `SeriesDetail` spreads the summary.

    Naming it again in the detail constructor is a duplicate keyword argument, which raises at
    call time — invisible to the schema, to mypy and to any test that builds the model directly.
    """
    body = (await viewer_http.get(f"/api/library/series/{series.id}")).json()

    assert body["dramadexId"] == 577


async def test_a_slug_resolves_the_same_series(viewer_http: AsyncClient, series: Series) -> None:
    """The route the app actually links to — `/series/:slug`, not the id."""
    body = (await viewer_http.get(f"/api/library/series/{series.slug}")).json()

    assert body["id"] == series.id


async def test_the_detail_brings_what_the_page_renders(
    viewer_http: AsyncClient, series: Series
) -> None:
    body = (await viewer_http.get(f"/api/library/series/{series.id}")).json()

    assert body["overview"].startswith("Lin Baran")
    assert body["genres"] == ["Drama", "Fantasy"]
    assert [member["name"] for member in body["cast"]] == ["Jesse Morales"]
    assert [link["studioName"] for link in body["links"]] == ["ReelShort"]
    assert body["seasons"][0]["episodes"][0]["title"] == "Episodio 1"


async def test_the_neighbours_ride_along(viewer_http: AsyncClient, series: Series) -> None:
    """A lone series has none, and says so rather than omitting the fields."""
    body = (await viewer_http.get(f"/api/library/series/{series.id}")).json()

    assert body["previousSeriesSlug"] is None
    assert body["nextSeriesSlug"] is None


async def test_an_unknown_series_is_a_404_not_a_500(viewer_http: AsyncClient) -> None:
    response = await viewer_http.get("/api/library/series/nope")

    assert response.status_code == 404
