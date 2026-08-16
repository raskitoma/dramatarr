"""TMDB's episode count, and when it must be refused.

Vertical short drama reaches TMDB as a *stub*: somebody creates the show and one episode and
never comes back. This household's catalogue is full of the consequence — an entry declaring
**1** episode against 66 files on the NAS, another declaring **14** against 62 — and writing
that number through turned the library screen into "66 of 1 available".

That is not a scan in progress. It is the application repeating, in its own voice, a claim it
had everything it needed to check.

There is a second reason this file exists. The guard needs a count from the database, so it
added `func` and `MediaFile` to a module that imported neither — and because both are only
named inside a function body, importing the module still succeeded and the whole suite stayed
green. The same latent NameError this project has already shipped once. The test below walks
the real code path with a real session, which is the only thing that catches that.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Episode, MediaFile, MediaStatus, Series
from app.services.metadata import _apply_series_fields, _episodes_with_files

pytestmark = pytest.mark.anyio


def details(episodes: int) -> dict[str, Any]:
    return {"name": "Whatever", "number_of_episodes": episodes, "overview": "x"}


async def series_with_files(db: AsyncSession, files: int) -> Series:
    series = Series(
        title="You Fired A Genius Surgeon",
        slug="genius-surgeon",
        sort_title="you fired a genius surgeon",
        library_path="You Fired A Genius Surgeon",
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
                path=f"You Fired A Genius Surgeon/E{number}.mp4",
                size_bytes=1,
                status=MediaStatus.PRESENT,
            )
        )
    await db.commit()
    return series


async def test_counting_the_files_runs_against_a_real_session(db: AsyncSession) -> None:
    """The import check. `func` and `MediaFile` are referenced only inside this helper, so a
    missing import surfaces here and nowhere earlier."""
    series = await series_with_files(db, 5)
    assert await _episodes_with_files(db, series.id) == 5


async def test_a_stub_count_below_the_files_is_refused(db: AsyncSession) -> None:
    """The live case: TMDB says 1, the NAS holds 66."""
    series = await series_with_files(db, 66)

    _apply_series_fields(series, details(1), [], "es", episodes_on_disk=66)

    assert series.declared_episode_count is None


async def test_an_honest_count_is_written(db: AsyncSession) -> None:
    """The guard must not swallow the ordinary case, or every conventional series loses its
    total and the "X of Y" line disappears from the whole library."""
    series = await series_with_files(db, 8)

    _apply_series_fields(series, details(12), [], "es", episodes_on_disk=8)

    assert series.declared_episode_count == 12


async def test_a_count_equal_to_what_is_on_disk_is_written(db: AsyncSession) -> None:
    """A complete series is the boundary, and it belongs on the accepted side."""
    series = await series_with_files(db, 12)

    _apply_series_fields(series, details(12), [], "es", episodes_on_disk=12)

    assert series.declared_episode_count == 12


async def test_a_stub_does_not_overwrite_a_good_count(db: AsyncSession) -> None:
    """A refresh that brings a worse number back must leave the better one in place."""
    series = await series_with_files(db, 66)
    series.declared_episode_count = 70

    _apply_series_fields(series, details(1), [], "es", episodes_on_disk=66)

    assert series.declared_episode_count == 70
