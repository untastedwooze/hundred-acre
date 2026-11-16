from decimal import Decimal

import pytest

from hundred_acre.spend import record
from hundred_acre.spend.features import FakeFeatureSource, FeatureSourceUnavailable, feature_spend
from hundred_acre.spend.reattribute import list_sessions, reattribute

from test_spend_features import issue, session
from transcripts import creates_issue, text_line


def two_feature_session(tmp_path, name="s"):
    # Created #1 (strongest) and merely mentioned #2: attributed to #1, low confidence.
    return session(tmp_path, name, 1, *creates_issue(1, session=name), text_line("see #2", session=name))


def src():
    return FakeFeatureSource({1: issue(1, "One"), 2: issue(2, "Two")})


def test_multi_feature_session_goes_to_strongest_candidate_and_is_low_confidence(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path)])
    [row] = list_sessions(spend_engine, src())
    assert (row.session_id, row.source, row.target_issue, row.target_title, row.low_confidence) == ("s", "inferred", 1, "One", True)
    assert row.dollars == Decimal("10")


def test_needs_review_lists_only_low_confidence_inferred_sessions(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path, "doubt"), session(tmp_path, "sure", 1, *creates_issue(1, session="sure"))])
    assert sorted(r.session_id for r in list_sessions(spend_engine, src())) == ["doubt", "sure"]
    assert [r.session_id for r in list_sessions(spend_engine, src(), needs_review=True)] == ["doubt"]


def test_reattribute_to_a_feature_moves_spend_and_clears_review(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path)])
    reattribute(spend_engine, "s", 2)
    [row] = list_sessions(spend_engine, src())
    assert (row.source, row.target_issue, row.low_confidence) == ("manual", 2, False)
    assert list_sessions(spend_engine, src(), needs_review=True) == []
    report = feature_spend(spend_engine, src())
    assert {f.issue: f.dollars for f in report.features} == {2: Decimal("10")}


def test_reattribute_to_overhead_and_survives_record(spend_engine, tmp_path):
    t = two_feature_session(tmp_path)
    record(spend_engine, [t])
    reattribute(spend_engine, "s", None)
    record(spend_engine, [t])
    [row] = list_sessions(spend_engine, src())
    assert (row.source, row.target_issue, row.target_title) == ("manual", None, None)
    assert feature_spend(spend_engine, src()).overhead_dollars == Decimal("10")


def test_reattribute_rolls_a_sub_issue_up_to_its_feature(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "s", 1)])
    source = FakeFeatureSource({1: issue(1, "Top"), 5: issue(5, "Sub", parent=1)})
    reattribute(spend_engine, "s", 5, source)
    [row] = list_sessions(spend_engine, source)
    assert row.target_issue == 1


def test_reattribute_rejects_unknown_session_and_unknown_issue(spend_engine, tmp_path):
    record(spend_engine, [session(tmp_path, "s", 1)])
    with pytest.raises(LookupError, match="session"):
        reattribute(spend_engine, "nope", None)
    with pytest.raises(LookupError, match="issue"):
        reattribute(spend_engine, "s", 99, src())
    assert list_sessions(spend_engine, src())[0].source == "inferred"


def test_listing_needs_github_to_resolve_references(spend_engine, tmp_path):
    record(spend_engine, [two_feature_session(tmp_path)])
    with pytest.raises(FeatureSourceUnavailable):
        list_sessions(spend_engine, FakeFeatureSource({}, available=False))
