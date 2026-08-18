"""The index rebuilds itself when the shape of what it indexes changes.

Without this, adding a row kind is a change that only affects series somebody happens to edit
afterwards — which for a settled household catalogue is none of them. The generation marker is
what turns "the code now indexes cast" into "the library is now searchable by cast".
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.anyio


async def a_series_with_a_cast(db: AsyncSession):
    from app.models import Series, SeriesCast

    series = Series(
        title="Feast of Power",
        slug="feast-of-power",
        sort_title="feast of power",
        library_path="Feast of Power (2025)",
        year=2025,
    )
    db.add(series)
    await db.flush()
    db.add(
        SeriesCast(series_id=series.id, dramadex_person_id=1, name="Adam Daniel", billing_order=0)
    )
    await db.commit()
    return series


async def test_an_index_from_before_the_counter_is_rebuilt_once(db: AsyncSession) -> None:
    from app.services.search.index import (
        INDEX_GENERATION,
        META_TABLE,
        ensure_generation,
        ensure_index,
        reindex_series,
        stored_generation,
    )
    from app.services.search.service import search_library

    series = await a_series_with_a_cast(db)
    await reindex_series(db, series.id)
    await db.commit()

    # Exactly the state a deployed install is in the moment this code ships: rows written by
    # the previous shape, and no marker at all.
    await db.execute(sql(f"DELETE FROM {META_TABLE}"))
    await db.execute(sql("DELETE FROM search_index WHERE kind = 'series_cast'"))
    await db.commit()
    assert await stored_generation(db) == 0

    before = await search_library(db, "Adam Daniel")
    assert before.series == [], "the fixture did not reproduce the pre-upgrade index"

    assert await ensure_generation(db) is True
    assert await stored_generation(db) == INDEX_GENERATION

    after = await search_library(db, "Adam Daniel")
    assert [hit.series.slug for hit in after.series] == ["feast-of-power"]
    await ensure_index(db)


async def test_a_current_index_is_left_alone(db: AsyncSession) -> None:
    """Every boot calls this; only the first one after a shape change may do any work."""
    from app.services.search.index import ensure_generation, rebuild_index

    await a_series_with_a_cast(db)
    await rebuild_index(db)

    assert await ensure_generation(db) is False


async def test_a_rebuild_that_dies_part_way_is_retried_next_time(db: AsyncSession) -> None:
    """The marker is written last, so a crash leaves the index stale *and* known to be stale.

    The opposite order would mark a half-built index as current and leave the library quietly
    missing rows with nothing to notice it.
    """
    from app.services.search import index as index_module

    await a_series_with_a_cast(db)

    async def explode(session: AsyncSession, series_id: str) -> None:
        raise RuntimeError("disk full")

    real = index_module.reindex_series
    index_module.reindex_series = explode  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await index_module.ensure_generation(db)
    finally:
        index_module.reindex_series = real  # type: ignore[assignment]
    await db.rollback()

    assert await index_module.stored_generation(db) == 0
    assert await index_module.ensure_generation(db) is True
    assert await index_module.stored_generation(db) == index_module.INDEX_GENERATION
