"""person structured facts and aliases

DramaBox publishes a biography after all -- this project had recorded that it
does not. The earlier check read `bookInfo.performerList`, which carries only
performerId/performerName/performerAvatar/videoCount, and concluded the source
had no such field anywhere. It has one at `/name/{performerId}`, keyed on the
same id we already store, and only in the zhHans locale: `/en/name/{id}` returns
the identical shape with `introduction` empty.

That prose is Chinese, and this index's reader does not read Chinese. So the
facts inside the sentence are extracted -- gender, height, weight, star sign --
and the prose itself is deliberately not stored. `person_alias` holds the Han
form of the name for display, while `person.name` stays romanised and remains
the only thing search touches, because a name the reader cannot type is not a
usable search key.

Revision ID: b8d3e7c2a915
Revises: f4a91c6b2d38
Create Date: 2026-08-18 05:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8d3e7c2a915'
down_revision: str | None = 'f4a91c6b2d38'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN, native on both engines. NOT batch_alter_table: `person`
    # is the parent of person_source, title_person and now person_alias, all
    # CASCADE, and app/db.py sets PRAGMA foreign_keys=ON -- under which batch
    # mode's DROP of the original table fires every one of those cascades. That
    # pattern destroyed 638,600 rows on `title` once already.
    op.add_column('person', sa.Column('gender', sa.String(length=16), nullable=True))
    op.add_column('person', sa.Column('height_cm', sa.Integer(), nullable=True))
    op.add_column('person', sa.Column('weight_kg', sa.Integer(), nullable=True))
    op.add_column('person', sa.Column('zodiac', sa.String(length=16), nullable=True))

    op.create_table(
        'person_alias',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('alias', sa.String(length=255), nullable=False),
        sa.Column('normalized_alias', sa.String(length=255), nullable=False),
        sa.Column('lang', sa.String(length=16), nullable=True),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_id'], ['source.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('person_id', 'normalized_alias', 'source_id',
                            name='uq_person_alias'),
    )
    op.create_index('ix_person_alias_normalized', 'person_alias',
                    ['normalized_alias'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_person_alias_normalized', table_name='person_alias')
    op.drop_table('person_alias')
    op.drop_column('person', 'zodiac')
    op.drop_column('person', 'weight_kg')
    op.drop_column('person', 'height_cm')
    op.drop_column('person', 'gender')
