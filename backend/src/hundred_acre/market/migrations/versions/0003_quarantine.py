"""Quarantined Bars and Provisional Securities (nullable FIGI, verified flag).

Revision ID: 0003_quarantine
Revises: 0002_ticker_periods
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_quarantine"
down_revision = "0002_ticker_periods"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("securities", "share_class_figi", existing_type=sa.Text, nullable=True)
    op.add_column("securities", sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.true()))
    op.create_table(
        "quarantined_bars",
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
        sa.Column("vwap", sa.Float),
        sa.Column("trade_count", sa.BigInteger),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ticker", "trading_date"),
    )


def downgrade() -> None:
    op.drop_table("quarantined_bars")
    op.drop_column("securities", "verified")
    op.execute("DELETE FROM ticker_periods WHERE security_id IN (SELECT security_id FROM securities WHERE share_class_figi IS NULL)")
    op.execute("DELETE FROM securities WHERE share_class_figi IS NULL")
    op.alter_column("securities", "share_class_figi", existing_type=sa.Text, nullable=False)
