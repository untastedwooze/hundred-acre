"""Dividend type on Corporate Action revisions, and the market a Security trades on (review fixes).

Revision ID: 0025
Revises: 0021
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0021"
branch_labels = None
depends_on = None

_VIEW = (
    "CREATE VIEW corporate_actions_current AS SELECT DISTINCT ON (kind, source_key) * "
    "FROM corporate_action_revisions ORDER BY kind, source_key, revision DESC"
)


def upgrade() -> None:
    op.execute("DROP VIEW corporate_actions_current")
    op.add_column("corporate_action_revisions", sa.Column("dividend_type", sa.Text))  # Massive: CD, SC, LT, ST
    op.execute(_VIEW)
    op.add_column("securities", sa.Column("market", sa.Text))  # "stocks", "otc", ...; null when unknown (Provisional)


def downgrade() -> None:
    op.drop_column("securities", "market")
    op.execute("DROP VIEW corporate_actions_current")
    op.drop_column("corporate_action_revisions", "dividend_type")
    op.execute(_VIEW)
