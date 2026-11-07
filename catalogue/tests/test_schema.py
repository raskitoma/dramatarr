"""M0 verification: the once-daily rule is a database invariant.

This is deliberately tested against the real schema rather than mocked. An
advisory check in application code races -- two workers can both read "no run
today" before either writes. A partial unique index cannot.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, JobType, Source, SyncRun


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://www.dramabox.com"))
        s.add(Source(id=2, key="reelshort", base_url="https://www.reelshort.com"))
        s.commit()
        yield s


def scheduled(source_id: int, job_type: str = JobType.INCREMENTAL) -> SyncRun:
    return SyncRun(
        source_id=source_id,
        job_type=job_type,
        run_date=date(2026, 8, 16),
        manual=False,
    )


def test_second_scheduled_run_same_day_is_rejected(session) -> None:
    session.add(scheduled(1))
    session.commit()

    session.add(scheduled(1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_manual_runs_bypass_the_daily_cap(session) -> None:
    """The one stated exception: user-triggered runs are always allowed."""
    session.add(scheduled(1))
    session.commit()

    for _ in range(3):
        session.add(
            SyncRun(
                source_id=1,
                job_type=JobType.INCREMENTAL,
                run_date=date(2026, 8, 16),
                manual=True,
            )
        )
    session.commit()

    assert session.query(SyncRun).count() == 4


def test_different_sources_may_run_the_same_day(session) -> None:
    session.add(scheduled(1))
    session.add(scheduled(2))
    session.commit()

    assert session.query(SyncRun).count() == 2


def test_different_job_types_may_run_the_same_day(session) -> None:
    session.add(scheduled(1, JobType.INCREMENTAL))
    session.add(scheduled(1, JobType.ACTOR_ROSTER))
    session.commit()

    assert session.query(SyncRun).count() == 2


def test_the_same_job_may_run_again_tomorrow(session) -> None:
    session.add(scheduled(1))
    session.commit()

    tomorrow = SyncRun(
        source_id=1,
        job_type=JobType.INCREMENTAL,
        run_date=date(2026, 8, 17),
        manual=False,
    )
    session.add(tomorrow)
    session.commit()

    assert session.query(SyncRun).count() == 2


# --- which database are we on --------------------------------------------------


def test_the_db_url_default_is_sqlite_and_that_is_a_documented_footgun() -> None:
    """Pins the default so a change is deliberate.

    `settings.db_url` falls back to a SQLite file in the data volume, which is
    right for local development and tests. In the container it is a trap: losing
    DRAMADEX_DB_URL does not fail, it silently opens a SQLite file in /data. A
    pre-migration database left there is then migrated forward and served as if
    it were current -- stale data, no error. Production is Postgres; startup logs
    the resolved engine so that mistake is visible in one line.
    """
    from app.config import Settings

    assert Settings().db_url.startswith("sqlite"), (
        "if this default changes, update the startup warning in app/main.py"
    )


def test_the_database_description_hides_the_password() -> None:
    """It is logged on every start, so it must never carry the credential."""
    import app.db as db
    from app.config import settings

    original = settings.db_url
    try:
        settings.db_url = "postgresql+psycopg://user:hunter2@arr-postgres:5432/dramadex"
        described = db.describe_database()
    finally:
        settings.db_url = original

    assert "hunter2" not in described
    assert "user" not in described
    assert "arr-postgres:5432/dramadex" in described
