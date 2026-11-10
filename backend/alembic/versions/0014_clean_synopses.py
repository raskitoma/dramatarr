"""Normalize previously imported catalogue synopsis markup."""

import sqlalchemy as sa

from alembic import op
from app.services.synopsis import clean_synopsis

revision = "0014_clean_synopses"
down_revision = "0013_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    rows = db.execute(
        sa.text(
            "SELECT id, overview FROM series WHERE metadata_locked = false "
            "AND overview_source = 'dramadex' AND overview LIKE '%<%>%'"
        )
    ).all()
    for series_id, overview in rows:
        cleaned = clean_synopsis(overview)
        if cleaned and cleaned != overview:
            db.execute(
                sa.text("UPDATE series SET overview = :overview WHERE id = :id"),
                {"id": series_id, "overview": cleaned},
            )


def downgrade() -> None:
    # The original source remains in the catalogue. Do not reintroduce markup here.
    pass
