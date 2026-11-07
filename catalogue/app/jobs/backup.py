"""Backup and restore.

Uses SQLite's online backup API, not a file copy. `cp` of a live database is
the classic way to end up with a corrupt backup: pages can change underneath
the copy, and with WAL enabled the committed data may live in the -wal file
that a naive copy misses entirely. `Connection.backup()` is safe against a
database being written to.

Every backup is verified immediately after it is taken -- an unverified backup
is a guess, and you find out it was wrong on the day you need it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

TABLES = (
    "title",
    "title_source",
    "episode",
    "person",
    "title_person",
    "tag",
    "title_tag",
    "rating",
    "image",
    "title_image",
    "match_candidate",
    "sync_run",
)


def _sqlite_path(db_url: str) -> Path | None:
    if not db_url.startswith("sqlite"):
        return None
    return Path(db_url.split("sqlite:///")[-1].lstrip("/")).resolve() if "///" in db_url else None


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in TABLES:
        try:
            out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = -1
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_backup_any(db_url: str, dest_dir: Path, *, keep: int = 7) -> dict:
    """Back up whatever engine is configured.

    Postgres is backed up by copying it into a timestamped SQLite file rather
    than shelling out to pg_dump. Reasons, in order of weight:

      * it reuses the engine copier that is already tested and row-count
        verified, so a backup keeps the property that matters -- it is checked
        when taken, not hoped about;
      * no pg_dump in the image, and no risk of a client/server version
        mismatch against a server we are told not to major-bump;
      * the result is a single portable file readable without any server, which
        is what you want at 3am.

    Slower than pg_dump (~2 min for 636k rows) and that is an acceptable trade
    for a daily job.
    """
    if db_url.startswith("sqlite"):
        path = Path(db_url.split("sqlite:///", 1)[-1])
        return run_backup(path, dest_dir, keep=keep)
    return _backup_to_sqlite(db_url, dest_dir, keep=keep)


def _backup_to_sqlite(db_url: str, dest_dir: Path, *, keep: int) -> dict:
    from app.jobs.migrate_engine import copy_database, verify

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"dramadex-{stamp}.db"
    target_url = f"sqlite:///{target}"

    copy_database(db_url, target_url)
    report = verify(db_url, target_url)

    check = sqlite3.connect(target)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        counts = _counts(check)
    finally:
        check.close()

    manifest = {
        "file": target.name,
        "source_engine": db_url.split("://")[0],
        "taken_at": datetime.now(UTC).isoformat(),
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
        "integrity": integrity,
        "counts": counts,
        "mismatches": report["mismatches"],
        "verified": integrity == "ok" and not report["mismatches"],
    }
    target.with_suffix(".json").write_text(json.dumps(manifest, indent=2, default=str))

    if not manifest["verified"]:
        log.error("backup verification FAILED: %s", manifest["mismatches"])
    else:
        log.info("backup verified: %s (%s bytes)", target.name, manifest["bytes"])

    manifest["pruned"] = prune(dest_dir, keep=keep)
    return manifest


def run_backup(db_path: Path, dest_dir: Path, *, keep: int = 7) -> dict:
    """Take a verified hot backup of a SQLite database. Returns the manifest."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"dramadex-{stamp}.db"

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        backup = sqlite3.connect(target)
        try:
            source.backup(backup)
        finally:
            backup.close()
        live_counts = _counts(source)
    finally:
        source.close()

    # Verify the copy on its own terms rather than trusting that it worked.
    check = sqlite3.connect(target)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        backup_counts = _counts(check)
    finally:
        check.close()

    mismatches = {
        table: (live_counts.get(table), backup_counts.get(table))
        for table in TABLES
        if live_counts.get(table) != backup_counts.get(table)
    }

    manifest = {
        "file": target.name,
        "taken_at": datetime.now(UTC).isoformat(),
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
        "integrity": integrity,
        "counts": backup_counts,
        "mismatches": mismatches,
        "verified": integrity == "ok" and not mismatches,
    }
    target.with_suffix(".json").write_text(json.dumps(manifest, indent=2))

    if not manifest["verified"]:
        log.error("backup verification FAILED: %s", manifest)
    else:
        log.info("backup verified: %s (%s bytes)", target.name, manifest["bytes"])

    manifest["pruned"] = prune(dest_dir, keep=keep)
    return manifest


def prune(dest_dir: Path, *, keep: int) -> list[str]:
    """Keep the newest `keep` backups. Never prune below one."""
    backups = sorted(dest_dir.glob("dramadex-*.db"))
    doomed = backups[: max(0, len(backups) - max(1, keep))]
    removed = []
    for path in doomed:
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
        removed.append(path.name)
    return removed


def verify(backup_path: Path) -> dict:
    """Check a backup without restoring it."""
    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        return {
            "file": backup_path.name,
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "counts": _counts(conn),
        }
    finally:
        conn.close()


def restore(backup_path: Path, db_path: Path) -> dict:
    """Replace the live database with a backup.

    The current database is moved aside rather than deleted -- a restore that
    turns out to be the wrong choice should still be reversible.
    """
    if not backup_path.exists():
        raise FileNotFoundError(backup_path)

    checked = verify(backup_path)
    if checked["integrity"] != "ok":
        raise RuntimeError(f"refusing to restore a corrupt backup: {checked['integrity']}")

    superseded = None
    if db_path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        superseded = db_path.with_suffix(f".superseded-{stamp}")
        db_path.replace(superseded)
    # Stale WAL/SHM belong to the database we just moved; leaving them behind
    # makes SQLite refuse to open the restored file.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        sidecar.unlink(missing_ok=True)

    target = sqlite3.connect(db_path)
    try:
        source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        try:
            source.backup(target)
        finally:
            source.close()
    finally:
        target.close()

    return {
        "restored_from": backup_path.name,
        "counts": checked["counts"],
        "previous_database": superseded.name if superseded else None,
    }


def latest(dest_dir: Path) -> Path | None:
    backups = sorted(dest_dir.glob("dramadex-*.db"))
    return backups[-1] if backups else None
