import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from hundred_acre.main import app
from hundred_acre.spend.api import get_spend_engine
from hundred_acre.spend import record, total_spend

from test_spend_cli import _spend_executable
from transcripts import assistant_line, write_transcript


def test_total_spend_endpoint_returns_recorded_total(spend_engine, tmp_path):
    record(spend_engine, [write_transcript(tmp_path / "s.jsonl", [assistant_line("m1", output_tokens=1_234_567)])])
    app.dependency_overrides[get_spend_engine] = lambda: spend_engine
    try:
        response = TestClient(app).get("/api/spend/total")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {
        "dollars": "12.34567",
        "unpriced_messages": 0,
        "unpriced_tokens": 0,
        "unpriced_models": [],
        "has_unpriced": False,
    }


def test_total_spend_endpoint_flags_unpriced_usage(spend_engine, tmp_path):
    t = write_transcript(
        tmp_path / "s.jsonl", [assistant_line("m1", model="claude-future-9", input_tokens=7, output_tokens=3)]
    )
    record(spend_engine, [t])
    app.dependency_overrides[get_spend_engine] = lambda: spend_engine
    try:
        body = TestClient(app).get("/api/spend/total").json()
    finally:
        app.dependency_overrides.clear()
    assert body["dollars"] == "0"
    assert body["has_unpriced"] is True
    assert body["unpriced_messages"] == 1
    assert body["unpriced_tokens"] == 10
    assert body["unpriced_models"] == ["claude-future-9"]


def test_spend_price_cli_adds_a_price_schedule_entry(spend_engine, tmp_path):
    t = write_transcript(tmp_path / "s.jsonl", [assistant_line("m1", model="claude-future-9", output_tokens=1_000_000)])
    record(spend_engine, [t])
    env = {
        "SPEND_DATABASE_URL": spend_engine.url.render_as_string(hide_password=False),
        "PATH": str(Path(sys.executable).parent),
    }
    result = subprocess.run(
        [_spend_executable(), "price", "claude-future-9", "output", "7.5", "--from", "2025-01-01"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert total_spend(spend_engine).dollars == Decimal("7.5")


def test_spend_record_cli_ingests_a_transcript(spend_engine, tmp_path):
    t = write_transcript(tmp_path / "s.jsonl", [assistant_line("m1", output_tokens=1_000_000)])
    env = {
        "SPEND_DATABASE_URL": spend_engine.url.render_as_string(hide_password=False),
        "PATH": str(Path(sys.executable).parent),
    }
    result = subprocess.run([_spend_executable(), "record", str(t)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert total_spend(spend_engine).dollars == 10
