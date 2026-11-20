"""Watchlist (#21).

Revision ID: 0021
Revises: 0023
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("watchlist")
