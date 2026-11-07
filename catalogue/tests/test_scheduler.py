"""M5: run lifecycle -- creation caps, reaping, cancellation."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.jobs import locks, scheduler
from app.jobs.scheduler import JobRejected
from app.models import (
    Base,
    JobType,
    RunState,
    Source,
    SourceState,
    SyncProgress,
    SyncRun,
)


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/sched.db", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(engine, expire_on_commit=False, future=True)
    with Sess() as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        s.add(Source(id=2, key="reelshort", base_url="https://b.test", enabled=False))
        s.commit()
        yield s


# --- creation caps -----------------------------------------------------------


def test_a_second_scheduled_run_today_is_rejected_with_a_reason(session) -> None:
    scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)

    with pytest.raises(JobRejected, match="already ran today"):
        scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)


def test_the_session_is_usable_after_a_rejection(session) -> None:
    """A failed insert must roll back cleanly, or the next call blows up for an
    unrelated reason."""
    scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)
    with pytest.raises(JobRejected):
        scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)

    run = scheduler.create_run(session, "dramabox", JobType.FULL_CATALOG)
    assert run.id is not None


def test_manual_runs_bypass_the_daily_cap(session) -> None:
    scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)
    for _ in range(3):
        scheduler.create_run(session, "dramabox", JobType.INCREMENTAL, manual=True)

    assert session.query(SyncRun).count() == 4


def test_a_disabled_source_refuses_scheduled_runs_but_allows_manual(session) -> None:
    with pytest.raises(JobRejected, match="disabled"):
        scheduler.create_run(session, "reelshort", JobType.INCREMENTAL)

    assert scheduler.create_run(session, "reelshort", JobType.INCREMENTAL, manual=True)


def test_unknown_source_is_rejected(session) -> None:
    with pytest.raises(JobRejected, match="unknown source"):
        scheduler.create_run(session, "nope", JobType.INCREMENTAL)


# --- cancellation ------------------------------------------------------------


def test_cancel_marks_a_running_run(session) -> None:
    run = scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)
    run.state = RunState.RUNNING
    session.commit()

    assert scheduler.request_cancel(session, run.id) is True
    session.refresh(run)
    assert run.cancel_requested is True


def test_cancelling_a_finished_run_does_nothing(session) -> None:
    run = scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)
    run.state = RunState.SUCCEEDED
    session.commit()

    assert scheduler.request_cancel(session, run.id) is False


# --- reaping -----------------------------------------------------------------


def _running(session, *, pid: int, age_seconds: int) -> SyncRun:
    run = scheduler.create_run(session, "dramabox", JobType.INCREMENTAL, manual=True)
    run.state = RunState.RUNNING
    run.pid = pid
    run.started_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    session.add(
        SyncProgress(
            run_id=run.id,
            items_done=7,
            heartbeat_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
    )
    locks.acquire(session, 1, pid=pid)
    session.commit()
    return run


def test_a_dead_workers_run_is_reaped_and_its_lock_released(session) -> None:
    """Without this a SIGKILLed worker leaves the row RUNNING forever and its
    source locked forever with it."""
    dead = _dead_pid()
    run = _running(session, pid=dead, age_seconds=600)

    reaped = scheduler.reap_stale(session)

    assert reaped == [run.id]
    session.refresh(run)
    assert run.state == RunState.FAILED
    assert run.finished_at is not None
    assert "died" in run.error
    assert locks.holder(session, 1) is None


def test_a_live_worker_is_never_reaped(session) -> None:
    run = _running(session, pid=os.getpid(), age_seconds=600)

    assert scheduler.reap_stale(session) == []
    session.refresh(run)
    assert run.state == RunState.RUNNING


def test_a_freshly_started_worker_is_not_reaped_before_its_first_heartbeat(session) -> None:
    """A worker that has started but not yet written a heartbeat must get one
    timeout window, or the scheduler kills its own children at startup."""
    run = _running(session, pid=_dead_pid(), age_seconds=1)

    assert scheduler.reap_stale(session) == []
    session.refresh(run)
    assert run.state == RunState.RUNNING


def test_reaping_leaves_finished_runs_alone(session) -> None:
    run = scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)
    run.state = RunState.SUCCEEDED
    session.commit()

    assert scheduler.reap_stale(session) == []
    session.refresh(run)
    assert run.state == RunState.SUCCEEDED


def _dead_pid() -> int:
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    p = ctx.Process(target=_noop)
    p.start()
    p.join()
    return p.pid


def _noop() -> None:
    return None


# --- reaping backstops (review findings) -------------------------------------


def test_an_orphaned_pending_run_is_reaped(session) -> None:
    """A worker that dies before claiming its run left the row PENDING for
    ever -- and the partial unique index counts it, so that source's scheduled
    slot was burned for the rest of the day."""
    run = scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)
    run.started_at = datetime.now(UTC) - scheduler.PENDING_TIMEOUT - timedelta(minutes=1)
    session.commit()

    assert scheduler.reap_stale(session) == [run.id]
    session.refresh(run)
    assert run.state == RunState.FAILED
    assert "never started" in run.error


def test_a_recent_pending_run_is_left_alone(session) -> None:
    run = scheduler.create_run(session, "dramabox", JobType.INCREMENTAL)
    run.started_at = datetime.now(UTC)
    session.commit()

    assert scheduler.reap_stale(session) == []


def test_an_ancient_run_is_reaped_even_though_its_pid_looks_alive(session) -> None:
    """PID reuse: a recycled pid makes a dead worker look alive for ever, and a
    zombie child still answers kill(pid, 0). Age is the backstop."""
    run = _running(session, pid=os.getpid(), age_seconds=0)
    run.started_at = datetime.now(UTC) - scheduler.ABANDONED_AFTER - timedelta(hours=1)
    progress = session.query(SyncProgress).filter_by(run_id=run.id).one()
    progress.heartbeat_at = run.started_at
    session.commit()

    assert scheduler.reap_stale(session) == [run.id]
    session.refresh(run)
    assert "abandoned" in run.error
