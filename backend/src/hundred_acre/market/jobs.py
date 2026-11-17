"""Nightly market-data jobs."""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import polars as pl
from sqlalchemy import Engine

from hundred_acre.market.lake import BAR_SCHEMA as _SCHEMA
from hundred_acre.market.lake import Lake, write_bars
from hundred_acre.market.quarantine import quarantine_bars, release_quarantine
from hundred_acre.market.reference import security_on, sync_reference
from hundred_acre.market.source import MarketDataSource

SOURCE_NAME = "massive"



@dataclass
class LoadResult:
    trading_date: date
    bars_written: int
    security_ids: dict[str, int] = field(default_factory=dict)  # ticker -> Security id, for the Bars written
    unmatched_tickers: list[str] = field(default_factory=list)  # Tickers with no Security; their Bars are quarantined
    released_bars: int = 0  # Quarantined Bars (any date) released into the Daily Bars this run


def load_daily_bars(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> LoadResult:
    """Sync reference data, then load `as_of`'s Unadjusted grouped-daily Bars (OTC excluded) into the lake."""
    sync_reference(source, engine)
    bars = source.grouped_daily(as_of, adjusted=False, include_otc=False)
    fetched_at = datetime.now(timezone.utc)
    rows, matched, unmatched, quarantined = [], {}, [], []
    for b in bars:
        security_id = security_on(engine, b.ticker, as_of)
        if security_id is None:
            unmatched.append(b.ticker)
            quarantined.append((b, as_of))
            continue
        matched[b.ticker] = security_id
        rows.append(
            dict(
                security_id=security_id, trading_date=as_of, open=b.open, high=b.high, low=b.low, close=b.close,
                volume=b.volume, vwap=b.vwap, trade_count=b.trade_count, source=SOURCE_NAME, fetched_at=fetched_at,
            )
        )
    write_bars(lake, pl.DataFrame(rows, schema=_SCHEMA), as_of)
    quarantine_bars(engine, quarantined, source=SOURCE_NAME, fetched_at=fetched_at)
    released = release_quarantine(engine, lake)
    return LoadResult(as_of, len(rows), matched, unmatched, released)
