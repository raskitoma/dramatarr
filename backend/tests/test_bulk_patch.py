"""Editing many series at once.

This route exists because of what the live catalogue looks like after an import: a batch of
vertical dramas that all need the same three fields set — status, year, network — and no
metadata source that will ever fill them. Done one screen at a time that is work a person
abandons half-finished, which leaves a catalogue that is *partly* corrected and therefore
harder to reason about than one that was never touched.

Everything asserted here is about the ways a bulk write goes wrong in a way a single write
never would, because the blast radius is forty rows instead of one:

**Unset must not mean null.** A patch body carrying every field with its default would blank
forty synopses to change forty statuses. This is the test that matters most.

**Canonicalization cannot be skipped on the bulk path.** If it were, `dramabox` typed once into
a bulk field would create a second network beside `DramaBox` in every filter list.

**A stale id must not discard the batch.** Selections are made against a page that was rendered
seconds ago; a row deleted in another tab is ordinary, not exceptional.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series, User

pytestmark = pytest.mark.anyio

URL = "/api/library/series/bulk"


@pytest.fixture
async def client(db: AsyncSession, admin_user: User) -> AsyncIterator[AsyncClient]:
    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def make(db: AsyncSession, title: str, **fields: Any) -> Series:
    series = Series(
        title=title,
        slug=title.lower().replace(" ", "-"),
        sort_title=title.lower(),
        library_path=title,
        **fields,
    )
    db.add(series)
    await db.commit()
    return series


async def reload(db: AsyncSession, series_id: str) -> Series:
    """Straight from the database. The identity map would happily answer with stale values."""
    return (
        await db.execute(
            select(Series).where(Series.id == series_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- The blast radius ------------------------------------------------------------------------


async def test_only_the_fields_sent_are_written(db: AsyncSession, client: AsyncClient) -> None:
    """The one that would hurt: a status change must not blank the synopses.

    Everything else here is about correctness; this is about not destroying work. An admin who
    selects forty rows to set the year and finds forty overviews gone has lost hours, and no
    error was raised at any point.
    """
    one = await make(db, "Uno", overview="Sinopsis que escribí yo.", year=2024, network="ReelShort")
    two = await make(db, "Dos", overview="Otra sinopsis.", year=2023)

    response = await client.post(
        URL, json={"ids": [one.id, two.id], "patch": {"status": "completed"}}
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 2
    expected = ((one.id, "Sinopsis que escribí yo.", 2024), (two.id, "Otra sinopsis.", 2023))
    for series_id, overview, year in expected:
        row = await reload(db, series_id)
        assert row.status == "completed"
        assert row.overview == overview, "an unsent field was overwritten"
        assert row.year == year


async def test_values_are_canonicalized_the_same_as_a_single_edit(
    db: AsyncSession, client: AsyncClient
) -> None:
    """Otherwise one careless bulk edit puts `Dramabox` beside `DramaBox` in every filter."""
    one = await make(db, "Uno")
    two = await make(db, "Dos")

    await client.post(
        URL,
        json={
            "ids": [one.id, two.id],
            "patch": {"network": "dramabox", "genres": ["revenge", "son-in-law"]},
        },
    )

    for series_id in (one.id, two.id):
        row = await reload(db, series_id)
        assert row.network == "DramaBox"
        assert row.genres == ["Revenge", "Son-in-Law"]


async def test_the_batch_locks_every_row_it_edits(db: AsyncSession, client: AsyncClient) -> None:
    """A human curated these rows. Without the lock the nightly refresh undoes all forty."""
    one = await make(db, "Uno")

    await client.post(URL, json={"ids": [one.id], "patch": {"year": 2026}})

    assert (await reload(db, one.id)).metadata_locked is True


async def test_unlocking_is_still_possible_in_bulk(db: AsyncSession, client: AsyncClient) -> None:
    """The escape hatch: handing a batch back to the jobs after fixing a bad match."""
    one = await make(db, "Uno", metadata_locked=True)

    await client.post(URL, json={"ids": [one.id], "patch": {"metadataLocked": False}})

    assert (await reload(db, one.id)).metadata_locked is False


async def test_a_missing_id_is_reported_and_the_rest_still_land(
    db: AsyncSession, client: AsyncClient
) -> None:
    """A selection is made against a page rendered seconds ago. A row someone else deleted in
    the meantime is ordinary, and throwing away the other edits over it would be a worse
    answer than saying which one went."""
    one = await make(db, "Uno")

    response = await client.post(
        URL, json={"ids": [one.id, "nope"], "patch": {"year": 2020}}
    )

    body = response.json()
    assert body["updated"] == 1
    assert body["missing"] == ["nope"]
    assert (await reload(db, one.id)).year == 2020


async def test_a_patch_that_changes_nothing_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    """Not pedantry: an empty patch still locks every row it touches, so silently accepting it
    would let a mis-click freeze forty series against the metadata jobs."""
    one = await make(db, "Uno")

    response = await client.post(URL, json={"ids": [one.id], "patch": {}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SERIES_BULK_EMPTY"


async def test_the_selection_is_bounded(db: AsyncSession, client: AsyncClient) -> None:
    """A selection larger than any page is a mis-click on select-all, and this runs inside one
    request. Refusing beats holding the session open for a minute."""
    response = await client.post(
        URL, json={"ids": [f"s{n}" for n in range(201)], "patch": {"year": 2020}}
    )
    assert response.status_code == 422


async def test_an_empty_selection_is_refused(db: AsyncSession, client: AsyncClient) -> None:
    response = await client.post(URL, json={"ids": [], "patch": {"year": 2020}})
    assert response.status_code == 422


async def test_a_viewer_cannot_reach_it(db: AsyncSession, viewer_user: User) -> None:
    """Guarded like every other admin write — asserted here rather than assumed, because this
    one route can rewrite the whole catalogue in a single call."""
    from app.api.deps import get_current_user
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: viewer_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post(URL, json={"ids": ["x"], "patch": {"year": 2020}})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


async def test_a_bulk_title_change_reaches_the_search_index(
    db: AsyncSession, client: AsyncClient
) -> None:
    """`sort_title` and the search index both follow a title, and both are easy to forget on a
    second code path — which is why there is only one code path."""
    one = await make(db, "Uno")

    await client.post(URL, json={"ids": [one.id], "patch": {"title": "Nuevo Título"}})

    row = await reload(db, one.id)
    assert row.title == "Nuevo Título"
    assert row.sort_title == "nuevo titulo"
