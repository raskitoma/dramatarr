"""Federated sign-in: linked provider accounts and the short-lived OAuth state store.

Revision ID: 0002_oauth_identities
Revises: 0001_initial
Create Date: 2024-01-02 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_oauth_identities"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# As in 0001: `*_at` columns map to app.models.base.UTCDateTime, a TypeDecorator over
# DateTime whose DDL is plain DATETIME, so this file stays free of application imports.


def upgrade() -> None:
    op.create_table(
        "oauth_identities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_sub", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One provider account reaches at most one local account…
        sa.UniqueConstraint("provider", "provider_sub", name="uq_oauth_identity_provider_sub"),
        # …and one local account answers to at most one account per provider.
        sa.UniqueConstraint("provider", "user_id", name="uq_oauth_identity_provider_user"),
    )
    op.create_index(
        op.f("ix_oauth_identities_user_id"), "oauth_identities", ["user_id"], unique=False
    )
    # The lookup every sign-in performs: (provider, subject) → identity.
    op.create_index(
        "ix_oauth_identity_provider_sub",
        "oauth_identities",
        ["provider", "provider_sub"],
        unique=False,
    )

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("invite_id", sa.String(length=32), nullable=True),
        sa.Column("next_path", sa.String(length=512), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invite_id"], ["invites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_oauth_states_state"), "oauth_states", ["state"], unique=True)
    op.create_index(
        "ix_oauth_state_kind_expires", "oauth_states", ["kind", "expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_state_kind_expires", table_name="oauth_states")
    op.drop_index(op.f("ix_oauth_states_state"), table_name="oauth_states")
    op.drop_table("oauth_states")

    op.drop_index("ix_oauth_identity_provider_sub", table_name="oauth_identities")
    op.drop_index(op.f("ix_oauth_identities_user_id"), table_name="oauth_identities")
    op.drop_table("oauth_identities")
