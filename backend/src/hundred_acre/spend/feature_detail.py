"""Feature drill-down: one Feature's Spend by Stage, sub-issue, model and Session."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Engine, text

from hundred_acre.spend.attribution import STAGES
from hundred_acre.spend.features import FeatureSource, FeatureSourceUnavailable, IssueInfo, _infer_issue, _root
from hundred_acre.spend.ledger import session_model_spend


@dataclass(frozen=True)
class StageSpend:
    stage: str
    dollars: Decimal


@dataclass(frozen=True)
class SubIssueSpend:
    issue: int  # the Feature's own issue when work was attributed to it directly
    title: str
    dollars: Decimal


@dataclass(frozen=True)
class ModelSpend:
    model: str
    dollars: Decimal


@dataclass(frozen=True)
class SessionSpend:
    session_id: str
    stage: str
    sub_issue: int
    first_seen: datetime
    dollars: Decimal


@dataclass(frozen=True)
class FeatureDetail:
    issue: int
    title: str
    state: str
    dollars: Decimal
    by_stage: list[StageSpend]  # pipeline order; Stages with no Spend are omitted
    by_sub_issue: list[SubIssueSpend]  # highest Spend first
    by_model: list[ModelSpend]  # highest Spend first
    by_session: list[SessionSpend]  # highest Spend first
    unpriced_messages: int


def feature_detail(engine: Engine, source: FeatureSource, issue: int) -> FeatureDetail | None:
    """Spend breakdown for the Feature whose top-level issue is `issue`.

    Returns None if `issue` is not a top-level issue (a sub-issue belongs to its Feature's page).
    Raises FeatureSourceUnavailable if GitHub cannot be reached.
    """
    cache: dict[int, IssueInfo | None] = {}
    feature = _root(source, issue, cache)
    if feature is None or feature.number != issue:
        return None

    with engine.connect() as conn:
        attributions = {
            sid: (src, target, refs, stage)
            for sid, src, target, refs, stage in conn.execute(
                text("select session_id, source, target_issue, refs, stage from attributions")
            )
        }
        first_seen = {sid: t for sid, t in conn.execute(text("select id, first_seen from sessions"))}

    by_session: dict[str, list] = {}  # session -> [stage, sub-issue, dollars]
    by_model: dict[str, Decimal] = {}
    unpriced = 0
    for sid, model, dollars, n in session_model_spend(engine):
        src, target, refs, stage = attributions.get(sid, ("inferred", None, {}, "other"))
        if src == "manual":
            root = _root(source, target, cache) if target is not None else None
            sub = target
        else:
            root, sub, _ = _infer_issue(source, refs, cache)
        if root is None or root.number != feature.number:
            continue
        by_session.setdefault(sid, [stage, sub, Decimal(0)])[2] += dollars
        by_model[model] = by_model.get(model, Decimal(0)) + dollars
        unpriced += n

    stage_totals: dict[str, Decimal] = {}
    sub_totals: dict[int, Decimal] = {}
    for stage, sub, dollars in by_session.values():
        stage_totals[stage] = stage_totals.get(stage, Decimal(0)) + dollars
        sub_totals[sub] = sub_totals.get(sub, Decimal(0)) + dollars

    def title(number: int) -> str:
        info = cache.get(number)
        return info.title if info else f"#{number}"

    by_desc = lambda totals: sorted(totals.items(), key=lambda kv: kv[1], reverse=True)  # noqa: E731
    return FeatureDetail(
        issue=feature.number,
        title=feature.title,
        state=feature.state,
        dollars=sum(stage_totals.values(), Decimal(0)),
        by_stage=[StageSpend(s, stage_totals[s]) for s in STAGES if s in stage_totals],
        by_sub_issue=[SubIssueSpend(n, title(n), d) for n, d in by_desc(sub_totals)],
        by_model=[ModelSpend(m, d) for m, d in by_desc(by_model)],
        by_session=[
            SessionSpend(sid, stage, sub, first_seen[sid], dollars)
            for sid, (stage, sub, dollars) in sorted(by_session.items(), key=lambda kv: kv[1][2], reverse=True)
        ],
        unpriced_messages=unpriced,
    )
