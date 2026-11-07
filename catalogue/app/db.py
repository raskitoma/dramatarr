"""Engine and session setup.

Nothing dialect-specific belongs outside this module and `app/search.py` --
that separation is what keeps the SQLite -> Postgres move cheap.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        # busy_timeout. SQLite allows exactly one writer, and this project runs
        # jobs for different sources concurrently by design. Short transactions
        # are the real fix (see catalog.py); this is the margin for when two
        # workers still collide.
        eng = create_engine(url, future=True, connect_args={"timeout": 120})

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            # WAL lets the API read while a sync worker writes.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng

    return create_engine(url, future=True, pool_pre_ping=True)


engine = _make_engine(settings.db_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)


def describe_database() -> str:
    """The engine and where it points, with the password removed.

    Logged at startup so "which database am I on?" is answered by the log rather
    than by reasoning about which env vars were set.
    """
    url = settings.db_url
    scheme, _, rest = url.partition("://")
    if "@" in rest:  # strip user:password@
        rest = rest.rsplit("@", 1)[-1]
    return f"{scheme} -> {rest}"


def integrity_check() -> str:
    """Report SQLite's own verdict on the database file.

    Worth doing at startup because corruption presents as confusing errors deep
    inside unrelated queries. It happened here: running the CLI on the host
    against the container's bind-mounted database corrupted it, because SQLite
    WAL needs shared memory and file locking that macOS bind mounts do not
    fully provide. Fail loudly instead.
    """
    if not settings.db_url.startswith("sqlite"):
        return "ok"
    try:
        with engine.connect() as conn:
            return conn.exec_driver_sql("PRAGMA quick_check").scalar() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"unreadable: {exc}"


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
