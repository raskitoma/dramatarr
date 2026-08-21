"""Remember each folder's mtime, so a scan can skip the ones that did not change.

The nightly scan walked the whole mount and `os.stat`ed all 1141 files every time, on a
network-backed volume, to discover that nothing had moved. The expensive half — ffprobe — was
already skipped for unchanged files; the walk itself was not.

A directory's mtime changes when a file is added to it, removed from it or renamed inside it,
which is exactly the set of events a library scan exists to notice. So one stat per folder
replaces one stat per file, and for this library that is roughly nineteen instead of eleven
hundred.

**The dangerous half is the missing-file sweep, and it is why this table exists rather than
just a flag.** `_sweep_missing` marks every known file it did not see as MISSING. A scan that
skips folders sees none of their files, so without scoping it would mark the entire library
missing on the first incremental run — silently, and in a way that looks exactly like the mount
having gone away. The sweep is therefore restricted to directories actually examined, plus
directories that have vanished from the walk entirely, and this table is what lets the scanner
tell those two apart from "unchanged, deliberately skipped".

What an incremental scan cannot see: a file rewritten in place under the same name. Writing to
a file does not touch its parent's mtime. That is what `full` is for, and it is why the manual
scan stays complete and only the scheduled one goes incremental.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_scan_dirs"
down_revision: str | None = "0010_request_seen"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "scan_directories",
        sa.Column("rel_path", sa.String(length=1000), primary_key=True),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("scan_directories")
