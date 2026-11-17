"""Nightly market-data jobs."""

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.coverage import days_to_fetch, record_coverage
from hundred_acre.market.lake import BAR_SCHEMA as _SCHEMA
from hundred_acre.market.lake import (
    INTRADAY_SCHEMA, Lake, expire_snapshots, read_bars, read_intraday_bars, replace_bars, replace_intraday_bars,
    write_bars,
)
from hundred_acre.market.sessions import assign_sessions
from hundred_acre.market.quarantine import quarantine_bars, release_quarantine
from hundred_acre.market.reference import security_on, sync_reference
from hundred_acre.market.source import MarketDataSource, MarketDataUnavailable
from hundred_acre.market.trading_calendar import generate_calendar, read_trading_calendar

SOURCE_NAME = "massive"
INTRADAY_INTERVAL = "5m"



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
    with engine.begin() as conn:  # Bars first, then Coverage, so Coverage never claims Bars that were not written
        universe = set(conn.execute(text("SELECT security_id FROM securities")).scalars())
        for security_id in universe:
            answered = security_id in matched.values()
            record_coverage(
                conn, security_id, "1d", [as_of], status="complete" if answered else "empty", fetched_at=fetched_at,
                bar_counts={as_of: 1} if answered else None,
            )
    quarantine_bars(engine, quarantined, source=SOURCE_NAME, fetched_at=fetched_at)
    released = release_quarantine(engine, lake)
    return LoadResult(as_of, len(rows), matched, unmatched, released)


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
        periods = conn.execute(
            text("SELECT ticker, security_id, valid_from, valid_to FROM ticker_periods ORDER BY ticker, valid_from")
        ).all()
    for ticker, security_id, valid_from, valid_to in periods:  # each Ticker is fetched only for the days it was in use
        missing = [
            d
            for d in days_to_fetch(engine, security_id, "1d", start, as_of)
            if (valid_from is None or d >= valid_from) and (valid_to is None or d <= valid_to)
        ]
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


def watchlist_security_ids(engine: Engine) -> list[int]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT security_id FROM watchlist ORDER BY security_id")).scalars())


def backfill_intraday_bars(
    source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake, years: int = 2, since: date | None = None,
    security_ids: list[int] | None = None,
) -> None:
    """Fetch five-minute Intraday Bars (all Trading Sessions) for `security_ids` (default: the Watchlist) up to `as_of`.

    Fetches only Trading Calendar days minus complete or empty Intraday Coverage, one Ticker period at a time with
    its Bars and Coverage written together. Each Bar is tagged with its Trading Session from the Trading Calendar
    and gaps within a day are left as gaps: a day with any Bars is complete, a day without is empty.
    """
    start = since or _years_back(as_of, years)
    generate_calendar(engine, start - timedelta(days=7), as_of + timedelta(days=10))
    calendar = read_trading_calendar(engine, start - timedelta(days=7), as_of + timedelta(days=10))
    ids = watchlist_security_ids(engine) if security_ids is None else security_ids
    if not ids:
        return
    with engine.connect() as conn:
        periods = conn.execute(
            text(
                "SELECT ticker, security_id, valid_from, valid_to FROM ticker_periods "
                "WHERE security_id = ANY(:ids) ORDER BY security_id, valid_from"
            ),
            {"ids": ids},
        ).all()
    for ticker, security_id, valid_from, valid_to in periods:
        missing = [
            d
            for d in days_to_fetch(engine, security_id, INTRADAY_INTERVAL, start, as_of)
            if (valid_from is None or d >= valid_from) and (valid_to is None or d <= valid_to)
        ]
        if not missing:
            continue
        fetched_at = datetime.now(timezone.utc)
        try:  # overnight Bars lead into a trading day from the evening before, up to a long weekend earlier
            fetched = source.intraday_aggregates(ticker, missing[0] - timedelta(days=4), missing[-1], adjusted=False)
        except MarketDataUnavailable:
            with engine.begin() as conn:
                record_coverage(conn, security_id, INTRADAY_INTERVAL, missing, status="failed", fetched_at=fetched_at)
            continue
        wanted = set(missing)
        tagged = assign_sessions([t for t, _ in fetched], calendar)
        rows = [
            dict(
                security_id=security_id, trading_date=tag[0], bar_start=t, trading_session=tag[1], open=b.open,
                high=b.high, low=b.low, close=b.close, volume=b.volume, vwap=b.vwap, trade_count=b.trade_count,
                source=SOURCE_NAME, fetched_at=fetched_at,
            )
            for (t, b), tag in zip(fetched, tagged)
            if tag is not None and tag[0] in wanted
        ]
        counts: dict[date, int] = {}
        for r in rows:
            counts[r["trading_date"]] = counts.get(r["trading_date"], 0) + 1
        replace_intraday_bars(lake, pl.DataFrame(rows, schema=INTRADAY_SCHEMA), [security_id], missing)
        with engine.begin() as conn:
            record_coverage(
                conn, security_id, INTRADAY_INTERVAL, [d for d in missing if d in counts], status="complete",
                fetched_at=fetched_at, bar_counts=counts,
            )
            record_coverage(
                conn, security_id, INTRADAY_INTERVAL, [d for d in missing if d not in counts], status="empty",
                fetched_at=fetched_at,
            )


