"""Quarantined Bars and Provisional Securities.

A Bar whose Ticker has no Security on its date is held in quarantine rather than guessed at. Once reference
sync matches the Ticker, the Bar is released into the Daily Bars. A Ticker still unmatched after
PROVISIONAL_AFTER_TRADING_DAYS trading days becomes a Provisional Security (unverified) and its Bars are released.
"""

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.lake import BAR_SCHEMA, Lake, add_bars
from hundred_acre.market.reference import security_on
from hundred_acre.market.source import BarData

# Counted as distinct trading dates on which the Ticker's Bars were quarantined (no Trading Calendar needed).
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
    with engine.begin() as conn:
        aged = conn.execute(
            text("SELECT ticker, min(trading_date) FROM quarantined_bars GROUP BY ticker HAVING count(*) >= :n"),
            {"n": PROVISIONAL_AFTER_TRADING_DAYS},
        ).all()
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
    for q in list_quarantined_bars(engine):
        security_id = security_on(engine, q.ticker, q.trading_date)
        if security_id is None:
            continue
        by_date.setdefault(q.trading_date, []).append(
            dict(security_id=security_id, trading_date=q.trading_date, open=q.open, high=q.high, low=q.low,
                 close=q.close, volume=q.volume, vwap=q.vwap, trade_count=q.trade_count, source=q.source,
                 fetched_at=q.fetched_at)
        )
        released.append((q.ticker, q.trading_date))
    for day, rows in by_date.items():
        add_bars(lake, pl.DataFrame(rows, schema=BAR_SCHEMA), day)
    with engine.begin() as conn:
        for ticker, day in released:
            conn.execute(text("DELETE FROM quarantined_bars WHERE ticker = :t AND trading_date = :d"), {"t": ticker, "d": day})
    return len(released)
