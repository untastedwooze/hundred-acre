import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from hundred_acre.main import app
from hundred_acre.spend import record
from hundred_acre.spend.api import get_feature_source, get_spend_engine
from hundred_acre.spend.features import FakeFeatureSource, FeatureSourceUnavailable, GhFeatureSource

from test_spend_features import issue, session
from transcripts import creates_issue


def get_features(engine, source):
    app.dependency_overrides[get_spend_engine] = lambda: engine
    app.dependency_overrides[get_feature_source] = lambda: source
    try:
        return TestClient(app).get("/api/spend/features")
    finally:
        app.dependency_overrides.clear()


def test_features_endpoint_lists_feature_table_with_overhead(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a")), session(tmp_path, "b", 0.5)])
    response = get_features(spend_engine, FakeFeatureSource({1: issue(1, "Spend tracking")}))
    assert response.status_code == 200
    assert response.json() == {
        "github_available": True,
        "features": [
            {"issue": 1, "title": "Spend tracking", "state": "open", "dollars": "10", "sessions": 1, "low_confidence_sessions": 0}
        ],
        "overhead_dollars": "5",
        "unresolved_dollars": "0",
        "unpriced_messages": 0,
    }


def test_features_endpoint_says_github_is_unavailable_instead_of_failing(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a"))])
    response = get_features(spend_engine, FakeFeatureSource({}, available=False))
    assert response.status_code == 200
    body = response.json()
    assert body["github_available"] is False and body["features"] == [] and body["unresolved_dollars"] == "10"


def fake_gh(responses):
    """A `gh` runner answering `api repos/<repo>/<path>` from a dict; missing paths are 404s."""

    def run(args):
        path = args[1].removeprefix("repos/o/r/")
        if responses.get(path) is FeatureSourceUnavailable:
            raise FeatureSourceUnavailable("offline")
        if path not in responses:
            raise LookupError("404 Not Found")
        return responses[path]

    return run


def test_gh_adapter_reads_title_parent_state_and_first_closed_date():
    gh = GhFeatureSource(
        "o/r",
        run=fake_gh(
            {
                "issues/7": json.dumps({"title": "Features", "state": "closed"}),
                "issues/7/parent": json.dumps({"number": 1}),
                "issues/7/timeline": "2026-11-20T10:00:00Z\n2026-11-18T09:00:00Z\n",
            }
        ),
    )
    assert gh.issue(7) == issue(
        7, "Features", parent=1, state="closed", closed=datetime(2026, 11, 18, 9, tzinfo=timezone.utc)
    )


def test_gh_adapter_reads_ui_surface_from_issue_body():
    gh = GhFeatureSource(
        "o/r", run=fake_gh({"issues/1": json.dumps({"title": "T", "state": "open", "body": "x\nui-surface: spend\n"})})
    )
    assert gh.issue(1).ui_surface == "spend"


def get_surface(engine, source, name):
    app.dependency_overrides[get_spend_engine] = lambda: engine
    app.dependency_overrides[get_feature_source] = lambda: source
    try:
        return TestClient(app).get(f"/api/spend/surfaces/{name}")
    finally:
        app.dependency_overrides.clear()


def test_surface_endpoint_resolves_a_ui_surface_to_its_features_spend(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a"))])
    source = FakeFeatureSource({1: issue(1, "Spend tracking", surface="spend")})
    response = get_surface(spend_engine, source, "spend")
    assert response.status_code == 200
    assert response.json() == {
        "github_available": True,
        "surface": "spend",
        "feature": {"issue": 1, "title": "Spend tracking", "state": "open", "dollars": "10"},
    }


def test_surface_endpoint_404s_for_an_unknown_surface(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a"))])
    response = get_surface(spend_engine, FakeFeatureSource({1: issue(1, "Spend tracking", surface="spend")}), "nope")
    assert response.status_code == 404


def test_surface_endpoint_says_github_is_unavailable(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a"))])
    response = get_surface(spend_engine, FakeFeatureSource({}, available=False), "spend")
    assert response.status_code == 200
    assert response.json() == {"github_available": False, "surface": "spend", "feature": None}


def test_gh_adapter_open_top_level_issue_and_missing_issue():
    gh = GhFeatureSource("o/r", run=fake_gh({"issues/1": json.dumps({"title": "Top", "state": "open"})}))
    assert gh.issue(1) == issue(1, "Top")
    assert gh.issue(99) is None


def test_gh_adapter_reports_unreachable_github_as_unavailable():
    import pytest

    gh = GhFeatureSource("o/r", run=fake_gh({"issues/1": FeatureSourceUnavailable}))
    with pytest.raises(FeatureSourceUnavailable):
        gh.issue(1)
