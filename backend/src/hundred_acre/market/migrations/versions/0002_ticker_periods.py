"""Issuers, Security-to-Issuer link and dated Ticker periods (replaces the flat tickers table).

Revision ID: 0002_ticker_periods
Revises: 0001
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_ticker_periods"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("issuers", sa.Column("cik", sa.Text, primary_key=True), sa.Column("name", sa.Text))
    op.add_column("securities", sa.Column("issuer_cik", sa.Text, sa.ForeignKey("issuers.cik")))
    op.create_table(
        "ticker_periods",
        sa.Column("period_id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), nullable=False),
        sa.Column("valid_from", sa.Date),  # inclusive; null = since before recorded history
        sa.Column("valid_to", sa.Date),  # inclusive; null = still current
    )
    op.create_index("ix_ticker_periods_ticker", "ticker_periods", ["ticker"])
    op.execute("INSERT INTO ticker_periods (ticker, security_id) SELECT ticker, security_id FROM tickers")
    op.drop_table("tickers")


def downgrade() -> None:
    op.create_table(
        "tickers",
        sa.Column("ticker", sa.Text, primary_key=True),
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), nullable=False),
    )
    op.execute(
        "INSERT INTO tickers SELECT DISTINCT ON (ticker) ticker, security_id FROM ticker_periods "
        "ORDER BY ticker, valid_from DESC NULLS LAST"
    )
    op.drop_table("ticker_periods")
    op.drop_column("securities", "issuer_cik")
    op.drop_table("issuers")
