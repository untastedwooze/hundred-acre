"""Requests carry a Priority Class, stored by name.

Revision ID: 0028
Revises: 0026
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requests", sa.Column("priority", sa.Text, nullable=False, server_default="backfill"))


def downgrade() -> None:
    op.drop_column("requests", "priority")
