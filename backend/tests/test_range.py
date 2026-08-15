"""HTTP range requests.

Seeking in a browser is entirely built on these semantics, and getting a boundary off by one
produces a video that plays but cannot be scrubbed — so every edge is pinned here.
"""

from __future__ import annotations

import pytest

from app.services.streaming import parse_range_header

TOTAL = 1000


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-499", (0, 499)),
        ("bytes=500-999", (500, 999)),
        ("bytes=0-", (0, 999)),
        ("bytes=500-", (500, 999)),
        # Suffix form: the LAST n bytes, not "from n to the end".
        ("bytes=-200", (800, 999)),
        ("bytes=-2000", (0, 999)),
        # An end past EOF is clamped rather than rejected.
        ("bytes=900-5000", (900, 999)),
        ("bytes=999-999", (999, 999)),
        # Only the first range of a multi-range request is served.
        ("bytes=0-99,200-299", (0, 99)),
    ],
)
def test_valid_ranges(header: str, expected: tuple[int, int]) -> None:
    assert parse_range_header(header, TOTAL) == expected


def test_absent_range_returns_none() -> None:
    assert parse_range_header(None, TOTAL) is None
    assert parse_range_header("", TOTAL) is None


@pytest.mark.parametrize(
    "header",
    [
        "bytes=1000-1500",  # start at or past EOF
        "bytes=2000-",
        "bytes=500-400",  # inverted
        "bytes=-0",  # zero-length suffix
    ],
)
def test_unsatisfiable_ranges_raise(header: str) -> None:
    """Unsatisfiable must be distinguishable from absent — it becomes a 416, not a 200."""
    with pytest.raises(ValueError):
        parse_range_header(header, TOTAL)


@pytest.mark.parametrize("header", ["items=0-99", "bytes=abc-def", "garbage"])
def test_malformed_ranges_are_ignored(header: str) -> None:
    """A unit we do not understand is treated as no range at all, per RFC 9110."""
    assert parse_range_header(header, TOTAL) is None


def test_zero_length_file_is_unsatisfiable() -> None:
    with pytest.raises(ValueError):
        parse_range_header("bytes=0-", 0)
