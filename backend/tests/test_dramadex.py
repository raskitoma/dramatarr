"""The indexer that replaced the guesswork.

DramaDex is the household's own index of DramaBox and ReelShort. It matters because every
metadata path before it was a workaround for its absence: a metasearch engine scored by
hostname, an IMDb fallback that resolved one series in four, and an admin pasting synopses by
hand. Measured against this library, a normalised DramaDex lookup resolves 11 of 17 series.

What is tested hard here is not the happy path — it is the three places where being slightly
wrong is worse than doing nothing:

**Matching must refuse rather than guess.** Searching a title DramaDex has never indexed still
returns plausible neighbours; "The Call Boy I Met in Paris" comes back with "Call to Arms,
Call to Love". Binding that writes a wrong synopsis and a wrong cast onto a series, which is
precisely how the TMDB era put the Netflix thriller *You* on a Chinese surgeon drama.

**Editorial ratings must never escape.** Both platforms publish marketing scores — DramaBox
floor-clamps to 7.1-10, ReelShort's whole catalogue fits in 0.4 points. Only IMDb's are real.

**Applying must not destroy what a person wrote,** the same rule the OMDb path follows.

The apply test walks the real function against a real session because that is the only thing
that catches a name used in a function body and never imported — this module needed three
such fixes, and importing it succeeded every time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import Episode, MediaFile, MediaStatus, Series, SeriesCast, SeriesLink, SeriesTitle
from app.models.library import OverviewSource
from app.services import dramadex
from app.services import library as library_service

pytestmark = pytest.mark.anyio


def title_payload(**overrides: Any) -> dict[str, Any]:
    """A realistic `/titles/{id}` body, including both flavours of rating."""
    return {
        "id": 4242,
        "primary_title": "You Fired A Genius Surgeon",
        "year": 2025,
        "episode_count": 51,
        "audience_rating": "All Ages",
        "poster_url": "/titles/4242/poster",
        "synopsis": "After Dr. Adrian Marksman is dismissed, he...",
        "completed": True,
        "aliases": ["El cirujano que despediste"],
        "studios": [
            {
                "key": "reelshort",
                "name": "ReelShort",
                "external_id": "687f2ab2dafc1bd65e0e34ca",
                "watch_url": "https://www.reelshort.com/movie/687f2ab2dafc1bd65e0e34ca",
            },
            # IMDb is a source in DramaDex but not a place to watch anything.
            {"key": "imdb", "name": "IMDb", "external_id": "tt1", "watch_url": ""},
        ],
        "tags": [
            {"namespace": "reelshort.genre", "value": "Drama"},
            {"namespace": "reelshort.sub-genre", "value": "Family Drama"},
            {"namespace": "reelshort.theme", "value": "Modern"},
            {"namespace": "dramabox.category", "value": "All"},
            {"namespace": "reelshort.mood", "value": "Romantic"},
            {"namespace": "reelshort.setting", "value": "Hospital"},
        ],
        "cast": [
            {"id": 11, "name": "Jesse Morales", "role": "actor", "billing_order": 0},
            {"id": 8, "name": "Cameron Somers", "role": "actor", "billing_order": 1},
        ],
        "ratings": [
            {"source": "reelshort", "value": 4.8, "votes": None, "scale": 10.0, "is_editorial": True},
            {"source": "imdb", "value": 7.3, "votes": 113, "scale": 10.0, "is_editorial": False},
        ],
        **overrides,
    }


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        class Fake(httpx.AsyncClient):
            def __init__(self, *a: Any, **kw: Any) -> None:
                kw["transport"] = httpx.MockTransport(wrapped)
                super().__init__(*a, **kw)

        monkeypatch.setattr(dramadex.httpx, "AsyncClient", Fake)
        return seen

    return install


def client() -> dramadex.DramadexClient:
    return dramadex.DramadexClient("http://dramadex.test:8099", "k")


# --- Normalisation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        # Every one of these is a real pairing from this household's library.
        ("Nero Made Men", "Nero: Made Men"),
        ("Tempest The Last Mecha", "Tempest：The Last Mecha"),  # fullwidth colon
        ("You Fired a Tech Genius", "You Fired A Tech Genius"),
        ("Definitely Not the Dragon God", "Definitely Not The Dragon God"),
        ("American Sniper The Last Round", "American Sniper: The Last Round"),
    ],
)
def test_titles_that_differ_only_in_punctuation_are_one_title(ours: str, theirs: str) -> None:
    """Exact equality matched 6 of 17 series here; this matches 11."""
    assert dramadex.normalize_title(ours) == dramadex.normalize_title(theirs)


def test_different_shows_stay_different() -> None:
    """The negative control that keeps the normaliser from becoming a fuzzy matcher.

    These titles come from a template grammar — six different shows share the frame — so
    anything loose enough to merge them would confidently bind the wrong sibling.
    """
    a = dramadex.normalize_title("Fated to My Forbidden Alpha")
    b = dramadex.normalize_title("Fated to My Swapped Alpha")
    assert a != b


# --- Matching --------------------------------------------------------------------------


async def test_a_near_miss_is_not_a_match(serve: Callable[..., list[httpx.Request]]) -> None:
    """The failure this whole design exists to prevent.

    DramaDex answers a query it cannot satisfy with its best neighbours. Taking the top row
    is how a Chinese surgeon drama ended up with the cast and artwork of Netflix's *You*.
    """
    serve(
        lambda _r: httpx.Response(
            200,
            json={
                "items": [
                    {"id": 1, "primary_title": "Call to Arms, Call to Love", "studios": []},
                    {"id": 2, "primary_title": "Call Me By Fire", "studios": []},
                ],
                "next_cursor": None,
            },
        )
    )

    assert await client().best_match("The Call Boy I Met in Paris") is None


async def test_a_normalised_match_is_accepted(
    serve: Callable[..., list[httpx.Request]]
) -> None:
    serve(
        lambda _r: httpx.Response(
            200,
            json={
                "items": [
                    {"id": 9, "primary_title": "Some Other Show", "studios": []},
                    {"id": 7, "primary_title": "Nero: Made Men", "studios": []},
                ],
                "next_cursor": None,
            },
        )
    )

    found = await client().best_match("Nero Made Men")

    assert found is not None and found.id == 7


async def test_the_year_breaks_a_tie(serve: Callable[..., list[httpx.Request]]) -> None:
    """Two shows can share a normalised title; the year is the only thing separating them."""
    serve(
        lambda _r: httpx.Response(
            200,
            json={
                "items": [
                    {"id": 1, "primary_title": "Revenge", "year": 2019, "studios": []},
                    {"id": 2, "primary_title": "Revenge", "year": 2025, "studios": []},
                ],
                "next_cursor": None,
            },
        )
    )

    found = await client().best_match("Revenge", year=2025)

    assert found is not None and found.id == 2


# --- Parsing ---------------------------------------------------------------------------


async def test_editorial_ratings_are_kept_out_of_reach(
    serve: Callable[..., list[httpx.Request]]
) -> None:
    """DramaBox floor-clamps to 7.1-10 and ReelShort's catalogue fits in 0.4 points. Those
    numbers are marketing copy, and `genuine_ratings` is the only accessor there is."""
    serve(lambda _r: httpx.Response(200, json=title_payload()))

    found = await client().title(4242)

    assert found is not None
    assert len(found.ratings) == 2
    assert [r.source for r in found.genuine_ratings] == ["imdb"]
    assert found.genuine_ratings[0].votes == 113


async def test_genres_take_the_genre_shaped_namespaces_only(
    serve: Callable[..., list[httpx.Request]]
) -> None:
    """Sixteen namespaces, 543 values. `reelshort.mood` alone holds 114 — dumping them all in
    buries the three that mean something under forty that do not."""
    serve(lambda _r: httpx.Response(200, json=title_payload()))

    found = await client().title(4242)

    assert found is not None
    assert found.genres() == ("Drama", "Family Drama", "Modern")
    # `All` is a real DramaBox category and says nothing; mood and setting stay on `tags`.
    assert "All" not in found.genres()
    assert ("reelshort.setting", "Hospital") in found.tags


async def test_imdb_is_not_somewhere_to_watch(
    serve: Callable[..., list[httpx.Request]]
) -> None:
    serve(lambda _r: httpx.Response(200, json=title_payload()))

    found = await client().title(4242)

    assert found is not None
    assert [s.key for s in found.studios if s.is_watchable] == ["reelshort"]
    assert found.studio_name == "ReelShort"


async def test_the_key_travels_in_a_header(serve: Callable[..., list[httpx.Request]]) -> None:
    """Never a query parameter: DramaDex's own auth refuses that form because it lands in
    logs and Referer headers, and dramatarr should not undo that decision from this side."""
    seen = serve(lambda _r: httpx.Response(200, json=title_payload()))

    await client().title(4242)

    assert seen[0].headers["X-API-Key"] == "k"
    assert "k" not in str(seen[0].url)


async def test_a_rejected_key_says_so(serve: Callable[..., list[httpx.Request]]) -> None:
    serve(lambda _r: httpx.Response(401, json={"detail": "invalid or missing API key"}))

    with pytest.raises(AppError) as caught:
        await client().title(4242)
    assert caught.value.code == "DRAMADEX_UNAUTHORIZED"


async def test_an_unreachable_indexer_is_not_a_crash(
    serve: Callable[..., list[httpx.Request]]
) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    serve(boom)

    with pytest.raises(AppError) as caught:
        await client().title(4242)
    assert caught.value.code == "DRAMADEX_UNREACHABLE"


async def test_no_configuration_is_not_an_error(db: AsyncSession) -> None:
    """A household that has not connected DramaDex yet is an ordinary state, not a failure."""
    assert await dramadex.try_client(db) is None


# --- Applying to a series ---------------------------------------------------------------


async def make_series(db: AsyncSession, *, files: int = 0, **fields: Any) -> Series:
    series = Series(
        title=fields.pop("title", "You Fired A Genius Surgeon"),
        slug="genius-surgeon",
        sort_title="you fired a genius surgeon",
        library_path="You Fired A Genius Surgeon",
        **fields,
    )
    db.add(series)
    await db.flush()
    for number in range(1, files + 1):
        episode = Episode(series_id=series.id, season_number=1, episode_number=number)
        db.add(episode)
        await db.flush()
        db.add(
            MediaFile(
                episode_id=episode.id,
                path=f"x/E{number}.mp4",
                size_bytes=1,
                status=MediaStatus.PRESENT,
            )
        )
    await db.commit()
    return series


def parsed() -> dramadex.DramadexTitle:
    found = dramadex._title(title_payload())
    assert found is not None
    return found


async def test_applying_fills_a_bare_series(db: AsyncSession) -> None:
    """The path that carried three NameErrors until a test walked it with a real session."""
    series = await make_series(db, files=51)

    filled = await library_service.apply_dramadex_title(db, series, parsed())

    assert series.overview == "After Dr. Adrian Marksman is dismissed, he..."
    assert series.year == 2025
    assert series.genres == ["Drama", "Family Drama", "Modern"]
    assert series.network == "ReelShort"
    assert series.dramadex_id == 4242
    assert "cast" in filled and "links" in filled
    # An automatic fill is **not** a human edit, and it used to claim to be one. The lock says
    # "somebody edited this by hand, leave it alone"; setting it here made the series page tell
    # every admin they had edited a row they had never opened, and switched the scheduled
    # refresh off across the whole library one series at a time.
    assert series.metadata_locked is False
    assert series.overview_source == OverviewSource.DRAMADEX


async def test_applying_does_not_destroy_a_synopsis_somebody_wrote(db: AsyncSession) -> None:
    series = await make_series(db, overview="Una sinopsis que escribí yo.")

    await library_service.apply_dramadex_title(db, series, parsed())

    assert series.overview == "Una sinopsis que escribí yo."
    # The cast still lands: it is a cache of the indexer, not something anyone edits here.
    rows = (await db.execute(select(SeriesCast).where(SeriesCast.series_id == series.id))).scalars().all()
    assert len(rows) == 2


async def test_the_cast_is_replaced_not_merged(db: AsyncSession) -> None:
    """A merge would leave a wrong actor behind after the indexer fixed a bad scrape."""
    series = await make_series(db)
    db.add(
        SeriesCast(series_id=series.id, dramadex_person_id=999, name="Quien No Estaba")
    )
    await db.commit()

    await library_service.apply_dramadex_title(db, series, parsed())

    names = {
        r.name
        for r in (
            await db.execute(select(SeriesCast).where(SeriesCast.series_id == series.id))
        ).scalars()
    }
    assert names == {"Jesse Morales", "Cameron Somers"}


async def test_watch_links_are_stored_for_watchable_studios_only(db: AsyncSession) -> None:
    series = await make_series(db)

    await library_service.apply_dramadex_title(db, series, parsed())

    links = (
        await db.execute(select(SeriesLink).where(SeriesLink.series_id == series.id))
    ).scalars().all()
    assert [link.studio_key for link in links] == ["reelshort"]
    # Built from the id, so it survives the retitles these platforms do constantly.
    assert links[0].url.endswith("/687f2ab2dafc1bd65e0e34ca")


async def test_aliases_become_searchable_titles(db: AsyncSession) -> None:
    """So a member typing the platform's own spelling finds what the household filed under
    a different name."""
    series = await make_series(db)

    await library_service.apply_dramadex_title(db, series, parsed())

    titles = {
        r.title
        for r in (
            await db.execute(select(SeriesTitle).where(SeriesTitle.series_id == series.id))
        ).scalars()
    }
    assert "El cirujano que despediste" in titles


async def test_a_declared_count_below_the_files_is_still_refused(db: AsyncSession) -> None:
    """The same guard every other writer obeys: the file count is the number that is a fact."""
    series = await make_series(db, files=68)

    await library_service.apply_dramadex_title(db, series, parsed())  # claims 51

    assert series.declared_episode_count is None


async def test_a_platform_listing_a_show_twice_yields_one_link(db: AsyncSession) -> None:
    """Found by running against the live index, not by imagining it.

    GoodShort carries "Found A Homeless Genius To Save My Company" under two ids, and once
    DramaDex merged those into one title it returned both. Two rows for one studio violates
    the unique constraint — and even without it, two identical "Watch on GoodShort" buttons
    is a choice a reader cannot make.
    """
    series = await make_series(db)
    payload = title_payload(
        studios=[
            {
                "key": "goodshort",
                "name": "GoodShort",
                "external_id": "31001349156",
                "watch_url": "https://www.goodshort.com/drama/a-31001349156",
            },
            {
                "key": "goodshort",
                "name": "GoodShort",
                "external_id": "31001662876",
                "watch_url": "https://www.goodshort.com/drama/a-31001662876",
            },
        ]
    )
    found = dramadex._title(payload)
    assert found is not None

    await library_service.apply_dramadex_title(db, series, found)

    links = (
        await db.execute(select(SeriesLink).where(SeriesLink.series_id == series.id))
    ).scalars().all()
    assert [link.studio_key for link in links] == ["goodshort"]
    # The first one wins, deterministically.
    assert links[0].external_id == "31001349156"


# --- The wishlist, now that it asks one source ----------------------------------------------
#
# These four replace tests that lived in `test_requests.py` and were written against a TMDB
# fixture that no longer exists. The *behaviour* they covered survived the removal intact —
# an unconfigured search, a blank query, "we already have this", and "you already asked" — so
# the coverage is carried over rather than dropped.


async def connect(db: AsyncSession) -> None:
    from app.services.settings import SettingsService

    svc = SettingsService(db)
    await svc.set(dramadex.BASE_URL_SETTING, "http://dramadex.test:8099")
    await svc.set(dramadex.API_KEY_SETTING, "k")
    await db.commit()
    SettingsService.invalidate()


async def test_an_unconnected_indexer_is_a_state_not_an_error(db: AsyncSession) -> None:
    """A fresh install has no indexer, and that must not read as "nothing matched".

    This one caught a real regression: the rewritten search hardcoded `configured=True`, so a
    household that had connected nothing would have been told its query found no results.
    """
    from app.models import User, UserRole
    from app.services import requests as requests_service

    user = User(email="v@example.com", display_name="V", role=UserRole.VIEWER, locale="es")
    db.add(user)
    await db.commit()

    found = await requests_service.search(db, user, "cualquier cosa")

    assert found.configured is False
    assert found.providers == []
    assert found.results == []


async def test_a_blank_query_asks_nobody_anything(db: AsyncSession) -> None:
    from app.models import User, UserRole
    from app.services import requests as requests_service

    user = User(email="v2@example.com", display_name="V", role=UserRole.VIEWER, locale="es")
    db.add(user)
    await db.commit()
    await connect(db)

    found = await requests_service.search(db, user, "   ")

    assert found.query == ""
    assert found.results == []


async def test_a_refresh_replaces_what_the_indexer_owns(db: AsyncSession) -> None:
    """The complaint that started this: "Actualizar metadata" appeared to do nothing.

    It ran, and it filled blanks only — so on a series that was already complete it changed
    nothing and said so. Re-reading the one index that catalogues this library is the entire
    point of the button, so a refresh overwrites the fields that index owns.
    """
    series = await make_series(db)
    await library_service.apply_dramadex_title(db, series, parsed())

    # Something drifted: a stale year, a genre list from an older scrape.
    series.year = 1999
    series.genres = ["Wrong"]
    await db.flush()

    filled = await library_service.apply_dramadex_title(db, series, parsed(), overwrite=True)

    assert series.year == 2025
    assert series.genres == ["Drama", "Family Drama", "Modern"]
    assert "year" in filled and "genres" in filled


async def test_a_refresh_that_changes_nothing_reports_nothing(db: AsyncSession) -> None:
    """`filled` is what the job reports to the admin, so it has to mean something.

    Before the equality check every overwrite listed every field it had written, including the
    ones it had rewritten with the value already there — so a refresh that genuinely changed
    nothing announced nine changes.
    """
    series = await make_series(db)
    await library_service.apply_dramadex_title(db, series, parsed())

    filled = await library_service.apply_dramadex_title(db, series, parsed(), overwrite=True)

    scalar = [name for name in filled if name not in {"cast", "links"}]
    assert scalar == [], f"a no-op refresh reported {scalar}"


async def test_a_refresh_keeps_a_synopsis_a_person_wrote(db: AsyncSession) -> None:
    """`overwrite` must not mean "take the admin's paragraph away".

    The overview answers to provenance rather than to the flag: MANUAL is a person's and
    survives, and that is the whole reason DRAMADEX had to become a value of its own.
    """
    series = await make_series(db, overview="Una sinopsis que escribí yo.")
    series.overview_source = OverviewSource.MANUAL
    await db.flush()

    await library_service.apply_dramadex_title(db, series, parsed(), overwrite=True)

    assert series.overview == "Una sinopsis que escribí yo."
    assert series.overview_source == OverviewSource.MANUAL


async def test_a_refresh_replaces_a_synopsis_the_indexer_wrote(db: AsyncSession) -> None:
    """The other half, and the one that was impossible before.

    DramaDex's own synopsis was stored as MANUAL — the label meaning "never overwrite" — so the
    indexer could not correct its own text and the refresh had nothing to say about it, ever.
    """
    series = await make_series(db)
    await library_service.apply_dramadex_title(db, series, parsed())
    assert series.overview_source == OverviewSource.DRAMADEX

    series.overview = "Un resumen viejo que trajo el indexador."
    await db.flush()

    filled = await library_service.apply_dramadex_title(db, series, parsed(), overwrite=True)

    assert series.overview == "After Dr. Adrian Marksman is dismissed, he..."
    assert "overview" in filled


async def test_rows_written_before_the_dramadex_provenance_are_treated_as_a_persons(
    db: AsyncSession,
) -> None:
    """Every series in the live library carries `manual` from the old behaviour.

    There is no way to tell those apart from a paragraph somebody typed, so they are protected.
    Being wrong in this direction costs a stale synopsis; being wrong the other way destroys
    writing nobody can recover.
    """
    series = await make_series(db, overview="Texto viejo, procedencia ambigua.")
    series.overview_source = OverviewSource.MANUAL
    await db.flush()

    await library_service.apply_dramadex_title(db, series, parsed(), overwrite=True)

    assert series.overview == "Texto viejo, procedencia ambigua."


async def test_a_poster_whose_file_is_gone_is_refetched(db: AsyncSession, tmp_env) -> None:
    """The broken image the owner reported, and why it could never heal.

    `refresh_series` skipped the artwork fetch whenever `poster_path` was non-empty. The image
    cache is derived data and the database is not, so a wiped cache, a restore that missed
    `DATA_PATH`, or one failed write leaves a row pointing at nothing — and the only control
    that could have repaired it declined to, precisely *because* the broken path was set.
    """
    series = await make_series(db)
    series.poster_path = "series/abc/poster.webp"  # a cached-shaped path with no file behind it
    await db.flush()

    assert dramadex._needs_poster(series) is True


async def test_a_poster_that_is_actually_there_is_left_alone(db: AsyncSession, tmp_env) -> None:
    """The other side: re-downloading a working poster on every refresh is the worse bug."""
    from app.config import get_settings

    series = await make_series(db)
    relative = "series/abc/poster.webp"
    target = get_settings().data_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not really a webp, but non-empty and on disk")
    series.poster_path = relative
    await db.flush()

    assert dramadex._needs_poster(series) is False


async def test_a_remote_poster_is_taken_on_trust(db: AsyncSession, tmp_env) -> None:
    """There is nothing local to stat, so a URL cannot be checked and is not second-guessed."""
    series = await make_series(db)
    series.poster_path = "https://images.example/poster.jpg"
    await db.flush()

    assert dramadex._needs_poster(series) is False


async def test_no_poster_at_all_is_the_obvious_case(db: AsyncSession, tmp_env) -> None:
    series = await make_series(db)
    series.poster_path = None
    await db.flush()

    assert dramadex._needs_poster(series) is True
