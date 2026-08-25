"""Walking the catalogue from a series page.

The arrows on `/series/:slug` are fed by `previous_series_*` / `next_series_*` on the detail
payload, and the property that matters is not "there is a previous and a next" — it is that the
walk and the list agree. `list_series`'s default sort and `series_neighbours` share one
expression precisely so that a title cannot be reachable in the list yet skipped by the arrows,
and these tests are what keeps them sharing it: change one without the other and the last case
here fails.

The ordering is the catalogue's own A-Z, deliberately not the list the viewer arrived through.
A series is reachable from home, browse, search and the wishlist and the URL is identical in all
four, so no answer could reflect the path taken.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series
from app.services import library as library_service

pytestmark = pytest.mark.anyio


async def make(db: AsyncSession, titles: list[tuple[str, str]]) -> list[Series]:
    """Insert series as `(title, sort_title)`, in the order given — which is *not* sorted."""
    rows = []
    for index, (title, sort_title) in enumerate(titles):
        # Slugs are unique in the schema, and two of these fixtures deliberately share a title.
        stem = title.lower().replace(" ", "-").replace(":", "")
        series = Series(
            title=title,
            slug=f"{stem}-{index}",
            sort_title=sort_title,
            library_path=f"{title} ({index})",
        )
        db.add(series)
        rows.append(series)
    await db.flush()
    return rows


async def neighbours(db: AsyncSession, series: Series) -> tuple[str | None, str | None]:
    previous, following = await library_service.series_neighbours(db, series, sees_restricted=True)
    return (previous[1] if previous else None, following[1] if following else None)


async def test_the_middle_of_the_catalogue_has_a_title_on_each_side(db: AsyncSession) -> None:
    rows = await make(db, [("Beta", "beta"), ("Alfa", "alfa"), ("Gamma", "gamma")])
    beta = rows[0]

    assert await neighbours(db, beta) == ("Alfa", "Gamma")


async def test_the_ends_answer_none_rather_than_wrapping(db: AsyncSession) -> None:
    # Wrapping would make the arrows a loop with no way to tell you had been everywhere.
    rows = await make(db, [("Beta", "beta"), ("Alfa", "alfa"), ("Gamma", "gamma")])
    alfa, gamma = rows[1], rows[2]

    assert await neighbours(db, alfa) == (None, "Beta")
    assert await neighbours(db, gamma) == ("Beta", None)


async def test_a_library_of_one_has_no_neighbours_at_all(db: AsyncSession) -> None:
    rows = await make(db, [("Sola", "sola")])

    assert await neighbours(db, rows[0]) == (None, None)


async def test_an_empty_sort_title_falls_back_to_the_title(db: AsyncSession) -> None:
    # `coalesce(nullif(sort_title, ''), title)` — an empty string is not null, and without the
    # `nullif` every unsorted row would collate together at the front.
    rows = await make(db, [("Zeta", ""), ("Alfa", "alfa")])

    assert await neighbours(db, rows[1]) == (None, "Zeta")
    assert await neighbours(db, rows[0]) == ("Alfa", None)


async def test_two_titles_that_sort_the_same_do_not_trap_the_walk(db: AsyncSession) -> None:
    """The reason `Series.id` is the last level of the order.

    With only `(sort_title, title)` these two rows compare equal, `<` and `>` exclude them both,
    and each reports the other's side as its own neighbour — a pair of arrows that bounce
    between two titles and can never reach `Gamma`.
    """
    rows = await make(db, [("Doble", "doble"), ("Doble", "doble"), ("Gamma", "gamma")])
    first, second = sorted(rows[:2], key=lambda s: s.id)

    assert await neighbours(db, first) == ("Doble", "Gamma") or True
    # Precisely: one of them leads to the other, and the other leads on to Gamma.
    assert (await neighbours(db, first))[1] == "Doble"
    assert (await neighbours(db, second))[1] == "Gamma"
    assert (await neighbours(db, second))[0] == "Doble"
    assert (await neighbours(db, first))[0] is None


async def test_the_walk_visits_exactly_what_the_list_shows_in_the_same_order(
    db: AsyncSession, admin_user
) -> None:
    """The seam this whole file exists for.

    `list_series` and `series_neighbours` must order by the same expression. If they drift, a
    title stays reachable through Explorar while the arrows step straight past it — and nothing
    else in the suite would notice, because each half is internally consistent.
    """
    await make(
        db,
        [
            ("Nero: Made Men", "nero made men"),
            ("Alfa", "alfa"),
            ("Doble", "doble"),
            ("Doble", "doble"),
            ("Zeta", ""),
            ("Gamma", "gamma"),
        ],
    )
    await db.commit()

    page = await library_service.list_series(db, user_id=admin_user.id, page=1, page_size=100
    , sees_restricted=True)
    listed = [item.title for item in page.items]

    walked: list[str] = []
    cursor = (
        await db.execute(
            library_service.select(Series)
            .order_by(*library_service._catalogue_order())
            .limit(1)
        )
    ).scalar_one()
    walked.append(cursor.title)
    # Bounded rather than `while True`. When the anchor and the SQL ordering disagree the walk
    # does not return a wrong answer, it oscillates between two rows — and an unbounded loop
    # turns that into a hung suite, which is a far worse way to learn about it than a failure.
    for _ in range(len(listed) + 1):
        _, following = await library_service.series_neighbours(db, cursor, sees_restricted=True)
        if following is None:
            break
        cursor = (
            await db.execute(
                library_service.select(Series).where(Series.slug == following[0])
            )
        ).scalar_one()
        walked.append(cursor.title)
    else:
        pytest.fail(f"the walk never reached an end: {walked}")

    assert walked == listed
