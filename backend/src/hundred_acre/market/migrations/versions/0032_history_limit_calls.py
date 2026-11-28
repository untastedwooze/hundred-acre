"""Calls record the path actually sent and the History Limit applied; Requests can note why they made no Call (ADR-0006).

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("sent_path", sa.Text))  # the window actually sent may differ from the Request's
    op.add_column("calls", sa.Column("history_limit", sa.Date))  # set for Bar endpoints only
    op.add_column("requests", sa.Column("note", sa.Text))  # e.g. why a Request completed without a Call


def downgrade() -> None:
    op.drop_column("requests", "note")
    op.drop_column("calls", "history_limit")
    op.drop_column("calls", "sent_path")
