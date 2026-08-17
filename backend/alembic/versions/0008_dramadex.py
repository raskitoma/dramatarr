"""Bind series to DramaDex, and give them a cast and platform links.

DramaDex is the household's own indexer of DramaBox and ReelShort, and it is the first source
that actually knows this library: measured against these seventeen series, a normalised title
lookup resolves eleven, where TMDB resolved two and both of those were stubs.

Three things arrive with it, and each needed storage:

* `series.dramadex_id` — the binding. Deliberately a *third* id column rather than a reuse of
  `tmdb_id`: they answer different questions and a series can legitimately carry both.
* `series_cast` — who is in it. This genre had no cast data anywhere before. The row keeps
  DramaDex's person id, which is what makes an actor followable to their other shorts rather
  than just a name printed on a page.
* `series_links` — one "Watch on …" per platform, with the URL DramaDex renders from the
  platform's id rather than its slug, so the link survives the retitles these platforms do
  constantly.

Cast and links are **copies**, not foreign keys into the indexer. The series page has to
render when DramaDex is down, moved, or not configured at all.

The downgrade is complete and lossless in the direction that matters: it drops two tables of
cached third-party data and one id column. Nothing an admin typed lives here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_dramadex"
down_revision: str | None = "0007_normalize_vocabulary"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("series") as batch:
        batch.add_column(sa.Column("dramadex_id", sa.Integer(), nullable=True))
    op.create_index("ix_series_dramadex_id", "series", ["dramadex_id"])

    op.create_table(
        "series_cast",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "series_id",
            sa.String(length=32),
            sa.ForeignKey("series.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dramadex_person_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=True),
        sa.Column("billing_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("series_id", "dramadex_person_id", name="uq_series_cast_person"),
    )
    op.create_index("ix_series_cast_series_id", "series_cast", ["series_id"])
    # The index that makes "what else is this actor in" a lookup rather than a scan.
    op.create_index("ix_series_cast_person", "series_cast", ["dramadex_person_id"])

    op.create_table(
        "series_links",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "series_id",
            sa.String(length=32),
            sa.ForeignKey("series.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("studio_key", sa.String(length=32), nullable=False),
        sa.Column("studio_name", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("series_id", "studio_key", name="uq_series_link_studio"),
    )
    op.create_index("ix_series_links_series_id", "series_links", ["series_id"])


def downgrade() -> None:
    op.drop_index("ix_series_links_series_id", table_name="series_links")
    op.drop_table("series_links")
    op.drop_index("ix_series_cast_person", table_name="series_cast")
    op.drop_index("ix_series_cast_series_id", table_name="series_cast")
    op.drop_table("series_cast")
    op.drop_index("ix_series_dramadex_id", table_name="series")
    with op.batch_alter_table("series") as batch:
        batch.drop_column("dramadex_id")
