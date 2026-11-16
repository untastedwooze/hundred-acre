"""Thin HTTP layer over the Spend module."""

from datetime import timezone
from decimal import Decimal
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine

from hundred_acre.spend import cumulative_spend, total_spend
from hundred_acre.spend.db import spend_database_url
from hundred_acre.spend.feature_detail import feature_detail
from hundred_acre.spend.features import FeatureSource, FeatureSourceUnavailable, GhFeatureSource, feature_spend
from hundred_acre.spend.reattribute import list_sessions, reattribute

router = APIRouter(prefix="/api/spend")


@lru_cache
def get_spend_engine() -> Engine:
    return create_engine(spend_database_url())


def get_feature_source() -> FeatureSource:
    return GhFeatureSource()


def _dollars(value: Decimal) -> str:
    return format(value.normalize(), "f")


@router.get("/features")
def get_features(
    engine: Engine = Depends(get_spend_engine), source: FeatureSource = Depends(get_feature_source)
) -> dict:
    """Per-Feature Spend and Overhead. When GitHub is unreachable, `github_available` is false and
    `features` is empty; this endpoint still answers 200 so only the Spend views degrade."""
    report = feature_spend(engine, source)
    return {
        "github_available": report.github_available,
        "features": [
            {
                "issue": f.issue,
                "title": f.title,
                "state": f.state,
                "dollars": _dollars(f.dollars),
                "build_dollars": _dollars(f.build_dollars),
                "maintenance_dollars": _dollars(f.maintenance_dollars),
                "completed_at": f.first_closed_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if f.first_closed_at
                else None,
                "sessions": f.sessions,
                "low_confidence_sessions": f.low_confidence_sessions,
            }
            for f in report.features
        ],
        "overhead_dollars": _dollars(report.overhead_dollars),
        "unresolved_dollars": _dollars(report.unresolved_dollars),
        "unpriced_messages": report.unpriced_messages,
    }


@router.get("/features/{issue}")
def get_feature_detail(
    issue: int, engine: Engine = Depends(get_spend_engine), source: FeatureSource = Depends(get_feature_source)
) -> dict:
    """One Feature's Spend by Stage, sub-issue, model and Session. 404 unless `issue` is a top-level
    issue; when GitHub is unreachable it answers 200 with `github_available` false."""
    try:
        detail = feature_detail(engine, source, issue)
    except FeatureSourceUnavailable:
        return {"github_available": False}
    if detail is None:
        raise HTTPException(status_code=404, detail="not a Feature")
    return {
        "github_available": True,
        "issue": detail.issue,
        "title": detail.title,
        "state": detail.state,
        "dollars": _dollars(detail.dollars),
        "build_dollars": _dollars(detail.build_dollars),
        "maintenance_dollars": _dollars(detail.maintenance_dollars),
        "unpriced_messages": detail.unpriced_messages,
        "by_stage": [{"stage": s.stage, "dollars": _dollars(s.dollars)} for s in detail.by_stage],
        "by_sub_issue": [{"issue": s.issue, "title": s.title, "dollars": _dollars(s.dollars)} for s in detail.by_sub_issue],
        "by_model": [{"model": m.model, "dollars": _dollars(m.dollars)} for m in detail.by_model],
        "by_session": [
            {
                "session_id": s.session_id,
                "stage": s.stage,
                "sub_issue": s.sub_issue,
                "first_seen": s.first_seen.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dollars": _dollars(s.dollars),
            }
            for s in detail.by_session
        ],
    }


@router.get("/surfaces/{name}")
def get_surface_spend(
    name: str, engine: Engine = Depends(get_spend_engine), source: FeatureSource = Depends(get_feature_source)
) -> dict:
    """The Spend of the Feature that declares UI Surface `name` ($0 for a Feature with no Spend yet). 404 if
    no Feature declares it; 200 with `github_available: false` when GitHub is unreachable."""
    unavailable = {"github_available": False, "surface": name, "feature": None}
    report = feature_spend(engine, source)
    if not report.github_available:
        return unavailable
    found = next((f for f in report.features if f.ui_surface == name), None)
    if found is not None:
        feature = {"issue": found.issue, "title": found.title, "state": found.state, "dollars": _dollars(found.dollars)}
    else:
        try:
            declared = source.issues_declaring_surface(name)
        except FeatureSourceUnavailable:
            return unavailable
        if not declared:
            raise HTTPException(status_code=404, detail=f"unknown UI Surface: {name}")
        feature = {"issue": declared[0].number, "title": declared[0].title, "state": declared[0].state, "dollars": "0"}
    return {"github_available": True, "surface": name, "feature": feature}


@router.get("/total")
def get_total_spend(engine: Engine = Depends(get_spend_engine)) -> dict[str, str | int | bool | list[str]]:
    total = total_spend(engine)
    return {
        "dollars": format(total.dollars.normalize(), "f"),  # full precision; the UI rounds to the cent
        "unpriced_messages": total.unpriced_messages,
        "unpriced_tokens": total.unpriced_tokens,
        "unpriced_models": total.unpriced_models,
        "has_unpriced": total.unpriced_messages > 0,  # the totals are incomplete: show the warning
    }


@router.get("/series")
def get_spend_series(engine: Engine = Depends(get_spend_engine)) -> dict:
    series = cumulative_spend(engine)
    return {
        "points": [
            {"timestamp": p.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "dollars": format(p.dollars.normalize(), "f")}
            for p in series.points
        ],
        "unpriced_messages": series.unpriced_messages,
    }


@router.get("/sessions")
def get_sessions(
    needs_review: bool = False,
    engine: Engine = Depends(get_spend_engine),
    source: FeatureSource = Depends(get_feature_source),
) -> dict:
    """Sessions with their Attribution. `needs_review=true` lists only low-confidence ones.
    When GitHub is unreachable, `github_available` is false and `sessions` is empty."""
    try:
        sessions = list_sessions(engine, source, needs_review)
    except FeatureSourceUnavailable:
        return {"github_available": False, "sessions": []}
    return {
        "github_available": True,
        "sessions": [
            {
                "session_id": s.session_id,
                "dollars": _dollars(s.dollars),
                "last_seen": s.last_seen.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": s.source,
                "target_issue": s.target_issue,
                "target_title": s.target_title,
                "low_confidence": s.low_confidence,
            }
            for s in sessions
        ],
    }


class AttributionBody(BaseModel):
    target_issue: int | None  # null = Overhead


@router.post("/sessions/{session_id}/attribution")
def post_attribution(
    session_id: str,
    body: AttributionBody,
    engine: Engine = Depends(get_spend_engine),
    source: FeatureSource = Depends(get_feature_source),
) -> dict:
    """Manually re-attribute a Session to a Feature (any issue in it) or Overhead."""
    try:
        reattribute(engine, session_id, body.target_issue, source)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except FeatureSourceUnavailable:
        raise HTTPException(503, "GitHub is unreachable")
    return {"session_id": session_id, "source": "manual", "target_issue": body.target_issue}
