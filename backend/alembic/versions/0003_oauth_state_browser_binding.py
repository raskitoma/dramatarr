"""Bind a stored OAuth state to the browser that minted it.

Without this column `state` is a bearer token: whoever holds it can complete the round trip
from any browser, so an attacker who authorizes at Google themselves and then hands the
resulting `code`/`state` to a victim as an ordinary link signs that victim into the
*attacker's* account (RFC 6819 §4.4.1.8). The column stores the sha256 of a short-lived,
HttpOnly cookie set by `/api/auth/google/start`.

Nullable because the table may already hold rows: they simply stop being redeemable, which is
correct — they were minted before any browser was recorded, and they expire within minutes.

Revision ID: 0003_oauth_state_browser
Revises: 0002_oauth_identities
Create Date: 2024-01-03 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_oauth_state_browser"
down_revision: str | None = "0002_oauth_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_states",
        sa.Column("browser_hash", sa.String(length=64), nullable=True),
    )
    # In-flight rows carry no binding and must not be completable, so clear them rather than
    # leaving rows a NULL check would have to decide about.
    op.execute("DELETE FROM oauth_states")


def downgrade() -> None:
    op.drop_column("oauth_states", "browser_hash")
