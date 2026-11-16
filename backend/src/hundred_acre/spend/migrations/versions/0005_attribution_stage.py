"""Stage on each Session's attribution row.

The Stage is inferred from the first pipeline skill the Session runs (see attribution.STAGE_BY_SKILL)
and is stored whether the Attribution is inferred or manual. Existing rows default to 'other' until
their transcripts are recorded again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attributions", sa.Column("stage", sa.Text, nullable=False, server_default="other"))
    op.create_check_constraint(
        "ck_attributions_stage",
        "attributions",
        "stage in ('grill', 'spec', 'implement', 'review', 'fix', 'other')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_attributions_stage", "attributions")
    op.drop_column("attributions", "stage")
