"""Pricing modifiers: fast mode and long context rates, plus a fast-mode flag on messages.

Rates are dollars per million units, checked against
https://platform.claude.com/docs/en/about-claude/pricing and
https://platform.claude.com/docs/en/build-with-claude/fast-mode on 2026-11-16:

- Fast mode (usage.speed == "fast") has its own input/output rates; cache writes and reads are the
  usual multipliers on the fast input rate (1.25x 5m, 2x 1h, read 0.05x on Opus 5.5 and 0.1x on
  Opus 4.8). Fast mode exists for Fable 5.2, Mythos 5.2, Opus 5.5, Opus 5 and Opus 4.8; only the
  Opus models are seeded because they are the ones used in this project.
- Long context: only Claude Haiku 5.5 is priced by prompt length (over 100,000 tokens, counting
  input + cache reads + cache writes). All other current models include the 1M window at standard
  rates, so they need no long-context rows.
- Web search is $10 per 1,000 searches for every model and is not changed by fast mode.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = "2025-01-01T00:00:00+00:00"
DIMENSIONS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# model: (input, cache_write_5m, cache_write_1h, cache_read, output) in $/MTok
STANDARD = {"claude-haiku-5-5": ("0.10", "0.125", "0.20", "0.01", "0.50")}
FAST = {
    "claude-opus-5-5": ("8", "10", "16", "0.40", "40"),
    "claude-opus-4-8": ("10", "12.50", "20", "1", "50"),
}
LONG_CONTEXT = {"claude-haiku-5-5": ("0.50", "0.625", "1", "0.05", "2.50")}
LONG_CONTEXT_OVER_PROMPT_TOKENS = 100_000


def upgrade() -> None:
    op.add_column("price_schedule", sa.Column("modifier", sa.Text, nullable=False, server_default="standard"))
    op.add_column("price_schedule", sa.Column("above_prompt_tokens", sa.BigInteger))
    op.drop_constraint("price_schedule_pkey", "price_schedule")
    op.create_primary_key("price_schedule_pkey", "price_schedule", ["model", "dimension", "modifier", "effective_from"])
    op.add_column("messages", sa.Column("fast_mode", sa.Boolean, nullable=False, server_default=sa.false()))
    op.execute("update messages set fast_mode = true where usage->>'speed' = 'fast'")

    price = sa.table(
        "price_schedule",
        sa.column("model", sa.Text),
        sa.column("dimension", sa.Text),
        sa.column("modifier", sa.Text),
        sa.column("above_prompt_tokens", sa.BigInteger),
        sa.column("rate_per_million", sa.Numeric),
        sa.column("effective_from", sa.DateTime(timezone=True)),
    )
    rows = []

    def add(table: dict, modifier: str, above: int | None = None) -> None:
        for model, rates in table.items():
            for dim, rate in zip(DIMENSIONS, rates):
                rows.append(
                    {
                        "model": model,
                        "dimension": dim,
                        "modifier": modifier,
                        "above_prompt_tokens": above,
                        "rate_per_million": rate,
                        "effective_from": EFFECTIVE_FROM,
                    }
                )

    add(STANDARD, "standard")
    add(FAST, "fast")
    add(LONG_CONTEXT, "long_context", LONG_CONTEXT_OVER_PROMPT_TOKENS)
    rows.append(
        {
            "model": "claude-haiku-5-5",
            "dimension": "web_search",
            "modifier": "standard",
            "above_prompt_tokens": None,
            "rate_per_million": "10000",
            "effective_from": EFFECTIVE_FROM,
        }
    )
    op.bulk_insert(price, rows)


def downgrade() -> None:
    op.execute("delete from price_schedule where modifier <> 'standard' or model = 'claude-haiku-5-5'")
    op.drop_column("messages", "fast_mode")
    op.drop_constraint("price_schedule_pkey", "price_schedule")
    op.create_primary_key("price_schedule_pkey", "price_schedule", ["model", "dimension", "effective_from"])
    op.drop_column("price_schedule", "above_prompt_tokens")
    op.drop_column("price_schedule", "modifier")
