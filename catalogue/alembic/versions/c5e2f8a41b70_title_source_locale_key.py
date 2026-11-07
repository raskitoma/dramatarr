"""title_source locale_key and lang

ReelShort publishes a SEPARATE catalogue per language, and the same show carries
a different `book_id` in each. The crawler only ever walked the English tree, so
a Spanish title was not missed -- it was never a candidate. Adding the other
trees is only safe if an edition can be recognised as the same work, and the
source publishes exactly that: `t_book_id` is `<locale prefix><serial>`, and the
serial is identical across editions (200000 en, 300000 es, 400000 pt).

Measured over 12 listing pages per locale: 0/2/1 serial collisions inside en/es/pt,
77 shared en-es serials and 79 en-pt, every joined pair a correct translation. So
this is a source-published identifier rather than a name match, which is the only
basis this codebase allows identity to rest on.

Revision ID: c5e2f8a41b70
Revises: b8d3e7c2a915
Create Date: 2026-08-18 20:05:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c5e2f8a41b70'
down_revision: str | None = 'b8d3e7c2a915'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN and CREATE INDEX, native on both engines. Never
    # batch_alter_table: it rebuilds by dropping, and under PRAGMA
    # foreign_keys=ON that fires every child CASCADE -- the mechanism that
    # destroyed 638,600 rows on `title` once already.
    op.add_column('title_source', sa.Column('locale_key', sa.String(length=32), nullable=True))
    op.add_column('title_source', sa.Column('lang', sa.String(length=16), nullable=True))
    op.create_index(
        'ix_title_source_locale', 'title_source', ['source_id', 'locale_key'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_title_source_locale', table_name='title_source')
    op.drop_column('title_source', 'lang')
    op.drop_column('title_source', 'locale_key')
