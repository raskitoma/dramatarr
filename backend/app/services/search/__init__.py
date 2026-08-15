"""Multilingual search: normalization, the FTS5 index, and the query path."""

from app.services.search.index import (
    ensure_index,
    ensure_title_variants,
    rebuild_index,
    reindex_series,
)
from app.services.search.normalize import detect_script, normalize, romanize, search_keys
from app.services.search.service import search_library

__all__ = [
    "detect_script",
    "ensure_index",
    "ensure_title_variants",
    "normalize",
    "rebuild_index",
    "reindex_series",
    "romanize",
    "search_keys",
    "search_library",
]
