"""Renaming files on the library mount.

This is the only module in dramatarr that writes to `LIBRARY_ROOT`, so the tests are weighted
towards what it must **refuse** rather than what it does. The happy path is one rename; the
rest of this file is the reasons a file is left alone.

Four properties carry the safety of the feature, and each has its own test:

* **Never overwrite.** A target that already exists is a collision, always, with no flag to
  force it. Losing an episode to a name clash is the most expensive bug this module could have,
  and it would be invisible until somebody went looking for that episode.
* **Never leave the library.** Source and target both go through `safe_media_path`, which
  resolves symlinks first — so a title crafted to contain `../` cannot address a file outside
  the root, and neither can a symlink pointing out of it.
* **Rename, never move.** The target's parent is taken from the source and never recomputed.
* **The catalogue follows the disk.** `media_files.path` is updated with the rename, per file,
  so an interrupted run leaves the rows agreeing with what is actually there.

The plan is asserted separately from the apply because the plan is what an admin reads before
approving anything: a collision that only showed up in the job log afterwards would be a
different, worse feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Episode, MediaFile, MediaStatus, Series
from app.services import renamer
from app.services.renamer import (
    STATUS_COLLISION,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_UNCHANGED,
    sanitize_filename,
    target_filename,
)

pytestmark = pytest.mark.anyio


# --- Naming -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ruta de Cristal", "Ruta de Cristal"),
        # A separator in a title would change the target's directory. It cannot survive.
        ("Love/Redemption", "Love Redemption"),
        ("Back\\Slash", "Back Slash"),
        ("../../etc/passwd", "etc passwd"),
        # Collapsed to one space rather than deleted, so words do not run together.
        ("Love: Redemption", "Love Redemption"),
        ('He said "no"', "He said no"),
        ("What? Why!", "What Why!"),
        # Windows silently drops trailing dots and spaces, which would make a share
        # round-trip a name into a different one.
        ("Series Name...", "Series Name"),
        ("  padded  ", "padded"),
        ("", ""),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_target_filename_keeps_the_source_extension() -> None:
    """Renaming must never change the container — the file is not being re-encoded."""
    assert (
        target_filename("Ruta de Cristal", 1, 3, "whatever/abc123.mkv")
        == "Ruta de Cristal - S01E03.mkv"
    )
    assert target_filename("Ruta", 2, 11, "a.mp4") == "Ruta - S02E11.mp4"


def test_a_title_that_is_entirely_unsafe_still_produces_a_filename() -> None:
    """`////` must not yield a name of `` — an empty stem is not a file."""
    assert target_filename("////", 1, 1, "a.mp4") == "Series - S01E01.mp4"


# --- Fixtures ------------------------------------------------------------------------------


async def make_series(
    db: AsyncSession,
    tmp_env: Path,
    *,
    title: str = "Ruta de Cristal",
    files: list[tuple[int, int, str]] | None = None,
    create: bool = True,
) -> Series:
    """A series whose episodes point at real files under the temp library root."""
    library = tmp_env / "library"
    series = Series(
        title=title,
        slug="ruta-de-cristal",
        sort_title="ruta de cristal",
        library_path="Ruta de Cristal (2024)",
        year=2024,
    )
    db.add(series)
    await db.flush()

    folder = library / "Ruta de Cristal (2024)"
    folder.mkdir(parents=True, exist_ok=True)

    for season, number, filename in files or [(1, 1, "cdn-object-9f8a7b.mp4")]:
        episode = Episode(
            series_id=series.id, season_number=season, episode_number=number, title=None
        )
        db.add(episode)
        await db.flush()

        relative = f"Ruta de Cristal (2024)/{filename}"
        if create:
            destination = library / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"video-bytes")
        db.add(
            MediaFile(
                episode_id=episode.id,
                path=relative,
                size_bytes=11,
                status=MediaStatus.PRESENT,
            )
        )

    await db.commit()
    return series


def library_files(tmp_env: Path) -> set[str]:
    root = tmp_env / "library"
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# --- The happy path ------------------------------------------------------------------------


async def test_a_cdn_named_file_becomes_its_episode(db: AsyncSession, tmp_env: Path) -> None:
    """The whole point: the names these files arrive with mean nothing."""
    series = await make_series(db, tmp_env)

    plan = await renamer.plan(db, series.id)
    assert [item.status for item in plan.items] == [STATUS_OK]
    assert plan.items[0].proposed_path == (
        "Ruta de Cristal (2024)/Ruta de Cristal - S01E01.mp4"
    )

    tally = await renamer.apply(db, series.id)
    assert tally[STATUS_OK] == 1

    assert library_files(tmp_env) == {
        "Ruta de Cristal (2024)/Ruta de Cristal - S01E01.mp4"
    }
    # The catalogue followed the disk; without this the next stream request 404s.
    media = (await db.get(Series, series.id)) and None
    rows = await db.execute(__import__("sqlalchemy").select(MediaFile.path))
    assert list(rows.scalars()) == ["Ruta de Cristal (2024)/Ruta de Cristal - S01E01.mp4"]
    assert media is None  # keeps the linter honest about the unused fetch


async def test_running_it_twice_changes_nothing_the_second_time(
    db: AsyncSession, tmp_env: Path
) -> None:
    """Idempotent, and it must *say* it is idempotent rather than reporting phantom work."""
    series = await make_series(db, tmp_env)
    await renamer.apply(db, series.id)

    plan = await renamer.plan(db, series.id)
    assert [item.status for item in plan.items] == [STATUS_UNCHANGED]
    assert plan.renamable == []

    tally = await renamer.apply(db, series.id)
    assert tally[STATUS_OK] == 0
    assert tally[STATUS_UNCHANGED] == 1


# --- What it refuses -----------------------------------------------------------------------


async def test_an_existing_target_is_never_overwritten(
    db: AsyncSession, tmp_env: Path
) -> None:
    """The most expensive possible bug, and there is deliberately no flag to force it."""
    series = await make_series(db, tmp_env)
    occupied = tmp_env / "library" / "Ruta de Cristal (2024)" / "Ruta de Cristal - S01E01.mp4"
    occupied.write_bytes(b"something-else-entirely")

    plan = await renamer.plan(db, series.id)
    assert plan.items[0].status == STATUS_COLLISION
    assert plan.renamable == []

    tally = await renamer.apply(db, series.id)
    assert tally[STATUS_OK] == 0
    assert tally[STATUS_COLLISION] == 1
    # Both files are still there, and the occupant is untouched.
    assert occupied.read_bytes() == b"something-else-entirely"
    assert "Ruta de Cristal (2024)/cdn-object-9f8a7b.mp4" in library_files(tmp_env)


async def test_two_files_on_one_episode_cannot_both_take_the_name(
    db: AsyncSession, tmp_env: Path
) -> None:
    """One episode, two files — a duplicate rip, or the same episode in two qualities.

    Both would compute the same target. The second must be refused rather than clobber the
    first, and it has to be refused *in the plan*, before either rename runs, or the admin
    finds out by looking for an episode that is no longer there. (Two *episodes* cannot
    collide: `uq_episode_number` already makes a season/number pair unique.)
    """
    series = await make_series(db, tmp_env, files=[(1, 1, "first.mp4")])
    episode = (
        (
            await db.execute(
                __import__("sqlalchemy").select(Episode).where(Episode.series_id == series.id)
            )
        )
        .scalars()
        .one()
    )
    second = tmp_env / "library" / "Ruta de Cristal (2024)" / "second.mp4"
    second.write_bytes(b"another-rip")
    db.add(
        MediaFile(
            episode_id=episode.id,
            path="Ruta de Cristal (2024)/second.mp4",
            size_bytes=11,
            status=MediaStatus.PRESENT,
        )
    )
    await db.commit()

    plan = await renamer.plan(db, series.id)
    assert sorted(item.status for item in plan.items) == [STATUS_COLLISION, STATUS_OK]

    await renamer.apply(db, series.id)
    remaining = library_files(tmp_env)
    # One got the name, the other kept its own. Two files in, two files out.
    assert "Ruta de Cristal (2024)/Ruta de Cristal - S01E01.mp4" in remaining
    assert len(remaining) == 2


async def test_a_title_full_of_traversal_cannot_address_anything_outside_the_root(
    db: AsyncSession, tmp_env: Path
) -> None:
    """The title is admin-editable free text, so it is an input like any other."""
    series = await make_series(db, tmp_env, title="../../../../etc/cron.d/evil")
    outside = tmp_env / "etc"

    plan = await renamer.plan(db, series.id)
    proposed = plan.items[0].proposed_path
    # Sanitized into a plain filename, still inside the series' own folder.
    assert proposed.startswith("Ruta de Cristal (2024)/")
    assert ".." not in proposed

    await renamer.apply(db, series.id)
    assert not outside.exists()
    assert all(name.startswith("Ruta de Cristal (2024)/") for name in library_files(tmp_env))


async def test_a_file_that_is_not_on_the_mount_is_reported_not_renamed(
    db: AsyncSession, tmp_env: Path
) -> None:
    """A NAS that is unmounted must produce a report, not a pile of exceptions."""
    series = await make_series(db, tmp_env, create=False)

    plan = await renamer.plan(db, series.id)
    assert plan.items[0].status == STATUS_MISSING
    tally = await renamer.apply(db, series.id)
    assert tally[STATUS_MISSING] == 1
    assert tally[STATUS_OK] == 0


async def test_the_rename_stays_in_the_files_own_directory(
    db: AsyncSession, tmp_env: Path
) -> None:
    """Rename, not move: the parent comes from the source and is never recomputed."""
    series = await make_series(
        db, tmp_env, files=[(2, 5, "Season 02/cdn-77.mkv")]
    )
    (tmp_env / "library" / "Ruta de Cristal (2024)" / "Season 02").mkdir(
        parents=True, exist_ok=True
    )
    (tmp_env / "library" / "Ruta de Cristal (2024)" / "Season 02" / "cdn-77.mkv").write_bytes(
        b"x"
    )

    plan = await renamer.plan(db, series.id)
    assert plan.items[0].proposed_path == (
        "Ruta de Cristal (2024)/Season 02/Ruta de Cristal - S02E05.mkv"
    )

    await renamer.apply(db, series.id)
    assert "Ruta de Cristal (2024)/Season 02/Ruta de Cristal - S02E05.mkv" in library_files(
        tmp_env
    )


async def test_an_unknown_series_is_a_clean_error(db: AsyncSession, tmp_env: Path) -> None:
    from app.errors import AppError

    with pytest.raises(AppError) as caught:
        await renamer.plan(db, "nope")
    assert caught.value.code == "SERIES_NOT_FOUND"


# --- Through the API -----------------------------------------------------------------------


async def test_the_plan_route_writes_nothing(
    db: AsyncSession, tmp_env: Path, admin_user: Any
) -> None:
    """A preview that renamed something would be the worst kind of surprise."""
    from httpx import ASGITransport, AsyncClient

    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    series = await make_series(db, tmp_env)
    before = library_files(tmp_env)

    async def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            answer = await http.get(f"/api/library/series/{series.id}/rename-plan")
    finally:
        app.dependency_overrides.clear()

    assert answer.status_code == 200
    body = answer.json()
    assert body["renamableCount"] == 1
    assert body["items"][0]["proposedPath"].endswith("Ruta de Cristal - S01E01.mp4")
    assert library_files(tmp_env) == before


async def test_a_viewer_cannot_rename_anything(
    db: AsyncSession, tmp_env: Path, viewer_user: Any
) -> None:
    from httpx import ASGITransport, AsyncClient

    from app.api.deps import get_current_user
    from app.db import get_session
    from app.main import app

    series = await make_series(db, tmp_env)

    async def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: viewer_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            plan = await http.get(f"/api/library/series/{series.id}/rename-plan")
            run = await http.post(f"/api/library/series/{series.id}/rename")
    finally:
        app.dependency_overrides.clear()

    assert plan.status_code == 403
    assert run.status_code == 403
    assert library_files(tmp_env) == {"Ruta de Cristal (2024)/cdn-object-9f8a7b.mp4"}
