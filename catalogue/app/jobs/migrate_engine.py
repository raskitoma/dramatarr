"""Copy the whole database from one engine to another.

Written for the SQLite -> Postgres move, but engine-agnostic: it reads through
the ORM metadata rather than dumping SQL, so it does not care about dialect
syntax and cannot smuggle SQLite-isms into Postgres.

Order matters. Tables are copied in dependency order so foreign keys are
satisfied as they are written, rather than by disabling constraints and hoping.
Sequences are reset afterwards, because Postgres does not learn the next id from
rows inserted with explicit primary keys -- miss that and the first write after
a migration fails on a duplicate key.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.orm import Session

from app.models import Base

log = logging.getLogger(__name__)

#: Dependency order. Parents before children.
ORDER = (
    "source",
    "studio",
    "title",
    "image",
    "title_source",
    "title_alias",
    "title_change",
    "person",
    "person_alias",
    "person_source",
    "episode",
    "title_person",
    "tag",
    "title_tag",
    "title_image",
    "rating",
    "match_candidate",
    # After `title`: both of its FKs point there. Adding a table and forgetting
    # this list means it is silently absent from every backup -- which is what
    # happened here, and was caught only because run_backup_any compares row
    # counts table by table. tests/test_backup.py pins the list against the ORM.
    "title_merge",
    "sync_run",
    "sync_progress",
    "source_state",
    "parse_quarantine",
    "alembic_version",
)

BATCH = 2000


def copy_database(source_url: str, target_url: str, *, progress=None) -> dict:
    src_engine = create_engine(source_url, future=True)
    dst_engine = create_engine(target_url, future=True)

    stats: dict[str, int] = {}
    tables = {t.name: t for t in Base.metadata.sorted_tables}

    with dst_engine.begin() as conn:
        Base.metadata.create_all(conn)

    with Session(src_engine, future=True) as src, Session(dst_engine, future=True) as dst:
        for name in ORDER:
            table = tables.get(name)
            if table is None:
                # alembic_version is not in the ORM metadata; copy it by hand so
                # the target does not try to re-run every migration.
                if name == "alembic_version":
                    stats[name] = _copy_alembic_version(src, dst)
                continue

            total = src.scalar(select(func.count()).select_from(table)) or 0
            if total == 0:
                stats[name] = 0
                continue

            # ORDER BY is mandatory, not tidiness. LIMIT/OFFSET without it has
            # no defined order in Postgres, so successive pages overlap: the
            # same row arrives twice and others are skipped. It only looked
            # correct copying OUT of SQLite, which happens to return rowid
            # order. Copying out of Postgres failed on a duplicate primary key.
            order = list(table.primary_key.columns) or [list(table.columns)[0]]

            copied = 0
            while copied < total:
                rows = [
                    dict(r._mapping)
                    for r in src.execute(
                        select(table).order_by(*order).limit(BATCH).offset(copied)
                    )
                ]
                if not rows:
                    break
                dst.execute(insert(table), rows)
                dst.commit()
                copied += len(rows)
                if progress:
                    progress(name, copied, total)
            stats[name] = copied
            log.info("copied %s: %s rows", name, copied)

        if dst_engine.dialect.name == "postgresql":
            stats["sequences_reset"] = _reset_sequences(dst, tables)

    return stats


def _copy_alembic_version(src: Session, dst: Session) -> int:
    try:
        version = src.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return 0
    if not version:
        return 0
    dst.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version"
            " (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
    )
    dst.execute(text("DELETE FROM alembic_version"))
    dst.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                {"v": version})
    dst.commit()
    return 1


def _reset_sequences(dst: Session, tables: dict) -> int:
    """Point each identity sequence past the highest id we just inserted.

    Rows were written with explicit primary keys, so Postgres' sequences are
    still at 1 and the very next insert would collide.
    """
    reset = 0
    for name, table in tables.items():
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not pk[0].autoincrement:
            continue
        column = pk[0].name
        dst.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{name}', '{column}'),"
                f" COALESCE((SELECT MAX({column}) FROM {name}), 1), true)"
            )
        )
        reset += 1
    dst.commit()
    return reset


def verify(source_url: str, target_url: str) -> dict:
    """Compare row counts table by table. A migration nobody counted is a hope."""
    src_engine = create_engine(source_url, future=True)
    dst_engine = create_engine(target_url, future=True)
    report: dict[str, tuple[int, int]] = {}
    with Session(src_engine, future=True) as src, Session(dst_engine, future=True) as dst:
        for table in Base.metadata.sorted_tables:
            a = src.scalar(select(func.count()).select_from(table)) or 0
            b = dst.scalar(select(func.count()).select_from(table)) or 0
            report[table.name] = (a, b)
    return {
        "tables": report,
        "mismatches": {k: v for k, v in report.items() if v[0] != v[1]},
    }
