from decimal import Decimal

from sqlalchemy import text

from hundred_acre.spend import record
from hundred_acre.spend.features import FakeFeatureSource, feature_spend

from test_spend_features import issue, session, spend_by_feature
from transcripts import assistant_line, bash_call, creates_issue, text_line, write_transcript


def test_sub_issues_roll_up_to_their_top_level_feature_at_any_depth(spend_engine, tmp_path):
    src = FakeFeatureSource({1: issue(1, "Top"), 2: issue(2, parent=1), 3: issue(3, parent=2), 9: issue(9, "Solo")})
    record(
        spend_engine,
        [
            session(tmp_path, "a", 1, *creates_issue(3, session="a")),  # $10, depth-3 sub-issue
            session(tmp_path, "b", 2, *creates_issue(2, session="b")),  # $20
            session(tmp_path, "c", 4, *creates_issue(9, session="c")),  # $40, standalone issue
        ],
    )
    report = feature_spend(spend_engine, src)
    assert spend_by_feature(report) == {9: Decimal("40"), 1: Decimal("30")}
    assert [f.issue for f in report.features] == [9, 1]  # highest Spend first


def test_sessions_without_issue_references_are_overhead(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1)])
    report = feature_spend(spend_engine, FakeFeatureSource())
    assert report.features == []
    assert report.overhead_dollars == Decimal("10")


def test_references_that_are_not_issues_are_overhead(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1, text_line("see #404", session="a"))])
    assert feature_spend(spend_engine, FakeFeatureSource()).overhead_dollars == Decimal("10")


def test_stronger_signal_beats_more_references_and_flags_low_confidence(spend_engine, tmp_path):
    src = FakeFeatureSource({1: issue(1), 2: issue(2)})
    lines = [*creates_issue(2, session="a"), text_line("#1 #1 #1", session="a")]
    record(spend_engine, [session(tmp_path, "a", 1, *lines)])
    report = feature_spend(spend_engine, src)
    assert spend_by_feature(report) == {2: Decimal("10")}
    assert report.features[0].low_confidence_sessions == 1


def test_closed_beats_branch_beats_mentioned(spend_engine, tmp_path):
    src = FakeFeatureSource({n: issue(n) for n in (2, 3, 4)})
    closes = bash_call("gh issue close 2", tool_id="c", session="s")
    record(spend_engine, [session(tmp_path, "s", 1, closes, text_line("#4", session="s"), branch="ticket-3")])
    assert spend_by_feature(feature_spend(spend_engine, src)) == {2: Decimal("10")}

    record(spend_engine, [session(tmp_path, "t", 1, text_line("#4", session="t"), branch="ticket-3")])
    report = feature_spend(spend_engine, src)
    assert spend_by_feature(report) == {2: Decimal("10"), 3: Decimal("10")}


def test_pr_closing_keyword_counts_as_closed(spend_engine, tmp_path):
    src = FakeFeatureSource({5: issue(5), 6: issue(6)})
    pr = bash_call('gh pr create --body "Closes #6"', tool_id="p", session="s")
    record(spend_engine, [session(tmp_path, "s", 1, text_line("#5", session="s"), pr)])
    assert spend_by_feature(feature_spend(spend_engine, src)) == {6: Decimal("10")}


def test_equal_signals_are_broken_by_reference_count(spend_engine, tmp_path):
    src = FakeFeatureSource({1: issue(1), 2: issue(2)})
    record(spend_engine, [session(tmp_path, "s", 1, text_line("#1 and #2, #2 again", session="s"))])
    assert spend_by_feature(feature_spend(spend_engine, src)) == {2: Decimal("10")}


def test_sub_issues_of_one_feature_are_not_low_confidence(spend_engine, tmp_path):
    src = FakeFeatureSource({1: issue(1), 2: issue(2, parent=1), 3: issue(3, parent=1)})
    record(spend_engine, [session(tmp_path, "s", 1, text_line("#2 #3", session="s"))])
    assert feature_spend(spend_engine, src).features[0].low_confidence_sessions == 0


def test_open_feature_shows_live_spend_as_more_is_recorded(spend_engine, tmp_path):
    src = FakeFeatureSource({1: issue(1)})
    record(spend_engine, [session(tmp_path, "s", 1, *creates_issue(1, session="s"))])
    assert feature_spend(spend_engine, src).features[0].dollars == Decimal("10")
    more = write_transcript(
        tmp_path / "s.jsonl",
        [
            assistant_line("m_s", session="s", output_tokens=1_000_000),
            assistant_line("m_2", session="s", output_tokens=1_000_000),
            *creates_issue(1, session="s"),
        ],
    )
    record(spend_engine, [more])
    top = feature_spend(spend_engine, src).features[0]
    assert (top.dollars, top.state) == (Decimal("20"), "open")


def test_recording_while_github_is_down_resolves_when_queried_later(spend_engine, tmp_path):
    # record never needs GitHub: raw issue references are stored and resolved at query time.
    src = FakeFeatureSource({1: issue(1, "Late")}, available=False)
    record(spend_engine, [session(tmp_path, "s", 1, *creates_issue(1, session="s"))])
    down = feature_spend(spend_engine, src)
    assert not down.github_available
    assert down.features == [] and down.unresolved_dollars == Decimal("10")
    src.available = True
    up = feature_spend(spend_engine, src)
    assert up.github_available and spend_by_feature(up) == {1: Decimal("10")}


def test_overhead_is_still_reported_when_github_is_down(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "a", 1), session(tmp_path, "b", 2, *creates_issue(1, session="b"))])
    report = feature_spend(spend_engine, FakeFeatureSource({}, available=False))
    assert (report.overhead_dollars, report.unresolved_dollars) == (Decimal("10"), Decimal("20"))


def test_rerecording_attributes_a_backfilled_session(spend_engine, tmp_path):
    src = FakeFeatureSource({1: issue(1)})
    t = session(tmp_path, "s", 1, text_line("grilling about #1", session="s"))
    record(spend_engine, [t])
    record(spend_engine, [t])
    assert spend_by_feature(feature_spend(spend_engine, src)) == {1: Decimal("10")}


def test_manual_attribution_survives_a_later_record(spend_engine, tmp_path):
    t = session(tmp_path, "s", 1, *creates_issue(1, session="s"))
    record(spend_engine, [t])
    with spend_engine.begin() as conn:  # what the reattribute ticket will do
        conn.execute(text("update attributions set source = 'manual', target_issue = null where session_id = 's'"))
    record(spend_engine, [t])
    report = feature_spend(spend_engine, FakeFeatureSource({1: issue(1)}))
    assert report.features == [] and report.overhead_dollars == Decimal("10")


def test_no_conversation_text_is_stored(spend_engine, tmp_path):
    secret = "SECRET-PROMPT-TEXT"
    record(spend_engine, [session(tmp_path, "s", 1, text_line(f"{secret} #1", session="s"), *creates_issue(1, session="s"))])
    with spend_engine.connect() as conn:
        dump = " ".join(str(r) for r in conn.execute(text("select * from attributions")).all())
        dump += " ".join(str(r) for r in conn.execute(text("select * from messages")).all())
    assert secret not in dump
