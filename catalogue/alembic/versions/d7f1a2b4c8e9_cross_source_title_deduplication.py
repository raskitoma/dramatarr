"""cross-source title deduplication

Adds the tombstone pointer, the indexed identity key, and the merge audit/review
table. Backfills `identity_key` for every existing row -- without the backfill the
ingest hook cannot recognise a title it already holds, so the very next sync would
create the duplicates all over again.

Revision ID: d7f1a2b4c8e9
Revises: c46aa6f834ad
Create Date: 2026-08-17 16:05:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd7f1a2b4c8e9'
down_revision: str | None = 'c46aa6f834ad'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NEVER use batch_alter_table on `title`. It is the parent of nine tables that
    # all declare ON DELETE CASCADE, and app/db.py sets `PRAGMA foreign_keys=ON`.
    # Batch mode emulates ALTER by rebuilding the table -- create, copy, DROP the
    # original, rename -- and under FK enforcement SQLite's DROP TABLE performs an
    # implicit DELETE FROM, which fires every one of those cascades. The first
    # draft of this migration did exactly that and destroyed 638,600 episode rows,
    # 10,165 source links and 99,182 tag links on a real catalogue while leaving
    # `title` itself intact, because its rows had already been copied across.
    #
    # The pragma cannot be turned off from here either: Alembic runs SQLite
    # migrations inside a transaction, and `PRAGMA foreign_keys` is a silent no-op
    # in one. So the rule is to avoid the rebuild entirely.
    #
    # ADD COLUMN and CREATE INDEX are both native on SQLite and rebuild nothing.
    # Only the FK constraint forces batch mode, so it is added on engines that
    # support ALTER ... ADD CONSTRAINT and skipped where it would cost the data.
    # Nothing depends on the constraint: `merge_titles` sets the pointer and
    # `resolve_survivor` validates the chain.
    op.add_column('title', sa.Column('identity_key', sa.String(length=512), nullable=True))
    op.add_column('title', sa.Column('merged_into_id', sa.Integer(), nullable=True))
    op.add_column('title', sa.Column('merged_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_title_identity', 'title', ['identity_key'], unique=False)
    op.create_index('ix_title_merged_into', 'title', ['merged_into_id'], unique=False)
    if op.get_bind().dialect.name != 'sqlite':
        op.create_foreign_key(
            'fk_title_merged_into', 'title', 'title',
            ['merged_into_id'], ['id'], ondelete='SET NULL',
        )

    op.create_table(
        'title_merge',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('loser_id', sa.Integer(), nullable=False),
        sa.Column('survivor_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=32), nullable=False),
        sa.Column('key_kind', sa.String(length=32), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('evidence_json', sa.Text(), nullable=False),
        sa.Column('report_json', sa.Text(), nullable=False),
        sa.Column('merged_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['loser_id'], ['title.id']),
        sa.ForeignKeyConstraint(['survivor_id'], ['title.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('loser_id', 'survivor_id', name='uq_title_merge_pair'),
    )
    op.create_index('ix_title_merge_state', 'title_merge', ['state'], unique=False)
    op.create_index('ix_title_merge_loser_id', 'title_merge', ['loser_id'], unique=False)
    op.create_index('ix_title_merge_survivor_id', 'title_merge', ['survivor_id'], unique=False)

    # Backfill identity_key by CALLING app.dedup.match_key, not by reimplementing
    # it. The key has to be byte-identical to what the ingest hook computes at
    # runtime, and a second implementation is a guaranteed drift: the first draft
    # of this migration squashed with an [^a-z0-9] class and wrote '' for the one
    # CJK title in the catalogue, where match_key returns the real characters. A
    # mismatch there is silent -- the hook simply stops recognising that title.
    #
    # Importing application code into a migration is normally worth avoiding, and
    # is right here for exactly that reason. Batched so a 10,000-row catalogue does
    # not build one enormous statement.
    from app.dedup import match_key

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, primary_title FROM title")).all()
    for start in range(0, len(rows), 500):
        connection.execute(
            sa.text("UPDATE title SET identity_key = :k WHERE id = :i"),
            [
                {"i": row[0], "k": match_key(row[1] or "")}
                for row in rows[start:start + 500]
            ],
        )


def downgrade() -> None:
    op.drop_index('ix_title_merge_survivor_id', table_name='title_merge')
    op.drop_index('ix_title_merge_loser_id', table_name='title_merge')
    op.drop_index('ix_title_merge_state', table_name='title_merge')
    op.drop_table('title_merge')

    # Same prohibition as upgrade(): no batch_alter_table on `title`. SQLite has
    # supported native DROP COLUMN since 3.35 (2021), so plain ops suffice and
    # nothing is rebuilt.
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint('fk_title_merged_into', 'title', type_='foreignkey')
    op.drop_index('ix_title_merged_into', table_name='title')
    op.drop_index('ix_title_identity', table_name='title')
    op.drop_column('title', 'merged_at')
    op.drop_column('title', 'merged_into_id')
    op.drop_column('title', 'identity_key')
