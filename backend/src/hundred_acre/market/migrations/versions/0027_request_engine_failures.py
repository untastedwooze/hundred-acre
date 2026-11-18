"""Request Engine failure handling: attempts, retry time and the global 429 pause.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requests", sa.Column("attempts", sa.Integer, nullable=False, server_default="0"))
    op.add_column("requests", sa.Column("retry_at", sa.Float))  # engine clock; null when eligible now
    op.add_column("request_engine_state", sa.Column("paused_until", sa.Float))


def downgrade() -> None:
    op.drop_column("request_engine_state", "paused_until")
    op.drop_column("requests", "retry_at")
    op.drop_column("requests", "attempts")
