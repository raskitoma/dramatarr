"""Run lifecycle: create, spawn, cancel, reap.

The once-daily rule is enforced by a partial unique index in the schema, not
here -- an application-level check would race, since two schedulers can both
read "nothing ran today" before either writes.
"""

from __future__ import annotations

import multiprocessing as mp
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.jobs import locks
from app.models import RunState, Source, SyncProgress, SyncRun

#: A RUNNING row whose worker has not checked in for this long, and whose pid
#: is gone, is presumed dead.
HEARTBEAT_TIMEOUT = timedelta(minutes=2)


class JobRejected(RuntimeError):
    """A run was refused, with a reason worth showing the operator."""


def create_run(
    session: Session, source_key: str, job_type: str, *, manual: bool = False
) -> SyncRun:
    source = session.scalar(select(Source).where(Source.key == source_key))
    if source is None:
        raise JobRejected(f"unknown source {source_key!r}")
    if not source.enabled and not manual:
        raise JobRejected(f"source {source_key!r} is disabled")

    run = SyncRun(
        source_id=source.id,
        job_type=job_type,
        run_date=settings.today(),
        state=RunState.PENDING,
        manual=manual,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # The partial unique index refused a second scheduled run today. This
        # is the requirement working, not an error to paper over.
        raise JobRejected(
            f"{source_key}/{job_type} already ran today "
            f"({settings.today()}); use a manual trigger to override"
        ) from exc
    return run


def spawn(run_id: int) -> mp.Process:
    """Start a worker in its own process.

    'spawn' explicitly: fork is unsafe in a process that already has threads
    (the API server does), and the default differs by platform and Python
    version. The target is a module-level function taking an int, so it
    pickles cleanly under spawn.
    """
    from app.jobs.runner import run_job

    process = mp.get_context("spawn").Process(
        target=run_job, args=(run_id,), daemon=False
    )
    process.start()
    return process


def request_cancel(session: Session, run_id: int) -> bool:
    """Cooperative cancel: the worker notices between items and commits what it
    already has, rather than being killed mid-write."""
    result = session.execute(
        update(SyncRun)
        .where(SyncRun.id == run_id, SyncRun.state.in_([RunState.PENDING, RunState.RUNNING]))
        .values(cancel_requested=True)
    )
    session.commit()
    return result.rowcount == 1


#: A PENDING run whose worker never reached RUNNING is abandoned after this.
#: Without it, a worker that dies during startup leaves the row PENDING for
#: ever -- and, because the partial unique index counts it, burns that
#: source's scheduled slot for the whole day.
PENDING_TIMEOUT = timedelta(minutes=5)

#: Absolute backstop for a RUNNING row, regardless of what pid_alive says.
#: PIDs get recycled, and a recycled pid makes a dead worker look alive for
#: ever; a zombie child also still answers kill(pid, 0).
ABANDONED_AFTER = timedelta(hours=6)


def reap_stale(session: Session) -> list[int]:
    """Fail runs whose worker died without finishing.

    Without this a killed worker leaves a row RUNNING for ever, and its source
    locked with it.
    """
    now = datetime.now(UTC)
    cutoff = now - HEARTBEAT_TIMEOUT
    reaped: list[int] = []

    rows = session.execute(
        select(SyncRun, SyncProgress)
        .outerjoin(SyncProgress, SyncProgress.run_id == SyncRun.id)
        .where(SyncRun.state.in_([RunState.RUNNING, RunState.PENDING]))
    ).all()

    for run, progress in rows:
        beat = progress.heartbeat_at if progress else None
        last = beat or run.started_at
        reason: str | None = None

        if run.state == RunState.PENDING:
            # PENDING rows have no pid to check; age is the only signal.
            created = run.started_at
            if created is None or _aware(created) < now - PENDING_TIMEOUT:
                # Fall back to the run's own date when started_at was never set.
                if run.started_at is not None or run.run_date < now.date():
                    reason = "never started (worker died before claiming the run)"
        elif last is not None and _aware(last) < now - ABANDONED_AFTER:
            # The backstop: too old to be real whatever the pid says.
            reason = "abandoned (no heartbeat for hours)"
        elif locks.pid_alive(run.pid):
            continue
        elif last is None or _aware(last) < cutoff:
            reason = "worker died without finishing (no heartbeat, pid gone)"

        if reason is None:
            continue

        run.state = RunState.FAILED
        run.error = reason
        run.finished_at = now
        if run.pid:
            locks.release(session, run.source_id, run.pid)
        reaped.append(run.id)

    session.commit()
    return reaped


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
