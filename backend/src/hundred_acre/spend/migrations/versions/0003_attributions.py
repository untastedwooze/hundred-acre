"""Attributions: which Feature each Session worked on.

`refs` holds the issue references found in the Session's transcript (issue number -> signal -> count),
never conversation text. Inferred Attributions are resolved to Features at query time from `refs`;
a manual Attribution names its `target_issue` (null = Overhead) and is never overwritten by `record`.

Revision ID: 0003_attributions
Revises: 0002
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_attributions"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attributions",
        sa.Column("session_id", sa.Text, sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("target_issue", sa.Integer),
        sa.Column("refs", JSONB, nullable=False),
        sa.CheckConstraint("source in ('inferred', 'manual')", name="ck_attributions_source"),
    )


def downgrade() -> None:
    op.drop_table("attributions")
