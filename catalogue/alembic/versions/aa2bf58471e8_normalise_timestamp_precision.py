"""normalise timestamp precision

Revision ID: aa2bf58471e8
Revises: 19e0353eddcd
Create Date: 2026-08-16 17:24:57.688754
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'aa2bf58471e8'
down_revision: str | None = '19e0353eddcd'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Rows written before this migration used server_default=func.now(), which on
# SQLite is CURRENT_TIMESTAMP and stores second granularity:
#     '2026-08-16 22:24:08'
# SQLAlchemy binds a Python datetime with microseconds:
#     '2026-08-16 22:24:08.000000'
# SQLite compares those as strings, so equality never matched. That silently
# truncated keyset pagination at the first page whenever rows shared a
# timestamp -- a downstream client would believe it had synced everything.
#
# Defaults are now generated in Python; this pads the rows already stored so
# every value in the table is comparable.
_COLUMNS = [
    ("title", "created_at"),
    ("title", "updated_at"),
    ("title_source", "first_seen"),
    ("title_source", "last_seen"),
    ("title_change", "observed_at"),
    ("episode", "first_seen"),
    ("image", "fetched_at"),
    ("sync_progress", "heartbeat_at"),
    ("parse_quarantine", "created_at"),
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return  # Postgres stores real timestamps; nothing to normalise.
    for table, column in _COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = {column} || '.000000' "
                f"WHERE {column} IS NOT NULL AND length({column}) = 19"
            )
        )


def downgrade() -> None:
    # Padding is lossless; there is nothing to undo.
    pass
