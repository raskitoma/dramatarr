"""Per-domain rate limiting.

A minimum interval between requests plus a concurrency cap. Both matter:
ReelShort's WAF is triggered by concurrency (clean at 4, challenges at 16),
not by total volume.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

from app.fetch.types import RateSpec


class DomainLimiter:
    """Minimum-interval gate plus a concurrency semaphore, for one domain."""

    def __init__(self, spec: RateSpec) -> None:
        self.spec = spec
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._slots = threading.BoundedSemaphore(spec.concurrency)

    def __enter__(self) -> DomainLimiter:
        self._slots.acquire()
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.spec.min_interval
        return self

    def __exit__(self, *exc: object) -> None:
        self._slots.release()

    def backoff(self, seconds: float) -> None:
        """Push the next allowed time out. Used on Retry-After or a challenge."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


class RateLimiterRegistry:
    """One limiter per domain, created on demand."""

    def __init__(self, default: RateSpec | None = None) -> None:
        self._default = default or RateSpec()
        self._limiters: dict[str, DomainLimiter] = {}
        self._lock = threading.Lock()

    def configure(self, domain: str, spec: RateSpec) -> None:
        with self._lock:
            self._limiters[domain] = DomainLimiter(spec)

    def for_url(self, url: str) -> DomainLimiter:
        domain = urlsplit(url).netloc.lower()
        with self._lock:
            limiter = self._limiters.get(domain)
            if limiter is None:
                limiter = DomainLimiter(self._default)
                self._limiters[domain] = limiter
            return limiter
