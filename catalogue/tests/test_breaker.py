from __future__ import annotations

from datetime import timedelta

import pytest

from app.fetch.breaker import BreakerOpen, CircuitBreaker
from app.fetch.types import Verdict


def test_consecutive_challenges_trip_the_breaker() -> None:
    breaker = CircuitBreaker("reelshort", threshold=3)
    for _ in range(3):
        breaker.record(Verdict.CHALLENGE)

    assert breaker.is_open
    with pytest.raises(BreakerOpen):
        breaker.check()


def test_a_success_resets_the_run(tmp_path) -> None:
    breaker = CircuitBreaker("reelshort", threshold=3)
    breaker.record(Verdict.CHALLENGE)
    breaker.record(Verdict.CHALLENGE)
    breaker.record(Verdict.OK)
    breaker.record(Verdict.CHALLENGE)

    assert not breaker.is_open


def test_not_found_does_not_trip_the_breaker() -> None:
    """404s are ordinary data -- probing the DramaBox /name id space produces
    them by design. Only pushback and errors count."""
    breaker = CircuitBreaker("dramabox", threshold=3)
    for _ in range(10):
        breaker.record(Verdict.NOT_FOUND)

    assert not breaker.is_open


def test_breaker_closes_again_once_the_window_passes() -> None:
    breaker = CircuitBreaker("reelshort", threshold=1, open_for=timedelta(seconds=-1))
    breaker.record(Verdict.CHALLENGE)

    assert not breaker.is_open
    breaker.check()
