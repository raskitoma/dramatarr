"""The platform name the indexer sends, and the spelling that reaches the row.

`network` was the one vocabulary field on the DramaDex apply path that went in raw while its
two neighbours — genres and content rating — each went through their canonicaliser. That made
the indexer the only writer of this column free to spell a platform however it liked.

The existing coverage could not see it: `test_dramadex.py`'s fixture already spells the studio
canonically, so the assertion passes with or without the canonicaliser. These tests spell it
the way a source actually might.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series

pytestmark = pytest.mark.anyio


def a_title(studio_name: str):
    from app.services.dramadex import DramadexTitle, StudioLink

    return DramadexTitle(
        id=13032,
        primary_title="Right Beside Me",
        studios=(
            StudioLink(
                key="netshort",
                name=studio_name,
                external_id="1808387443220733954",
                watch_url="https://netshort.com/full-episodes/1808387443220733954",
            ),
        ),
    )


async def a_series(db: AsyncSession, slug: str) -> Series:
    series = Series(
        title="Right Beside Me", slug=slug, sort_title="right beside me", library_path=slug
    )
    db.add(series)
    await db.commit()
    return series


@pytest.mark.parametrize("sent", ["netshort", "NETSHORT", "net short", "NetShort"])
async def test_however_the_indexer_spells_it_the_row_says_netshort(
    db: AsyncSession, sent: str
) -> None:
    from app.services.library import apply_dramadex_title

    series = await a_series(db, f"rbm-{sent.replace(' ', '-').lower()}")
    await apply_dramadex_title(db, series, a_title(sent), overwrite=True)

    assert series.network == "NetShort"


async def test_an_unknown_platform_still_gets_consistent_casing(db: AsyncSession) -> None:
    """The canonicaliser is not an allowlist. A platform nobody has heard of is title-cased
    rather than left as typed, which is what stops one arriving twice in two spellings."""
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "rbm-unknown")
    await apply_dramadex_title(db, series, a_title("brandnewapp"), overwrite=True)

    assert series.network == "Brandnewapp"


async def test_a_studio_with_no_name_does_not_blank_the_column(db: AsyncSession) -> None:
    """`studio_name` is None when nothing is watchable, and `put` refuses empty values — so a
    series that already names its platform keeps it."""
    from app.services.dramadex import DramadexTitle
    from app.services.library import apply_dramadex_title

    series = await a_series(db, "rbm-nameless")
    series.network = "ReelShort"
    await db.commit()

    await apply_dramadex_title(
        db, series, DramadexTitle(id=1, primary_title="Right Beside Me"), overwrite=True
    )

    assert series.network == "ReelShort"


async def test_the_preview_shows_what_the_apply_will_write(db: AsyncSession) -> None:
    """The dialog previews `apply_dramadex_title`. A difference between the two would show the
    admin one string and store another."""
    from app.services.library import apply_dramadex_title, plan_dramadex_title

    series = await a_series(db, "rbm-preview")
    found = a_title("netshort")

    plan = await plan_dramadex_title(db, series, found)
    shown = next(row for row in plan if row.field == "network")

    await apply_dramadex_title(db, series, found, overwrite=True)
    # `incoming` is rendered as a list by `_as_list`, so compare on its single entry.
    assert shown.incoming == ["NetShort"]
    assert series.network == "NetShort"
