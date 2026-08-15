"""Put the founding account out of the web UI's reach.

Until now the only thing standing between a household and a locked door was the last-admin
guard: any admin could demote, deactivate or delete any other admin, and the account the box
was bootstrapped with — the recovery path of last resort — was no more privileged than one
invited yesterday. `users.is_protected` marks that account; `/api/admin/users` then refuses
every mutation of the row with `PROTECTED_ACCOUNT`.

Two details about applying this to a database that already holds a household:

* The column is NOT NULL, so it needs a `server_default` — an `ALTER TABLE ADD COLUMN` has to
  say what the existing rows get. The default stays on the column afterwards rather than being
  dropped: removing it would mean a batch rebuild of `users`, which is the table half the
  schema points a foreign key at, and buys nothing. `False` is the right answer for a plain
  `INSERT` that omits the column anyway.
* Existing installs get their founder marked here, once. The oldest *active* admin is that
  account by construction — enrollment is invite-only and the bootstrap invite is what mints
  the first admin — and doing it in the migration means it happens exactly once, on upgrade,
  and is never silently re-asserted afterwards over an operator's `unprotect`. Inactive
  admins are skipped on purpose: a protected row cannot be reactivated from the web UI, so
  marking a disabled account would strand it.

Revision ID: 0004_user_is_protected
Revises: 0003_oauth_state_browser
Create Date: 2024-01-04 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_user_is_protected"
down_revision: str | None = "0003_oauth_state_browser"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_protected", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    # `id` breaks a tie on identical timestamps so the statement is deterministic; SQLite is
    # the only shipped backend, hence the 0/1 comparison on `is_active`.
    op.execute(
        """
        UPDATE users
           SET is_protected = 1
         WHERE id = (
                 SELECT id
                   FROM users
                  WHERE role = 'admin' AND is_active = 1
                  ORDER BY created_at ASC, id ASC
                  LIMIT 1
               )
        """
    )


def downgrade() -> None:
    op.drop_column("users", "is_protected")
