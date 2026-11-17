"""Nightly market-data jobs."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.coverage import days_to_fetch, record_coverage
from hundred_acre.market.lake import Lake, expire_snapshots, replace_bars, write_bars
from hundred_acre.market.reference import sync_reference
from hundred_acre.market.source import MarketDataSource, MarketDataUnavailable
from hundred_acre.market.trading_calendar import generate_calendar

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
    with engine.begin() as conn:  # Bars first, then Coverage, so Coverage never claims Bars that were not written
        universe = set(conn.execute(text("SELECT security_id FROM securities")).scalars())
        for security_id in universe:
            answered = security_id in matched.values()
            record_coverage(
                conn, security_id, "1d", [as_of], status="complete" if answered else "empty", fetched_at=fetched_at,
                bar_counts={as_of: 1} if answered else None,
            )
    return LoadResult(as_of, len(rows), matched, unmatched)


def _years_back(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years) + timedelta(days=1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28) + timedelta(days=1)


def backfill_daily_bars(
    source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake, years: int = 2, since: date | None = None
) -> None:
    """Fetch per-Security Unadjusted Daily Bars for the last `years` years up to `as_of` (or from `since`).

    Fetches only Trading Calendar days minus complete or empty Coverage, one Security at a time with its Bars and
    Coverage written together, so an interrupted run resumes without refetching completed days. A Security the
    source cannot answer for is marked failed and retried on the next run.
    """
    start = since or _years_back(as_of, years)
    generate_calendar(engine, start, as_of)
    sync_reference(source, engine)
    with engine.connect() as conn:
        universe = conn.execute(text("SELECT ticker, security_id FROM tickers ORDER BY ticker")).all()
    for ticker, security_id in universe:
        missing = days_to_fetch(engine, security_id, "1d", start, as_of)
        if not missing:
            continue
        fetched_at = datetime.now(timezone.utc)
        try:
            fetched = source.aggregates(ticker, missing[0], missing[-1], adjusted=False)
        except MarketDataUnavailable:
            with engine.begin() as conn:
                record_coverage(conn, security_id, "1d", missing, status="failed", fetched_at=fetched_at)
            continue
        by_day = {d: b for d, b in fetched if d in set(missing)}
        rows = [
            dict(
                security_id=security_id, trading_date=d, open=b.open, high=b.high, low=b.low, close=b.close,
                volume=b.volume, vwap=b.vwap, trade_count=b.trade_count, source=SOURCE_NAME, fetched_at=fetched_at,
            )
            for d, b in sorted(by_day.items())
        ]
        replace_bars(lake, pl.DataFrame(rows, schema=_SCHEMA), [security_id], missing)
        with engine.begin() as conn:
            record_coverage(
                conn, security_id, "1d", [d for d in missing if d in by_day], status="complete", fetched_at=fetched_at,
                bar_counts={d: 1 for d in by_day},
            )
            record_coverage(conn, security_id, "1d", [d for d in missing if d not in by_day], status="empty", fetched_at=fetched_at)


def nightly_daily_bars(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> LoadResult:
    """Load `as_of`'s Daily Bars and re-fetch the previous trading day's to catch late corrections, then expire old snapshots.

    Each day's Bars are replaced in one DuckLake transaction, so the pre-correction version stays readable by snapshot
    until it expires (retention: `snapshot_retention_days`).
    """
    generate_calendar(engine, as_of - timedelta(days=14), as_of)
    with engine.connect() as conn:
        previous = conn.execute(
            text("SELECT max(trading_date) FROM trading_calendar WHERE calendar_id = 'XNYS' AND trading_date < :d"),
            {"d": as_of},
        ).scalar_one()
    if previous is not None:
        load_daily_bars(source, previous, engine=engine, lake=lake)
    result = load_daily_bars(source, as_of, engine=engine, lake=lake)
    expire_snapshots(lake)
    return result
