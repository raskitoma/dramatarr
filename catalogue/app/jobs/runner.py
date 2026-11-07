"""The worker process.

Runs one SyncRun to completion in its own process, holding the source lease for
its lifetime and renewing it so a long run is never mistaken for an abandoned
one.

Ordering in here is deliberate and was got wrong once: the lease must be
released on EVERY exit path, including one where writing the run row itself
fails, or the source stays locked until the lease expires.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from datetime import UTC, datetime

from app.config import settings
from app.db import session_scope
from app.fetch.archive import Archive
from app.fetch.breaker import BreakerOpen, CircuitBreaker
from app.fetch.client import Fetcher
from app.fetch.ratelimit import RateLimiterRegistry
from app.fetch.types import RateSpec
from app.fetch.robots import RobotsCache
from app.jobs import locks
from app.jobs.context import RunContext
from app.jobs.progress import ProgressReporter
from app.models import BreakerState, JobType, RunState, Source, SyncRun
from app.sources.base import Mode

log = logging.getLogger(__name__)

from app.sources.registry import ADAPTERS as SOURCE_MODULES


def _load(source_key: str):
    from app.sources.registry import load

    return load(source_key)


class _LeaseRenewer:
    """Renews the source lease on a timer.

    A timer rather than a progress hook: `discover()` can run for minutes
    without emitting a single progress callback, and that is exactly the window
    in which a lease would silently expire.
    """

    def __init__(self, source_id: int, pid: int) -> None:
        self.source_id = source_id
        self.pid = pid
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        interval = locks.RENEW_INTERVAL.total_seconds()
        while not self._stop.wait(interval):
            try:
                with session_scope() as session:
                    if not locks.renew(session, self.source_id, self.pid):
                        # Someone else owns the source now. Stop crawling.
                        self.lost.set()
                        return
            except Exception:  # noqa: BLE001 - a failed renewal must not kill the run
                log.warning("lease renewal failed for source %s", self.source_id)


def run_job(run_id: int) -> int:
    """Entry point for the child process. Returns an exit code."""
    pid = os.getpid()

    with session_scope() as session:
        run = session.get(SyncRun, run_id)
        if run is None:
            log.error("run %s does not exist", run_id)
            return 2
        source = session.get(Source, run.source_id)
        source_key, source_id, job_type = source.key, source.id, run.job_type

    with session_scope() as session:
        if not locks.acquire(session, source_id, pid):
            busy = locks.holder(session, source_id)
            log.warning("source %s is locked by pid %s", source_key, busy)
            run = session.get(SyncRun, run_id)
            if run is not None and not run.manual:
                # A scheduled run that never got to start must NOT leave a row
                # behind: the partial unique index counts it, so recording a
                # failure here would burn this source's only slot for the day
                # over a transient lock. Drop it and let the next tick retry.
                session.delete(run)
            elif run is not None:
                run.state = RunState.FAILED
                run.error = f"source busy: locked by pid {busy}"
                run.finished_at = datetime.now(UTC)
            return 3

    # From here the lease is HELD. Everything below runs inside a try whose
    # finally releases it -- including setup that can raise before the run row
    # is ever touched, which previously leaked the lease and stranded the run
    # in PENDING.
    renewer = _LeaseRenewer(source_id, pid)
    stats: dict = {}
    state = RunState.SUCCEEDED
    error: str | None = None
    cancelled = {"flag": False}

    def _on_term(_signum, _frame):
        cancelled["flag"] = True

    try:
        signal.signal(signal.SIGTERM, _on_term)
        renewer.start()

        module = _load(source_key)
        archive = Archive(settings.archive_dir, settings.archive_keep_versions)
        breaker = _restore_breaker(source_key, source_id)

        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            run.state = RunState.RUNNING
            run.started_at = datetime.now(UTC)
            run.pid = pid
            session.commit()

            reporter = ProgressReporter(session, run_id, total=_initial_total(job_type))
            ctx = RunContext(
                fetcher=None,
                max_items=settings.max_items,
                watermark=_watermark(session, source_id),
            )

            def _cancelled() -> bool:
                if cancelled["flag"] or renewer.lost.is_set():
                    return True
                session.expire_all()
                return bool(session.get(SyncRun, run_id).cancel_requested)

            ctx.cancelled = _cancelled  # type: ignore[method-assign]
            ctx.on_progress = lambda done, total, phase: reporter.update(
                done, total, phase
            )

            with Fetcher(
                settings.user_agent,
                archive,
                # Not every adapter crawls through the Fetcher: IMDb downloads
                # bulk TSVs directly, so it declares no crawl rate. A default
                # keeps the runner generic rather than making every enricher
                # invent a pacing figure it never uses.
                RateLimiterRegistry(getattr(module, "RATE", RateSpec())),
                RobotsCache(settings.user_agent),
                breaker,
            ) as fetcher:
                ctx.fetcher = fetcher
                stats = _dispatch(module, session, ctx, job_type, archive)

            if renewer.lost.is_set():
                state = RunState.FAILED
                error = "lost the source lease mid-run; another worker took it"
            elif _cancelled():
                state = RunState.CANCELLED
            reporter.finish(stats.get("reparsed") or stats.get("discovered", 0))
            _persist_breaker(session, source_id, breaker)

    except BreakerOpen as exc:
        state, error = RunState.FAILED, str(exc)
        log.warning("run %s halted: %s", run_id, exc)
    except Exception as exc:  # noqa: BLE001 - the run must record why it died
        state, error = RunState.FAILED, f"{type(exc).__name__}: {exc}"
        log.exception("run %s failed", run_id)
    finally:
        try:
            with session_scope() as session:
                run = session.get(SyncRun, run_id)
                if run is not None:
                    run.state = state
                    run.error = error
                    run.finished_at = datetime.now(UTC)
                    # Keep whatever the job managed to report, even on failure:
                    # partial stats are the only clue to where it stopped.
                    run.stats_json = json.dumps(stats, default=str)
                    # A watermark is a promise that everything before it was
                    # processed. Advancing it on a failed or cancelled run
                    # silently skips whatever that run did not get to.
                    if state is RunState.SUCCEEDED:
                        _save_watermark(session, source_id, stats.get("watermark"))
        finally:
            renewer.stop()
            with session_scope() as session:
                locks.release(session, source_id, pid)

    return 0 if state is RunState.SUCCEEDED else 1


def _initial_total(job_type: str) -> int | None:
    """Only a capped full crawl knows its size up front.

    Reporting settings.max_items for every job type invented a total for
    incremental runs, and reported 0 -- meaning 'unknown' -- for uncapped ones,
    which are precisely the runs that need an ETA. Jobs that can count their
    work report it themselves via ctx.progress.
    """
    if job_type == JobType.FULL_CATALOG and settings.max_items:
        return settings.max_items
    return None


def _dispatch(module, session, ctx, job_type: str, archive: Archive) -> dict:
    from app.jobs.catalog import run_catalog_sync
    from app.jobs.reparse import run_reparse

    if job_type == JobType.FULL_CATALOG:
        stats = run_catalog_sync(module, session, ctx, Mode.FULL)
    elif job_type == JobType.INCREMENTAL:
        stats = run_catalog_sync(module, session, ctx, Mode.INCREMENTAL)
    elif job_type == JobType.REPARSE:
        stats = run_reparse(module, session, archive, ctx=ctx)
    elif job_type == JobType.ACTOR_ROSTER:
        from app.jobs.actor_roster import run_actor_roster

        stats = run_actor_roster(module, session, ctx)
    elif job_type == JobType.PERSON_FACTS:
        from app.jobs.person_facts import run_person_facts

        stats = run_person_facts(module, session, ctx)
    elif job_type == JobType.ROBOTS_CHECK:
        from app.jobs.robots_check import run_robots_check

        stats = run_robots_check(session, ctx=ctx)
    elif job_type == JobType.POSTER_FETCH:
        from app.fetch.types import RateSpec
        from app.images import fetch_posters

        # Image CDNs are separate hosts from the catalogue APIs, so they get
        # their own budget rather than inheriting a source's pacing.
        with Fetcher(
            settings.user_agent,
            archive,
            RateLimiterRegistry(RateSpec(concurrency=4, min_interval=0.2)),
            RobotsCache(settings.user_agent),
        ) as image_fetcher:
            stats = fetch_posters(
                session,
                image_fetcher,
                settings.image_dir,
                progress=lambda d, t, p: ctx.progress(d, t, p),
            )
    elif job_type == JobType.AVATAR_FETCH:
        from app.fetch.types import RateSpec
        from app.images import fetch_avatars

        # Same reasoning as poster_fetch: image CDNs are separate hosts from the
        # catalogue APIs and get their own budget rather than a source's pacing.
        with Fetcher(
            settings.user_agent,
            archive,
            RateLimiterRegistry(RateSpec(concurrency=4, min_interval=0.2)),
            RobotsCache(settings.user_agent),
        ) as image_fetcher:
            stats = fetch_avatars(
                session,
                image_fetcher,
                settings.image_dir,
                progress=lambda d, t, p: ctx.progress(d, t, p),
            )
    elif job_type == JobType.DEDUP:
        from app.jobs.dedup import run_dedup

        # Dry run unless explicitly told otherwise. A merge rewrites eleven tables
        # and removes ids a deployed consumer stores, so it is not something a
        # scheduled run should ever do by accident -- `apply` has to be asked for.
        stats = run_dedup(
            session,
            dry_run=not settings.dedup_apply,
            progress=lambda d, t: ctx.progress(d, t, None),
        )
    elif job_type == JobType.BACKUP:
        from app.jobs.backup import run_backup_any

        stats = run_backup_any(
            settings.db_url, settings.backup_dir, keep=settings.backup_keep
        )
    elif job_type == JobType.IMDB_REFRESH:
        from app.jobs.imdb_sync import run_imdb_refresh

        stats = run_imdb_refresh(session, settings.data_dir, ctx=ctx)
    else:
        raise ValueError(f"unsupported job type {job_type!r}")

    if ctx.new_watermark:
        stats["watermark"] = ctx.new_watermark
    return stats


def _restore_breaker(source_key: str, source_id: int) -> CircuitBreaker:
    """Rebuild breaker state from the database.

    The breaker lives in worker-process memory, so without this a source that
    tripped is re-hammered by the very next run.
    """
    breaker = CircuitBreaker(source_key)
    with session_scope() as session:
        state = locks.ensure_state(session, source_id)
        if state.breaker_state == BreakerState.OPEN and state.breaker_until:
            until = state.breaker_until
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if until > datetime.now(UTC):
                breaker._open_until = until  # noqa: SLF001 - restoring our own state
    return breaker


def _persist_breaker(session, source_id: int, breaker: CircuitBreaker) -> None:
    state = locks.ensure_state(session, source_id)
    until = breaker.open_until()
    state.breaker_state = BreakerState.OPEN if until else BreakerState.CLOSED
    state.breaker_until = until
    session.commit()


def _watermark(session, source_id: int) -> str | None:
    return locks.ensure_state(session, source_id).watermark


def _save_watermark(session, source_id: int, watermark: str | None) -> None:
    if not watermark:
        return
    state = locks.ensure_state(session, source_id)
    state.watermark = watermark
    state.last_success_at = datetime.now(UTC)
