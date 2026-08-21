"""Let a member be told once that their request landed.

Until now a fulfilled request changed colour on a screen nobody had a reason to open. The
member who asked for a show found out by going to look, which for the household's actual use —
ask, forget, travel, come back — means finding out weeks late or not at all.

`seen_at` is the whole mechanism. It records when *this member* acknowledged the answer to
their own request, so the alert can be shown once and then stop, on every device rather than on
whichever browser happened to see it first. Client-side "last checked" state was the other
option and it is wrong here: the household reads this app on a laptop, an iPad and a phone, and
a badge that clears on one and nags on the other two is worse than no badge.

Only *terminal* answers are worth acknowledging — fulfilled and declined. `pending` and
`approved` are the admin saying "not yet", and nagging somebody about a thing that has not
happened is what makes people stop reading notifications.

Existing rows: NULL, which reads as "not seen". That is deliberate rather than convenient — a
member with a request already fulfilled has genuinely never been told, so telling them once now
is correct, not a false alarm.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_request_seen"
down_revision: str | None = "0009_artwork_lock"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("series_requests") as batch:
        batch.add_column(sa.Column("seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("series_requests") as batch:
        batch.drop_column("seen_at")
