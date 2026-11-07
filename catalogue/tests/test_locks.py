"""M5: per-source locking.

The requirement is a politeness constraint -- never two jobs against the same
site at once -- so it has to hold under genuine concurrency, not just in the
happy path.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.jobs import locks
from app.models import Base, Source, SourceState


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path}/locks.db"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        s.add(Source(id=2, key="reelshort", base_url="https://b.test"))
        s.commit()
    return url, sessionmaker(engine, expire_on_commit=False, future=True)


def test_lock_is_exclusive_per_source(db) -> None:
    _, Sess = db
    with Sess() as a, Sess() as b:
        assert locks.acquire(a, 1, pid=1111) is True
        assert locks.acquire(b, 1, pid=2222) is False
        assert locks.holder(a, 1) == 1111


def test_different_sources_lock_independently(db) -> None:
    """Cross-source parallelism is required, not merely allowed."""
    _, Sess = db
    with Sess() as a, Sess() as b:
        assert locks.acquire(a, 1, pid=1111) is True
        assert locks.acquire(b, 2, pid=2222) is True


def test_release_frees_the_lock(db) -> None:
    _, Sess = db
    with Sess() as a, Sess() as b:
        locks.acquire(a, 1, pid=1111)
        locks.release(a, 1, pid=1111)
        assert locks.acquire(b, 1, pid=2222) is True


def test_release_by_a_non_holder_is_a_no_op(db) -> None:
    """A worker must never be able to stomp another worker's lock -- that would
    silently permit two crawlers against one site."""
    _, Sess = db
    with Sess() as a, Sess() as b:
        locks.acquire(a, 1, pid=1111)
        locks.release(b, 1, pid=9999)
        assert locks.holder(a, 1) == 1111
        assert locks.acquire(b, 1, pid=2222) is False


def test_a_dead_owners_lock_can_be_taken(db) -> None:
    """Otherwise a SIGKILLed worker locks its source out permanently."""
    _, Sess = db
    with Sess() as s:
        locks.acquire(s, 1, pid=_dead_pid())
        # Past the grace period: a dead owner is only presumed gone after it
        # has had a chance to prove otherwise.
        state = s.query(SourceState).filter_by(source_id=1).one()
        state.lock_acquired_at = (
            datetime.now(UTC) - locks.DEAD_OWNER_GRACE - timedelta(seconds=5)
        )
        s.commit()
        assert locks.acquire(s, 1, pid=os.getpid()) is True


def test_an_ancient_lock_is_reclaimed_even_if_the_pid_looks_alive(db) -> None:
    """Guards against PID reuse: a recycled pid can make a long-abandoned lock
    look live forever."""
    _, Sess = db
    with Sess() as s:
        locks.acquire(s, 1, pid=os.getpid())
        state = s.query(SourceState).filter_by(source_id=1).one()
        state.lock_acquired_at = datetime.now(UTC) - locks.STALE_AFTER - timedelta(minutes=1)
        s.commit()

        assert locks.acquire(s, 1, pid=os.getpid() + 1) is True


def test_a_fresh_lock_held_by_a_live_pid_is_not_stolen(db) -> None:
    _, Sess = db
    with Sess() as s:
        locks.acquire(s, 1, pid=os.getpid())
        assert locks.acquire(s, 1, pid=os.getpid() + 1) is False


def test_a_fresh_lock_is_not_stolen_even_when_the_owner_looks_dead(db) -> None:
    """The container case: a worker in another PID namespace looks dead from
    outside it. Stealing its lock immediately would put two crawlers on one
    site -- exactly what the lock exists to prevent."""
    _, Sess = db
    with Sess() as s:
        locks.acquire(s, 1, pid=_dead_pid())
        assert locks.acquire(s, 1, pid=os.getpid()) is False


# --- genuine concurrency -----------------------------------------------------


def _contend(url: str, source_id: int, queue) -> None:
    engine = create_engine(url, future=True, connect_args={"timeout": 30})
    with Session(engine, future=True) as session:
        queue.put(locks.acquire(session, source_id, pid=os.getpid()))


def test_only_one_of_many_racing_processes_wins_the_lock(db) -> None:
    """The check-then-set version of this passes every single-threaded test and
    still lets two crawlers onto one site."""
    url, _ = db
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()

    procs = [ctx.Process(target=_contend, args=(url, 1, queue)) for _ in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = [queue.get() for _ in procs]
    assert sum(results) == 1, f"expected exactly one winner, got {sum(results)}"


def _dead_pid() -> int:
    """A pid that has certainly exited."""
    ctx = mp.get_context("spawn")
    p = ctx.Process(target=_noop)
    p.start()
    p.join()
    return p.pid


def _noop() -> None:
    return None


# --- lease renewal (regression: the critical review finding) -----------------


def test_a_renewing_worker_never_loses_its_lease(db) -> None:
    """The bug an adversarial review caught and the original tests missed.

    `lock_acquired_at` was stamped once and never refreshed, so STALE_AFTER
    measured "time since acquisition" rather than "time since last check-in".
    A live, heartbeating worker therefore lost its lock the moment its run
    outlived STALE_AFTER -- and an uncapped first crawl runs for hours.
    """
    _, Sess = db
    with Sess() as s:
        locks.acquire(s, 1, pid=os.getpid())

        # Simulate a long run: age the lease past expiry, then check in.
        state = s.query(SourceState).filter_by(source_id=1).one()
        state.lock_acquired_at = datetime.now(UTC) - locks.STALE_AFTER - timedelta(minutes=1)
        s.commit()

        assert locks.renew(s, 1, pid=os.getpid()) is True
        assert locks.acquire(s, 1, pid=os.getpid() + 1) is False, (
            "a worker that is still checking in must keep its lease"
        )


def test_an_expired_lease_is_still_reclaimable(db) -> None:
    """Renewal must not defeat the backstop: a worker that stops checking in
    still loses the lease, otherwise a crashed run locks its source for ever."""
    _, Sess = db
    with Sess() as s:
        locks.acquire(s, 1, pid=os.getpid())
        state = s.query(SourceState).filter_by(source_id=1).one()
        state.lock_acquired_at = datetime.now(UTC) - locks.STALE_AFTER - timedelta(minutes=1)
        s.commit()

        assert locks.acquire(s, 1, pid=os.getpid() + 1) is True


def test_renew_fails_once_the_lease_has_been_taken(db) -> None:
    """A worker that has lost its lease must find out, so it can stop rather
    than crawl alongside whoever took it."""
    _, Sess = db
    with Sess() as s:
        locks.acquire(s, 1, pid=1111)
        state = s.query(SourceState).filter_by(source_id=1).one()
        state.lock_acquired_at = datetime.now(UTC) - locks.STALE_AFTER - timedelta(minutes=1)
        s.commit()
        locks.acquire(s, 1, pid=os.getpid())

        assert locks.renew(s, 1, pid=1111) is False


def test_renew_interval_is_well_inside_the_expiry_window(db) -> None:
    assert locks.RENEW_INTERVAL * 4 < locks.STALE_AFTER
