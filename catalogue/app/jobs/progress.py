"""Progress heartbeats and ETA.

The rate is an EWMA over a short window rather than a lifetime average, so a
run that slows down says so instead of hiding behind a flattering number from
its first minute.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import SyncProgress

#: Smoothing time constant, in seconds. The rate forgets roughly this far
#: back, so an ETA reflects recent throughput rather than the whole run.
TAU_SECONDS = 60.0
#: Do not write to the database more often than this.
MIN_INTERVAL = 2.0
#: Ignore rate samples taken over a shorter window than this. Two updates
#: milliseconds apart imply an enormous items/sec and would produce an ETA of
#: roughly zero -- the first heartbeat of a run is the usual victim.
MIN_SAMPLE_INTERVAL = 0.5


class ProgressReporter:
    def __init__(self, session: Session, run_id: int, total: int | None = None) -> None:
        self.session = session
        self.run_id = run_id
        self.total = total or None
        self._rate: float | None = None
        self._last_time = time.monotonic()
        self._last_done = 0
        self._last_write = 0.0

        row = session.query(SyncProgress).filter_by(run_id=run_id).one_or_none()
        if row is None:
            row = SyncProgress(run_id=run_id, items_total=self.total)
            session.add(row)
            session.commit()
        self.row = row

    def update(
        self, done: int, total: int | None = None, phase: str | None = None, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if total:
            self.total = total

        elapsed = now - self._last_time
        if elapsed >= MIN_SAMPLE_INTERVAL and done >= self._last_done:
            # Sampled on elapsed time, not only on progress. A run that stalls
            # still produces samples of rate 0, so the average decays and the
            # ETA grows. Gating on `done > last_done` instead froze the clock
            # during a stall and kept reporting the pre-stall rate for ever.
            instant = (done - self._last_done) / elapsed
            # Time-weighted decay, so the smoothing constant means the same
            # thing whether samples arrive every 2s or every 30s.
            weight = 1.0 - math.exp(-elapsed / TAU_SECONDS)
            self._rate = (
                instant
                if self._rate is None
                else weight * instant + (1 - weight) * self._rate
            )
            self._last_time = now
            self._last_done = done

        if not force and now - self._last_write < MIN_INTERVAL:
            return
        self._last_write = now

        eta = None
        if self._rate and self._rate > 0 and self.total and self.total > done:
            eta = max(0, int((self.total - done) / self._rate))

        self.row.items_done = done
        self.row.items_total = self.total
        self.row.rate_ewma = self._rate
        self.row.eta_seconds = eta
        if phase is not None:
            # A call that omits the phase must not blank the one already shown.
            self.row.phase = phase
        self.row.heartbeat_at = datetime.now(UTC)
        self.session.commit()

    def finish(self, done: int, phase: str = "done") -> None:
        self.update(done, phase=phase, force=True)
