from decimal import Decimal

from sqlalchemy import text

from hundred_acre.spend import record, total_spend

from transcripts import assistant_line, write_transcript


def test_recorded_transcript_is_priced_from_tokens_at_schedule_rates(spend_engine, tmp_path):
    # claude-sonnet-5-5: $2 in / $10 out / $2.50 5m write / $4 1h write / $0.10 read per MTok.
    t = write_transcript(
        tmp_path / "sess-1.jsonl",
        [
            assistant_line(
                "msg_a",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                cache_5m=1_000_000,
                cache_1h=1_000_000,
                cache_read=1_000_000,
            )
        ],
    )
    record(spend_engine, [t])
    total = total_spend(spend_engine)
    assert total.dollars == Decimal("18.60")
    assert total.unpriced_messages == 0


def test_repeated_lines_sharing_a_message_id_count_once(spend_engine, tmp_path):
    lines = [
        assistant_line("msg_a", output_tokens=1_000_000, block="thinking"),
        assistant_line("msg_a", output_tokens=1_000_000, block="text"),
        assistant_line("msg_a", output_tokens=1_000_000, block="tool_use"),
    ]
    result = record(spend_engine, [write_transcript(tmp_path / "sess-1.jsonl", lines)])
    assert result.messages_added == 1
    assert total_spend(spend_engine).dollars == Decimal("10")


def test_recording_twice_changes_nothing(spend_engine, tmp_path):
    t = write_transcript(tmp_path / "sess-1.jsonl", [assistant_line("msg_a", output_tokens=500_000)])
    record(spend_engine, [t])
    second = record(spend_engine, [t])
    assert second.messages_added == 0
    assert total_spend(spend_engine).dollars == Decimal("5")


def test_subagent_transcripts_count_toward_the_parent_session(spend_engine, tmp_path):
    main = write_transcript(tmp_path / "sess-1.jsonl", [assistant_line("msg_a", output_tokens=1_000_000)])
    write_transcript(
        tmp_path / "sess-1" / "subagents" / "agent-x.jsonl",
        [assistant_line("msg_sub", output_tokens=1_000_000, subagent=True)],
    )
    result = record(spend_engine, [main])
    assert result.messages_added == 2
    assert total_spend(spend_engine).dollars == Decimal("20")


def test_unknown_model_is_unpriced_not_free(spend_engine, tmp_path):
    t = write_transcript(
        tmp_path / "sess-1.jsonl",
        [
            assistant_line("msg_a", output_tokens=1_000_000),
            assistant_line("msg_b", model="claude-future-9", output_tokens=1_000_000),
        ],
    )
    record(spend_engine, [t])
    total = total_spend(spend_engine)
    assert total.dollars == Decimal("10")
    assert total.unpriced_messages == 1


def test_web_search_requests_are_priced_per_request(spend_engine, tmp_path):
    t = write_transcript(tmp_path / "sess-1.jsonl", [assistant_line("msg_a", web_search=100)])
    record(spend_engine, [t])
    assert total_spend(spend_engine).dollars == Decimal("1")


def test_no_conversation_text_is_stored(spend_engine, tmp_path):
    record(spend_engine, [write_transcript(tmp_path / "sess-1.jsonl", [assistant_line("msg_a", output_tokens=1)])])
    with spend_engine.connect() as conn:
        dump = conn.execute(text("select string_agg(m::text, ' ') from messages m")).scalar()
        dump += conn.execute(text("select string_agg(s::text, ' ') from sessions s")).scalar()
    assert "REDACTED" not in dump
