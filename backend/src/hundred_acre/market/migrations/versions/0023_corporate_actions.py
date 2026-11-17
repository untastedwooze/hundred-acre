"""Corporate Actions (splits and dividends), append-only with revisions, and a current view (#23).

Revision ID: 0023
Revises: 0017
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_action_revisions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("source_key", sa.Text, nullable=False),  # Massive's record id (or ticker:date when it has none)
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("security_id", sa.BigInteger, sa.ForeignKey("securities.security_id"), nullable=False),
        sa.Column("action_date", sa.Date, nullable=False),  # split execution date or dividend ex-date
        sa.Column("split_from", sa.Float),
        sa.Column("split_to", sa.Float),
        sa.Column("cash_amount", sa.Float),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "source_key", "revision"),
        sa.CheckConstraint("kind IN ('split', 'dividend')", name="corporate_action_kind"),
    )
    op.create_index("corporate_action_revisions_security", "corporate_action_revisions", ["security_id"])
    op.execute(
        "CREATE VIEW corporate_actions_current AS SELECT DISTINCT ON (kind, source_key) * "
        "FROM corporate_action_revisions ORDER BY kind, source_key, revision DESC"
    )


def downgrade() -> None:
    op.execute("DROP VIEW corporate_actions_current")
    op.drop_table("corporate_action_revisions")
