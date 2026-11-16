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


CLOSED = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)


def test_spend_splits_into_build_and_maintenance_by_the_top_level_issue_first_closed_date(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a"))])  # message at 03:00
    closed = datetime(2026, 11, 16, 4, tzinfo=timezone.utc)
    (f,) = feature_spend(spend_engine, FakeFeatureSource({1: issue(1, state="closed", closed=closed)})).features
    assert (f.build_dollars, f.maintenance_dollars, f.dollars) == (Decimal("10"), Decimal(0), Decimal("10"))
    assert f.first_closed_at == closed


def test_a_session_spanning_the_close_is_split_per_message_and_the_closing_instant_counts_as_build(spend_engine, tmp_path):
    lines = [
        assistant_line("m1", session="a", ts="2026-11-16T02:00:00.000Z", output_tokens=1_000_000),
        assistant_line("m2", session="a", ts="2026-11-16T03:00:00.000Z", output_tokens=1_000_000),
        assistant_line("m3", session="a", ts="2026-11-16T05:00:00.000Z", output_tokens=1_000_000),
        *creates_issue(1, session="a"),
    ]
    record(spend_engine, [write_transcript(tmp_path / "a.jsonl", lines)])
    (f,) = feature_spend(spend_engine, FakeFeatureSource({1: issue(1, state="closed", closed=CLOSED)})).features
    assert (f.build_dollars, f.maintenance_dollars) == (Decimal("20"), Decimal("10"))


def test_bug_sub_issue_worked_after_the_feature_closed_is_maintenance_even_if_reopened(spend_engine, tmp_path):
    bug_session = write_transcript(
        tmp_path / "bug.jsonl",
        [
            assistant_line("b1", session="bug", ts="2026-11-20T00:00:00.000Z", output_tokens=1_000_000),
            *creates_issue(2, session="bug"),
        ],
    )
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a")), bug_session])
    source = FakeFeatureSource(
        {
            1: issue(1, state="open", closed=datetime(2026, 11, 16, 3, 30, tzinfo=timezone.utc)),  # reopened since
            2: issue(2, parent=1),
        }
    )
    (f,) = feature_spend(spend_engine, source).features
    assert (f.build_dollars, f.maintenance_dollars) == (Decimal("10"), Decimal("10"))


def test_a_feature_never_closed_is_all_build(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, *creates_issue(1, session="a"))])
    (f,) = feature_spend(spend_engine, FakeFeatureSource({1: issue(1)})).features
    assert (f.build_dollars, f.maintenance_dollars, f.first_closed_at) == (Decimal("10"), Decimal(0), None)


def test_one_unresolvable_session_does_not_move_other_features_to_unresolved(spend_engine, tmp_path):
    from hundred_acre.spend.features import FeatureSourceUnavailable

    class FlakySource(FakeFeatureSource):
        def issue(self, number):
            if number == 2:
                raise FeatureSourceUnavailable("flaky")
            return super().issue(number)

    record(
        spend_engine,
        [
            session(tmp_path, "ok", 1, *creates_issue(1, session="ok")),
            session(tmp_path, "bad", 2, *creates_issue(2, session="bad")),
        ],
    )
    report = feature_spend(spend_engine, FlakySource({1: issue(1), 2: issue(2)}))
    assert not report.github_available  # some lookup failed: the table may be incomplete
    assert spend_by_feature(report) == {1: Decimal("10")}
    assert report.unresolved_dollars == Decimal("20")
