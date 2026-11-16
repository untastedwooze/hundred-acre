"""Feature drill-down: one Feature's Spend by Stage, sub-issue, model and Session."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Engine, text

from hundred_acre.spend.attribution import STAGES, Stage
from hundred_acre.spend.features import (
    FeatureSource,
    IssueInfo,
    resolve_attribution,
    root_issue,
    split_build_maintenance,
)
from hundred_acre.spend.ledger import message_spend, session_model_spend


@dataclass(frozen=True)
class StageSpend:
    stage: Stage
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
    stage: Stage
    sub_issue: int
    first_seen: datetime
    dollars: Decimal


@dataclass
class _SessionTotal:
    stage: Stage
    sub_issue: int | None
    dollars: Decimal = Decimal(0)


@dataclass(frozen=True)
class FeatureDetail:
    issue: int
    title: str
    state: str
    dollars: Decimal
    build_dollars: Decimal  # Build Spend: messages at or before the top-level issue first closed
    maintenance_dollars: Decimal  # Maintenance Spend: messages after it first closed
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
    feature = root_issue(source, issue, cache)
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

    messages = message_spend(engine)
    by_session: dict[str, _SessionTotal] = {}
    by_model: dict[str, Decimal] = {}
    build = maintenance = Decimal(0)
    unpriced = 0
    for sid, model, dollars, n in session_model_spend(engine):
        src, target, refs, stage = attributions.get(sid, ("inferred", None, {}, "other"))
        resolution = resolve_attribution(source, src, target, refs, cache)
        if resolution.feature is None or resolution.feature.number != feature.number:
            continue
        if sid not in by_session:
            by_session[sid] = _SessionTotal(stage, resolution.sub_issue)
            session_build, session_maintenance = split_build_maintenance(feature.first_closed_at, messages.get(sid, []))
            build += session_build
            maintenance += session_maintenance
        by_session[sid].dollars += dollars
        by_model[model] = by_model.get(model, Decimal(0)) + dollars
        unpriced += n

    stage_totals: dict[Stage, Decimal] = {}
    sub_totals: dict[int, Decimal] = {}
    for total in by_session.values():
        stage_totals[total.stage] = stage_totals.get(total.stage, Decimal(0)) + total.dollars
        sub_totals[total.sub_issue] = sub_totals.get(total.sub_issue, Decimal(0)) + total.dollars

    def title(number: int) -> str:
        info = cache.get(number)
        return info.title if info else f"#{number}"

    by_desc = lambda totals: sorted(totals.items(), key=lambda kv: kv[1], reverse=True)  # noqa: E731
    return FeatureDetail(
        issue=feature.number,
        title=feature.title,
        state=feature.state,
        dollars=sum(stage_totals.values(), Decimal(0)),
        build_dollars=build,
        maintenance_dollars=maintenance,
        by_stage=[StageSpend(s, stage_totals[s]) for s in STAGES if s in stage_totals],
        by_sub_issue=[SubIssueSpend(n, title(n), d) for n, d in by_desc(sub_totals)],
        by_model=[ModelSpend(m, d) for m, d in by_desc(by_model)],
        by_session=[
            SessionSpend(sid, t.stage, t.sub_issue, first_seen[sid], t.dollars)
            for sid, t in sorted(by_session.items(), key=lambda kv: kv[1].dollars, reverse=True)
        ],
        unpriced_messages=unpriced,
    )
