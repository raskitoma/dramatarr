"""The serialized job queue.

One asyncio task, one job at a time. That is a hard requirement, not a tunable: scans,
metadata refreshes and ffmpeg runs all contend for the same NAS mount, the same SQLite file
and the same TMDB rate limit, and a serial queue is the only shape where progress reporting
and cooperative cancellation stay honest.

Sessions: the worker keeps a *control* session for the job row (status, progress, logs) and
hands the handler a separate *work* session. Control writes therefore survive a handler
rollback — a failed job still has its error, its logs and its final status.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, or_, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.errors import AppError
from app.models import Job, JobLog, JobStatus, JobType, LogLevel, Series
from app.services.settings import SettingsService
from app.utils.time import in_seconds, utcnow
from app.workers.events import (
    JOB_CREATED,
    JOB_FINISHED,
    JOB_PROGRESS,
    JOB_STARTED,
    QUEUE_STATE,
    publish,
    subscriber_count,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

#: How long the worker sleeps when it found nothing to claim. A wake event short-circuits it.
IDLE_INTERVAL_SECONDS = 2.0
#: How often the running job's `cancel_requested` flag is re-read from the database.
CANCEL_POLL_SECONDS = 1.0
#: `progress()` publishes every call but only writes this often.
PROGRESS_WRITE_INTERVAL = 1.0
#: Retry delay is `2 ** attempts * BACKOFF_BASE_SECONDS`.
BACKOFF_BASE_SECONDS = 30
#: Grace given to the in-flight job at shutdown before its task is cancelled.
SHUTDOWN_GRACE_SECONDS = 10.0


class JobCanceled(Exception):
    """Raised by a handler that noticed `ctx.cancelled()` and unwound cleanly.

    Raising it is how a handler says "I did not finish". Returning normally always means
    the work is done, even if a shutdown was in progress, so a handler that stops early must
    raise rather than return.
    """


# --- Serialization ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def series_title_for(session: AsyncSession, series_id: str | None) -> str | None:
    """Display label for a job's series. One scalar lookup, or none at all when unset."""
    if not series_id:
        return None
    return await session.scalar(select(Series.title).where(Series.id == series_id))


def job_event_payload(job: Job, series_title: str | None = None) -> dict[str, Any]:
    """The job as it goes over SSE. camelCase, same shape the REST job schema exposes.

    `seriesTitle` is part of that shape and the admin queue row reads it directly, so it is
    always present as a key. The SSE consumer replaces a cached job row wholesale, so a
    payload that merely *omitted* it would blank the series label on the first progress tick.
    """
    return {
        "seriesTitle": series_title,
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "priority": job.priority,
        "payload": dict(job.payload or {}),
        "progressPercent": round(float(job.progress_percent or 0.0), 2),
        "progressMessage": job.progress_message or "",
        "attempts": job.attempts,
        "maxAttempts": job.max_attempts,
        "error": job.error,
        "cancelRequested": bool(job.cancel_requested),
        "seriesId": job.series_id,
        "episodeId": job.episode_id,
        "createdById": job.created_by_id,
        "createdAt": _iso(job.created_at),
        "startedAt": _iso(job.started_at),
        "finishedAt": _iso(job.finished_at),
        "nextAttemptAt": _iso(job.next_attempt_at),
    }


async def queue_snapshot(session: AsyncSession) -> dict[str, Any]:
    """Payload of `queue.state` and of `GET /api/admin/queue`."""
    paused = await SettingsService(session).get_bool("queue.paused", False)
    queued = await session.scalar(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED)
    )
    worker = get_worker()
    current = worker.current_payload if worker is not None else None
    return {
        "paused": paused,
        "running": current is not None,
        "queuedCount": int(queued or 0),
        "current": current,
    }


async def publish_queue_state(session: AsyncSession) -> None:
    """Broadcast the queue snapshot — but only when something is listening.

    Building the snapshot costs a COUNT, and `enqueue` calls this once per job. A scan that
    fans out a `metadata_refresh` per new series would otherwise pay that count hundreds of
    times over with no admin tab open to receive any of it.
    """
    if subscriber_count() == 0:
        return
    await publish(QUEUE_STATE, await queue_snapshot(session))


