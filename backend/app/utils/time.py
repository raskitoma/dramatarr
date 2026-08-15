"""Time helpers. Everything in dramatarr is timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def in_seconds(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


def in_minutes(minutes: float) -> datetime:
    return utcnow() + timedelta(minutes=minutes)


def in_days(days: float) -> datetime:
    return utcnow() + timedelta(days=days)


def as_aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; re-attach UTC so comparisons never explode."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_expired(value: datetime | None) -> bool:
    aware = as_aware(value)
    return aware is not None and aware <= utcnow()