def run_backfill(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> None:
    """The `backfill` command: two years of Daily Bars for the Universe, then of Intraday Bars for the Watchlist."""
    backfill_daily_bars(source, as_of, engine=engine, lake=lake)
    backfill_intraday_bars(source, as_of, engine=engine, lake=lake)


def nightly_intraday_bars(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> None:
    """Load the Watchlist's Intraday Bars for `as_of`, plus any day of the past week still missing (e.g. after a failure)."""
    backfill_intraday_bars(source, as_of, engine=engine, lake=lake, since=as_of - timedelta(days=7))


DEFAULT_VOLUME_MISMATCH_THRESHOLD = 0.05
WEEKLY_SKIPPED_TYPES = ("warrant", "unit", "right")  # these keep Daily Bars but get no Intraday Bars


def volume_mismatch_threshold() -> float:
    """Relative Intraday-vs-Daily volume difference above which a day is suspect; MARKET_VOLUME_MISMATCH_THRESHOLD."""
    return float(os.environ.get("MARKET_VOLUME_MISMATCH_THRESHOLD", DEFAULT_VOLUME_MISMATCH_THRESHOLD))


def mark_suspect_days(
    engine: Engine, lake: Lake, security_ids: list[int], start: date, end: date, *, threshold: float | None = None
) -> list[tuple[int, date]]:
    """Mark suspect the complete Intraday days in [start, end] whose total volume (all Trading Sessions) differs from
    the Daily Bar's volume by more than `threshold` (default: configured). Days without a Daily Bar are not judged."""
    limit = volume_mismatch_threshold() if threshold is None else threshold
    if not security_ids:
        return []
    daily = read_bars(lake, security_ids, start, end, columns=["security_id", "trading_date", "volume"])
    intraday = (
        read_intraday_bars(lake, security_ids, start, end, columns=["security_id", "trading_date", "volume"], sessions=None)
        .group_by("security_id", "trading_date")
        .agg(pl.col("volume").sum().alias("intraday_volume"))
    )
    suspect = (
        daily.join(intraday, on=["security_id", "trading_date"])
        .filter((pl.col("intraday_volume") - pl.col("volume")).abs() > limit * pl.col("volume"))
        .select("security_id", "trading_date")
        .rows()
    )
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for security_id, day in suspect:
            conn.execute(
                text(
                    "UPDATE coverage SET status = 'suspect', fetched_at = :f WHERE security_id = :s AND interval = :i "
                    "AND trading_date = :d AND status = 'complete'"
                ),
                {"f": now, "s": security_id, "i": INTRADAY_INTERVAL, "d": day},
            )
    return suspect


def run_weekly(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> None:
    """The `weekly` job: a week of Intraday Bars for every non-Watchlist Security (one call each, warrants, units and
    rights skipped), then suspect detection against Daily Bars with a re-fetch of each suspect day."""
    since = as_of - timedelta(days=7)
    sync_reference(source, engine)
    with engine.connect() as conn:
        ids = list(
            conn.execute(
                text(
                    "SELECT security_id FROM securities WHERE security_id NOT IN (SELECT security_id FROM watchlist) "
                    "AND (type IS NULL OR type <> ALL(:skipped)) ORDER BY security_id"
                ),
                {"skipped": list(WEEKLY_SKIPPED_TYPES)},
            ).scalars()
        )
    backfill_intraday_bars(source, as_of, engine=engine, lake=lake, since=since, security_ids=ids)
    if mark_suspect_days(engine, lake, ids, since, as_of):
        # Suspect days are not complete, so the same backfill now fetches exactly those days and replaces them atomically.
        backfill_intraday_bars(source, as_of, engine=engine, lake=lake, since=since, security_ids=ids)
        mark_suspect_days(engine, lake, ids, since, as_of)  # a mismatch the re-fetch did not resolve stays suspect