# --- Enqueue ---------------------------------------------------------------------------------


async def enqueue(
    session: AsyncSession,
    job_type: JobType | str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 100,
    created_by_id: str | None = None,
    series_id: str | None = None,
    episode_id: str | None = None,
) -> Job:
    """Add a job. Flushes so the caller gets an id, but leaves the commit to the caller."""
    try:
        resolved = JobType(str(job_type))
    except ValueError as exc:
        raise AppError("UNKNOWN_JOB_TYPE", f"Unknown job type {job_type!r}", 400) from exc

    job = Job(
        type=str(resolved),
        status=JobStatus.QUEUED,
        priority=priority,
        payload=dict(payload or {}),
        created_by_id=created_by_id,
        series_id=series_id,
        episode_id=episode_id,
    )
    session.add(job)
    await session.flush()

    await publish(JOB_CREATED, job_event_payload(job, await series_title_for(session, series_id)))
    await publish_queue_state(session)
    wake_worker()
    return job


# --- Claiming --------------------------------------------------------------------------------


async def claim_next_job(session: AsyncSession) -> Job | None:
    """Move the oldest eligible QUEUED job to RUNNING and return it, or None.

    Eligible means `status == queued` and `next_attempt_at` is null or already past. Order is
    `priority` ascending then `created_at` ascending, which is exactly the index on `jobs`.
    Claiming is a transaction of its own: it commits before returning, so a claimed job is
    never left invisible to a concurrent claimant.
    """
    now = utcnow()
    await _begin_immediate(session)

    candidate = (
        await session.execute(
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED,
                or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
            )
            .order_by(Job.priority.asc(), Job.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if candidate is None:
        await session.rollback()
        return None

    # Compare-and-swap on status: even if two claimants ever raced past the lock, only
    # one of them can move the row out of QUEUED.
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Job)
            .where(Job.id == candidate.id, Job.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.RUNNING,
                started_at=now,
                finished_at=None,
                next_attempt_at=None,
                error=None,
                progress_percent=0.0,
                progress_message="",
                attempts=Job.attempts + 1,
            )
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - lost race
        await session.rollback()
        return None

    await session.commit()
    await session.refresh(candidate)
    return candidate


# --- Job context -----------------------------------------------------------------------------


class JobContext:
    """What a handler is given.

    `session` is the handler's own unit of work. `progress`/`log` write through a separate
    control session, so they are durable even when the handler's transaction is rolled back.
    """

    def __init__(
        self,
        job: Job,
        session: AsyncSession,
        *,
        control: AsyncSession | None = None,
        worker: QueueWorker | None = None,
    ) -> None:
        self.job = job
        self.session = session
        self._control = control if control is not None else session
        self._worker = worker
        self._cancel_requested = False
        self._last_write = 0.0
        #: Read once, while the instance is certainly live. Every later use is in an error
        #: path where `job.id` may be an expired attribute on a session that needs a
        #: rollback — reading it there raises out of the very handler meant to swallow.
        self._job_id = job.id
        #: Resolved once by the worker. A progress tick fires several times a second, so
        #: looking the label up per event would be a query per tick for a constant value.
        self.series_title: str | None = None

    @property
    def job_id(self) -> str:
        return self._job_id

    # --- Cancellation ---------------------------------------------------------------------

    def cancelled(self) -> bool:
        """True once an admin asked for a cancel, or once the process is shutting down.
        Handlers must poll this inside their loops."""
        if self._cancel_requested:
            return True
        if self._flagged_on_row():
            self._cancel_requested = True
            return True
        return self._worker is not None and self._worker.is_stopping

    def _flagged_on_row(self) -> bool:
        """Read `Job.cancel_requested` off the in-memory row.

        The worker's watchdog is what refreshes it from the database, but a caller holding
        the same session as the writer (an admin route, a test) sees the flag here without
        waiting a poll interval. The attribute is only touched when it is already loaded:
        a lazy refresh would emit IO from a sync context and raise `MissingGreenlet`.
        """
        try:
            state = sa_inspect(self.job)
            if state.detached or "cancel_requested" in state.unloaded:
                return False
        except Exception:  # pragma: no cover - not an ORM instance (hand-built test double)
            return bool(getattr(self.job, "cancel_requested", False))
        return bool(self.job.cancel_requested)

    @property
    def cancel_requested(self) -> bool:
        """Only the admin-initiated cancel, which is what makes the job CANCELED rather
        than re-queued for the next boot."""
        return self._cancel_requested

    def mark_cancelled(self) -> None:
        self._cancel_requested = True

    # --- Reporting ------------------------------------------------------------------------

    async def progress(self, percent: float, message: str = "") -> None:
        percent = max(0.0, min(100.0, float(percent)))
        self.job.progress_percent = percent
        if message:
            self.job.progress_message = message[:500]

        now = time.monotonic()
        if percent >= 100.0 or now - self._last_write >= PROGRESS_WRITE_INTERVAL:
            self._last_write = now
            if not await self._commit_control():
                # Nothing durable to broadcast, and the row may not be readable: building
                # the payload would reload expired attributes and raise. Progress is
                # advisory — dropping one tick is the whole point.
                return

        await publish(JOB_PROGRESS, job_event_payload(self.job, self.series_title))

    async def log(self, level: str, message: str) -> None:
        self._control.add(JobLog(job_id=self._job_id, level=str(level), message=message))
        await self._commit_control()

    async def info(self, message: str) -> None:
        await self.log(LogLevel.INFO, message)

    async def warn(self, message: str) -> None:
        await self.log(LogLevel.WARNING, message)

    # --- Internals ------------------------------------------------------------------------

    async def _commit_control(self) -> bool:
        """Progress and logs are advisory: a write failure must never kill a job. Rolling
        back expires the Job instance, so it is refreshed before the caller touches it
        again — a lazy refresh inside async SQLAlchemy would raise."""
        try:
            await self._control.commit()
            return True
        except SQLAlchemyError:
            # Roll back *first*. The failed commit left the session needing one, so any
            # attribute read on `self.job` — including the `job.id` this used to log —
            # raises PendingRollbackError straight out of the handler that exists to
            # swallow the failure, killing the job over an advisory write.
            with suppress(SQLAlchemyError):
                await self._control.rollback()
            log.debug("control write failed for job %s", self._job_id, exc_info=True)
            with suppress(SQLAlchemyError):
                await self._control.refresh(self.job)
            return False


# --- Worker ----------------------------------------------------------------------------------


class QueueWorker:
    def __init__(self, *, idle_interval: float = IDLE_INTERVAL_SECONDS) -> None:
        self.idle_interval = idle_interval
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._current: JobContext | None = None
        self._current_payload: dict[str, Any] | None = None
        self._current_title: str | None = None
        self._paused_state: bool | None = None

    # --- State ----------------------------------------------------------------------------

    @property
    def is_stopping(self) -> bool:
        return self._stopping

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_job_id(self) -> str | None:
        return self._current.job.id if self._current is not None else None

    @property
    def current_payload(self) -> dict[str, Any] | None:
        return self._current_payload

    def wake(self) -> None:
        self._wake.set()

    def request_cancel(self, job_id: str) -> bool:
        """In-process shortcut so an admin's cancel lands immediately instead of waiting for
        the database poll. The DB flag remains the source of truth."""
        if self._current is not None and self._current.job.id == job_id:
            self._current.mark_cancelled()
            return True
        return False

    # --- Lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        if self.is_running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="dramatarr-queue-worker")

    async def stop(self, *, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        self._stopping = True
        self._wake.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except TimeoutError:
            log.warning("queue worker still busy after %.0fs; cancelling", grace)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:  # pragma: no cover - shutdown races
            task.cancel()
            raise
        except Exception:  # pragma: no cover - the loop already logs its own failures
            log.exception("queue worker exited with an error")
        finally:
            self._task = None

    # --- Main loop ------------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            await self._recover_interrupted()
        except Exception:  # pragma: no cover - recovery is best-effort
            log.exception("failed to recover interrupted jobs")

        while not self._stopping:
            worked = False
            try:
                worked = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The worker outliving every failure is the point; a job that explodes in
                # an unexpected place must not take the queue with it.
                log.exception("queue worker tick failed")
                await asyncio.sleep(1.0)
            if not worked and not self._stopping:
                await self._idle()

    async def _idle(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_interval)
        except TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _tick(self) -> bool:
        async with get_sessionmaker()() as ctl:
            paused = await SettingsService(ctl).get_bool("queue.paused", False)
            if paused != self._paused_state:
                self._paused_state = paused
                await publish_queue_state(ctl)
            if paused:
                return False

            job = await claim_next_job(ctl)
            if job is None:
                return False
            try:
                await self._run_job(ctl, job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A claimed job must never be left RUNNING, even if the failure came from
                # the bookkeeping around the handler rather than the handler itself.
                log.exception("job %s left the runner unexpectedly", _job_identity(job))
                self._current = None
                with suppress(Exception):
                    await self._fail(ctl, job, exc)
                self._current_payload = None
            return True

    # --- Claiming -------------------------------------------------------------------------

    async def _recover_interrupted(self) -> None:
        """Jobs left RUNNING by an unclean shutdown. They are re-queued rather than failed:
        nothing is known about how far they got, and every handler is idempotent."""
        async with get_sessionmaker()() as session:
            rows = (
                (await session.execute(select(Job).where(Job.status == JobStatus.RUNNING)))
                .scalars()
                .all()
            )
            for job in rows:
                job.status = JobStatus.QUEUED
                job.attempts = max(0, job.attempts - 1)
                job.started_at = None
                job.next_attempt_at = None
                job.progress_percent = 0.0
                job.progress_message = ""
                session.add(
                    JobLog(
                        job_id=job.id,
                        level=LogLevel.WARNING,
                        message="Re-queued after an interrupted run",
                    )
                )
            if rows:
                await session.commit()
                log.info("re-queued %d interrupted job(s)", len(rows))

    # --- Running --------------------------------------------------------------------------

    async def _run_job(self, ctl: AsyncSession, job: Job) -> None:
        # Resolved once per job. Every event this job emits carries it, including the
        # progress ticks that fire several times a second.
        self._current_title = await series_title_for(ctl, job.series_id)
        self._current_payload = job_event_payload(job, self._current_title)
        await publish(JOB_STARTED, self._current_payload)
        await publish_queue_state(ctl)

        if job.cancel_requested:
            await self._finish(ctl, job, JobStatus.CANCELED)
            return

        async with get_sessionmaker()() as work:
            ctx = JobContext(job, work, control=ctl, worker=self)
            ctx.series_title = self._current_title
            self._current = ctx
            watchdog = asyncio.create_task(
                self._watch_cancel(ctx), name=f"job-cancel-watch-{job.id}"
            )
            unwound = False
            try:
                handler = _resolve_handler(job.type)
                await ctx.log(LogLevel.INFO, f"Started {job.type}")
                await handler(ctx)
                await work.commit()
            except JobCanceled:
                # The handler stopped early. Why it stopped decides the outcome below, so
                # this must not be conflated with an admin's cancel.
                await _safe_rollback(work)
                unwound = True
            except asyncio.CancelledError:
                await _safe_rollback(work)
                await self._requeue(ctl, job, "Interrupted by shutdown")
                raise
            except Exception as exc:
                await _safe_rollback(work)
                await self._fail(ctl, job, exc)
                return
            finally:
                watchdog.cancel()
                with suppress(asyncio.CancelledError):
                    await watchdog
                self._current = None

            if ctx.cancel_requested:
                await ctx.log(LogLevel.WARNING, "Canceled on request")
                await self._finish(ctl, job, JobStatus.CANCELED)
            elif unwound and self._stopping:
                # It stopped because the process is going away, not because anyone cancelled
                # the work itself: hand it back to the next boot.
                await self._requeue(ctl, job, "Interrupted by shutdown")
            elif unwound:
                await ctx.log(LogLevel.WARNING, "Handler stopped early")
                await self._finish(ctl, job, JobStatus.CANCELED)
            else:
                await ctx.log(LogLevel.INFO, f"Finished {job.type}")
                await self._finish(ctl, job, JobStatus.SUCCEEDED)

    async def _watch_cancel(self, ctx: JobContext) -> None:
        """Polls the row's `cancel_requested` on its own session so an admin's cancel reaches
        a handler that is busy inside its own transaction."""
        sessionmaker = get_sessionmaker()
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            try:
                async with sessionmaker() as probe:
                    flagged = await probe.scalar(
                        select(Job.cancel_requested).where(Job.id == ctx.job_id)
                    )
            except SQLAlchemyError:  # pragma: no cover - transient lock
                log.debug("cancel poll failed for job %s", ctx.job_id, exc_info=True)
                continue
            if flagged:
                ctx.mark_cancelled()
                return

    # --- Transitions ----------------------------------------------------------------------

    async def _finish(self, ctl: AsyncSession, job: Job, status: JobStatus) -> None:
        job.status = status
        job.finished_at = utcnow()
        job.next_attempt_at = None
        if status == JobStatus.SUCCEEDED:
            job.progress_percent = 100.0
            job.error = None
        await _safe_commit(ctl, f"finish job {job.id}")

        self._current_payload = None
        await publish(JOB_FINISHED, job_event_payload(job, self._current_title))
        await publish_queue_state(ctl)

    async def _fail(self, ctl: AsyncSession, job: Job, exc: BaseException) -> None:
        message = _describe(exc)
        # This is the last chance to move the row out of RUNNING, so it must not itself
        # depend on the control session being healthy — a broken one is one of the reasons
        # a handler raises. Revive it before touching a single attribute of `job`: after a
        # failed write every one of them is expired, and reading one here would leave the
        # job RUNNING forever, invisible to both the queue page and the next boot's requeue.
        if not await _revive(ctl, job):
            await self._force_terminal(job, message)
            return
        log.warning("job %s (%s) failed: %s", job.id, job.type, message)
        ctl.add(JobLog(job_id=job.id, level=LogLevel.ERROR, message=message))
        job.error = message

        if job.attempts < job.max_attempts:
            delay = (2**job.attempts) * BACKOFF_BASE_SECONDS
            job.status = JobStatus.QUEUED
            job.finished_at = None
            job.next_attempt_at = in_seconds(delay)
            await _safe_commit(ctl, f"retry job {job.id}")
            self._current_payload = None
            await publish(JOB_PROGRESS, job_event_payload(job, self._current_title))
            await publish_queue_state(ctl)
            return

        await self._finish(ctl, job, JobStatus.FAILED)

    async def _force_terminal(self, job: Job, message: str) -> None:
        """Last resort: record the failure without the control session or the ORM instance.

        Reached only when the row cannot be reloaded. Anything short of this leaves the job
        RUNNING for good — the queue page shows it working forever and `_recover_interrupted`
        is the only thing that would ever touch it again.
        """
        self._current_payload = None
        job_id = _job_identity(job) or (self._current.job_id if self._current else None)
        if job_id is None:  # pragma: no cover - identity is set for any persisted row
            log.error("a job failed and could not be identified; it stays RUNNING: %s", message)
            return
        try:
            async with get_sessionmaker()() as rescue:
                await rescue.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status=JobStatus.FAILED,
                        error=message,
                        finished_at=utcnow(),
                        next_attempt_at=None,
                    )
                )
                await rescue.commit()
            log.warning("job %s marked failed through a rescue session: %s", job_id, message)
        except SQLAlchemyError:  # pragma: no cover - the database is gone
            log.exception("job %s could not be marked failed; it stays RUNNING", job_id)

    async def _requeue(self, ctl: AsyncSession, job: Job, reason: str) -> None:
        job.status = JobStatus.QUEUED
        job.started_at = None
        job.finished_at = None
        job.next_attempt_at = None
        job.progress_percent = 0.0
        job.progress_message = ""
        # The attempt never completed, so it should not count against max_attempts.
        job.attempts = max(0, job.attempts - 1)
        ctl.add(JobLog(job_id=job.id, level=LogLevel.WARNING, message=reason))
        await _safe_commit(ctl, f"requeue job {job.id}")
        self._current_payload = None
        await publish(JOB_PROGRESS, job_event_payload(job, self._current_title))
        await publish_queue_state(ctl)


# --- Helpers ---------------------------------------------------------------------------------


def _resolve_handler(job_type: str) -> Any:
    # Imported lazily: handlers pull in the scanner/metadata/image services, and the queue
    # module must stay importable on its own.
    from app.workers.handlers import get_handler

    return get_handler(job_type)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, AppError):
        return f"{exc.code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"[:2000]


