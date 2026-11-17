"""Securities (one per share-class FIGI) and the Tickers they trade under.

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "securities",
        sa.Column("security_id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("share_class_figi", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text),
        sa.Column("type", sa.Text),
    )
    op.create_table(
        "tickers",
        sa.Column("ticker", sa.Text, primary_key=True),
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tickers")
    op.drop_table("securities")
