"""Resolve a Ticker and read its Factor Exposures, explaining an empty answer (shared by the API and the CLI, #43 #47)."""

from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.factors.exposures import read_factor_exposures
from hundred_acre.market.factors.reference_returns import latest_vintage
from hundred_acre.market.lake import Lake, read_bars
from hundred_acre.market.reference import security_on

UNKNOWN_TICKER = "unknown_ticker"
NO_FACTOR_RETURNS = "no_factor_returns"
TOO_LITTLE_HISTORY = "too_little_history"
FACTOR_RETURNS_NOT_AT_MONTH_END = "factor_returns_not_at_month_end"


@dataclass(frozen=True)
class ExposureLookup:
    """`exposures` is None exactly when `empty_reason` and `message` are set."""

    security_id: int | None
    exposures: pl.DataFrame | None
    empty_reason: str | None = None
    message: str | None = None


def _month_end_exists(engine: Engine, start: date, end: date) -> bool:
    """Whether a month-end trading day lies in [start, end]."""
    with engine.connect() as conn:
        days = [
            r[0] for r in conn.execute(
                text("SELECT trading_date FROM trading_calendar WHERE calendar_id = 'XNYS' AND trading_date >= :s "
                     "AND trading_date <= :e ORDER BY trading_date"),
                {"s": start, "e": end + timedelta(days=10)},
            )
        ]
    return any((d.year, d.month) != (nxt.year, nxt.month) and d <= end for d, nxt in zip(days, days[1:]))


def lookup_factor_exposures(engine: Engine, lake: Lake, ticker: str, model: str, on: date) -> ExposureLookup:
    """Resolve `ticker` as of `on` and read its `model` Factor Exposures with window end on or before `on`.

    An empty answer says why: an unknown Ticker, no Reference Factor Returns imported yet, Factor Returns that do not
    reach a month-end since the Security's first Bar, or otherwise too little history for a window.
    """
    label = model.upper()
    security_id = security_on(engine, ticker, on)
    if security_id is None:
        return ExposureLookup(None, None, UNKNOWN_TICKER, f"Unknown Ticker {ticker!r} on {on}: no Security has it.")
    vintage = latest_vintage(lake)
    if vintage is None:
        return ExposureLookup(
            security_id, None, NO_FACTOR_RETURNS,
            "No Reference Factor Returns yet: run `market factors import` first.",
        )
    df = read_factor_exposures(lake, [security_id], date.min, on, model=model)
    if df.height:
        return ExposureLookup(security_id, df)
    bars = read_bars(lake, [security_id], date.min, date.max, columns=["trading_date"])
    reach = min(vintage.last_date, on)
    if bars.height and not _month_end_exists(engine, bars["trading_date"].min(), reach):
        return ExposureLookup(
            security_id, None, FACTOR_RETURNS_NOT_AT_MONTH_END,
            f"No {label} exposures for {ticker} on or before {on}: Factor Returns (through {vintage.last_date}) "
            "do not reach a month-end after its first Bar yet.",
        )
    return ExposureLookup(
        security_id, None, TOO_LITTLE_HISTORY,
        f"No {label} exposures for {ticker} on or before {on}: too little history (a window needs enough days "
        "with both a return and a Factor Return).",
    )
