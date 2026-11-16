"""Usage Ledger (sessions, messages) and Price Schedule, seeded with base rates.

Rates are dollars per million units (tokens, or web-search requests), checked against
https://platform.claude.com/docs/en/about-claude/pricing on 2026-11-16. They are effective from
2025-01-01, an arbitrary epoch before any recorded usage; historical price changes before that
date were not verified.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = "2025-01-01T00:00:00+00:00"
WEB_SEARCH_PER_MILLION = "10000"  # $10 per 1,000 searches

# model: (input, cache_write_5m, cache_write_1h, cache_read, output) in $/MTok
SEED_RATES = {
    "claude-opus-5-5": ("4", "5", "8", "0.20", "20"),
    "claude-sonnet-5-5": ("2", "2.50", "4", "0.10", "10"),
    "claude-opus-4-8": ("5", "6.25", "10", "0.50", "25"),
}
DIMENSIONS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project_path", sa.Text),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("api_request_id", sa.Text),
        sa.Column("session_id", sa.Text, sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("is_subagent", sa.Boolean, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("usage", JSONB, nullable=False),
        sa.Column("input_tokens", sa.BigInteger, nullable=False),
        sa.Column("output_tokens", sa.BigInteger, nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger, nullable=False),
        sa.Column("cache_write_5m_tokens", sa.BigInteger, nullable=False),
        sa.Column("cache_write_1h_tokens", sa.BigInteger, nullable=False),
        sa.Column("web_search_requests", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])
    price = op.create_table(
        "price_schedule",
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("dimension", sa.Text, nullable=False),
        sa.Column("rate_per_million", sa.Numeric(20, 10), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("model", "dimension", "effective_from"),
    )
    rows = []
    for model, rates in SEED_RATES.items():
        for dim, rate in zip(DIMENSIONS, rates):
            rows.append({"model": model, "dimension": dim, "rate_per_million": rate, "effective_from": EFFECTIVE_FROM})
        rows.append(
            {"model": model, "dimension": "web_search", "rate_per_million": WEB_SEARCH_PER_MILLION, "effective_from": EFFECTIVE_FROM}
        )
    op.bulk_insert(price, rows)


def downgrade() -> None:
    op.drop_table("price_schedule")
    op.drop_table("messages")
    op.drop_table("sessions")
