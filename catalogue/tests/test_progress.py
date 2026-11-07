"""M5: progress heartbeats and ETA."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs.progress import ProgressReporter
from app.models import Base, JobType, Source, SyncProgress, SyncRun


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/p.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        s.add(
            SyncRun(
                id=1,
                source_id=1,
                job_type=JobType.FULL_CATALOG,
                run_date=date(2026, 8, 16),
            )
        )
        s.commit()
        yield s


def test_reporter_creates_a_progress_row(session) -> None:
    ProgressReporter(session, 1, total=50)
    row = session.query(SyncProgress).filter_by(run_id=1).one()
    assert row.items_total == 50


def test_progress_and_eta_are_written(session) -> None:
    reporter = ProgressReporter(session, 1, total=50)
    reporter._last_time -= 8.0  # 10 items over 8 seconds -> 1.25 items/sec
    reporter.update(10, force=True)

    row = session.query(SyncProgress).filter_by(run_id=1).one()
    assert row.items_done == 10
    assert row.eta_seconds == 32  # (50 - 10) / 1.25


def test_a_near_instant_sample_does_not_invent_a_rate(session) -> None:
    """Two updates milliseconds apart imply an enormous items/sec and would
    report an ETA of about zero. The first heartbeat of a run is the usual
    victim, and a confidently wrong ETA is worse than none."""
    reporter = ProgressReporter(session, 1, total=50)
    reporter.update(10, force=True)  # no measurable elapsed time

    assert reporter._rate is None
    assert session.query(SyncProgress).filter_by(run_id=1).one().eta_seconds is None


def test_eta_is_absent_when_the_total_is_unknown(session) -> None:
    """Incremental runs do not know their size up front. An invented ETA is
    worse than none."""
    reporter = ProgressReporter(session, 1, total=None)
    reporter._rate = 5.0
    reporter.update(10, force=True)

    assert session.query(SyncProgress).filter_by(run_id=1).one().eta_seconds is None


def test_eta_is_never_negative_when_done_exceeds_the_total(session) -> None:
    """The cap is an estimate; a source can hand back more than expected."""
    reporter = ProgressReporter(session, 1, total=50)
    reporter._rate = 5.0
    reporter.update(80, force=True)

    eta = session.query(SyncProgress).filter_by(run_id=1).one().eta_seconds
    assert eta is None or eta >= 0


def test_no_rate_yet_means_no_eta(session) -> None:
    reporter = ProgressReporter(session, 1, total=50)
    reporter.update(0, force=True)

    assert session.query(SyncProgress).filter_by(run_id=1).one().eta_seconds is None


def test_writes_are_throttled_but_the_final_one_always_lands(session) -> None:
    """The last heartbeat must be written even if it falls inside the throttle
    window, or a finished run looks stale to the reaper."""
    reporter = ProgressReporter(session, 1, total=50)
    reporter.update(1)
    reporter.update(2)  # inside MIN_INTERVAL, should not write
    assert session.query(SyncProgress).filter_by(run_id=1).one().items_done in (0, 1)

    reporter.finish(50)
    assert session.query(SyncProgress).filter_by(run_id=1).one().items_done == 50


def test_rate_reacts_to_a_slowdown(session) -> None:
    """An EWMA over a short window, not a lifetime average: a run that slows
    down should say so rather than hide behind its fast first minute."""
    reporter = ProgressReporter(session, 1, total=1000)
    reporter._rate = 100.0
    reporter._last_time -= 10.0  # 10s elapsed, only 1 item done: very slow
    reporter.update(1, force=True)

    assert reporter._rate < 100.0
