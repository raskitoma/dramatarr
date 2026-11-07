"""Migrations run against data, not just against an empty schema.

The whole suite builds its schema with `Base.metadata.create_all`, so until this
file existed no test ever executed a migration. That gap hid a migration which
destroyed 638,600 episode rows, 10,165 source links and 99,182 tag links on the
real catalogue while leaving `title` itself perfectly intact -- so every
row-count check on the parent table passed.

The mechanism, because it will happen again to anyone touching `title`:
`app/db.py` sets `PRAGMA foreign_keys=ON`, and nine tables declare
`ON DELETE CASCADE` against `title.id`. Alembic's `batch_alter_table` emulates
unsupported SQLite ALTERs by rebuilding the table -- create, copy, DROP the
original, rename -- and under FK enforcement SQLite's `DROP TABLE` performs an
implicit `DELETE FROM` that fires every one of those cascades.

`PRAGMA foreign_keys` cannot be used to escape it either: Alembic runs SQLite
migrations inside a transaction, where that pragma is a silent no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import os
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent


def alembic(db_path: Path, *args: str) -> None:
    """Run alembic exactly the way the container does.

    In-process `alembic.command` is not usable here: `alembic/env.py` builds its
    engine from `app.config.settings.db_url`, which is a cached singleton, so a
    config-supplied URL is ignored and the migration runs against whatever the
    test session already loaded. A subprocess with DRAMADEX_DB_URL set is also
    what `docker-entrypoint.sh` does, so the test exercises the real path.
    """
    env = dict(os.environ, DRAMADEX_DB_URL=f"sqlite:///{db_path}")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(REPO / "alembic.ini"), *args],
        cwd=REPO, env=env, check=True, capture_output=True,
    )


#: The revision immediately before cross-source deduplication.
BEFORE_DEDUP = "c46aa6f834ad"


@pytest.fixture
def populated(tmp_path) -> Path:
    """A database migrated to the revision BEFORE dedup, holding child rows.

    Migrated rather than built with `create_all`, and populated with raw SQL
    rather than the ORM: at this revision `title` has none of the columns the
    current models declare, so an ORM insert would write a schema that does not
    exist yet and the migration under test would never run against the real
    "before" shape.
    """
    db = tmp_path / "populated.db"
    alembic(db, "upgrade", BEFORE_DEDUP)

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO source (id, key, base_url, enabled, rate_json, config_json)"
        " VALUES (1, 'dramabox', 'https://a.test', 1, '{}', '{}')"
    )
    conn.execute(
        "INSERT INTO studio (id, source_id, name, slug) VALUES (1, 1, 'DramaBox', 'dramabox')"
    )
    conn.execute("INSERT INTO tag (id, namespace, value) VALUES (1, 'genre', 'Revenge')")
    for n in range(5):
        conn.execute(
            "INSERT INTO title (id, primary_title, normalized_title, created_at, updated_at)"
            " VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (n + 1, f"Show {n}", f"show {n}"),
        )
        conn.execute(
            "INSERT INTO title_source (title_id, source_id, studio_id, external_id,"
            " first_seen, last_seen, parsed_json) VALUES (?, 1, 1, ?, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00', '{}')",
            (n + 1, f"ext-{n}"),
        )
        conn.execute(
            "INSERT INTO title_tag (title_id, tag_id, source_id) VALUES (?, 1, 1)",
            (n + 1,),
        )
        conn.execute(
            "INSERT INTO rating (title_id, source_id, value, votes, scale,"
            " is_editorial) VALUES (?, 1, 8.0, 5, 10.0, 1)",
            (n + 1,),
        )
        for e in range(10):
            conn.execute(
                "INSERT INTO episode (title_id, source_id, external_id, number,"
                " first_seen) VALUES (?, 1, ?, ?, '2026-01-01 00:00:00')",
                (n + 1, f"ep-{n}-{e}", e),
            )
    conn.commit()
    conn.close()
    return db


def _counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    out = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "title", "title_source", "episode", "title_tag", "rating",
            # `person` is the parent of two more CASCADE children, and the
            # tombstone migration adds columns to it. Same trap, smaller table.
            "person", "person_source", "title_person",
        )
    }
    conn.close()
    return out


def _add_people(db: Path) -> None:
    """Rows under `person`, so a migration touching it has something to destroy."""
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    for n in range(3):
        conn.execute(
            "INSERT INTO person (id, name, normalized_name) VALUES (?, ?, ?)",
            (n + 1, f"Actor {n}", f"actor {n}"),
        )
        conn.execute(
            "INSERT INTO person_source (person_id, source_id, external_id)"
            " VALUES (?, 1, ?)",
            (n + 1, f"p-{n}"),
        )
        conn.execute(
            "INSERT INTO title_person (title_id, person_id, role, billing_order,"
            " source_id) VALUES (1, ?, 'actor', ?, 1)",
            (n + 1, n),
        )
    conn.commit()
    conn.close()


def test_the_dedup_migration_keeps_every_child_row(populated) -> None:
    """The regression. `title` surviving is not evidence of anything -- its rows
    are copied before the drop, so the parent count is the one number a cascading
    wipe leaves untouched."""
    before = _counts(populated)
    assert before["episode"] == 50 and before["title_source"] == 5

    alembic(populated, "upgrade", "head")

    after = _counts(populated)
    assert after == before, f"the migration lost rows: {before} -> {after}"


def test_the_migration_runs_with_foreign_keys_enforced(populated) -> None:
    """The bug only appears when FK enforcement is on, which is how the app opens
    every SQLite connection (app/db.py). A migration test that leaves the pragma
    at its default would pass while production data burned."""
    alembic(populated, "upgrade", "head")

    conn = sqlite3.connect(populated)
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("SELECT count(*) FROM episode").fetchone()[0] == 50
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_the_migration_backfills_the_identity_key(populated) -> None:
    """An unset key makes the ingest hook silently match nothing, and the
    duplicates it was built to prevent come back on the next sync."""
    alembic(populated, "upgrade", "head")

    conn = sqlite3.connect(populated)
    rows = conn.execute(
        "SELECT primary_title, identity_key FROM title ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows[0] == ("Show 0", "show0")
    assert all(key for _, key in rows), "every row must get a key"


def test_the_migration_key_matches_the_runtime_key(populated) -> None:
    """The backfill and the ingest hook must agree byte for byte. An earlier draft
    reimplemented the squash with an ASCII class and wrote '' for a CJK title
    where match_key returns the real characters -- a mismatch that shows up as the
    hook simply never recognising that title again."""
    from app.dedup import match_key

    conn = sqlite3.connect(populated)
    conn.execute(
        "INSERT INTO title (id, primary_title, normalized_title, created_at, updated_at)"
        " VALUES (99, 'Café 都市', 'cafe du shi',"
        " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    alembic(populated, "upgrade", "head")

    conn = sqlite3.connect(populated)
    stored = conn.execute(
        "SELECT identity_key FROM title WHERE primary_title = 'Café 都市'"
    ).fetchone()[0]
    conn.close()
    assert stored == match_key("Café 都市")
    assert stored, "a non-Latin title must not be erased to an empty key"


def test_the_migration_is_reversible(populated) -> None:
    before = _counts(populated)
    alembic(populated, "upgrade", "head")
    alembic(populated, "downgrade", BEFORE_DEDUP)

    assert _counts(populated) == before
    conn = sqlite3.connect(populated)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(title)")]
    conn.close()
    assert "merged_into_id" not in columns


def test_the_person_tombstone_migration_keeps_every_child_row(populated) -> None:
    """`person` is the parent of `person_source` and `title_person`, both
    CASCADE. Adding merged_into_id via batch mode would empty them the same way
    the dedup migration emptied `episode` -- and leave `person` itself intact, so
    the obvious check would pass. Count the children.
    """
    _add_people(populated)
    before = _counts(populated)
    assert before["person"] == 3 and before["person_source"] == 3

    alembic(populated, "upgrade", "head")

    after = _counts(populated)
    assert after == before, f"the migration lost rows: {before} -> {after}"


def test_the_person_tombstone_columns_land(populated) -> None:
    """And it actually did the thing, rather than passing by doing nothing."""
    _add_people(populated)
    alembic(populated, "upgrade", "head")

    conn = sqlite3.connect(populated)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(person)")}
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(person)")}
    conn.close()
    assert {"merged_into_id", "merged_at"} <= columns
    assert "ix_person_merged_into" in indexes


def test_the_person_facts_migration_lands_and_keeps_children(populated) -> None:
    """Adds four columns to `person` and a new CASCADE child beneath it. Same
    rule as everywhere: plain ADD COLUMN, no batch rebuild, children intact."""
    _add_people(populated)
    before = _counts(populated)

    alembic(populated, "upgrade", "head")

    after = _counts(populated)
    assert after == before, f"the migration lost rows: {before} -> {after}"

    conn = sqlite3.connect(populated)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(person)")}
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    conn.close()
    assert {"gender", "height_cm", "weight_kg", "zodiac"} <= columns
    assert "person_alias" in tables


def test_person_alias_is_in_the_backup_order() -> None:
    """A table absent from migrate_engine.ORDER is silently missing from EVERY
    backup -- which already happened once, to `title_merge`, and was caught only
    by a per-table row count. tests/test_backup.py pins the whole list; this
    names the newest table so the failure says which one."""
    from app.jobs.migrate_engine import ORDER

    assert "person_alias" in ORDER
