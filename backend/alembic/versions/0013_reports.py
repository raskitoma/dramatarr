"""Add private reports and reporting restrictions."""

import sqlalchemy as sa

from alembic import op

revision = "0013_reports"
down_revision = "0012_restricted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reporter", sa.String(320), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("section", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reports_created_at", "reports", ["created_at"])
    op.create_table(
        "report_blocks",
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("report_blocks")
    op.drop_table("reports")
