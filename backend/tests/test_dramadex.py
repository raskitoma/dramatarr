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
    title = fields.pop("title", "You Fired A Genius Surgeon")
    slug = fields.pop("slug", "genius-surgeon")
    series = Series(
        title=title,
        slug=slug,
        sort_title=title.lower(),
        # Unique in the schema, so a second series in one test needs its own.
        library_path=fields.pop("path", title),
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


def cast_entry(
    person_id: int, name: str, billing: int | None, role: str = "actor"
) -> dramadex.CastMember:
    """The real type, not a stand-in.

    It used to be an ad-hoc class with the same four attributes, and that hid a defect the
    moment the merge started writing to them: `CastMember` is a **frozen** dataclass, so
    assignment raises `FrozenInstanceError` at call time while every test went on passing. Only
    a live refresh on the NAS found it.
    """
    return dramadex.CastMember(
        person_id=person_id, name=name, role=role, billing_order=billing
    )


def test_the_same_actor_twice_becomes_once() -> None:
    """Straight from the NAS: DramaDex sends one person under two ids.

    On `American Sniper: The Last Round` it returns Addison Bowman as person 273 *and* person
    43, and Piper Jean Jones as 343 *and* 216 — so the series page rendered eight names for six
    people. The ids genuinely differ, so nothing can dedupe on id.
    """
    collapsed = library_service._one_row_per_person(
        [
            cast_entry(273, "Addison Bowman", 0),
            cast_entry(11, "Jesse Morales", 1),
            cast_entry(343, "Piper Jean Jones", 5),
            cast_entry(43, "Addison Bowman", None),
            cast_entry(216, "Piper Jean Jones", None),
        ]
    )

    assert [m.name for m in collapsed] == [
        "Addison Bowman",
        "Jesse Morales",
        "Piper Jean Jones",
    ]


def test_the_billed_record_is_the_one_that_survives() -> None:
    """Which of the pair wins is not arbitrary.

    The strays arrive with no `billing_order` while the real entry carries its position, so the
    complete record has to win — otherwise the list keeps the right names in the wrong order.
    """
    collapsed = library_service._one_row_per_person(
        [
            cast_entry(43, "Addison Bowman", None),
            cast_entry(273, "Addison Bowman", 0),
        ]
    )

    assert len(collapsed) == 1
    assert collapsed[0].person_id == 273


def test_a_credit_with_no_billing_is_still_a_credit() -> None:
    """Dropping every unordered entry would lose real people, not just duplicates."""
    collapsed = library_service._one_row_per_person(
        [cast_entry(1, "Primero", 0), cast_entry(2, "Sin orden", None)]
    )

    assert [m.name for m in collapsed] == ["Primero", "Sin orden"]


def test_billing_order_is_preserved_and_strays_follow() -> None:
    collapsed = library_service._one_row_per_person(
        [
            cast_entry(3, "Tercero", 2),
            cast_entry(1, "Primero", 0),
            cast_entry(9, "Sin orden", None),
            cast_entry(2, "Segundo", 1),
        ]
    )

    assert [m.name for m in collapsed] == ["Primero", "Segundo", "Tercero", "Sin orden"]


def test_names_differing_only_by_spacing_or_case_are_one_person() -> None:
    collapsed = library_service._one_row_per_person(
        [cast_entry(1, "Addison  Bowman", 0), cast_entry(2, "addison bowman", None)]
    )

    assert len(collapsed) == 1


def test_an_entry_with_no_name_is_dropped_rather_than_grouped() -> None:
    """Empty names would otherwise all collapse into a single nameless credit."""
    collapsed = library_service._one_row_per_person(
        [cast_entry(1, "", 0), cast_entry(2, "   ", 1), cast_entry(3, "Real", 2)]
    )

    assert [m.name for m in collapsed] == ["Real"]


def test_a_character_name_survives_whichever_record_carried_it() -> None:
    """The reason the pair is merged rather than picked between.

    The two records exist because they came from different scrapes. Today the stray adds
    nothing — every role DramaDex sends reads "actor" — but the day one of them carries a
    character name it must not be the one thrown away.
    """
    billed = cast_entry(273, "Addison Bowman", 0)
    stray = cast_entry(43, "Addison Bowman", None, role="Sheriff Dalton")

    merged = library_service._one_row_per_person([billed, stray])

    assert len(merged) == 1
    assert merged[0].role == "Sheriff Dalton"
    # The billed record's id is the one kept, because that is what the name links to.
    assert merged[0].person_id == 273
    assert merged[0].billing_order == 0


def test_the_earliest_billing_of_the_pair_wins() -> None:
    first = cast_entry(1, "Alguien", 4)
    second = cast_entry(2, "Alguien", 1)

    merged = library_service._one_row_per_person([first, second])

    assert len(merged) == 1
    assert merged[0].billing_order == 1


def test_a_generic_role_never_overwrites_a_real_one() -> None:
    named = cast_entry(1, "Alguien", 0, role="Carl Oliver")
    generic = cast_entry(2, "Alguien", 1, role="actor")

    merged = library_service._one_row_per_person([named, generic])

    assert merged[0].role == "Carl Oliver"


def test_nothing_is_lost_but_repetition() -> None:
    """Straight from the live library: 34 entries, 27 distinct names, 27 rows kept.

    The owner's worry was that collapsing duplicates was dropping people. It is not — but the
    only way to keep that true is to assert it.
    """
    sent = [
        cast_entry(273, "Addison Bowman", 0),
        cast_entry(11, "Jesse Morales", 1),
        cast_entry(5, "Marc Herrmann", 2),
        cast_entry(16, "Mark H. McClafferty", 3),
        cast_entry(8, "Cameron Somers", 4),
        cast_entry(343, "Piper Jean Jones", 5),
        cast_entry(43, "Addison Bowman", None),
        cast_entry(216, "Piper Jean Jones", None),
    ]

    merged = library_service._one_row_per_person(sent)

    distinct = {" ".join(m.name.split()).casefold() for m in sent}
    assert len(merged) == len(distinct) == 6


# --- Artwork on a refresh ---------------------------------------------------------------------
#
# `_needs_poster` is gone. It answered "does this look like it needs a picture", and the answer
# was wrong in the case that mattered: a title already carrying the indexer's art could never
# pick up a newer version, which is what "actualizar metadata no reemplaza el póster" meant.
# A refresh runs for exactly one reason — somebody pressed the button — so it always asks again,
# and the cache decides whether that costs a write.


async def test_a_refresh_asks_for_the_art_every_time(db: AsyncSession, tmp_env) -> None:
    series = await make_series(db)
    assert dramadex._keeps_its_own_artwork(series) is False


async def test_artwork_an_admin_chose_is_never_overruled(db: AsyncSession, tmp_env) -> None:
    """The one exception. Choosing artwork locks the row, and that choice is theirs."""
    series = await make_series(db)
    series.artwork_locked = True
    await db.flush()

    assert dramadex._keeps_its_own_artwork(series) is True


def test_unchanged_art_keeps_its_filename(tmp_env) -> None:
    """Same bytes, same name — so a refresh on a title nobody retouched writes nothing."""
    from app.services import images

    first = images.dramadex_poster_name(4242)
    second = images.dramadex_poster_name(4242)
    assert first == second


def test_the_name_carries_the_title_so_a_thumbnail_is_still_distinguishable(tmp_env) -> None:
    """The half that keying on bytes alone would have lost.

    Key art and wishlist thumbnails share one directory and one served route, so without the
    title in the name there is no way to tell "this is the indexer's picture for this show" from
    "this is whatever illustrated the request".
    """
    from app.services import images

    name = images.dramadex_poster_name(4242)
    url = images.request_poster_url(f"{name}-abc123def456.webp")

    assert images.is_dramadex_poster(url, 4242) is True
    assert images.is_dramadex_poster(url, 999) is False
    assert images.is_dramadex_poster(images.request_poster_url("random.webp"), 4242) is False


def test_a_poster_written_before_the_digest_is_still_recognised(tmp_env) -> None:
    """Files already on the NAS are named `<prefix>.webp`, with no digest after it."""
    from app.services import images

    legacy = images.request_poster_url(f"{images.dramadex_poster_name(668)}.webp")
    assert images.is_dramadex_poster(legacy, 668) is True


# --- The preview, and its promise to the writer -------------------------------------------------
#
# "Actualizar metadata" used to be a button that went away and came back having done something.
# Now the admin is shown the rows first. The property that has to hold is not that the preview
# looks nice — it is that **the preview and the write share one decision path**, so what is
# shown is what happens. `plan_dramadex_title` is that path; these tests are what keeps the two
# from drifting.


async def test_the_plan_offers_the_poster_first(db: AsyncSession) -> None:
    """"INCLUIDA LA IMAGEN" — the owner's words, in capitals. It is row zero."""
    series = await make_series(db)

    rows = await library_service.plan_dramadex_title(db, series, parsed())

    assert rows[0].field == "poster"
    assert rows[0].kind == "image"
    # A URL, not bytes: previewing must cost no download.
    assert rows[0].incoming == ["/api/library/dramadex/titles/4242/poster"]


async def test_pinned_artwork_is_offered_but_not_ticked(db: AsyncSession) -> None:
    """Refusing to *show* the row would be a different lie. It is offered, unticked, explained."""
    series = await make_series(db)
    series.artwork_locked = True
    await db.flush()

    poster = (await library_service.plan_dramadex_title(db, series, parsed()))[0]

    assert poster.default_checked is False
    assert poster.note == "ownArtwork"


async def test_a_synopsis_somebody_wrote_is_offered_but_not_ticked(db: AsyncSession) -> None:
    series = await make_series(db, overview="Una sinopsis que escribí yo.")
    series.overview_source = OverviewSource.MANUAL
    await db.flush()

    rows = {row.field: row for row in await library_service.plan_dramadex_title(db, series, parsed())}

    assert rows["overview"].changed is True
    assert rows["overview"].default_checked is False
    assert rows["overview"].note == "manualOverview"


async def test_a_declared_count_below_the_files_on_disk_is_refused(db: AsyncSession) -> None:
    """The file count is the number that is a fact. Sixty-six files, a record claiming one."""
    series = await make_series(db, files=80)

    rows = {row.field: row for row in await library_service.plan_dramadex_title(db, series, parsed())}

    row = rows["declared_episode_count"]
    assert row.default_checked is False
    assert row.note == "fewerEpisodes"
    assert row.note_args == {"declared": "51", "actual": "80"}


async def test_applying_only_what_was_ticked_writes_only_that(db: AsyncSession) -> None:
    """The seam this whole design exists to close.

    An admin unticks the synopsis and ticks the year. Writing both would be the application
    doing something it showed no sign of intending.
    """
    series = await make_series(db)

    await library_service.apply_dramadex_title(db, series, parsed(), only=frozenset({"year"}))

    assert series.year == 2025
    assert series.overview is None
    assert series.network is None


async def test_an_empty_selection_writes_no_fields_but_still_binds(db: AsyncSession) -> None:
    """Ticking nothing must change nothing — except the binding, which is not a field.

    The binding is the answer to "which title is this", and it is what the admin settled by
    choosing a record at all. An unbound series with every box unticked would otherwise leave
    the dialog having established nothing, and the next visit would ask again.
    """
    series = await make_series(db, overview="Intacta.")
    before = series.year

    filled = await library_service.apply_dramadex_title(db, series, parsed(), only=frozenset())

    assert filled == ["dramadex_id"]
    assert series.dramadex_id == 4242
    assert series.overview == "Intacta."
    assert series.year == before


async def test_ticking_a_manual_synopsis_does_replace_it(db: AsyncSession) -> None:
    """The note warns; it does not overrule.

    `only` is the admin having read "esta sinopsis la escribiste tú" and ticked the box anyway.
    Refusing at that point would be the application arguing with somebody who already decided.
    """
    series = await make_series(db, overview="Una sinopsis que escribí yo.")
    series.overview_source = OverviewSource.MANUAL
    await db.flush()

    await library_service.apply_dramadex_title(db, series, parsed(), only=frozenset({"overview"}))

    assert series.overview == "After Dr. Adrian Marksman is dismissed, he..."


async def test_the_default_path_is_unchanged_by_any_of_this(db: AsyncSession) -> None:
    """`only=None` has to behave exactly as it did before the dialog existed.

    The import path and the refresh job both take it, and the provenance matrix above is only
    meaningful while this holds.
    """
    series = await make_series(db, overview="Una sinopsis que escribí yo.")
    series.overview_source = OverviewSource.MANUAL
    await db.flush()

    await library_service.apply_dramadex_title(db, series, parsed(), overwrite=True)

    assert series.overview == "Una sinopsis que escribí yo."


async def test_every_planned_field_is_a_field_the_writer_understands(db: AsyncSession) -> None:
    """The drift guard.

    The dialog sends back the `field` names the plan gave it. If the planner ever names a row
    the writer does not recognise, that checkbox becomes a control that does nothing — and
    nothing else in the suite would notice, because both halves stay internally consistent.
    """
    series = await make_series(db)
    rows = await library_service.plan_dramadex_title(db, series, parsed())

    for row in rows:
        await library_service.apply_dramadex_title(
            db, series, parsed(), only=frozenset({row.field})
        )

    # Applying them one at a time must end up where applying them together does.
    fresh = await make_series(db, title="Otra", slug="otra", path="Otra")
    await library_service.apply_dramadex_title(db, fresh, parsed())

    assert series.year == fresh.year
    assert series.network == fresh.network
    assert series.genres == fresh.genres
