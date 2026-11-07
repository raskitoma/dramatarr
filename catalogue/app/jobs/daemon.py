"""The scheduler daemon.

Runs inside the API container on a background thread. Every tick it reaps dead
runs and starts whatever is due.

It does NOT decide whether something already ran today -- the partial unique
index does, and `create_run` surfaces the refusal. That matters because two
ticks (or two containers) can both believe nothing has run yet, and only the
database can settle it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.jobs import scheduler
from app.jobs.scheduler import JobRejected
from app.models import JobType, Source

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Scheduled:
    source_key: str
    job_type: str
    #: Day-of-week for weekly jobs (0 = Monday); None means daily.
    weekday: int | None = None


#: What runs without being asked. Cadences come from PLAN.md 2.7: incremental
#: discovery is cheap (~3 requests for DramaBox), so it runs daily.
#:
#: The full catalogue used to be absent entirely, as a deliberate
#: operator-triggered event. That held while incremental discovery reached
#: everything a full pass would. It does not for two sources, and the gap was
#: invisible for days:
#:
#:   * ReelShort walks its language trees only in `_discover_full`, so the
#:     nightly job never touched Spanish or Portuguese. The catalogue held
#:     exactly ONE Spanish edition while the capability had shipped and looked
#:     delivered; a single manual full run took it to 1,614 es and 1,361 pt.
#:   * NetShort's genre route -- the one its own docstring calls mandatory,
#:     because titles exist that no sitemap lists -- runs only in FULL.
#:
#: A capability that runs only in a mode nobody triggers is a capability that
#: does not run. So these two are weekly and staggered onto different days,
#: because a full pass is hours of origin load, not minutes. DramaBox and
#: GoodShort stay operator-triggered: their incremental discovery already walks
#: their whole tree, so there is no measured gap to close and no reason to spend
#: the requests.
SCHEDULE: tuple[Scheduled, ...] = (
    # First, before anything crawls: if a source has changed its mind about
    # being crawled, we should find that out ourselves rather than by being
    # blocked. One request, and it can disable a source.
    Scheduled("dramabox", JobType.ROBOTS_CHECK),
    Scheduled("dramabox", JobType.INCREMENTAL),
    Scheduled("reelshort", JobType.INCREMENTAL),
    # GoodShort's browse view is newest-first with rel="next", so an incremental
    # pass stops as soon as a page holds nothing new -- typically one or two
    # requests. Cheap enough to sit beside the others.
    Scheduled("goodshort", JobType.INCREMENTAL),
    # Without this netshort is registered, enabled and shown on the dashboard,
    # but `tick()` iterates SCHEDULE -- so no incremental is ever created and the
    # catalogue silently stays at whatever a manual run left behind.
    Scheduled("netshort", JobType.INCREMENTAL),
    # Weekly full passes, on separate days so two multi-hour crawls never share
    # a night. Tuesday and Thursday are arbitrary but fixed -- what matters is
    # that they do not collide with each other or with the Monday roster job.
    Scheduled("reelshort", JobType.FULL_CATALOG, weekday=1),
    Scheduled("netshort", JobType.FULL_CATALOG, weekday=3),
    # Weekly, not daily: the roster is 229 curated pages that change slowly,
    # and it is the expensive job in this list. Monday.
    Scheduled("reelshort", JobType.ACTOR_ROSTER, weekday=0),
    # Key art for whatever the syncs above just discovered. Without this,
    # every title added after the initial run has no poster until someone
    # remembers to run the CLI.
    Scheduled("dramabox", JobType.POSTER_FETCH),
    # Headshots for whatever cast the syncs just discovered, on the same
    # reasoning as poster_fetch: without it, a person found today has no photo
    # until someone remembers to run the CLI.
    Scheduled("dramabox", JobType.AVATAR_FETCH),
    # After the syncs, because it is the syncs that create duplicates: a title
    # newly listed on the second platform is a duplicate the moment it lands.
    # Reports by default and merges nothing unless DRAMADEX_DEDUP_APPLY is set,
    # so leaving it here is safe -- the queue fills, the catalogue does not move.
    Scheduled("dramabox", JobType.DEDUP),
    # Cheap, and the one job whose absence is only discovered at the worst
    # possible moment. Attached to dramabox purely because a run needs a source.
    Scheduled("dramabox", JobType.BACKUP),
)


def due_today(entry: Scheduled) -> bool:
    if entry.weekday is None:
        return True
    return settings.today().weekday() == entry.weekday


def tick() -> dict:
    """One scheduler pass. Returns what it did, for logging and tests."""
    result = {"reaped": [], "started": [], "skipped": []}

    with session_scope() as session:
        result["reaped"] = scheduler.reap_stale(session)
        enabled = {
            s.key for s in session.scalars(select(Source).where(Source.enabled.is_(True)))
        }

    if settings.sync_hour is not None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if datetime.now(ZoneInfo(settings.tz)).hour < settings.sync_hour:
            return result

    for entry in SCHEDULE:
        if entry.source_key not in enabled or not due_today(entry):
            continue
        try:
            with session_scope() as session:
                run = scheduler.create_run(session, entry.source_key, entry.job_type)
                run_id = run.id
        except JobRejected as exc:
            # Almost always "already ran today", which is the rule working.
            result["skipped"].append(f"{entry.source_key}/{entry.job_type}: {exc}")
            continue
        scheduler.spawn(run_id)
        result["started"].append(run_id)
        log.info("scheduled %s/%s as run %s", entry.source_key, entry.job_type, run_id)

    return result


class SchedulerDaemon:
    def __init__(
        self, interval_seconds: int = 300, startup_delay_seconds: float = 15.0
    ) -> None:
        self.interval = interval_seconds
        # Let migrations and app startup settle before the first tick.
        self.startup_delay = startup_delay_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")

    def start(self) -> None:
        self._thread.start()
        log.info("scheduler daemon started (every %ss)", self.interval)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay):
            return
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001 - the daemon must outlive one bad tick
                log.exception("scheduler tick failed")
            if self._stop.wait(self.interval):
                return
