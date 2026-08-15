"""The wishlist table.

One new table and nothing else — no column is added to an existing one, so this migration
cannot disturb a live household's catalogue. `series_requests` starts empty and stays empty
until somebody asks for something.

The two indexes are the two questions the table is ever asked: "what has this member
requested" (covered by the unique constraint's leading column and the explicit index on
`user_id`) and "what is still open, oldest first" (`status, created_at`).

Both foreign keys into `users` are declared, and they differ on purpose: `user_id` cascades
because a member's wishlist belongs to the member, while `resolved_by_user_id` nulls out
because the record that a request *was answered* must outlive the admin who answered it.
`series_id` nulls for the same reason — deleting a series is not a reason to forget that
somebody asked for it.

SQLite enforces `ON DELETE` only when `PRAGMA foreign_keys=ON`, which the app sets on every
connection (`app.db`), so these are real behaviour and not documentation.

Revision ID: 0006_series_requests
Revises: 0005_series_overview_source
Create Date: 2024-01-06 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_series_requests"
down_revision: str | None = "0005_series_overview_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "series_requests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_url", sa.String(length=1000), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("source_domain", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("external_ids", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("series_id", sa.String(length=32), nullable=True),
        sa.Column("resolved_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_series_request_user_key"),
    )
    op.create_index("ix_series_requests_user_id", "series_requests", ["user_id"])
    op.create_index("ix_series_requests_status", "series_requests", ["status"])
    op.create_index("ix_series_requests_series_id", "series_requests", ["series_id"])
    op.create_index("ix_series_requests_dedupe_key", "series_requests", ["dedupe_key"])
    op.create_index(
        "ix_series_requests_status_created", "series_requests", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_series_requests_status_created", table_name="series_requests")
    op.drop_index("ix_series_requests_dedupe_key", table_name="series_requests")
    op.drop_index("ix_series_requests_series_id", table_name="series_requests")
    op.drop_index("ix_series_requests_status", table_name="series_requests")
    op.drop_index("ix_series_requests_user_id", table_name="series_requests")
    op.drop_table("series_requests")
