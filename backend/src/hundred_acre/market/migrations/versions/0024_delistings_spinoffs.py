"""Delistings (terminal outcome per Security) and spinoffs (child linked to parent) (#24).

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delistings",
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), primary_key=True),
        sa.Column("delisted_on", sa.Date, nullable=False),  # last trading day
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("cash_per_share", sa.Float),
        sa.Column("target_security_id", sa.BigInteger, sa.ForeignKey("securities.security_id")),
        sa.Column("ratio", sa.Float),
        sa.Column("assumed", sa.Boolean, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("outcome IN ('cash', 'conversion', 'worthless')", name="delisting_outcome"),
    )
    op.create_table(
        "spinoffs",
        sa.Column("child_security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), primary_key=True),
        sa.Column("parent_security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), nullable=False),
        sa.Column("ratio", sa.Float, nullable=False),  # child shares per parent share held
        sa.Column("spin_date", sa.Date, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("spinoffs")
    op.drop_table("delistings")
