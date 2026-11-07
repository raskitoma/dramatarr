"""M9: the scheduler daemon."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.jobs import daemon, scheduler
from app.jobs.daemon import Scheduled
from app.models import Base, JobType, RunState, Source, SyncRun


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/d.db", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(engine, expire_on_commit=False, future=True)
    with Sess() as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        s.add(Source(id=2, key="reelshort", base_url="https://b.test", enabled=False))
        s.commit()

    import contextlib

    @contextlib.contextmanager
    def _scope():
        session = Sess()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(daemon, "session_scope", _scope)
    monkeypatch.setattr(scheduler, "session_scope", _scope, raising=False)
    monkeypatch.setattr(daemon.settings, "sync_hour", None)
    # Never actually fork a worker in a unit test.
    monkeypatch.setattr(scheduler, "spawn", lambda run_id: None)
    monkeypatch.setattr(daemon.scheduler, "spawn", lambda run_id: None)
    return Sess


def test_a_due_job_is_started_once_per_day(db, monkeypatch) -> None:
    monkeypatch.setattr(
        daemon, "SCHEDULE", (Scheduled("dramabox", JobType.INCREMENTAL),)
    )

    first = daemon.tick()
    assert len(first["started"]) == 1

    # The second tick must not start it again -- and it is the database that
    # says so, not bookkeeping in the daemon.
    second = daemon.tick()
    assert second["started"] == []
    assert "already ran today" in second["skipped"][0]

    with db() as s:
        assert s.query(SyncRun).count() == 1


def test_a_disabled_source_is_not_scheduled(db, monkeypatch) -> None:
    monkeypatch.setattr(
        daemon, "SCHEDULE", (Scheduled("reelshort", JobType.INCREMENTAL),)
    )
    assert daemon.tick()["started"] == []


def test_weekly_jobs_only_run_on_their_day(db, monkeypatch) -> None:
    today = daemon.settings.today().weekday()
    assert daemon.due_today(Scheduled("dramabox", JobType.ACTOR_ROSTER, weekday=today))
    assert not daemon.due_today(
        Scheduled("dramabox", JobType.ACTOR_ROSTER, weekday=(today + 1) % 7)
    )


def test_sync_hour_delays_the_first_run_of_the_day(db, monkeypatch) -> None:
    """A quiet-hours window: nothing scheduled starts before it."""
    monkeypatch.setattr(
        daemon, "SCHEDULE", (Scheduled("dramabox", JobType.INCREMENTAL),)
    )
    monkeypatch.setattr(daemon.settings, "sync_hour", 25)  # never reached
    assert daemon.tick()["started"] == []


def test_the_tick_reaps_before_it_starts_anything(db, monkeypatch) -> None:
    """Otherwise a source stuck behind a dead worker's lock never recovers."""
    monkeypatch.setattr(daemon, "SCHEDULE", ())
    with db() as s:
        s.add(
            SyncRun(
                source_id=1,
                job_type=JobType.INCREMENTAL,
                run_date=date(2020, 1, 1),
                state=RunState.PENDING,
            )
        )
        s.commit()

    assert len(daemon.tick()["reaped"]) == 1


def test_a_failing_tick_does_not_kill_the_daemon(db, monkeypatch) -> None:
    calls = {"n": 0}

    def boom() -> dict:
        calls["n"] += 1
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(daemon, "tick", boom)
    d = daemon.SchedulerDaemon(interval_seconds=0.05, startup_delay_seconds=0.01)
    d._stop.clear()
    d.start()
    import time

    time.sleep(0.5)
    d.stop()

    assert calls["n"] >= 1, "the loop should have run despite the exception"
