"""Normalization helpers.

`normalize_title` is the matcher's hot path. Exact-match-on-normalized did
nearly all the work in recon (27/29 IMDb matches); fuzzy matching adds almost
nothing here and imports false positives, because these titles are generated
from a small template grammar.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_title(value: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    'Goodbye, My Ungrateful Family!' -> 'goodbye my ungrateful family'
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = _NON_ALNUM.sub(" ", stripped.lower())
    return _WHITESPACE.sub(" ", cleaned).strip()


def normalize_name(value: str) -> str:
    """Person names use the same rule; kept separate so it can diverge."""
    return normalize_title(value)


def year_from_timestamp(value: str | None) -> int | None:
    """DramaBox publishes no year field, so it is derived from the shelf date.

    Format is 'YYYY-MM-DD HH:MM:SS'.
    """
    if not value or len(value) < 4:
        return None
    try:
        year = int(value[:4])
    except ValueError:
        return None
    return year if 1900 < year < 2200 else None
