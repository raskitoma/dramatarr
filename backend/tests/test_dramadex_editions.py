"""The language a member actually reads.

A platform that runs a per-language catalogue publishes the same show more than once — its own
title, its own synopsis, its own page. Until the indexer exposed `editions[]`, all of that
arrived flattened: the Spanish title landed in `aliases[]` indistinguishable from an English
retitle, the Spanish synopsis was not exposed at all, and `overview_source_lang` was hardcoded
to `"en"` — a guess even then, since `primary_title` resolves to whichever edition the indexer
happened to see first.

**Measured before writing any of this**, against the live index on 2026-08-25: 3,667 links are
tagged `en` and exactly **one** is tagged `es`. So the case that has to keep working perfectly
is the one with no editions at all, and it is tested first and last.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series, SeriesLink, SeriesTitle, TitleKind
from app.models.library import OverviewSource

pytestmark = pytest.mark.anyio

EN_SYNOPSIS = "Aria won Luca's love in an arranged mafia marriage."
ES_SYNOPSIS = "Aria se ganó el amor de Luca en un matrimonio concertado por la mafia."
EN_URL = "https://www.reelshort.com/movie/69d91b3ee15105112e0dc022"
ES_URL = "https://www.reelshort.com/movie/6a166376c040bd96890c71bd"


def a_title(*, with_editions: bool = True):
    """Title 1065 as the live instance actually answers for it."""
    from app.services.dramadex import DramadexTitle, Edition, StudioLink

    editions = (
        Edition(
            lang="en",
            title="Bound by Love",
            synopsis=EN_SYNOPSIS,
            watch_url=EN_URL,
            is_primary=True,
            source="reelshort",
        ),
        Edition(
            lang="es",
            title="Atada por amor",
            synopsis=ES_SYNOPSIS,
            watch_url=ES_URL,
            is_primary=False,
            source="reelshort",
        ),
    )
    return DramadexTitle(
        id=1065,
        primary_title="Bound by Love",
        synopsis=EN_SYNOPSIS,
        aliases=("Atada por amor", "His Cure His Wife"),
        studios=(
            StudioLink(
                key="reelshort",
                name="ReelShort",
                external_id="69d91b3ee15105112e0dc022",
                watch_url=EN_URL,
            ),
        ),
        editions=editions if with_editions else (),
    )


async def a_series(db: AsyncSession, slug: str) -> Series:
    series = Series(title="Bound by Love", slug=slug, sort_title="bound by love", library_path=slug)
    db.add(series)
    await db.commit()
    return series


async def titles_of(db: AsyncSession, series: Series) -> list[SeriesTitle]:
    rows = await db.execute(select(SeriesTitle).where(SeriesTitle.series_id == series.id))
    return list(rows.scalars())


# --- The synopsis, which is the half worth the most ---------------------------------------------


async def test_the_household_reads_its_own_language(db: AsyncSession) -> None:
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "bbl-es")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    assert series.overview == ES_SYNOPSIS
    assert series.overview_source_lang == "es"
    assert series.overview_source == OverviewSource.DRAMADEX
    # The attribution points at what was quoted, not at the English page.
    assert series.overview_source_url == ES_URL


async def test_an_english_household_gets_the_english_one(db: AsyncSession) -> None:
    from app.services.library import apply_dramadex_title
    from app.services.settings import SettingsService

    await SettingsService(db).set("site.default_locale", "en")
    series = await a_series(db, "bbl-en")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    assert series.overview == EN_SYNOPSIS
    assert series.overview_source_lang == "en"


async def test_with_no_edition_in_their_language_nothing_changes(db: AsyncSession) -> None:
    """A household set to Portuguese, and no Portuguese edition: the primary text, labelled
    with the language it is actually in rather than an assumed `en`."""
    from app.services.library import apply_dramadex_title
    from app.services.settings import SettingsService

    await SettingsService(db).set("site.default_locale", "pt")
    series = await a_series(db, "bbl-pt")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    assert series.overview == EN_SYNOPSIS
    assert series.overview_source_lang == "en"


async def test_a_title_with_no_editions_at_all_is_untouched(db: AsyncSession) -> None:
    """3,666 of 3,667 tagged links are like this today. It is the normal path, not the edge."""
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "bbl-none")
    await apply_dramadex_title(db, series, a_title(with_editions=False), overwrite=True)

    assert series.overview == EN_SYNOPSIS
    assert series.overview_source_lang == "en"
    assert series.overview_source_url == EN_URL


async def test_a_paragraph_a_person_wrote_is_still_theirs(db: AsyncSession) -> None:
    """Provenance outranks language. A Spanish edition arriving does not license overwriting
    text an admin typed, which is the rule that existed before editions did."""
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "bbl-manual")
    series.overview = "Lo escribí yo."
    series.overview_source = OverviewSource.MANUAL
    await db.commit()

    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    assert series.overview == "Lo escribí yo."


# --- The titles, and the two columns that were never true ---------------------------------------


async def test_an_edition_title_says_which_language_it_is(db: AsyncSession) -> None:
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "bbl-titles")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    by_text = {t.title: t for t in await titles_of(db, series)}
    spanish = by_text["Atada por amor"]
    assert spanish.language == "es"
    assert spanish.kind == TitleKind.OFFICIAL


async def test_a_plain_alias_stays_unlabelled(db: AsyncSession) -> None:
    """ "His Cure His Wife" is another English title, not an edition. Claiming a language for it
    would be inventing one."""
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "bbl-alias")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    by_text = {t.title: t for t in await titles_of(db, series)}
    assert by_text["His Cure His Wife"].language == ""
    assert by_text["His Cure His Wife"].kind == TitleKind.ALTERNATE


async def test_the_same_string_is_not_stored_twice(db: AsyncSession) -> None:
    """`aliases[]` still carries every edition title, so both lists offer "Atada por amor".
    Editions are written first precisely so the labelled row is the one that survives."""
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "bbl-dupe")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    rows = [t for t in await titles_of(db, series) if t.title == "Atada por amor"]
    assert len(rows) == 1
    assert rows[0].language == "es"


async def test_the_spanish_title_is_findable(db: AsyncSession) -> None:
    """The point of storing it at all: a member types the name they know."""
    from app.services.library import apply_dramadex_title
    from app.services.search.normalize import normalize

    series = await a_series(db, "bbl-find")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    slugs = {t.normalized for t in await titles_of(db, series)}
    assert normalize("Atada por amor") in slugs


# --- The watch button ---------------------------------------------------------------------------


async def test_the_link_opens_the_edition_they_can_read(db: AsyncSession) -> None:
    """A localised edition is a different page on the platform, not a translation of the same
    one — the English id would land them somewhere they cannot read."""
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "bbl-link")
    await apply_dramadex_title(db, series, a_title(), overwrite=True)

    link = (
        (await db.execute(select(SeriesLink).where(SeriesLink.series_id == series.id)))
        .scalars()
        .one()
    )
    assert link.url == ES_URL
    assert link.studio_key == "reelshort"


async def test_a_link_for_a_platform_with_no_such_edition_is_left_alone(
    db: AsyncSession,
) -> None:
    """The Spanish edition belongs to one platform. Another platform's button must keep its own
    URL rather than inheriting a link into a catalogue it has nothing to do with."""
    from app.services.dramadex import StudioLink
    from app.services.library import apply_dramadex_title

    found = a_title()
    other = StudioLink(
        key="netshort",
        name="NetShort",
        external_id="180838",
        watch_url="https://netshort.com/full-episodes/180838",
    )
    found = type(found)(**{**found.__dict__, "studios": (*found.studios, other)})

    series = await a_series(db, "bbl-twoplatforms")
    await apply_dramadex_title(db, series, found, overwrite=True)

    by_key = {
        link.studio_key: link.url
        for link in (
            await db.execute(select(SeriesLink).where(SeriesLink.series_id == series.id))
        ).scalars()
    }
    assert by_key["reelshort"] == ES_URL
    assert by_key["netshort"] == "https://netshort.com/full-episodes/180838"
