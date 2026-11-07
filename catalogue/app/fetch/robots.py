"""robots.txt handling.

Two jobs: decide whether a URL may be fetched, and detect when a site's rules
change. The second matters -- if a source tightens its robots.txt we want to
stop on our own, rather than find out by being blocked.
"""

from __future__ import annotations

import hashlib
import threading
from urllib.parse import urlsplit, urlunsplit

import httpx
from protego import Protego


class RobotsCache:
    def __init__(self, user_agent: str, timeout: float = 15.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._lock = threading.Lock()
        self._parsers: dict[str, Protego | None] = {}
        self._digests: dict[str, str] = {}

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def _load(self, origin: str) -> tuple[Protego | None, str]:
        robots_url = f"{origin}/robots.txt"
        try:
            resp = httpx.get(
                robots_url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        except httpx.HTTPError:
            # Unreachable robots.txt is not permission to crawl freely, but it
            # is also not a reason to halt; treat as no rules and let the rate
            # limiter and breaker do their work.
            return None, ""
        if resp.status_code != 200:
            return None, ""
        text = resp.text
        return Protego.parse(text), hashlib.sha256(text.encode()).hexdigest()

    def _parser(self, origin: str) -> Protego | None:
        with self._lock:
            if origin in self._parsers:
                return self._parsers[origin]
        parser, digest = self._load(origin)
        with self._lock:
            self._parsers[origin] = parser
            if digest:
                self._digests[origin] = digest
        return parser

    def can_fetch(self, url: str) -> bool:
        parser = self._parser(self._origin(url))
        if parser is None:
            return True
        return bool(parser.can_fetch(url, self.user_agent))

    def crawl_delay(self, url: str) -> float | None:
        parser = self._parser(self._origin(url))
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    def digest(self, url: str) -> str | None:
        """sha256 of the live robots.txt, for change detection."""
        origin = self._origin(url)
        self._parser(origin)
        with self._lock:
            return self._digests.get(origin)


class RobotsDisallowed(RuntimeError):
    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows {url}")
        self.url = url
