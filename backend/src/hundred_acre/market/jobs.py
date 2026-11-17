"""Nightly market-data jobs."""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.lake import Lake, write_bars
from hundred_acre.market.reference import sync_reference
from hundred_acre.market.source import MarketDataSource

SOURCE_NAME = "massive"

_SCHEMA = {
    "security_id": pl.Int64, "trading_date": pl.Date, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
    "close": pl.Float64, "volume": pl.Float64, "vwap": pl.Float64, "trade_count": pl.Int64, "source": pl.String,
    "fetched_at": pl.Datetime("us", "UTC"),
}


@dataclass
class LoadResult:
    trading_date: date
    bars_written: int
    security_ids: dict[str, int] = field(default_factory=dict)  # ticker -> Security id, for the Bars written
    unmatched_tickers: list[str] = field(default_factory=list)  # Tickers with no Security; their Bars are not written


def load_daily_bars(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> LoadResult:
    """Sync reference data, then load `as_of`'s Unadjusted grouped-daily Bars (OTC excluded) into the lake."""
    sync_reference(source, engine)
    bars = source.grouped_daily(as_of, adjusted=False, include_otc=False)
    with engine.connect() as conn:
        ids = {t: s for t, s in conn.execute(text("SELECT ticker, security_id FROM tickers"))}

    fetched_at = datetime.now(timezone.utc)
    rows, matched, unmatched = [], {}, []
    for b in bars:
        security_id = ids.get(b.ticker)
        if security_id is None:
            unmatched.append(b.ticker)
            continue
        matched[b.ticker] = security_id
        rows.append(
            dict(
                security_id=security_id, trading_date=as_of, open=b.open, high=b.high, low=b.low, close=b.close,
                volume=b.volume, vwap=b.vwap, trade_count=b.trade_count, source=SOURCE_NAME, fetched_at=fetched_at,
            )
        )
    write_bars(lake, pl.DataFrame(rows, schema=_SCHEMA), as_of)
    return LoadResult(as_of, len(rows), matched, unmatched)
