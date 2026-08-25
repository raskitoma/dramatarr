"""Search by the actor, and search narrowed by the facets.

The two go together because they are the same screen: a member who remembers a face and not a
title needs both the name to reach the show and the year to cut the twelve shows that name is
in down to the one they mean.
"""

from __future__ import annotations

import pytest

from app.services.search.normalize import normalize

pytestmark = pytest.mark.anyio


@pytest.fixture
async def cast_library(db):
    """Three series: two share an actor, one shares only a year."""
    from app.models import Episode, MediaFile, Series, SeriesCast
    from app.services.search.index import ensure_index, reindex_series

    await ensure_index(db)

    made = {}
    for slug, title, year, genres in [
        ("first-frost", "First Frost", 2024, ["Romance"]),
        ("last-thaw", "Last Thaw", 2026, ["Drama"]),
        ("other-show", "Something Else Entirely", 2024, ["Comedy"]),
    ]:
        series = Series(
            title=title,
            slug=slug,
            sort_title=normalize(title),
            library_path=f"{title} ({year})",
            year=year,
            genres=genres,
        )
        db.add(series)
        await db.flush()
        made[slug] = series

    for slug, name, person_id in [
        ("first-frost", "Addison Bowman", 273),
        ("first-frost", "Cameron Porras", 41),
        ("last-thaw", "Addison Bowman", 273),
        ("other-show", "Avery Lynch", 88),
    ]:
        db.add(
            SeriesCast(
                series_id=made[slug].id,
                dramadex_person_id=person_id,
                name=name,
                billing_order=1,
            )
        )

    # One playable file, on `first-frost` only, so `availableOnly` has something to cut.
    episode = Episode(
        series_id=made["first-frost"].id, season_number=1, episode_number=1, title="Uno"
    )
    db.add(episode)
    await db.flush()
    db.add(
        MediaFile(
            episode_id=episode.id,
            path="First Frost (2024)/Season 01/First Frost - S01E01.mp4",
            size_bytes=1024,
            container="mp4",
        )
    )

    await db.commit()
    for series in made.values():
        await reindex_series(db, series.id)
    await db.commit()
    return made


# --- The name reaches the show ----------------------------------------------------------------


async def test_a_credited_name_finds_every_series_it_is_in(db, cast_library) -> None:
    from app.services.search.service import search_library

    results = await search_library(db, "Addison Bowman", sees_restricted=True)
    found = {hit.series.slug for hit in results.series}
    assert found == {"first-frost", "last-thaw"}


async def test_a_partial_name_finds_the_actor(db, cast_library) -> None:
    """Nobody types a full name into a search box."""
    from app.services.search.service import search_library

    results = await search_library(db, "addison", sees_restricted=True)
    assert "first-frost" in {hit.series.slug for hit in results.series}


async def test_the_hit_says_the_name_it_matched(db, cast_library) -> None:
    """Otherwise a show whose title looks nothing like the query reads as a bug."""
    from app.schemas.search import MatchField
    from app.services.search.service import search_library

    results = await search_library(db, "Addison Bowman", sees_restricted=True)
    hit = next(h for h in results.series if h.series.slug == "first-frost")
    assert hit.matched_field == MatchField.CAST
    assert hit.matched_title == "Addison Bowman"


async def test_a_title_still_beats_a_name(db, cast_library) -> None:
    """`CAST_WEIGHT` exists to break exactly this tie."""
    from app.schemas.search import MatchField
    from app.services.search.service import search_library

    results = await search_library(db, "First Frost", sees_restricted=True)
    hit = next(h for h in results.series if h.series.slug == "first-frost")
    assert hit.matched_field == MatchField.TITLE


async def test_browse_filters_by_cast_too(db, cast_library, viewer_user) -> None:
    """Explorar's own box searches the same library and must not disagree with Buscar."""
    from app.services import library as library_service

    page = await library_service.list_series(db, user_id=viewer_user.id, q="Cameron Porras", sees_restricted=True)
    assert [row.slug for row in page.items] == ["first-frost"]


# --- The facets narrow it ---------------------------------------------------------------------


async def test_year_narrows_a_cast_search(db, cast_library) -> None:
    from app.services.search.service import search_library

    results = await search_library(db, "Addison Bowman", year=2026, sees_restricted=True)
    assert [hit.series.slug for hit in results.series] == ["last-thaw"]
    assert results.total == 1


