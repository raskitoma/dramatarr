"""Tag every episode with the edition it came from

`episode` carried `title_id` and `source_id` and nothing else, so two editions of
one show published by one platform were indistinguishable once ingested. That is
not a corner case: 1,080 (title, source) pairs already hold more than one link
-- 501 DramaBox, 391 GoodShort, 188 ReelShort -- from `（DUBBED）` twins and
GoodShort republishes, covering 92,026 episode rows.

Until now they were merely redundant, because every edition was in the same
language. ReelShort's Spanish and Portuguese catalogues change that: title 1065
already answers `/episodes` with 236 rows numbered 1..118 twice, and `?source=`
cannot separate them because both editions ARE the same source. Language is a
property of the LINK, not of the platform, so the row has to name its link.

`title_source_id` is that pointer. Nullable on purpose: the backfill resolves
86.5% of rows by arithmetic that cannot be wrong (a title+source pair holding
exactly one link), and the rest from the response archive, which does not cover
every link. A row that stays NULL is an episode of unknown edition -- still
served, never silently hidden, and never matched by a `?lang=` filter.

Revision ID: a3f7c1e94d26
Revises: c5e2f8a41b70
Create Date: 2026-08-24 23:55:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a3f7c1e94d26'
down_revision: str | None = 'c5e2f8a41b70'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN and CREATE INDEX are native on both engines and rebuild nothing.
    # Deliberately NOT batch_alter_table: `episode` is a child of `title` and
    # `source`, app/db.py sets PRAGMA foreign_keys=ON, and batch mode's DROP of
    # the original table fires every CASCADE -- the mechanism that destroyed
    # 638,600 episode rows once already. Only the FK constraint would force the
    # rebuild, so it is added where ALTER ... ADD CONSTRAINT exists and skipped
    # on SQLite. Nothing depends on the constraint: ingest sets the pointer and
    # the API treats a dangling one as unknown, exactly as it treats NULL.
    op.add_column('episode', sa.Column('title_source_id', sa.Integer(), nullable=True))
    op.create_index('ix_episode_edition', 'episode', ['title_source_id'], unique=False)

    # Not decorative. `title_source` had no index on title_id at all -- lookups
    # by title were satisfied by a sequential scan, which was survivable for the
    # API and fatal for a backfill that asks the question once per episode row.
    # Ingest and `/titles/{id}` both take this path too.
    op.create_index(
        'ix_title_source_title', 'title_source', ['title_id', 'source_id'], unique=False
    )

    if op.get_bind().dialect.name != 'sqlite':
        op.create_foreign_key(
            'fk_episode_title_source', 'episode', 'title_source',
            ['title_source_id'], ['id'], ondelete='CASCADE',
        )

    # The backfill itself is NOT run here. It reads the response archive for the
    # ambiguous rows, needs a source adapter to re-parse with, and takes minutes
    # -- and a migration runs at container start, where a slow upgrade delays
    # every route and a crash loops the deploy. `dramadex fix-episodes
    # --tag-editions --apply` does it, dry-run by default, and is re-runnable.


def downgrade() -> None:
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint('fk_episode_title_source', 'episode', type_='foreignkey')
    op.drop_index('ix_title_source_title', table_name='title_source')
    op.drop_index('ix_episode_edition', table_name='episode')
    op.drop_column('episode', 'title_source_id')
