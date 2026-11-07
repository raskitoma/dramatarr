"""Core fetch types.

`RawDoc` is deliberately a plain dataclass over bytes rather than an httpx
object: it can be built from a file on disk, which is what makes classifiers
and parsers testable offline against the fixture corpus.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Verdict(StrEnum):
    OK = "ok"
    CHALLENGE = "challenge"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    EMPTY = "empty"
    ERROR = "error"


#: Verdicts that mean "the site pushed back"; these trip the circuit breaker.
PUSHBACK = frozenset({Verdict.CHALLENGE, Verdict.RATE_LIMITED})


@dataclass(frozen=True)
class RateSpec:
    """Per-source politeness budget."""

    concurrency: int = 2
    min_interval: float = 0.5
    daily_request_budget: int | None = None


@dataclass(frozen=True)
class RawDoc:
    url: str
    status: int
    headers: Mapping[str, str]  # keys lowercased
    body: bytes
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def __len__(self) -> int:
        return len(self.body)
