import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from hundred_acre.main import app
from hundred_acre.spend import record
from hundred_acre.spend.api import get_feature_source, get_spend_engine
from hundred_acre.spend.features import FakeFeatureSource
from hundred_acre.spend.reattribute import list_sessions

from test_spend_cli import _spend_executable
from test_spend_features import issue, session
from test_spend_reattribute import src, two_feature_session


def client(engine, source):
    app.dependency_overrides[get_spend_engine] = lambda: engine
    app.dependency_overrides[get_feature_source] = lambda: source
    return TestClient(app)


def test_sessions_endpoint_lists_and_filters_needs_review(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path, "doubt"), session(tmp_path, "plain", 1)])
    try:
        c = client(spend_engine, src())
        everything = c.get("/api/spend/sessions").json()
        review = c.get("/api/spend/sessions", params={"needs_review": "true"}).json()
    finally:
        app.dependency_overrides.clear()
    assert everything["github_available"] is True
    assert sorted(s["session_id"] for s in everything["sessions"]) == ["doubt", "plain"]
    assert review["sessions"] == [
        {
            "session_id": "doubt",
            "dollars": "10",
            "last_seen": review["sessions"][0]["last_seen"],
            "source": "inferred",
            "target_issue": 1,
            "target_title": "One",
            "low_confidence": True,
        }
    ]


def test_sessions_endpoint_degrades_when_github_is_down(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path)])
    try:
        body = client(spend_engine, FakeFeatureSource({}, available=False)).get("/api/spend/sessions").json()
    finally:
        app.dependency_overrides.clear()
    assert body == {"github_available": False, "sessions": []}


def test_post_attribution_reattributes_and_validates(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path)])
    try:
        c = client(spend_engine, src())
        ok = c.post("/api/spend/sessions/s/attribution", json={"target_issue": 2})
        overhead = c.post("/api/spend/sessions/s/attribution", json={"target_issue": None})
        bad_issue = c.post("/api/spend/sessions/s/attribution", json={"target_issue": 99})
        bad_session = c.post("/api/spend/sessions/zzz/attribution", json={"target_issue": None})
    finally:
        app.dependency_overrides.clear()
    assert (ok.status_code, overhead.status_code, bad_issue.status_code, bad_session.status_code) == (200, 200, 404, 404)
    [row] = list_sessions(spend_engine, src())
    assert (row.source, row.target_issue) == ("manual", None)


def run_cli(engine, *args):
    env = {"SPEND_DATABASE_URL": engine.url.render_as_string(hide_password=False), "PATH": str(Path(sys.executable).parent)}
    return subprocess.run([_spend_executable(), *args], capture_output=True, text=True, env=env)


def test_cli_reattribute_to_issue_and_overhead(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path)])
    done = run_cli(spend_engine, "reattribute", "s", "2", "--no-verify")
    assert done.returncode == 0, done.stderr
    assert list_sessions(spend_engine, src())[0].target_issue == 2
    done = run_cli(spend_engine, "reattribute", "s", "overhead")
    assert done.returncode == 0, done.stderr
    row = list_sessions(spend_engine, src())[0]
    assert (row.source, row.target_issue) == ("manual", None)


def test_cli_reattribute_unknown_session_fails(spend_engine):
    done = run_cli(spend_engine, "reattribute", "nope", "overhead")
    assert done.returncode != 0 and "no such session" in done.stderr
