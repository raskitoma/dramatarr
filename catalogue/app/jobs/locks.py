"""Per-source locking.

The rule: never two jobs against the same site at once, but different sites in
parallel. That is a politeness constraint, so it has to hold even when two
workers start in the same millisecond.

Acquisition is a compare-and-swap, not check-then-set. A read followed by an
unconditional write races: both workers see a takeable lock, both take it, and
the site gets double the traffic it agreed to.

A held lock is a LEASE that its owner renews. Treating `lock_acquired_at` as a
fixed start time instead meant a live, heartbeating worker lost its lock once
the run outlived STALE_AFTER -- and an uncapped first crawl runs for hours.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import SourceState

#: A lease that has not been renewed for this long is reclaimed, whether or not
#: its owner looks alive. Must stay comfortably larger than RENEW_INTERVAL.
STALE_AFTER = timedelta(minutes=10)

#: How often a worker should renew. See app/jobs/runner.py.
RENEW_INTERVAL = timedelta(seconds=30)

#: A lease whose owner appears dead is still respected for this long.
#: `pid_alive` is not authoritative -- a worker in a different PID namespace
#: (the normal case in a container) looks dead from outside it. Without this
#: grace period a live worker's lock is stolen immediately.
DEAD_OWNER_GRACE = timedelta(seconds=60)


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def ensure_state(session: Session, source_id: int) -> SourceState:
    """Get or create the state row, tolerating a concurrent creator."""
    state = session.query(SourceState).filter_by(source_id=source_id).one_or_none()
    if state is not None:
        return state
    state = SourceState(source_id=source_id)
    session.add(state)
    try:
        session.flush()
    except IntegrityError:
        # Another worker inserted it between our read and our write.
        session.rollback()
        state = session.query(SourceState).filter_by(source_id=source_id).one()
    return state


def acquire(session: Session, source_id: int, pid: int | None = None) -> bool:
    """Take the lease for a source. False if someone else holds a live one."""
    pid = pid or os.getpid()
    now = datetime.now(UTC)
    ensure_state(session, source_id)
    session.commit()

    state = session.query(SourceState).filter_by(source_id=source_id).one()
    current = state.lock_pid
    stamp = state.lock_acquired_at
    age = now - _aware(stamp) if stamp is not None else None

    # A free lease is always takeable. A held one only when it is old enough to
    # be trusted abandoned: either the lease expired, or its owner looks gone
    # and the grace period has passed. Age is required in both cases, so a
    # freshly-taken lease is never stolen.
    reclaimable = False
    if current is not None and age is not None:
        reclaimable = age > STALE_AFTER or (
            age > DEAD_OWNER_GRACE and not pid_alive(current)
        )

    # Compare-and-swap: the predicate pins BOTH the pid and the timestamp we
    # read, so a renewal landing between our read and our write makes the
    # UPDATE match zero rows instead of stealing a lease that just moved.
    predicate = SourceState.lock_pid.is_(None)
    if reclaimable:
        predicate = or_(
            predicate,
            (SourceState.lock_pid == current) & (SourceState.lock_acquired_at == stamp),
        )

    result = session.execute(
        update(SourceState)
        .where(SourceState.source_id == source_id, predicate)
        .values(lock_pid=pid, lock_acquired_at=now)
    )
    session.commit()
    return result.rowcount == 1


def renew(session: Session, source_id: int, pid: int | None = None) -> bool:
    """Extend our lease. False if we no longer hold it.

    A worker that has lost its lease should stop: something else is already
    crawling this source.
    """
    pid = pid or os.getpid()
    result = session.execute(
        update(SourceState)
        .where(SourceState.source_id == source_id, SourceState.lock_pid == pid)
        .values(lock_acquired_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount == 1


def release(session: Session, source_id: int, pid: int | None = None) -> None:
    """Release only if we still hold it -- never stomp another worker's lease."""
    pid = pid or os.getpid()
    session.execute(
        update(SourceState)
        .where(SourceState.source_id == source_id, SourceState.lock_pid == pid)
        .values(lock_pid=None, lock_acquired_at=None)
    )
    session.commit()


def holder(session: Session, source_id: int) -> int | None:
    state = session.query(SourceState).filter_by(source_id=source_id).one_or_none()
    return state.lock_pid if state else None


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def release_all(session: Session) -> int:
    """Drop every source lease. Safe ONLY at application startup.

    Workers are children of the API process, so before it starts none of ours
    can be running. Without this, a container restart leaves leases held by
    pids from the previous incarnation -- and because pids are reused, the new
    container can see one of those as "alive" and refuse to crawl for a full
    STALE_AFTER window.
    """
    result = session.execute(
        update(SourceState)
        .where(SourceState.lock_pid.isnot(None))
        .values(lock_pid=None, lock_acquired_at=None)
    )
    session.commit()
    return result.rowcount
