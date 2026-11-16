from datetime import datetime, timezone
from decimal import Decimal

from hundred_acre.spend import record
from hundred_acre.spend.features import FakeFeatureSource, IssueInfo, feature_spend

from transcripts import assistant_line, bash_call, creates_issue, text_line, tool_output, write_transcript


def issue(number, title="T", parent=None, state="open", closed=None, surface=None):
    return IssueInfo(
        number=number, title=title, parent=parent, state=state, first_closed_at=closed, ui_surface=surface
    )


def session(tmp_path, name, mtok_output, *extra, branch=None):
    """A session costing $10 per output MTok (sonnet) plus extra transcript lines."""
    lines = [assistant_line(f"m_{name}", session=name, output_tokens=int(mtok_output * 1_000_000))]
    if branch:
        lines[0]["gitBranch"] = branch
    return write_transcript(tmp_path / f"{name}.jsonl", lines + list(extra))


def spend_by_feature(report):
    return {f.issue: f.dollars for f in report.features}


def test_session_that_creates_an_issue_is_attributed_to_that_feature(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "s1", 1, *creates_issue(1, session="s1"))])
    report = feature_spend(spend_engine, FakeFeatureSource({1: issue(1, "Spend tracking")}))
    assert report.github_available
    assert [(f.issue, f.title, f.state, f.dollars) for f in report.features] == [
        (1, "Spend tracking", "open", Decimal("10"))
    ]
    assert report.overhead_dollars == 0


def test_ui_surface_line_in_an_issue_body_is_parsed_through_the_source():
    from hundred_acre.spend.features import parse_ui_surface

    assert parse_ui_surface("Intro\n\nui-surface: spend\n\nMore") == "spend"
    assert parse_ui_surface("  UI-Surface:   portfolio-chart  \r\nx") == "portfolio-chart"
    assert parse_ui_surface("no declaration; mentions ui-surface: inline") is None
    assert parse_ui_surface(None) is None


def test_feature_spend_carries_the_features_ui_surface(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "s1", 1, *creates_issue(1, session="s1"))])
    report = feature_spend(spend_engine, FakeFeatureSource({1: issue(1, "Spend", surface="spend")}))
    assert report.features[0].ui_surface == "spend"
