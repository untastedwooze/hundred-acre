"""Request waiters: renewable leases that keep a Request wanted.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_waiters",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.BigInteger, sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("priority", sa.Text, nullable=False),
        sa.Column("lease_expires_at", sa.Float, nullable=False),  # engine clock
    )
    op.create_index("request_waiters_request", "request_waiters", ["request_id"])
    op.create_index("requests_identity_state", "requests", ["identity_key", "state"])


def downgrade() -> None:
    op.drop_index("requests_identity_state", "requests")
    op.drop_table("request_waiters")
