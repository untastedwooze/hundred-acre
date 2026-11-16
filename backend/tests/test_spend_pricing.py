"""Price Schedule fidelity: modifiers, historical rates, and unpriced usage."""

from datetime import datetime, timezone
from decimal import Decimal

from hundred_acre.spend import add_price, record, total_spend

from transcripts import assistant_line, write_transcript


def test_each_message_is_priced_at_the_rate_in_effect_at_its_timestamp(spend_engine, tmp_path):
    add_price(
        spend_engine,
        model="claude-sonnet-5-5",
        dimension="output",
        rate_per_million=Decimal("20"),
        effective_from=datetime(2026, 11, 16, 12, tzinfo=timezone.utc),
    )
    t = write_transcript(
        tmp_path / "sess-1.jsonl",
        [
            assistant_line("msg_before", ts="2026-11-16T11:59:59.000Z", output_tokens=1_000_000),
            assistant_line("msg_after", ts="2026-11-16T12:00:00.000Z", output_tokens=1_000_000),
        ],
    )
    record(spend_engine, [t])
    assert total_spend(spend_engine).dollars == Decimal("30")


def test_fast_mode_messages_are_priced_at_fast_rates_including_cache(spend_engine, tmp_path):
    # claude-opus-5-5 fast: $8 in / $40 out / $10 5m write / $16 1h write / $0.40 read per MTok.
    t = write_transcript(
        tmp_path / "sess-1.jsonl",
        [
            assistant_line(
                "msg_fast",
                model="claude-opus-5-5",
                speed="fast",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                cache_5m=1_000_000,
                cache_1h=1_000_000,
                cache_read=1_000_000,
            ),
            assistant_line("msg_std", model="claude-opus-5-5", speed="standard", output_tokens=1_000_000),
        ],
    )
    record(spend_engine, [t])
    total = total_spend(spend_engine)
    assert total.dollars == Decimal("74.40") + Decimal("20")
    assert total.unpriced_messages == 0


def test_fast_mode_on_a_model_without_fast_rates_is_unpriced_not_standard_priced(spend_engine, tmp_path):
    t = write_transcript(
        tmp_path / "sess-1.jsonl",
        [assistant_line("msg_a", model="claude-sonnet-5-5", speed="fast", output_tokens=1_000_000)],
    )
    record(spend_engine, [t])
    total = total_spend(spend_engine)
    assert total.dollars == Decimal("0")
    assert total.unpriced_messages == 1


def test_long_context_premium_applies_over_the_prompt_threshold(spend_engine, tmp_path):
    # claude-haiku-5-5: <=100k prompt $0.10 in / $0.50 out; >100k prompt $0.50 in / $2.50 out.
    # Prompt length counts input + cache reads + cache writes.
    t = write_transcript(
        tmp_path / "sess-1.jsonl",
        [
            assistant_line("msg_short", model="claude-haiku-5-5", input_tokens=100_000, output_tokens=1_000_000),
            assistant_line(
                "msg_long", model="claude-haiku-5-5", input_tokens=1, cache_read=100_000, output_tokens=1_000_000
            ),
        ],
    )
    record(spend_engine, [t])
    # short: 100k*$0.10/M + $0.50 = 0.51 ; long: 1*$0.50/M + 100k*$0.05/M + $2.50 = 2.5050005
    assert total_spend(spend_engine).dollars == Decimal("3.0150005")


def test_unpriced_usage_is_counted_in_tokens_then_priced_once_the_model_is_added(spend_engine, tmp_path):
    t = write_transcript(
        tmp_path / "sess-1.jsonl",
        [assistant_line("msg_a", model="claude-future-9", input_tokens=10, output_tokens=20, cache_read=5)],
    )
    record(spend_engine, [t])
    before = total_spend(spend_engine)
    assert before.dollars == Decimal("0")
    assert before.unpriced_messages == 1
    assert before.unpriced_tokens == 35
    assert before.unpriced_models == ["claude-future-9"]

    when = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for dim, rate in {"input": "1", "output": "1000000", "cache_read": "0"}.items():
        add_price(spend_engine, model="claude-future-9", dimension=dim, rate_per_million=Decimal(rate), effective_from=when)
    after = total_spend(spend_engine)
    assert after.dollars == Decimal("20.00001")
    assert after.unpriced_messages == 0
    assert after.unpriced_tokens == 0
    assert after.unpriced_models == []
