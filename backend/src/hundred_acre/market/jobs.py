"""Nightly market-data jobs."""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import polars as pl
from sqlalchemy import Engine

from hundred_acre.market.coverage import days_to_fetch, mark_suspect, record_coverage
from hundred_acre.market.factors import (
    FrenchDataLibrarySource, ReferenceFactorSource, import_reference_factor_returns, refresh_factor_exposures,
)
from hundred_acre.market.lake import BAR_SCHEMA as _SCHEMA
from hundred_acre.market.lake import (
    INTRADAY_SCHEMA, Lake, bar_row, expire_snapshots, read_bars, read_intraday_bars, replace_bars,
    replace_intraday_bars, write_bars,
)
from hundred_acre.market.quarantine import quarantine_bars, release_quarantine
from hundred_acre.market.reference import list_ticker_periods, period_preference, security_on, securities_expected_to_trade, sync_reference
from hundred_acre.market.priority import PriorityClass, at_priority
from hundred_acre.market.sessions import assign_sessions
from hundred_acre.market.source import BarData, MarketDataSource, MarketDataUnavailable
from hundred_acre.market.statistics import refresh_statistics
from hundred_acre.market.ticker_history import fetch_ticker_history
from hundred_acre.market.trading_calendar import (
    CalendarDisagreement, generate_calendar, previous_trading_day, read_trading_calendar,
)
from hundred_acre.market.watchlist import non_watchlist_security_ids, watchlist

SOURCE_NAME = "massive"
INTRADAY_INTERVAL = "5m"
log = logging.getLogger(__name__)


@dataclass
class LoadResult:
    trading_date: date
    bars_written: int
    security_ids: dict[str, int] = field(default_factory=dict)  # ticker -> Security id, for every Ticker that matched a Security
    unmatched_tickers: list[str] = field(default_factory=list)  # Tickers with no Security; their Bars are quarantined
    released_bars: int = 0  # Quarantined Bars (any date) released into the Daily Bars this run
    calendar_disagreements: list[CalendarDisagreement] = field(default_factory=list)  # Trading Calendar vs Massive


def refresh_calendar(source: MarketDataSource, engine: Engine, start: date, end: date) -> list[CalendarDisagreement]:
    """Generate the Trading Calendar for [start, end] and cross-check it against Massive's upcoming holidays.

    Disagreements are logged as warnings and returned. An unreachable source skips the cross-check only.
    """
    try:
        found = generate_calendar(engine, start, end, source=source)
    except MarketDataUnavailable as exc:
        log.warning("trading calendar cross-check skipped: %s", exc)
        return []
    for d in found:
        log.warning("trading calendar disagrees with Massive on %s: Massive says %s, ours says %s", d.date, d.massive_status, d.ours)
    return found


def _period_preferences(engine: Engine, on: date) -> dict[tuple[int, str], tuple[int, str]]:
    """(Security id, Ticker) -> `period_preference` of that Ticker's period covering `on`; only covering periods are listed."""
    return {
        (security_id, ticker): period_preference(ticker, valid_from)
        for ticker, security_id, valid_from, valid_to in list_ticker_periods(engine)
        if (valid_from is None or valid_from <= on) and (valid_to is None or valid_to >= on)
    }


