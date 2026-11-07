"""title_source poster_url

Revision ID: aef035733707
Revises: aa2bf58471e8
Create Date: 2026-08-16

Hand-corrected. Autogenerate additionally emitted drop_table() for the FTS5
search index and its shadow tables (title_fts, _data, _idx, _config, _docsize),
because they are created by app/search.py rather than by the ORM and therefore
look like schema drift. Running that dropped three of the five and left the
virtual table in a state where it could not even be DROPped.

alembic/env.py now hides anything named title_fts* from Alembic, so this cannot
recur. The index is derived data; `dramadex reindex` rebuilds it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aef035733707"
down_revision: str | None = "aa2bf58471e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("title_source", schema=None) as batch_op:
        batch_op.add_column(sa.Column("poster_url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("title_source", schema=None) as batch_op:
        batch_op.drop_column("poster_url")