def _is_sqlite(session: AsyncSession) -> bool:
    dialect = getattr(session.bind, "dialect", None)
    return getattr(dialect, "name", "") == "sqlite"


async def _begin_immediate(session: AsyncSession) -> None:
    """SQLite opens transactions deferred, so two readers could both see the same QUEUED row
    before either writes. BEGIN IMMEDIATE takes the write lock up front; the guarded UPDATE
    in `claim_next_job` is the backstop if the statement is not available.

    It is skipped when the driver already holds a transaction: SQLite refuses a nested BEGIN,
    and the write lock cannot be upgraded after the fact anyway. Nothing is rolled back on
    the way out — `claim_next_job` is a public entry point and may be handed a session that
    is carrying the caller's own uncommitted work.
    """
    if not _is_sqlite(session):
        return
    try:
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        driver = getattr(raw, "driver_connection", None)
        if getattr(driver, "in_transaction", False):
            return
        await session.execute(text("BEGIN IMMEDIATE"))
    except SQLAlchemyError:
        log.debug("BEGIN IMMEDIATE unavailable; relying on the guarded update", exc_info=True)


async def _safe_rollback(session: AsyncSession) -> None:
    with suppress(SQLAlchemyError):
        await session.rollback()


def _job_identity(job: Job) -> str | None:
    """The primary key, without touching a single mapped attribute.

    `job.id` re-loads whenever the attribute is expired — exactly the state a failed write
    leaves it in — so it is unusable on the paths that handle that failure. The identity key
    lives in the session's identity map and needs no IO.
    """
    try:
        identity = sa_inspect(job).identity
    except Exception:  # pragma: no cover - not a mapped instance
        return None
    return str(identity[0]) if identity else None


