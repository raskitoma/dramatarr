"""Cron-driven library scans.

The schedule lives in the `library.scan_cron` setting, not in env, so it is re-read on every
tick: an admin who changes it in the UI sees the new schedule take effect without a restart.
An empty expression disables scheduling entirely.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING

from croniter import croniter  # type: ignore[import-untyped]  # no stubs published
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models import Job, JobStatus, JobType
from app.utils.time import utcnow
from app.workers.queue import enqueue

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

TICK_SECONDS = 30.0


class ScanScheduler:
    def __init__(self, *, tick_seconds: float = TICK_SECONDS) -> None:
        self.tick_seconds = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._expression = ""
        self._next_due: datetime | None = None
        self._warned_about = ""

    @property
    def next_due(self) -> datetime | None:
        return self._next_due

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- Lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="dramatarr-scan-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # --- Loop -----------------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scan scheduler tick failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)

    async def tick(self) -> bool:
        """One evaluation of the schedule. Returns True when a scan was enqueued."""
        async with get_sessionmaker()() as session:
            from app.services.settings import SettingsService

            expression = (await SettingsService(session).get_str("library.scan_cron")).strip()

            if not expression:
                if self._expression:
                    log.info("library scan schedule disabled")
                self._expression = ""
                self._next_due = None
                return False

            if expression != self._expression or self._next_due is None:
                if not croniter.is_valid(expression):
                    if self._warned_about != expression:
                        self._warned_about = expression
                        log.warning("invalid library.scan_cron %r; scheduling disabled", expression)
                    self._expression = expression
                    self._next_due = None
                    return False
                self._expression = expression
                self._warned_about = ""
                # Arm from now: a restart never fires a scan that was due while down.
                self._next_due = croniter(expression, utcnow()).get_next(datetime)
                log.info("next scheduled library scan at %s", self._next_due.isoformat())
                return False

            now = utcnow()
            if now < self._next_due:
                return False

            self._next_due = croniter(expression, now).get_next(datetime)

            if await _scan_already_pending(session):
                log.info("skipping scheduled scan; one is already queued or running")
                return False

            # Incremental, because this one runs unattended every night on a network-backed
            # mount and its whole job is noticing what moved. A manual scan stays complete:
            # the blind spot here is a file rewritten in place, and the button somebody presses
            # when they think something is wrong should not share it.
            job = await enqueue(
                session, JobType.LIBRARY_SCAN, {"full": False, "incremental": True}
            )
            await session.commit()
            log.info("enqueued scheduled library scan %s", job.id)
            return True


async def _scan_already_pending(session: AsyncSession) -> bool:
    pending = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.type == JobType.LIBRARY_SCAN,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    )
    return bool(pending)


_scheduler: ScanScheduler | None = None


def get_scheduler() -> ScanScheduler | None:
    return _scheduler


async def start_scheduler(app: FastAPI | None = None) -> ScanScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_running:
        _scheduler = ScanScheduler()
        _scheduler.start()
    if app is not None:
        app.state.scan_scheduler = _scheduler
    log.info("scan scheduler started")
    return _scheduler


async def stop_scheduler(app: FastAPI | None = None) -> None:
    global _scheduler
    scheduler = _scheduler
    if scheduler is not None:
        await scheduler.stop()
    _scheduler = None
    if app is not None:
        app.state.scan_scheduler = None


__all__ = [
    "ScanScheduler",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
