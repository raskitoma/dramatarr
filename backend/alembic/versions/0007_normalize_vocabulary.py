"""Canonicalize the genres, networks and ratings already in the database.

`app.services.vocab` normalizes these three columns on write, which fixes everything written
from now on and nothing written before. This household's rows are the reason the normalizer
exists — one series carries `["Mecha", "Revenge", "son-in-law", "secret-identity", "ai"]` and
two carry `Dramabox` / `Reelshort` for platforms that spell themselves DramaBox and ReelShort
— so leaving them as they are would mean the editor shows one spelling and the database holds
another.

A data migration rather than a normalize-on-read, for the usual reason: read-time cleanup
still sorts, groups and filters on the raw value, and the genre facet on the browse screen
would keep listing `ai` and `AI` as two separate genres.

It is written to be safe on any row it finds:

* Values are read and rewritten through the same functions the application uses, imported
  rather than copied, so the migration cannot drift from the runtime rule.
* A row whose normalized value equals its stored value is skipped, so re-running this on an
  already-clean database writes nothing.
* Unparseable JSON in `genres` is left exactly as it is rather than replaced with `[]` —
  losing an admin's tags to a migration would be worse than leaving one odd row.

There is no meaningful downgrade. The original casing is not recoverable and was not
information; the downgrade is a no-op that says so.

Revision ID: 0007_normalize_vocabulary
Revises: 0006_series_requests
Create Date: 2024-01-07 00:00:00
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.services.vocab import canonical_content_rating, canonical_network, normalize_genres

revision: str = "0007_normalize_vocabulary"
down_revision: str | None = "0006_series_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, genres, network, content_rating FROM series")
    ).fetchall()

    for series_id, genres_raw, network, rating in rows:
        updates: dict[str, object] = {}

        if genres_raw:
            try:
                current = json.loads(genres_raw)
            except (TypeError, ValueError):
                current = None  # keep whatever is there; see the docstring
            if isinstance(current, list):
                cleaned = normalize_genres(str(item) for item in current)
                if cleaned != current:
                    updates["genres"] = json.dumps(cleaned, ensure_ascii=False)

        cleaned_network = canonical_network(network)
        if cleaned_network != network:
            updates["network"] = cleaned_network

        cleaned_rating = canonical_content_rating(rating)
        if cleaned_rating != rating:
            updates["content_rating"] = cleaned_rating

        if not updates:
            continue

        assignments = ", ".join(f"{column} = :{column}" for column in updates)
        bind.execute(
            sa.text(f"UPDATE series SET {assignments} WHERE id = :id"),
            {**updates, "id": series_id},
        )


def downgrade() -> None:
    """No-op. The pre-normalization spellings were not recorded and are not recoverable."""
