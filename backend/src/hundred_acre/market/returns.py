"""Daily total returns of Securities (#42), with Delisting final-day returns.

Close-to-close returns on the total-return basis, so splits produce no return and dividends are reinvested. A delisted
Security's series ends with one extra row on the first trading day after its Delisting date, whose return comes from
the Delisting outcome (cash per share, or ratio x target close, over the last close, minus 1; worthless is -1).
Delisted Securities are never dropped. Factor Exposures, Own Factors and the backtester all read returns through here.
"""

import logging
from collections.abc import Sequence
from datetime import date, timedelta

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.delistings import Delisting, delisting
from hundred_acre.market.lake import Lake, read_bars

log = logging.getLogger(__name__)
RETURN_COLUMNS = ("security_id", "trading_date", "total_return")
_SCHEMA = {"security_id": pl.Int64, "trading_date": pl.Date, "total_return": pl.Float64}


def _next_trading_day(engine: Engine, after: date) -> date:
    """The first Trading Calendar day after `after`; the next weekday when the calendar does not reach that far."""
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT min(trading_date) FROM trading_calendar WHERE trading_date > :d"), {"d": after}
        ).scalar_one()
    if found is not None:
        return found
    day = after + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _close_on_or_before(lake: Lake, security_id: int, day: date) -> float | None:
    bars = read_bars(lake, [security_id], date.min, day, columns=["trading_date", "close"])
    return bars.sort("trading_date")["close"][-1] if bars.height else None


def _final_return(lake: Lake, d: Delisting, last_close: float) -> float | None:
    if d.outcome == "worthless":
        return -1.0
    if d.outcome == "cash":
        return d.cash_per_share / last_close - 1
    target_close = _close_on_or_before(lake, d.target_security_id, d.delisted_on)
    return None if target_close is None else d.ratio * target_close / last_close - 1


def daily_total_returns(
    engine: Engine, lake: Lake, security_ids: Sequence[int], start: date, end: date
) -> pl.DataFrame:
    """Daily total returns with `start <= trading_date <= end`, by Security then date.

    Columns: security_id, trading_date, total_return. A Security's first Bar has no return. The previous close may lie
    before `start`. A Delisting adds a final row dated the first trading day after the Delisting date, if in range.
    """
    bars = read_bars(lake, list(security_ids), date.min, end, columns=["security_id", "trading_date", "close"], basis="total_return")
    frames = [
        bars.sort("security_id", "trading_date")
        .select("security_id", "trading_date", total_return=(pl.col("close") / pl.col("close").shift(1).over("security_id") - 1))
        .drop_nulls("total_return")
    ]
    extra = []
    for sid in security_ids:
        d = delisting(engine, sid)
        last_close = _close_on_or_before(lake, sid, d.delisted_on) if d else None
        if d is None or last_close is None:
            continue
        final_day = _next_trading_day(engine, d.delisted_on)
        value = _final_return(lake, d, last_close)
        if value is None:
            log.warning(
                "Security %s: Conversion Delisting final return skipped, target Security %s has no close on or before %s",
                sid, d.target_security_id, d.delisted_on,
            )
        elif final_day <= end:
            extra.append((sid, final_day, value))
    frames.append(pl.DataFrame(extra, schema=_SCHEMA, orient="row"))
    return (
        pl.concat(frames).filter(pl.col("trading_date") >= start).sort("security_id", "trading_date")
    )
