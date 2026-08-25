"""Mark a series as restricted, and seed it from the ratings already on record.

The household asked for an age filter expressed as a role: "Espectador" and "Espectador-R". The
role itself needs no migration — `users.role` is `String(16)` and a third value fits — but the
series needs somewhere to carry the decision.

**A column, not a rule over `content_rating`.** That field is free text on purpose
(`vocab.canonical_content_rating` preserves whatever an admin or an indexer typed), and DramaDex
writes ReelShort's vocabulary rather than the film/TV standard: measured on this library, 17
series say "All Ages", 3 say "PG-13", 1 says "NC-17" and **4 say nothing at all**. Deriving the
permission from that would leave those four decided by an absence, and would change meaning the
next time an indexer used a new word. One column, one decision, and the rating stays what it is:
information.

**Seeded from the clearly-adult ratings only**, which on this library flags exactly one series.
Everything else starts unrestricted and the admin marks what they want. That is the direction
this errs in, and it is worth being explicit: a series nobody has classified is visible until
somebody says otherwise. The alternative — restricting the four unrated ones — hides real
content from everybody on the strength of a missing field, and gets switched off in irritation
within a week, which protects nothing.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0012_restricted"
down_revision: str | None = "0011_scan_dirs"
branch_labels: str | None = None
depends_on: str | None = None

#: Ratings that mean "adults only" in the vocabularies this library actually contains, plus the
#: standard ones an admin might type. Matched case-insensitively against the stored text.
ADULT_RATINGS = ("NC-17", "R", "TV-MA", "X", "XXX", "18+", "R18+", "NR-18")


def upgrade() -> None:
    with op.batch_alter_table("series") as batch:
        batch.add_column(
            sa.Column("restricted", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    series = sa.table(
        "series", sa.column("restricted", sa.Boolean), sa.column("content_rating", sa.String)
    )
    adult = [rating.upper() for rating in ADULT_RATINGS]
    op.execute(
        series.update()
        .where(sa.func.upper(sa.func.trim(series.c.content_rating)).in_(adult))
        .values(restricted=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("series") as batch:
        batch.drop_column("restricted")
