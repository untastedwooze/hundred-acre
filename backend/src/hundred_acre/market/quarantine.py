"""Quarantined Bars and Provisional Securities.

A Bar whose Ticker has no Security on its date is held in quarantine rather than guessed at. Once reference
sync matches the Ticker, the Bar is released into the Daily Bars. A Ticker still unmatched after
PROVISIONAL_AFTER_TRADING_DAYS trading days becomes a Provisional Security (unverified) and its Bars are released.
"""

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.coverage import record_coverage
from hundred_acre.market.lake import BAR_SCHEMA, Lake, add_bars, bar_row, securities_with_bars
from hundred_acre.market.reference import period_preference_on, security_on
from hundred_acre.market.source import BarData
from hundred_acre.market.trading_calendar import count_trading_days, generate_calendar

# Counted as Trading Calendar days from the Ticker's first quarantined Bar to its latest, inclusive.
PROVISIONAL_AFTER_TRADING_DAYS = 5


@dataclass(frozen=True)
class QuarantinedBar:
    ticker: str
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    trade_count: int | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class ProvisionalSecurity:
    security_id: int
    ticker: str
    first_seen: date | None  # first trading date the Ticker was seen


_BAR_FIELDS = "ticker, trading_date, open, high, low, close, volume, vwap, trade_count, source, fetched_at"


def quarantine_bars(engine: Engine, bars: list[tuple[BarData, date]], *, source: str, fetched_at: datetime) -> None:
    with engine.begin() as conn:
        for b, day in bars:
            conn.execute(
                text(
                    f"INSERT INTO quarantined_bars ({_BAR_FIELDS}) VALUES "
                    "(:ticker, :d, :open, :high, :low, :close, :volume, :vwap, :tc, :source, :at) "
                    "ON CONFLICT (ticker, trading_date) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, "
                    "low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume, vwap = EXCLUDED.vwap, "
                    "trade_count = EXCLUDED.trade_count, source = EXCLUDED.source, fetched_at = EXCLUDED.fetched_at"
                ),
                dict(ticker=b.ticker, d=day, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
                     vwap=b.vwap, tc=b.trade_count, source=source, at=fetched_at),
            )


def list_quarantined_bars(engine: Engine) -> list[QuarantinedBar]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {_BAR_FIELDS} FROM quarantined_bars ORDER BY trading_date, ticker")).all()
    return [QuarantinedBar(*r) for r in rows]


def list_provisional_securities(engine: Engine) -> list[ProvisionalSecurity]:
    """Unverified Securities, i.e. those still without a FIGI."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT s.security_id, p.ticker, p.valid_from FROM securities s JOIN ticker_periods p USING (security_id) "
                "WHERE NOT s.verified ORDER BY s.security_id"
            )
        ).all()
    return [ProvisionalSecurity(*r) for r in rows]


def _create_provisional_securities(engine: Engine) -> None:
    with engine.connect() as conn:
        spans = conn.execute(
            text("SELECT ticker, min(trading_date), max(trading_date) FROM quarantined_bars GROUP BY ticker")
        ).all()
    if not spans:
        return
    generate_calendar(engine, min(s[1] for s in spans), max(s[2] for s in spans))
    aged = [
        (ticker, first_seen)
        for ticker, first_seen, last_seen in spans
        if count_trading_days(engine, first_seen, last_seen) >= PROVISIONAL_AFTER_TRADING_DAYS
    ]
    with engine.begin() as conn:
        for ticker, first_seen in aged:
            security_id = conn.execute(
                text("INSERT INTO securities (verified) VALUES (false) RETURNING security_id")
            ).scalar_one()
            conn.execute(
                text("INSERT INTO ticker_periods (ticker, security_id, valid_from) VALUES (:t, :s, :f)"),
                {"t": ticker, "s": security_id, "f": first_seen},
            )


def release_quarantine(engine: Engine, lake: Lake) -> int:
    """Promote aged Tickers to Provisional Securities, then release every Quarantined Bar that now has a Security.

    Returns the number of Bars released into the Daily Bars.
    """
    _create_provisional_securities(engine)
    by_date: dict[date, list[dict]] = {}
    released: list[tuple[str, date]] = []
    covered: list[tuple[int, date, datetime]] = []
    taken: dict[date, set[int]] = {}  # Securities that already have a Daily Bar on the day: one Bar per Security per day
    # (day, Security) -> (preference, Bar): of several Quarantined Bars of one Security on a day, the most preferred Ticker
    # period wins, as in `load_daily_bars`, whatever order the Bars are listed in.
    winners: dict[tuple[date, int], tuple[tuple, QuarantinedBar]] = {}
    for q in list_quarantined_bars(engine):
        security_id = security_on(engine, q.ticker, q.trading_date)
        if security_id is None:
            continue
        preference = period_preference_on(engine, q.ticker, security_id, q.trading_date)
        if preference is None:
            continue
        key = (q.trading_date, security_id)
        if key not in winners or preference < winners[key][0]:
            winners[key] = (preference, q)
    for (day, security_id), (_, q) in winners.items():
        if day not in taken:
            taken[day] = securities_with_bars(lake, day)
        if security_id in taken[day]:
            continue  # stays quarantined: that Security already has its Bar for the day
        by_date.setdefault(q.trading_date, []).append(
            bar_row(security_id, q.trading_date, q, source=q.source, fetched_at=q.fetched_at)
        )
        released.append((q.ticker, q.trading_date))
        covered.append((security_id, q.trading_date, q.fetched_at))
    for day, rows in by_date.items():
        add_bars(lake, pl.DataFrame(rows, schema=BAR_SCHEMA), day)
    with engine.begin() as conn:
        for security_id, day, fetched_at in covered:  # released Bars are fetched data, so they get Coverage too
            record_coverage(conn, security_id, "1d", [day], status="complete", fetched_at=fetched_at, bar_counts={day: 1})
        for ticker, day in released:
            conn.execute(text("DELETE FROM quarantined_bars WHERE ticker = :t AND trading_date = :d"), {"t": ticker, "d": day})
    return len(released)
