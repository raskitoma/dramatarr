"""Scanning only what moved — and, above all, not marking the rest missing.

The nightly scan walked the whole mount and stat'd all 1141 files on a network volume to learn
that nothing had changed. A directory's mtime moves when a file is added, removed or renamed
inside it, which is the entire set of events this scan exists to notice, so one stat per folder
does the same work.

**Most of this file is about `_sweep_missing`.** It marks every known file it did not see, and
an incremental scan sees none of the files in a skipped folder. Unscoped, the first incremental
run would mark the whole library missing — silently, and looking exactly like the mount having
gone away.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaStatus, ScanDirectory

pytestmark = pytest.mark.anyio


def make_library(root: Path) -> None:
    for series, episodes in [("Alpha (2025)", 3), ("Beta (2025)", 2)]:
        folder = root / series / "Season 01"
        folder.mkdir(parents=True)
        for number in range(1, episodes + 1):
            (folder / f"{series.split(' (')[0]} - S01E{number:02d}.mp4").write_bytes(b"x" * 64)


async def scan(db: AsyncSession, *, incremental: bool = False):
    from app.services.scanner.scan import scan_library

    return await scan_library(db, incremental=incremental)


@pytest.fixture
def library(tmp_env: Path) -> Path:
    from app.config import get_settings

    root = Path(get_settings().library_root)
    root.mkdir(parents=True, exist_ok=True)
    make_library(root)
    return root


async def live_paths(db: AsyncSession) -> set[str]:
    """Files the scan still believes are on the mount.

    Not `PRESENT`: these fixtures are 64 bytes of nonsense, so ffprobe rejects them and they
    land as `error`. That is fine and even useful — what this file is about is whether an
    incremental pass wrongly flips a row to `MISSING`, and `error` rows are swept exactly like
    healthy ones.
    """
    from app.models import MediaFile

    rows = (await db.execute(select(MediaFile))).scalars().all()
    return {row.path for row in rows if row.status != MediaStatus.MISSING}


# --- The thing that must not happen -----------------------------------------------------------


async def test_an_incremental_rescan_does_not_empty_the_library(
    db: AsyncSession, library: Path
) -> None:
    """The whole reason the sweep is scoped. Every file is in a folder the second pass skips."""
    await scan(db)
    before = await live_paths(db)
    assert len(before) == 5

    report = await scan(db, incremental=True)

    assert await live_paths(db) == before
    assert report.files_missing == 0


async def test_it_actually_skips(db: AsyncSession, library: Path) -> None:
    """Otherwise the test above would pass for the wrong reason — a scan that quietly examined
    everything would also keep the files present."""
    await scan(db)

    report = await scan(db, incremental=True)

    # Nothing moved, so no file was looked at twice.
    assert report.files_seen == 0


# --- And what it must still notice -------------------------------------------------------------


async def test_a_new_episode_in_a_touched_folder_is_found(
    db: AsyncSession, library: Path
) -> None:
    await scan(db)
    (library / "Alpha (2025)" / "Season 01" / "Alpha - S01E04.mp4").write_bytes(b"x" * 64)

    await scan(db, incremental=True)

    assert any(path.endswith("S01E04.mp4") for path in await live_paths(db))


async def test_a_deleted_file_is_marked_missing(db: AsyncSession, library: Path) -> None:
    await scan(db)
    (library / "Beta (2025)" / "Season 01" / "Beta - S01E02.mp4").unlink()

    report = await scan(db, incremental=True)

    assert report.files_missing == 1
    assert not any(path.endswith("Beta - S01E02.mp4") for path in await live_paths(db))


async def test_a_deleted_folder_is_marked_missing(db: AsyncSession, library: Path) -> None:
    """The case a naive scoping misses: the folder is gone, so it is in neither the scanned set
    nor the walk at all, and its files would otherwise stay 'present' for ever."""
    import shutil

    await scan(db)
    shutil.rmtree(library / "Beta (2025)")

    report = await scan(db, incremental=True)

    assert report.files_missing == 2
    assert all("Beta" not in path for path in await live_paths(db))


async def test_a_brand_new_series_is_found(db: AsyncSession, library: Path) -> None:
    await scan(db)
    folder = library / "Gamma (2026)" / "Season 01"
    folder.mkdir(parents=True)
    (folder / "Gamma - S01E01.mp4").write_bytes(b"x" * 64)

    await scan(db, incremental=True)

    assert any("Gamma" in path for path in await live_paths(db))


# --- The bookkeeping ---------------------------------------------------------------------------


async def test_the_first_scan_records_every_folder(db: AsyncSession, library: Path) -> None:
    await scan(db)

    rows = (await db.execute(select(ScanDirectory))).scalars().all()
    paths = {row.rel_path for row in rows}
    assert "Alpha (2025)" in paths
    assert "Alpha (2025)/Season 01" in paths


async def test_a_complete_scan_forgets_folders_that_are_gone(
    db: AsyncSession, library: Path
) -> None:
    """A merge-only write would keep a deleted folder alive in the table for ever, and its
    stale mtime would never match anything again — harmless but untrue."""
    import shutil

    await scan(db)
    shutil.rmtree(library / "Beta (2025)")

    await scan(db)

    rows = (await db.execute(select(ScanDirectory))).scalars().all()
    assert all("Beta" not in row.rel_path for row in rows)


async def test_full_overrides_incremental(db: AsyncSession, library: Path) -> None:
    """`full` means re-examine everything; asking for both is asking for the thorough one."""
    from app.services.scanner.scan import scan_library

    await scan(db)

    report = await scan_library(db, incremental=True, full=True)

    assert report.files_seen == 5


async def test_a_touched_folder_alone_is_re_examined(db: AsyncSession, library: Path) -> None:
    """Only the folder that moved, not its siblings."""
    await scan(db)
    target = library / "Alpha (2025)" / "Season 01"
    (target / "Alpha - S01E04.mp4").write_bytes(b"x" * 64)
    os.utime(target, None)

    report = await scan(db, incremental=True)

    # Four in Alpha's season folder; Beta's two were skipped entirely.
    assert report.files_seen == 4
