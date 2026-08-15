"""Identifier helpers.

Every primary key in dramatarr is a UUIDv4 hex string. Integers would leak how much is in
the library and make URLs guessable; hex strings keep API responses stable across restores.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid

_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_SPACE = re.compile(r"[\s_-]+")


def new_id() -> str:
    """A fresh 32-char hex primary key."""
    return uuid.uuid4().hex


def new_token(nbytes: int = 32) -> str:
    """A URL-safe secret for sessions, invites and magic links."""
    return secrets.token_urlsafe(nbytes)


def slugify(value: str, *, fallback: str = "untitled") -> str:
    """URL-safe slug that keeps non-Latin scripts intact.

    CJK titles must not collapse to an empty string, so we only strip punctuation and
    whitespace rather than forcing ASCII. Callers add a disambiguating suffix on collision.
    """
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    cleaned = _SLUG_STRIP.sub("", normalized)
    slug = _SLUG_SPACE.sub("-", cleaned).strip("-")
    return slug or fallback
