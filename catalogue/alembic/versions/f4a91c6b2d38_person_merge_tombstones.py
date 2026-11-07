"""person merge tombstones

`merge_people` used to DELETE the row it absorbed, on the reasoning that nothing
points at `person` except `person_source` and `title_person` and that no consumer
stored person ids. The first half is still true; the second was an assumption, and
it was wrong -- the downstream app renders cast lists and caches what it renders.
118 ids started answering 404 with no way to discover where they had gone, which is
exactly the failure `title` tombstones exist to prevent.

So `person` gets the same two columns as `title`, and the API resolves them the
same way. Restoring the 118 already-deleted rows is a separate, data-only step
(`dramadex people --restore-tombstones`): it needs a pre-merge backup to read the
mapping out of, which a migration has no business reaching for.

Revision ID: f4a91c6b2d38
Revises: e3c8b1d5a742
Create Date: 2026-08-18 00:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f4a91c6b2d38'
down_revision: str | None = 'e3c8b1d5a742'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN and CREATE INDEX are native on both engines and rebuild nothing.
    # Deliberately NOT batch_alter_table: `person` is the parent of two tables
    # that declare ON DELETE CASCADE, and app/db.py sets PRAGMA foreign_keys=ON,
    # under which batch mode's DROP of the original table fires those cascades.
    # That pattern destroyed 638,600 rows on `title` once already; the FK is
    # skipped on SQLite for the same reason, since only it would force the
    # rebuild. Nothing depends on the constraint -- `merge_people` sets the
    # pointer and `resolve_person` validates the chain.
    op.add_column('person', sa.Column('merged_into_id', sa.Integer(), nullable=True))
    op.add_column('person', sa.Column('merged_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_person_merged_into', 'person', ['merged_into_id'], unique=False)
    if op.get_bind().dialect.name != 'sqlite':
        op.create_foreign_key(
            'fk_person_merged_into', 'person', 'person',
            ['merged_into_id'], ['id'], ondelete='SET NULL',
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint('fk_person_merged_into', 'person', type_='foreignkey')
    op.drop_index('ix_person_merged_into', table_name='person')
    op.drop_column('person', 'merged_at')
    op.drop_column('person', 'merged_into_id')
