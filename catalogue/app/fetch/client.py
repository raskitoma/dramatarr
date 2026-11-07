"""The fetch pipeline.

Order of operations for every request:

    breaker  ->  robots  ->  rate limit  ->  conditional headers
             ->  HTTP    ->  classify    ->  backoff/retry  ->  archive

`classify` is supplied by the source, not shared, because the failure
signatures genuinely conflict: an empty list body means "no results" on
DramaBox and "you were blocked" on ReelShort.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.fetch.archive import Archive
from app.fetch.breaker import CircuitBreaker
from app.fetch.ratelimit import RateLimiterRegistry
from app.fetch.robots import RobotsCache, RobotsDisallowed
from app.fetch.types import PUSHBACK, RawDoc, Verdict

Classifier = Callable[[RawDoc], Verdict]


def default_classify(doc: RawDoc) -> Verdict:
    if doc.status == 404:
        return Verdict.NOT_FOUND
    if doc.status == 429:
        return Verdict.RATE_LIMITED
    if doc.status in (401, 403, 503):
        return Verdict.CHALLENGE
    if doc.status >= 400:
        return Verdict.ERROR
    if not doc.body:
        return Verdict.EMPTY
    return Verdict.OK


@dataclass(frozen=True)
class FetchResult:
    doc: RawDoc
    verdict: Verdict
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK


def _retry_after(doc: RawDoc) -> float | None:
    raw = doc.header("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        archive: Archive,
        limiters: RateLimiterRegistry | None = None,
        robots: RobotsCache | None = None,
        breaker: CircuitBreaker | None = None,
        *,
        timeout: float = 30.0,
        max_attempts: int = 4,
        obey_robots: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.archive = archive
        self.limiters = limiters or RateLimiterRegistry()
        self.robots = robots or RobotsCache(user_agent)
        self.breaker = breaker
        self.max_attempts = max_attempts
        self.obey_robots = obey_robots
        #: Verdict tally for the run. `challenge` being non-zero is the signal
        #: that pacing is wrong for a source.
        self.verdicts: Counter[str] = Counter()
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        classify: Classifier | None = None,
        revalidate: bool = False,
    ) -> FetchResult:
        if self.breaker is not None:
            self.breaker.check()
        if self.obey_robots and not self.robots.can_fetch(url):
            raise RobotsDisallowed(url)

        classifier = classify or default_classify
        limiter = self.limiters.for_url(url)

        crawl_delay = self.robots.crawl_delay(url)
        if crawl_delay:
            limiter.backoff(crawl_delay)

        request_headers = dict(headers or {})
        if revalidate:
            request_headers.update(self.archive.conditional_headers(url))

        last: FetchResult | None = None
        for attempt in range(1, self.max_attempts + 1):
            with limiter:
                try:
                    resp = self._client.get(url, headers=request_headers)
                except httpx.HTTPError as exc:
                    doc = RawDoc(url=url, status=0, headers={}, body=str(exc).encode())
                    last = FetchResult(doc, Verdict.ERROR)
                    self._record(Verdict.ERROR)
                    self._sleep_backoff(limiter, attempt, None)
                    continue

            doc = RawDoc(
                url=str(resp.url),
                status=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=resp.content,
            )

            # Unchanged since last fetch: serve the archived body.
            if doc.status == 304:
                cached = self.archive.get(url)
                if cached is not None:
                    self._record(Verdict.OK)
                    return FetchResult(cached, Verdict.OK, from_cache=True)

            verdict = classifier(doc)
            self._record(verdict)
            last = FetchResult(doc, verdict)

            if verdict in PUSHBACK:
                self._sleep_backoff(limiter, attempt, _retry_after(doc))
                continue

            if verdict is Verdict.OK:
                self.archive.put(doc)
            return last

        assert last is not None
        return last

    def _record(self, verdict: Verdict) -> None:
        self.verdicts[str(verdict)] += 1
        if self.breaker is not None:
            self.breaker.record(verdict)

    @staticmethod
    def _sleep_backoff(limiter, attempt: int, retry_after: float | None) -> None:
        # Honour Retry-After when offered; otherwise exponential with jitter.
        # ReelShort's WAF challenge clears in ~34s, so the ceiling is generous.
        delay = retry_after if retry_after is not None else min(60.0, 2.0**attempt)
        delay += random.uniform(0, delay * 0.25)
        limiter.backoff(delay)
        time.sleep(delay)
