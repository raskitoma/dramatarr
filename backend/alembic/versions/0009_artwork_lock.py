"""Separate "an admin chose this picture" from "an admin edited this record".

`series.metadata_locked` was doing both jobs and they are not the same decision. Choosing a
poster set it, so picking a picture quietly opted the series out of every future metadata
update — the owner's words: "elegir poster no debería bloquear la actualización". He is right,
and the two states genuinely differ: I want *this* artwork and I want the indexer to keep
correcting the synopsis is an entirely reasonable thing to want.

Worth recording what the audit found, because it changes what the old flag was ever protecting.
`Series.metadata_locked` has exactly **one** reader in the whole application that changes
behaviour, and it is the artwork guard added the same week. There is no scheduled metadata
refresh at all: `workers/scheduler.py` enqueues `LIBRARY_SCAN` and nothing else, so the comments
about "the nightly refresh" describe a job that has not existed since TMDB was removed. The flag
was defending against something that no longer runs.

So after this, `metadata_locked` means only what it says on the series page — a person edited
these fields by hand — and `artwork_locked` is what stops a refresh replacing a chosen poster.

Existing rows: `False` for both. Every series on the NAS was cleared of the old flag when it
turned out to have been set by an automatic fill rather than by anybody, so there is no state
here to carry over. A row that genuinely wants its artwork kept gets the flag the next time
somebody chooses one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_artwork_lock"
down_revision: str | None = "0008_dramadex"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("series") as batch:
        batch.add_column(
            sa.Column(
                "artwork_locked",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("series") as batch:
        batch.drop_column("artwork_locked")
