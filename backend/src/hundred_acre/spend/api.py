"""Thin HTTP layer over the Spend module."""

from decimal import Decimal
from functools import lru_cache

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, create_engine

from hundred_acre.spend import total_spend
from hundred_acre.spend.db import spend_database_url
from hundred_acre.spend.features import FeatureSource, GhFeatureSource, feature_spend

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
                "sessions": f.sessions,
                "low_confidence_sessions": f.low_confidence_sessions,
            }
            for f in report.features
        ],
        "overhead_dollars": _dollars(report.overhead_dollars),
        "unresolved_dollars": _dollars(report.unresolved_dollars),
        "unpriced_messages": report.unpriced_messages,
    }


@router.get("/total")
def get_total_spend(engine: Engine = Depends(get_spend_engine)) -> dict[str, str | int]:
    total = total_spend(engine)
    return {
        "dollars": format(total.dollars.normalize(), "f"),  # full precision; the UI rounds to the cent
        "unpriced_messages": total.unpriced_messages,
    }
