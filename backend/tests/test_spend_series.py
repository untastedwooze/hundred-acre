from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from hundred_acre.main import app
from hundred_acre.spend import cumulative_spend, record
from hundred_acre.spend.api import get_spend_engine

from transcripts import assistant_line, write_transcript


def _record_two_days(engine, tmp_path):
    record(
        engine,
        [
            write_transcript(
                tmp_path / "s.jsonl",
                [
                    assistant_line("m1", output_tokens=1_000_000, ts="2026-11-14T10:05:00.000Z"),
                    assistant_line("m2", output_tokens=500_000, ts="2026-11-14T10:40:00.000Z"),
                    assistant_line("m3", output_tokens=1_000_000, ts="2026-11-16T03:00:00.000Z"),
                ],
            )
        ],
    )


def test_cumulative_series_is_empty_for_an_empty_ledger(spend_engine):
    series = cumulative_spend(spend_engine)
    assert series.points == []
    assert series.unpriced_messages == 0


def test_cumulative_series_grows_over_time_in_utc(spend_engine, tmp_path):
    _record_two_days(spend_engine, tmp_path)
    points = cumulative_spend(spend_engine).points
    assert [(p.timestamp, p.dollars) for p in points] == [
        (datetime(2026, 11, 14, 10, tzinfo=timezone.utc), Decimal("15")),
        (datetime(2026, 11, 16, 3, tzinfo=timezone.utc), Decimal("25")),
    ]


def test_cumulative_series_excludes_unpriced_messages_and_counts_them(spend_engine, tmp_path):
    record(
        spend_engine,
        [
            write_transcript(
                tmp_path / "s.jsonl",
                [
                    assistant_line("m1", output_tokens=1_000_000),
                    assistant_line(
                        "m2", model="claude-unknown-9", output_tokens=1_000_000, ts="2026-11-17T00:00:00.000Z"
                    ),
                ],
            )
        ],
    )
    series = cumulative_spend(spend_engine)
    assert [p.dollars for p in series.points] == [Decimal("10")]
    assert series.unpriced_messages == 1


def test_series_endpoint_returns_utc_iso_timestamps(spend_engine, tmp_path):
    _record_two_days(spend_engine, tmp_path)
    app.dependency_overrides[get_spend_engine] = lambda: spend_engine
    try:
        response = TestClient(app).get("/api/spend/series")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {
        "points": [
            {"timestamp": "2026-11-14T10:00:00Z", "dollars": "15"},
            {"timestamp": "2026-11-16T03:00:00Z", "dollars": "25"},
        ],
        "unpriced_messages": 0,
    }