async def test_genre_narrows_a_cast_search(db, cast_library) -> None:
    from app.services.search.service import search_library

    results = await search_library(db, "Addison Bowman", genre="Romance", sees_restricted=True)
    assert [hit.series.slug for hit in results.series] == ["first-frost"]


async def test_a_facet_that_matches_nothing_returns_nothing(db, cast_library) -> None:
    from app.services.search.service import search_library

    results = await search_library(db, "Addison Bowman", year=1999, sees_restricted=True)
    assert results.series == []
    assert results.total == 0


async def test_available_only_keeps_the_series_with_a_file(db, cast_library, viewer_user) -> None:
    from app.services.search.service import search_library

    results = await search_library(
        db, "Addison Bowman", user_id=viewer_user.id, available_only=True
    , sees_restricted=True)
    assert [hit.series.slug for hit in results.series] == ["first-frost"]


async def test_the_facets_are_the_ones_explorar_uses(db, cast_library, viewer_user) -> None:
    """One helper, so a genre cannot mean two things on two screens."""
    from app.services import library as library_service
    from app.services.search.service import search_library

    browsed = await library_service.list_series(db, user_id=viewer_user.id, year=2024, sees_restricted=True)
    searched = await search_library(db, "e", year=2024, sees_restricted=True)
    assert {row.slug for row in browsed.items} >= {hit.series.slug for hit in searched.series}


# --- The route -------------------------------------------------------------------------------


async def test_the_search_route_takes_camel_case_filters(db, cast_library, viewer_user) -> None:
    """Query parameters are not converted for us the way request bodies are."""
    from httpx import ASGITransport, AsyncClient

    from app.api.deps import get_current_user
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: viewer_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/library/search", params={"q": "Addison Bowman", "availableOnly": "true"}
        )
    assert response.status_code == 200
    assert [hit["series"]["slug"] for hit in response.json()["series"]] == ["first-frost"]


async def test_the_years_facet_lists_what_the_library_holds(db, cast_library) -> None:
    from app.services import library as library_service

    facets = await library_service.year_facets(db, sees_restricted=True)
    assert [(f.year, f.count) for f in facets] == [(2026, 1), (2024, 2)]


# --- What a short row may claim ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "candidate", "ceiling"),
    [
        # The measured case: "n" is the romanized acronym of 你, and it scored 90 against a
        # seven-letter query, outranking the actor by 0.7 points.
        ("cameron", "n", 20.0),
        ("addison", "ni", 40.0),
    ],
)
def test_a_much_shorter_row_cannot_claim_a_long_query(
    query: str, candidate: str, ceiling: float
) -> None:
    from app.services.search.service import _score

    assert _score(query, candidate) < ceiling


@pytest.mark.parametrize(("query", "candidate"), [("mq", "mq"), ("infat", "infatuation")])
def test_the_cap_leaves_the_short_query_path_alone(query: str, candidate: str) -> None:
    """Acronyms exist so that two characters can find a Chinese title; that must still work."""
    from app.services.search.service import MIN_SCORE, _score

    assert _score(query, candidate) >= MIN_SCORE


async def test_the_actor_outranks_a_stray_acronym(db, cast_library) -> None:
    """The whole point, end to end: the hit is explained by the name that was typed."""
    from app.models import SeriesTitle, TitleKind, TitleSource
    from app.schemas.search import MatchField
    from app.services.search.index import reindex_series
    from app.services.search.service import search_library

    series = cast_library["first-frost"]
    db.add(
        SeriesTitle(
            series_id=series.id,
            language="",
            title="n",
            kind=TitleKind.ROMANIZATION,
            source=TitleSource.MANUAL,
            normalized="n",
        )
    )
    await db.commit()
    await reindex_series(db, series.id)
    await db.commit()

    # A one-word query, which is what a member types. The uncapped scores were 90 for the
    # stray letter against 89.3 for the actor.
    results = await search_library(db, "addison", sees_restricted=True)
    hit = next(h for h in results.series if h.series.slug == "first-frost")
    assert hit.matched_field == MatchField.CAST
    assert hit.matched_title == "Addison Bowman"
