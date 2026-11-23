"""Ticker History per Security, and the Reference Records each Security was last synced from (ADR 0005).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("securities", sa.Column("reference_records", JSONB, nullable=True))
    op.create_table(
        "ticker_history",
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), primary_key=True),
        sa.Column("events", JSONB, nullable=False),  # every event the source returned; [] means no History (404)
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("fetched_against", JSONB, nullable=False),  # the Security's Reference Records at fetch time
    )


def downgrade() -> None:
    op.drop_table("ticker_history")
    op.drop_column("securities", "reference_records")