def load_daily_bars(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> LoadResult:
    """Load `as_of`'s Unadjusted grouped-daily Bars (OTC excluded) into the lake.

    Coverage is recorded for Securities expected to trade that day (exchange-listed, Ticker period covering it); an
    over-the-counter or not-yet-listed Security gets none unless it traded. If the source is unreachable those
    Securities get failed Coverage and the error propagates.
    """
    expected = set(securities_expected_to_trade(engine, as_of))
    fetched_at = datetime.now(timezone.utc)
    try:
        bars = source.grouped_daily(as_of, adjusted=False, include_otc=False)
    except MarketDataUnavailable:
        with engine.begin() as conn:
            for security_id in expected:
                record_coverage(conn, security_id, "1d", [as_of], status="failed", fetched_at=fetched_at)
        raise
    matched, unmatched, quarantined = {}, [], []
    kept: dict[int, tuple[tuple, BarData]] = {}  # Security id -> (preference, Bar) kept so far: one Bar per Security per day
    preferences = _period_preferences(engine, as_of)
    for b in bars:
        security_id = security_on(engine, b.ticker, as_of)
        if security_id is None:
            unmatched.append(b.ticker)
            quarantined.append((b, as_of))
            continue
        # `security_on` found a period of this Ticker covering the day, so it has a preference; the lowest one is kept.
        preference = preferences[(security_id, b.ticker)]
        if security_id in kept:
            loser = b if preference >= kept[security_id][0] else kept[security_id][1]
            quarantined.append((loser, as_of))
            if loser is not b:
                kept[security_id] = (preference, b)
        else:
            kept[security_id] = (preference, b)
        matched[b.ticker] = security_id
    rows = [bar_row(sid, as_of, b, source=SOURCE_NAME, fetched_at=fetched_at) for sid, (_, b) in kept.items()]
    write_bars(lake, pl.DataFrame(rows, schema=_SCHEMA), as_of)
    answered = set(matched.values())
    with engine.begin() as conn:  # Bars first, then Coverage, so Coverage never claims Bars that were not written
        for security_id in expected | answered:
            record_coverage(
                conn, security_id, "1d", [as_of], status="complete" if security_id in answered else "empty",
                fetched_at=fetched_at, bar_counts={as_of: 1} if security_id in answered else None,
            )
    quarantine_bars(engine, quarantined, source=SOURCE_NAME, fetched_at=fetched_at)
    released = release_quarantine(engine, lake)
    return LoadResult(as_of, len(rows), matched, unmatched, released)


def _years_back(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years) + timedelta(days=1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28) + timedelta(days=1)


def _backfill(
    engine: Engine, lake: Lake, interval: str, start: date, as_of: date, periods: list[tuple], *,
    fetch: Callable[[str, list[date]], list],
    build: Callable[[int, list[date], list, datetime, str], tuple[pl.DataFrame, dict[date, int]]],
    replace: Callable[[Lake, pl.DataFrame, list[int], list[date]], None],
) -> None:
    """Shared skeleton of the per-Ticker backfills.

    Per Ticker period, fetches only Trading Calendar days minus complete or empty Coverage (within the period's dates),
    then writes the Bars and Coverage together so an interrupted run resumes without refetching finished days. A Ticker
    the source cannot answer for is marked failed and retried next run. `build` turns the fetched data into the Bars
    frame and a per-day Bar count; days without Bars are recorded empty.
    """
    for ticker, security_id, valid_from, valid_to in periods:  # each Ticker is fetched only for the days it was in use
        missing = [
            d
            for d in days_to_fetch(engine, security_id, interval, start, as_of)
            if (valid_from is None or d >= valid_from) and (valid_to is None or d <= valid_to)
        ]
        if not missing:
            continue
        fetched_at = datetime.now(timezone.utc)
        try:
            fetched = fetch(ticker, missing)
        except MarketDataUnavailable:
            with engine.begin() as conn:
                record_coverage(conn, security_id, interval, missing, status="failed", fetched_at=fetched_at)
            continue
        frame, counts = build(security_id, missing, fetched, fetched_at, ticker)
        replace(lake, frame, [security_id], missing)
        with engine.begin() as conn:
            record_coverage(
                conn, security_id, interval, [d for d in missing if d in counts], status="complete",
                fetched_at=fetched_at, bar_counts=counts,
            )
            record_coverage(
                conn, security_id, interval, [d for d in missing if d not in counts], status="empty", fetched_at=fetched_at
            )


def backfill_daily_bars(
    source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake, years: int = 2, since: date | None = None
) -> None:
    """Fetch per-Security Unadjusted Daily Bars for the last `years` years up to `as_of` (or from `since`).

    Fetches only Trading Calendar days minus complete or empty Coverage, one Security at a time with its Bars and
    Coverage written together, so an interrupted run resumes without refetching completed days. A Security the
    source cannot answer for is marked failed and retried on the next run.
    """
    start = since or _years_back(as_of, years)
    refresh_calendar(source, engine, start, date(as_of.year + 1, 12, 31))

    def build(security_id, missing, fetched, fetched_at, ticker):
        by_day = {d: b for d, b in fetched if d in set(missing)}
        rows = [bar_row(security_id, d, b, source=SOURCE_NAME, fetched_at=fetched_at, ticker=ticker)
            for d, b in sorted(by_day.items())]
        return pl.DataFrame(rows, schema=_SCHEMA), {d: 1 for d in by_day}

    _backfill(
        engine, lake, "1d", start, as_of, list_ticker_periods(engine),
        fetch=lambda ticker, missing: source.aggregates(ticker, missing[0], missing[-1], adjusted=False),
        build=build, replace=replace_bars,
    )


def nightly_daily_bars(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> LoadResult:
    """Load `as_of`'s Daily Bars and re-fetch the previous trading day's to catch late corrections, then expire old snapshots.

    Each day's Bars are replaced in one DuckLake transaction, so the pre-correction version stays readable by snapshot
    until it expires (retention: `snapshot_retention_days`). The Trading Calendar is regenerated through next year and
    cross-checked against Massive; disagreements come back on the result.
    """
    sync_reference(source, engine)
    disagreements = refresh_calendar(source, engine, as_of - timedelta(days=14), date(as_of.year + 1, 12, 31))
    previous = previous_trading_day(engine, as_of)
    if previous is not None:
        load_daily_bars(source, previous, engine=engine, lake=lake)
    result = load_daily_bars(source, as_of, engine=engine, lake=lake)
    result.calendar_disagreements = disagreements
    expire_snapshots(lake)
    return result


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
    ids = watchlist(engine) if security_ids is None else security_ids
    if not ids:
        return

    def fetch(ticker, missing):  # overnight Bars lead into a trading day from the evening before, up to a long weekend earlier
        return source.intraday_aggregates(ticker, missing[0] - timedelta(days=4), missing[-1], adjusted=False)

    def build(security_id, missing, fetched, fetched_at, ticker):
        wanted = set(missing)
        tagged = assign_sessions([t for t, _ in fetched], calendar)
        rows = [
            bar_row(security_id, tag[0], b, source=SOURCE_NAME, fetched_at=fetched_at, ticker=ticker, bar_start=t, trading_session=tag[1])
            for (t, b), tag in zip(fetched, tagged)
            if tag is not None and tag[0] in wanted
        ]
        counts: dict[date, int] = {}
        for r in rows:
            counts[r["trading_date"]] = counts.get(r["trading_date"], 0) + 1
        return pl.DataFrame(rows, schema=INTRADAY_SCHEMA), counts

    _backfill(
        engine, lake, INTRADAY_INTERVAL, start, as_of, list_ticker_periods(engine, ids),
        fetch=fetch, build=build, replace=replace_intraday_bars,
    )


def run_backfill(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake, since: date | None = None) -> None:
    """The `backfill` command: Ticker History for Securities without any, then two years of Daily Bars for the Universe, then of Intraday Bars for the Watchlist.

    `since` starts both backfills on that date instead of two years before `as_of`.
    """
    sync_reference(source, engine)
    fetch_ticker_history(at_priority(source, PriorityClass.BACKFILL), engine, only_missing=True)
    backfill_daily_bars(source, as_of, engine=engine, lake=lake, since=since)
    backfill_intraday_bars(source, as_of, engine=engine, lake=lake, since=since)


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
            mark_suspect(conn, security_id, INTRADAY_INTERVAL, day, fetched_at=now)
    return suspect


def _resolve_suspect_days(
    source: MarketDataSource, as_of: date, since: date, ids: list[int], *, engine: Engine, lake: Lake
) -> None:
    """Mark suspect days against Daily Bars and re-fetch each; a mismatch the re-fetch did not resolve stays suspect."""
    if mark_suspect_days(engine, lake, ids, since, as_of):
        # Suspect days are not complete, so the same backfill now fetches exactly those days and replaces them atomically.
        backfill_intraday_bars(source, as_of, engine=engine, lake=lake, since=since, security_ids=ids)
        mark_suspect_days(engine, lake, ids, since, as_of)


def nightly_intraday_bars(source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake) -> None:
    """Load the Watchlist's Intraday Bars for `as_of`, plus any day of the past week still missing (e.g. after a failure),
    then mark suspect any day whose volume disagrees with its Daily Bar and re-fetch it."""
    since = as_of - timedelta(days=7)
    backfill_intraday_bars(source, as_of, engine=engine, lake=lake, since=since)
    _resolve_suspect_days(source, as_of, since, watchlist(engine), engine=engine, lake=lake)


def run_weekly(
    source: MarketDataSource, as_of: date, *, engine: Engine, lake: Lake,
    factor_source: ReferenceFactorSource | None = None, exposure_window_days: int | None = None,
) -> None:
    """The `weekly` job: the Reference Factor Return import (French's site, not Massive), then a week of Intraday Bars
    for every non-Watchlist Security (one call each, warrants, units and rights skipped), then suspect detection
    against Daily Bars with a re-fetch of each suspect day, then a refresh of the Daily Security Statistics the new
    Bars invalidated, then the Factor Exposure refresh. A failed import is logged and does not block the rest: the
    exposure refresh uses the last good vintage. `factor_source` defaults to French's Data Library."""
    sync_reference(source, engine)
    if factor_source is None:
        factor_source = FrenchDataLibrarySource()
    try:
        import_reference_factor_returns(lake, factor_source)
    except Exception as exc:  # whatever the import hit, the rest of the weekly job still runs
        log.warning("Reference Factor Return import failed, using the last good vintage: %s", exc, exc_info=True)
    since = as_of - timedelta(days=7)
    ids = non_watchlist_security_ids(engine, skipped_types=WEEKLY_SKIPPED_TYPES)
    backfill_intraday_bars(source, as_of, engine=engine, lake=lake, since=since, security_ids=ids)
    _resolve_suspect_days(source, as_of, since, ids, engine=engine, lake=lake)
    refresh_statistics(lake)
    extra = {} if exposure_window_days is None else {"window_days": exposure_window_days}
    refresh_factor_exposures(engine, lake, **extra)
