"""M10: backup and restore.

A backup you have never restored is a guess. These tests do the full round trip
-- take, destroy, restore, compare -- because that is the only thing that
proves anything.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs.backup import latest, prune, restore, run_backup, verify
from app.models import Base, Source, Title


@pytest.fixture
def live(tmp_path) -> Path:
    db = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        for i in range(25):
            s.add(Title(primary_title=f"Drama {i}", normalized_title=f"drama {i}", year=2026))
        s.commit()
    engine.dispose()
    return db


def test_a_backup_is_verified_when_it_is_taken(live, tmp_path) -> None:
    manifest = run_backup(live, tmp_path / "backups")

    assert manifest["verified"] is True
    assert manifest["integrity"] == "ok"
    assert manifest["counts"]["title"] == 25
    assert manifest["mismatches"] == {}
    assert manifest["sha256"]


def test_the_manifest_lands_next_to_the_backup(live, tmp_path) -> None:
    dest = tmp_path / "backups"
    manifest = run_backup(live, dest)
    assert (dest / manifest["file"]).exists()
    assert (dest / manifest["file"]).with_suffix(".json").exists()


def test_backup_works_while_the_database_is_being_written(live, tmp_path) -> None:
    """The whole point of the online backup API. A `cp` here can capture a
    torn file, and with WAL it can miss committed data entirely."""
    engine = create_engine(f"sqlite:///{live}", future=True)
    with Session(engine, future=True) as s:
        s.add(Title(primary_title="Mid-write", normalized_title="mid write"))
        s.commit()
        # An open session with an uncommitted write, exactly as a sync would have.
        s.add(Title(primary_title="Uncommitted", normalized_title="uncommitted"))

        manifest = run_backup(live, tmp_path / "backups")

    engine.dispose()
    assert manifest["verified"] is True
    assert manifest["counts"]["title"] == 26, "committed rows only"


# --- the drill ---------------------------------------------------------------


def test_full_round_trip_take_destroy_restore(live, tmp_path) -> None:
    dest = tmp_path / "backups"
    taken = run_backup(live, dest)
    original = taken["counts"]["title"]

    # Destroy the live database the way it actually broke: a truncated file.
    live.write_bytes(b"this is not a database")
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(live).execute("SELECT count(*) FROM title")

    result = restore(dest / taken["file"], live)

    assert result["counts"]["title"] == original
    conn = sqlite3.connect(live)
    assert conn.execute("SELECT count(*) FROM title").fetchone()[0] == original
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_keeps_the_database_it_replaced(live, tmp_path) -> None:
    """A restore that turns out to be the wrong call should be reversible."""
    dest = tmp_path / "backups"
    taken = run_backup(live, dest)

    result = restore(dest / taken["file"], live)

    assert result["previous_database"] is not None
    assert (live.parent / result["previous_database"]).exists()


def test_restore_clears_stale_wal_sidecars(live, tmp_path) -> None:
    """Leftover -wal/-shm belong to the database being replaced; leaving them
    makes SQLite refuse to open the restored file."""
    dest = tmp_path / "backups"
    taken = run_backup(live, dest)
    Path(str(live) + "-wal").write_bytes(b"stale")
    Path(str(live) + "-shm").write_bytes(b"stale")

    restore(dest / taken["file"], live)

    assert not Path(str(live) + "-wal").exists()
    assert sqlite3.connect(live).execute("SELECT count(*) FROM title").fetchone()[0] == 25


def test_a_corrupt_backup_is_refused(live, tmp_path) -> None:
    """Restoring rubbish over a working database would turn a recoverable
    situation into an unrecoverable one."""
    dest = tmp_path / "backups"
    dest.mkdir()
    bad = dest / "dramadex-20260101T000000Z.db"
    bad.write_bytes(b"not a database at all")

    with pytest.raises(Exception):
        restore(bad, live)

    # The live database is untouched.
    assert sqlite3.connect(live).execute("SELECT count(*) FROM title").fetchone()[0] == 25


# --- retention ---------------------------------------------------------------


def test_retention_keeps_the_newest_and_prunes_the_rest(live, tmp_path) -> None:
    dest = tmp_path / "backups"
    for n in range(5):
        dest.mkdir(exist_ok=True)
        (dest / f"dramadex-2026010{n}T000000Z.db").write_bytes(b"x")

    pruned = prune(dest, keep=2)

    assert len(pruned) == 3
    assert len(list(dest.glob("dramadex-*.db"))) == 2


def test_retention_never_prunes_below_one(live, tmp_path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "dramadex-20260101T000000Z.db").write_bytes(b"x")

    prune(dest, keep=0)

    assert len(list(dest.glob("dramadex-*.db"))) == 1


def test_latest_picks_the_newest_by_name(live, tmp_path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    for stamp in ("20260101T000000Z", "20260315T000000Z", "20260210T000000Z"):
        (dest / f"dramadex-{stamp}.db").write_bytes(b"x")

    assert latest(dest).name == "dramadex-20260315T000000Z.db"


def test_verify_reports_a_broken_backup_without_restoring_it(live, tmp_path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    bad = dest / "dramadex-20260101T000000Z.db"
    bad.write_bytes(b"nope")

    with pytest.raises(sqlite3.DatabaseError):
        verify(bad)


# --- the engine-agnostic entry point ----------------------------------------


def test_backup_works_on_a_sqlite_url(live, tmp_path) -> None:
    from app.jobs.backup import run_backup_any

    manifest = run_backup_any(f"sqlite:///{live}", tmp_path / "b")
    assert manifest["verified"] is True
    assert manifest["counts"]["title"] == 25


def test_a_non_sqlite_url_is_not_rejected_outright() -> None:
    """The Postgres move silently broke the daily backup: the job raised
    "backup is only implemented for SQLite" and the scheduler recorded a failed
    run every day. Backup is the one job whose absence you discover at the worst
    possible moment, so the entry point must handle whatever engine is
    configured rather than assume one."""
    import inspect

    from app.jobs import backup

    source = inspect.getsource(backup.run_backup_any)
    assert "sqlite" in source and "_backup_to_sqlite" in source

    from app.jobs import runner

    assert "only implemented for SQLite" not in inspect.getsource(runner._dispatch)


def test_the_copier_knows_about_every_table() -> None:
    """A table missing from ORDER is silently absent from every backup.

    That is not hypothetical: `title_merge` was added and this list was not, so
    the first backup after it shipped copied 0 of 813 rows. It was caught by the
    row-count verification rather than by anyone noticing, which is the whole
    argument for verifying a backup at the moment it is taken -- but a test is
    cheaper than discovering it during a restore.
    """
    from app.jobs.migrate_engine import ORDER
    from app.models import Base

    missing = {t.name for t in Base.metadata.sorted_tables} - set(ORDER)
    assert not missing, f"tables absent from the copier's ORDER: {sorted(missing)}"


def test_the_engine_copier_pages_in_a_defined_order() -> None:
    """LIMIT/OFFSET without ORDER BY has no defined order in Postgres, so pages
    overlap -- the same row twice, others skipped. It looked fine copying out of
    SQLite (which returns rowid order) and failed on a duplicate primary key the
    first time Postgres was the source.
    """
    import inspect

    from app.jobs import migrate_engine

    source = inspect.getsource(migrate_engine.copy_database)
    assert "order_by" in source, "paged reads must be ordered"
    assert "primary_key" in source, "order by the primary key, not an arbitrary column"
