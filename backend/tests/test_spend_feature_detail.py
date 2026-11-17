import pytest
from sqlalchemy import text

from hundred_acre.spend import record
from hundred_acre.spend.feature_detail import feature_detail
from hundred_acre.spend.features import FakeFeatureSource

from test_spend_features import issue, session
from transcripts import creates_issue, skill_call, slash_command


def stage_dollars(detail):
    return {s.stage: format(s.dollars.normalize(), "f") for s in detail.by_stage}


def detail_for(engine, tmp_path, *extra, name="a"):
    record(engine, [session(tmp_path, name, 1, *creates_issue(1, session=name), *extra)])
    return feature_detail(engine, FakeFeatureSource({1: issue(1, "Spend tracking")}), 1)


@pytest.mark.parametrize(
    "skill, stage",
    [
        ("mattpocock-skills:grilling", "grill"),
        ("to-spec", "spec"),
        ("to-tickets", "spec"),
        ("mattpocock-skills:tdd", "implement"),
        ("implement-spec", "implement"),
        ("code-review", "review"),
        ("mattpocock-skills:diagnosing-bugs", "fix"),
    ],
)
def test_stage_comes_from_the_pipeline_skill_in_the_mapping_table(spend_engine, tmp_path, skill, stage):
    detail = detail_for(spend_engine, tmp_path, skill_call(skill, session="a"))
    assert stage_dollars(detail) == {stage: "10"}


def test_stage_is_the_first_pipeline_skill_and_ignores_non_pipeline_skills(spend_engine, tmp_path):
    detail = detail_for(
        spend_engine,
        tmp_path,
        skill_call("run", tool_id="s0", session="a"),
        skill_call("tdd", tool_id="s1", session="a"),
        skill_call("code-review", tool_id="s2", session="a"),
    )
    assert stage_dollars(detail) == {"implement": "10"}


def test_slash_commands_count_as_skills(spend_engine, tmp_path):
    assert stage_dollars(detail_for(spend_engine, tmp_path, slash_command("grilling", session="a"))) == {"grill": "10"}


def test_session_without_a_pipeline_skill_is_other(spend_engine, tmp_path):
    assert stage_dollars(detail_for(spend_engine, tmp_path)) == {"other": "10"}


def dollars(value):
    return format(value.normalize(), "f")


def test_breaks_down_a_features_spend_by_sub_issue_model_and_session(spend_engine, tmp_path):
    source = FakeFeatureSource({1: issue(1, "Top"), 2: issue(2, "Sub", parent=1), 3: issue(3, "Other")})
    record(
        spend_engine,
        [
            session(tmp_path, "a", 1, *creates_issue(1, session="a"), skill_call("tdd", session="a")),
            session(tmp_path, "b", 2, *creates_issue(2, session="b"), skill_call("code-review", session="b")),
            session(tmp_path, "c", 4, *creates_issue(3, session="c")),
            session(tmp_path, "d", 8),
        ],
    )
    detail = feature_detail(spend_engine, source, 1)
    assert (detail.title, detail.state, dollars(detail.dollars)) == ("Top", "open", "30")
    assert [(s.stage, dollars(s.dollars)) for s in detail.by_stage] == [("implement", "10"), ("review", "20")]
    assert [(s.issue, s.title, dollars(s.dollars)) for s in detail.by_sub_issue] == [(2, "Sub", "20"), (1, "Top", "10")]
    assert [(m.model, dollars(m.dollars)) for m in detail.by_model] == [("claude-sonnet-5-5", "30")]
    assert [(s.session_id, s.stage, s.sub_issue, dollars(s.dollars)) for s in detail.by_session] == [
        ("b", "review", 2, "20"),
        ("a", "implement", 1, "10"),
    ]


def test_manual_attribution_places_the_session_under_its_target_sub_issue(spend_engine, tmp_path):
    source = FakeFeatureSource({1: issue(1, "Top"), 2: issue(2, "Sub", parent=1)})
    record(spend_engine, [session(tmp_path, "a", 1, skill_call("tdd", session="a"))])
    with spend_engine.begin() as conn:
        conn.execute(text("update attributions set source = 'manual', target_issue = 2"))
    detail = feature_detail(spend_engine, source, 1)
    assert [(s.session_id, s.stage, s.sub_issue) for s in detail.by_session] == [("a", "implement", 2)]


def test_only_top_level_issues_have_a_detail_view(spend_engine):
    source = FakeFeatureSource({1: issue(1), 2: issue(2, parent=1)})
    assert feature_detail(spend_engine, source, 2) is None
    assert feature_detail(spend_engine, source, 99) is None


def test_detail_splits_spend_into_build_and_maintenance_per_message(spend_engine, tmp_path):
    from datetime import datetime, timezone

    from transcripts import assistant_line, write_transcript

    lines = [
        assistant_line("m1", session="a", ts="2026-11-16T02:00:00.000Z", output_tokens=1_000_000),
        assistant_line("m2", session="a", ts="2026-11-16T03:00:00.000Z", output_tokens=1_000_000),
        assistant_line("m3", session="a", ts="2026-11-16T05:00:00.000Z", output_tokens=1_000_000),
        *creates_issue(1, session="a"),
    ]
    record(spend_engine, [write_transcript(tmp_path / "a.jsonl", lines)])
    closed = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)
    detail = feature_detail(spend_engine, FakeFeatureSource({1: issue(1, state="closed", closed=closed)}), 1)
    assert (detail.build_dollars, detail.maintenance_dollars, detail.dollars) == (20, 10, 30)
