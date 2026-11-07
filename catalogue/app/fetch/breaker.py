"""Per-source circuit breaker.

Consecutive pushback or error verdicts trip the source OPEN for the rest of the
day. This is what stops a broken parser or a newly-hostile site from turning
into a thousand pointless requests.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from app.fetch.types import PUSHBACK, Verdict


class BreakerOpen(RuntimeError):
    def __init__(self, source_key: str, until: datetime) -> None:
        super().__init__(f"circuit breaker open for {source_key} until {until:%Y-%m-%d %H:%M}Z")
        self.source_key = source_key
        self.until = until


class CircuitBreaker:
    def __init__(
        self,
        source_key: str,
        threshold: int = 5,
        open_for: timedelta = timedelta(hours=24),
    ) -> None:
        self.source_key = source_key
        self.threshold = threshold
        self.open_for = open_for
        self._lock = threading.Lock()
        self._consecutive = 0
        self._open_until: datetime | None = None

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._check_open()

    def _check_open(self) -> bool:
        if self._open_until is None:
            return False
        if datetime.now(UTC) >= self._open_until:
            self._open_until = None
            self._consecutive = 0
            return False
        return True

    def check(self) -> None:
        """Raise if the source is currently tripped."""
        with self._lock:
            if self._check_open():
                assert self._open_until is not None
                raise BreakerOpen(self.source_key, self._open_until)

    def record(self, verdict: Verdict) -> None:
        with self._lock:
            if verdict in PUSHBACK or verdict is Verdict.ERROR:
                self._consecutive += 1
                if self._consecutive >= self.threshold:
                    self._open_until = datetime.now(UTC) + self.open_for
            else:
                self._consecutive = 0

    def open_until(self) -> datetime | None:
        with self._lock:
            return self._open_until