async def _revive(session: AsyncSession, job: Job) -> bool:
    """Make `job`'s attributes readable again, rolling the session back if that is what it
    needs. False means the caller must not touch the instance at all."""
    for attempt in (1, 2):
        try:
            await session.refresh(job)
            return True
        except SQLAlchemyError:
            if attempt == 1:
                await _safe_rollback(session)
    log.warning("could not reload job row %s", _job_identity(job), exc_info=True)
    return False


async def _safe_commit(session: AsyncSession, what: str) -> bool:
    for attempt in (1, 2):
        try:
            await session.commit()
            return True
        except SQLAlchemyError:
            log.warning("commit failed (%s), attempt %d", what, attempt, exc_info=True)
            await _safe_rollback(session)
            if attempt == 1:
                await asyncio.sleep(0.25)
    return False


# --- Module-level worker ---------------------------------------------------------------------

_worker: QueueWorker | None = None


def get_worker() -> QueueWorker | None:
    return _worker


def wake_worker() -> None:
    if _worker is not None:
        _worker.wake()


def request_cancel(job_id: str) -> bool:
    """Signal the running job in-process. Callers must also set `cancel_requested` on the
    row so the intent survives a restart."""
    return _worker.request_cancel(job_id) if _worker is not None else False


async def start_worker(app: FastAPI | None = None) -> QueueWorker:
    global _worker
    if _worker is None or not _worker.is_running:
        _worker = QueueWorker()
        _worker.start()
    if app is not None:
        app.state.queue_worker = _worker
    log.info("queue worker started (concurrency=1)")
    return _worker


async def stop_worker(app: FastAPI | None = None) -> None:
    global _worker
    worker = _worker
    if worker is not None:
        await worker.stop()
    _worker = None
    if app is not None:
        app.state.queue_worker = None
    log.info("queue worker stopped")


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "JobCanceled",
    "JobContext",
    "QueueWorker",
    "claim_next_job",
    "enqueue",
    "get_worker",
    "job_event_payload",
    "publish_queue_state",
    "queue_snapshot",
    "request_cancel",
    "start_worker",
    "stop_worker",
    "wake_worker",
]
