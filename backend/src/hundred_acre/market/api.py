"""Thin HTTP layer over the market package: Ticker resolution and Factor Exposures (#47)."""

from datetime import date
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Engine, create_engine

from hundred_acre.market.db import market_database_url
from hundred_acre.market.factors import FACTOR_LABELS, FRENCH_ATTRIBUTION, MODELS, latest_vintage, lookup_factor_exposures
from hundred_acre.market.lake import Lake
from hundred_acre.market.reference import security_on

router = APIRouter(prefix="/api/market")


@lru_cache
def get_market_engine() -> Engine:
    return create_engine(market_database_url())


@lru_cache
def get_lake() -> Lake:
    return Lake.from_env()


def _vintage(lake: Lake) -> dict | None:
    v = latest_vintage(lake)
    if v is None:
        return None
    return {"vintage": v.vintage, "imported_at": v.imported_at.isoformat(), "through_date": str(v.last_date)}


@router.get("/securities/{ticker}")
def resolve_ticker(ticker: str, on: date | None = None, engine: Engine = Depends(get_market_engine)) -> dict:
    """The Security a Ticker referred to on a date (default today)."""
    on = on or date.today()
    security_id = security_on(engine, ticker, on)
    if security_id is None:
        raise HTTPException(404, f"unknown Ticker {ticker!r} on {on}")
    return {"ticker": ticker, "security_id": security_id, "on": str(on)}


@router.get("/factors/vintage")
def factor_vintage(lake: Lake = Depends(get_lake)) -> dict:
    """The Reference Factor Return vintage in use and its through-date; `vintage` is null before the first import."""
    return {**(_vintage(lake) or {"vintage": None, "imported_at": None, "through_date": None}), "attribution": FRENCH_ATTRIBUTION}


@router.get("/factors/exposures")
def factor_exposures(
    ticker: str,
    model: Literal["ff6", "ff3", "capm"] = "ff6",
    on: date | None = None,
    engine: Engine = Depends(get_market_engine),
    lake: Lake = Depends(get_lake),
) -> dict:
    """One Security's rolling Factor Exposures for a model. Always 200: when there is nothing to chart,
    `series` is empty and `empty_reason`/`message` say why."""
    on = on or date.today()
    vintage = _vintage(lake)
    base = {"ticker": ticker, "model": model, "vintage": vintage, "attribution": FRENCH_ATTRIBUTION, "series": []}

    found = lookup_factor_exposures(engine, lake, ticker, model, on)
    if found.exposures is None:
        return {**base, "empty_reason": found.empty_reason, "message": found.message}
    df, security_id = found.exposures, found.security_id
    series = [
        {
            "window_start": str(r["window_start"]), "window_end": str(r["window_end"]), "n_obs": r["n_obs"],
            "alpha": r["alpha"], "t_alpha": r["t_alpha"], "r_squared": r["r_squared"],
            "betas": [{"factor": FACTOR_LABELS[f], "value": r[f"beta_{f}"], "t": r[f"t_{f}"]} for f in MODELS[model]],
        }
        for r in df.sort("window_end").iter_rows(named=True)
    ]
    return {**base, "security_id": security_id, "series": series, "empty_reason": None, "message": None}
