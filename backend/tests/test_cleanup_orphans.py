"""Cleanup of unmatched media rows whose file is gone.

A row with `episode_id IS NULL` belongs to no episode, so the episode-centric half of cleanup
can never reach it. Renaming a folder produces exactly these: the old paths linger as
`missing` unmatched rows and would otherwise sit in the admin's view forever, naming files
that no longer exist.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import MediaFile, MediaStatus
from app.services.library import cleanup_catalogue


async def _media_rows(db) -> int:
    return await db.scalar(select(func.count()).select_from(MediaFile))

pytestmark = pytest.mark.anyio


async def _unmatched(db, path: str, status: str) -> MediaFile:
    row = MediaFile(episode_id=None, path=path, size_bytes=1, status=status)
    db.add(row)
    await db.flush()
    return row


async def test_orphan_missing_row_is_removed(db, tmp_env) -> None:
    await _unmatched(db, "Show/old-cdn-name-0001.mp4", MediaStatus.MISSING)
    await db.commit()

    result = await cleanup_catalogue(db, dry_run=False)
    await db.commit()

    assert result.removed_media_files == 1
    assert await _media_rows(db) == 0


async def test_dry_run_reports_but_keeps_the_row(db, tmp_env) -> None:
    await _unmatched(db, "Show/old-cdn-name-0001.mp4", MediaStatus.MISSING)
    await db.commit()

    preview = await cleanup_catalogue(db, dry_run=True)
    assert preview.removed_media_files == 1
    assert preview.dry_run is True

    assert await _media_rows(db) == 1

    real = await cleanup_catalogue(db, dry_run=False)
    await db.commit()
    assert real.removed_media_files == preview.removed_media_files


async def test_unmatched_row_whose_file_is_present_is_kept(db, tmp_env) -> None:
    """An unparseable file that is really on the mount is a curation task, not debris."""
    from app.config import get_settings

    root = get_settings().library_root
    target = root / "Show" / "mystery.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"data")

    await _unmatched(db, "Show/mystery.mp4", MediaStatus.PRESENT)
    await db.commit()

    result = await cleanup_catalogue(db, dry_run=False)
    await db.commit()

    assert result.removed_media_files == 0
    assert await _media_rows(db) == 1


async def test_row_marked_missing_but_back_on_disk_is_kept(db, tmp_env) -> None:
    """`missing` is the scanner's last opinion; the filesystem overrules it."""
    from app.config import get_settings

    root = get_settings().library_root
    target = root / "Show" / "returned.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"data")

    await _unmatched(db, "Show/returned.mp4", MediaStatus.MISSING)
    await db.commit()

    result = await cleanup_catalogue(db, dry_run=False)
    await db.commit()

    assert result.removed_media_files == 0


async def test_cleanup_never_touches_the_library(db, tmp_env) -> None:
    from app.config import get_settings

    root = get_settings().library_root
    keep = root / "Show" / "keeper.mp4"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(b"payload")

    await _unmatched(db, "Show/keeper.mp4", MediaStatus.MISSING)
    await _unmatched(db, "Show/vanished.mp4", MediaStatus.MISSING)
    await db.commit()

    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    await cleanup_catalogue(db, dry_run=False)
    await db.commit()
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    assert before == after
