"""Request Engine state: Requests, Calls and the engine row (ADR-0003).

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requests",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("identity_key", sa.Text, nullable=False),  # hash of path + canonical params
        sa.Column("requester", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),  # queued|in_progress|complete|failed
        sa.Column("next_cursor", sa.Text),
        sa.Column("last_error", sa.Text),
        sa.Column("last_status", sa.Integer),
        sa.Column("created_at", sa.Float, nullable=False),  # engine clock, seconds since the epoch
        sa.Column("finished_at", sa.Float),
    )
    op.create_index("requests_state_id", "requests", ["state", "id"])
    op.create_table(
        "calls",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.BigInteger, sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("page", sa.Integer, nullable=False),
        sa.Column("started_at", sa.Float, nullable=False),  # the persisted rate window reads this
        sa.Column("duration", sa.Float),  # seconds; null while the Call is in flight
        sa.Column("http_status", sa.Integer),  # 0 when no HTTP status was received
        sa.Column("response_bytes", sa.Integer),
        sa.Column("body_path", sa.Text),  # relative to the responses directory; null when no body
    )
    op.create_index("calls_started_at", "calls", ["started_at"])
    op.create_table(
        "request_engine_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("state", sa.Text, nullable=False, server_default="running"),
        sa.Column("halt_reason", sa.Text),
        sa.Column("heartbeat_at", sa.Float),
        sa.CheckConstraint("id = 1"),
    )
    op.execute("INSERT INTO request_engine_state (id, state) VALUES (1, 'running')")


def downgrade() -> None:
    op.drop_table("request_engine_state")
    op.drop_table("calls")
    op.drop_table("requests")
