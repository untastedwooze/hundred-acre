"""One non-terminal Request per identity key, and a pinned priority for Requests no caller waits on.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requests", sa.Column("pinned_priority", sa.Text, nullable=True))
    # Duplicates created before this index existed: keep the oldest, fail the rest.
    op.execute(
        "UPDATE requests SET state = 'failed', last_error = 'superseded by an identical Request' "
        "WHERE state IN ('queued', 'in_progress', 'abandoned') AND id NOT IN ("
        "SELECT min(id) FROM requests WHERE state IN ('queued', 'in_progress', 'abandoned') GROUP BY identity_key)")
    op.create_index(
        "requests_identity_nonterminal", "requests", ["identity_key"], unique=True,
        postgresql_where=sa.text("state IN ('queued', 'in_progress', 'abandoned')"))


def downgrade() -> None:
    op.drop_index("requests_identity_nonterminal", "requests")
    op.drop_column("requests", "pinned_priority")
