"""person_source avatar_url

Adds the column that remembers where a source publishes an actor's headshot, so
`avatar_fetch` can download it after the crawl rather than during it.

`avatar_image_id` already existed and was never populated -- both adapters parsed
`avatar_url` into PersonCredit and `_sync_cast` dropped it on the floor. This is
the missing half.

Revision ID: e3c8b1d5a742
Revises: d7f1a2b4c8e9
Create Date: 2026-08-17 21:05:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e3c8b1d5a742'
down_revision: str | None = 'd7f1a2b4c8e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN, native on both engines. Deliberately NOT
    # batch_alter_table: `person_source` is a child here, but the rule this repo
    # learned the hard way is that batch mode rebuilds a table by dropping it,
    # and under PRAGMA foreign_keys=ON that fires ON DELETE CASCADE. Never reach
    # for it when a plain ALTER will do.
    op.add_column(
        'person_source',
        sa.Column('avatar_url', sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('person_source', 'avatar_url')
