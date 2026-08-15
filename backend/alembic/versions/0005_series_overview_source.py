"""Record where a series' overview came from.

Until now `series.overview` was, by construction, TMDB's — there was nowhere else for it to
come from, so nothing had to say so. Wikipedia changes that: its text is licensed for reuse
**with attribution**, which means the page URL is part of the data and not a nice-to-have, and
an admin may deliberately take a synopsis in a language the household does not read (a Korean
drama with no Spanish entry anywhere). Three columns, written and cleared together:

* `overview_source`      — `tmdb` | `wikipedia` | `manual`
* `overview_source_url`  — the page the text came from; the attribution
* `overview_source_lang` — the language of the *text*, not of the household

All three are nullable and no backfill runs. That is the point: NULL is the historical state
and reads as `tmdb` everywhere it matters, so upgrading a live household is an `ALTER TABLE`
and nothing else. A backfill would have to claim a source URL and a language it does not know,
and inventing provenance is worse than admitting there is none — the next metadata refresh
stamps the real values on every unlocked row anyway.

Nullable columns need no `server_default`, so this stays three plain `ADD COLUMN`s: no batch
rebuild of `series`, which four tables point a foreign key at.

Revision ID: 0005_series_overview_source
Revises: 0004_user_is_protected
Create Date: 2024-01-05 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_series_overview_source"
down_revision: str | None = "0004_user_is_protected"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("series", sa.Column("overview_source", sa.String(length=16), nullable=True))
    op.add_column("series", sa.Column("overview_source_url", sa.String(length=1000), nullable=True))
    op.add_column("series", sa.Column("overview_source_lang", sa.String(length=16), nullable=True))


def downgrade() -> None:
    # Plain drops rather than a batch rebuild: four tables point a foreign key at `series`, and
    # recreating it under SQLite to remove three nullable columns risks far more than it fixes.
    op.drop_column("series", "overview_source_lang")
    op.drop_column("series", "overview_source_url")
    op.drop_column("series", "overview_source")
