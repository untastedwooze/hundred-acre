"""Trading Calendar: which days Bars should exist, generated from exchange_calendars (NYSE by default)."""

from dataclasses import dataclass
from datetime import date

import exchange_calendars as xcals
import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.source import MarketDataSource

NYSE = "XNYS"
_MASSIVE_EXCHANGE = {NYSE: "NYSE"}


@dataclass(frozen=True)
class CalendarDisagreement:
    date: date
    massive_status: str  # "closed" | "early-close"
    ours: str  # what our calendar says: "trading day", "full day" or "closed"


def generate_calendar(
    engine: Engine,
    start: date,
    end: date,
    *,
    calendar_id: str = NYSE,
    source: MarketDataSource | None = None,
) -> list[CalendarDisagreement]:
    """(Re)generate `calendar_id`'s trading days in [start, end]; safe to repeat.

    With a `source`, cross-checks Massive's upcoming holidays for the exchange and returns every disagreement.
    """
    cal = xcals.get_calendar(calendar_id)
    first, last = cal.first_session.date(), cal.last_session.date()
    sessions = cal.schedule.loc[str(max(start, first)) : str(min(end, last))]
    early = {ts.date() for ts in cal.early_closes}
    rows = [
        {
            "c": calendar_id,
            "d": day.date(),
            "o": r["open"].to_pydatetime(),
            "x": r["close"].to_pydatetime(),
            "h": day.date() in early,
        }
        for day, r in sessions.iterrows()
    ]
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM trading_calendar WHERE calendar_id = :c AND trading_date BETWEEN :s AND :e"),
            {"c": calendar_id, "s": start, "e": end},
        )
        if rows:
            conn.execute(
                text(
                    'INSERT INTO trading_calendar (calendar_id, trading_date, "open", "close", half_day) '
                    "VALUES (:c, :d, :o, :x, :h)"
                ),
                rows,
            )
    if source is None:
        return []
    return _cross_check(engine, source, calendar_id, start, end)


def _cross_check(
    engine: Engine, source: MarketDataSource, calendar_id: str, start: date, end: date
) -> list[CalendarDisagreement]:
    exchange = _MASSIVE_EXCHANGE.get(calendar_id)
    found = []
    with engine.connect() as conn:
        ours = dict(
            conn.execute(
                text("SELECT trading_date, half_day FROM trading_calendar WHERE calendar_id = :c"), {"c": calendar_id}
            ).all()
        )
    for h in source.upcoming_market_holidays():
        if h.exchange != exchange or not (start <= h.date <= end):
            continue
        if h.status == "closed" and h.date in ours:
            found.append(CalendarDisagreement(h.date, h.status, "trading day"))
        elif h.status == "early-close" and ours.get(h.date) is not True:
            found.append(CalendarDisagreement(h.date, h.status, "closed" if h.date not in ours else "full day"))
    return found


def read_trading_calendar(engine: Engine, start: date, end: date, *, calendar_id: str = NYSE) -> pl.DataFrame:
    """Trading days in [start, end] with columns trading_date, open, close (UTC) and half_day."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                'SELECT trading_date, "open", "close", half_day FROM trading_calendar '
                "WHERE calendar_id = :c AND trading_date BETWEEN :s AND :e ORDER BY trading_date"
            ),
            {"c": calendar_id, "s": start, "e": end},
        ).all()
    schema = {
        "trading_date": pl.Date,
        "open": pl.Datetime("us", "UTC"),
        "close": pl.Datetime("us", "UTC"),
        "half_day": pl.Boolean,
    }
    return pl.DataFrame([tuple(r) for r in rows], schema=schema, orient="row")
