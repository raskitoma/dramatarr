"""Append-only trail of administrative actions.

Rows are written on the caller's session and flushed, never committed here — an admin
mutation and its audit entry must land or fail together.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.services.settings import REDACTED, SECRET_KEYS


async def record(
    session: AsyncSession,
    user_id: str | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Log `action` performed by `user_id` on `entity_type`/`entity_id`."""
    row = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail or {},
    )
    session.add(row)
    await session.flush()
    return row


def scrub(values: Mapping[str, Any]) -> dict[str, Any]:
    """Replace secret setting values before they reach the trail.

    The audit log is readable by any admin and dumped in support bundles, so a TMDB key or
    SMTP password must never be recorded even though the writer legitimately knows it.
    """
    return {key: (REDACTED if key in SECRET_KEYS else value) for key, value in values.items()}


__all__ = ["record", "scrub"]
