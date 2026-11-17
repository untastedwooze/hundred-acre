"""Trading Calendar and Coverage (#17).

Revision ID: 0017
Revises: 0001
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trading_calendar",
        sa.Column("calendar_id", sa.Text, nullable=False),
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("open", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close", sa.DateTime(timezone=True), nullable=False),
        sa.Column("half_day", sa.Boolean, nullable=False),
        sa.PrimaryKeyConstraint("calendar_id", "trading_date"),
    )
    op.create_table(
        "coverage",
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), nullable=False),
        sa.Column("interval", sa.Text, nullable=False),
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bar_count", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("security_id", "interval", "trading_date"),
        sa.CheckConstraint("status IN ('complete', 'empty', 'failed', 'partial', 'suspect')", name="coverage_status"),
    )


def downgrade() -> None:
    op.drop_table("coverage")
    op.drop_table("trading_calendar")
