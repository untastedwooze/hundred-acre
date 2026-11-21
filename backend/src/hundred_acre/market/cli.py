"""`market` command line interface: run market-data jobs for an explicit as-of trading date."""

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Engine, create_engine, text

from hundred_acre.market.adjustments import rebuild_adjustment_factors, sync_corporate_actions
from hundred_acre.market.db import create_and_migrate, market_database_url
from hundred_acre.market.delistings import sync_delistings
from hundred_acre.market.factors.cli import add_parser as factors_add_parser, run as factors_run
from hundred_acre.market.jobs import nightly_daily_bars, nightly_intraday_bars
from hundred_acre.market.lake import Lake
from hundred_acre.market.priority import PriorityClass, at_priority
from hundred_acre.market.reference import security_on, sync_reference
from hundred_acre.market.source import MarketDataSource, MarketDataUnavailable
from hundred_acre.market.statistics import refresh_statistics
from hundred_acre.market.trading_calendar import latest_closed_trading_day
from hundred_acre.market.watchlist import add_to_watchlist, remove_from_watchlist, watchlist

IDLE_POLL_SECONDS = 1.0
JOBS = ("reference-sync", "nightly", "weekly", "backfill")


def _as_of(value: str) -> date | None:
    """Parse `--as-of`: a date, or None for 'latest' (resolved once in `main`, which has the engine and clock)."""
    return None if value == "latest" else date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market", description="hundred-acre market data jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="migrate the hundred_acre_market database to the latest revision")
    for job in JOBS:
        p = sub.add_parser(job, help=f"run the {job} job")
        p.add_argument("--as-of", type=_as_of, required=True,
                       help="as-of trading date, YYYY-MM-DD, or 'latest' (the last trading day whose after-hours session has closed)")
    re_ = sub.add_parser("request-engine", help="the Request Engine, the only process that calls Massive")
    re_sub = re_.add_subparsers(dest="request_engine_command", required=True)
    re_sub.add_parser("status", help="show engine state, queue depth, Call usage and recent failures (database only)")
    re_sub.add_parser("resume", help="clear a halt (after fixing the API key or plan); queued Requests then continue")
    run = re_sub.add_parser("run", help="serve queued Requests (configured by environment variables)")
    run.add_argument("--once", action="store_true", help="serve until the queue is empty, then exit")
    factors_add_parser(sub)
    wl = sub.add_parser("watchlist", help="manage the Watchlist (Securities with nightly Intraday refreshes)")
    wl_sub = wl.add_subparsers(dest="watchlist_command", required=True)
    for name, help_ in (("add", "add a Ticker and backfill its Intraday history"), ("remove", "remove a Ticker")):
        p = wl_sub.add_parser(name, help=help_)
        p.add_argument("ticker")
        p.add_argument("--as-of", type=date.fromisoformat, default=None, help="date the Ticker is looked up on (default: today)")
    wl_sub.add_parser("list", help="list Watchlist Securities")
    return parser


def _job_priority(job: str) -> PriorityClass:
    """The Priority Class a job's Requests carry (`watchlist add` overrides it per step: lookup interactive)."""
    return {
        "reference-sync": PriorityClass.NIGHTLY_DAILY, "nightly": PriorityClass.NIGHTLY_DAILY,
        "weekly": PriorityClass.WEEKLY_UNIVERSE, "backfill": PriorityClass.BACKFILL,
        "watchlist": PriorityClass.INTERACTIVE,
    }[job]


def main(argv: list[str] | None = None, *, source: MarketDataSource | None = None,
         engine: Engine | None = None, lake: Lake | None = None, request_engine=None, clock=time.time,
         factor_source=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        create_and_migrate()
        return 0

    if args.command == "request-engine":
        return _request_engine_run(args, engine, request_engine, clock)

    if args.command == "factors":
        try:
            return factors_run(args, engine or create_engine(market_database_url()), lake or Lake.from_env(), factor_source)
        except RuntimeError as exc:
            print(f"market: {exc}", file=sys.stderr)
            return 1

    if args.command == "watchlist" and args.watchlist_command in ("list", "remove"):
        return _watchlist_offline(args, engine)

    try:
        engine = engine or create_engine(market_database_url())
        if source is None:
            source = _engine_source(engine)
        if hasattr(source, "with_priority"):
            source = source.with_priority(_job_priority(args.command))
        lake = lake or Lake.from_env()
        if args.command in JOBS and args.as_of is None:
            args.as_of = latest_closed_trading_day(engine, datetime.fromtimestamp(clock(), timezone.utc))
            print(f"--as-of latest resolved to {args.as_of}")
    except RuntimeError as exc:
        print(f"market: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "reference-sync":
            sync_reference(source, engine)
        elif args.command == "nightly":
            result = nightly_daily_bars(source, args.as_of, engine=engine, lake=lake)
            print(f"loaded {result.bars_written} daily bars for {args.as_of}")
            for d in result.calendar_disagreements:
                print(f"market: trading calendar disagrees with Massive on {d.date}: Massive {d.massive_status}, ours {d.ours}")
            sync_corporate_actions(source, args.as_of - timedelta(days=7), engine=engine, lake=lake)
            # Bars changed for these Securities, so refresh their factors even if their actions did not change.
            with engine.connect() as conn:
                with_actions = {r[0] for r in conn.execute(text("SELECT DISTINCT security_id FROM corporate_actions_current"))}
            for security_id in sorted(with_actions & set(result.security_ids.values())):
                rebuild_adjustment_factors(engine, lake, security_id)
            sync_delistings(engine, lake)
            watchlist_source = at_priority(source, PriorityClass.NIGHTLY_WATCHLIST)
            nightly_intraday_bars(watchlist_source, args.as_of, engine=engine, lake=lake)
            print(f"loaded watchlist intraday bars for {args.as_of}")
            refresh_statistics(lake)  # incremental: only Securities whose Bars or factors changed
        elif args.command == "watchlist":  # add
            as_of = args.as_of or date.today()
            security_id = add_to_watchlist(source, engine, lake, args.ticker, as_of=as_of)
            print(f"added {args.ticker} (security {security_id}) to the watchlist")
        else:
            from hundred_acre.market import jobs

            job = getattr(jobs, f"run_{args.command}", None)
            if job is None:
                print(f"market: the {args.command} job is not available yet", file=sys.stderr)
                return 2
            extra = {"factor_source": factor_source} if args.command == "weekly" and factor_source is not None else {}
            job(source, args.as_of, engine=engine, lake=lake, **extra)
    except MarketDataUnavailable as exc:
        print(f"market: data source unavailable: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # e.g. unknown Ticker
        print(f"market: {exc}", file=sys.stderr)
        return 1
    return 0


def _engine_source(engine: Engine) -> MarketDataSource:
    """The default source: every call goes through the Request Engine's queue."""
    import os

    from hundred_acre.market.engine_source import EngineSource

    responses = os.environ.get("MARKET_RESPONSES_DIR")
    if not responses:
        raise RuntimeError("MARKET_RESPONSES_DIR is not set")
    return EngineSource(engine, responses)


def _request_engine_run(args: argparse.Namespace, engine: Engine | None, request_engine, clock=time.time) -> int:
    from hundred_acre.market.request_engine import PRUNE_INTERVAL, RequestEngine, resume

    if args.request_engine_command == "status":
        from hundred_acre.market.request_engine_status import render_status

        print(render_status(engine or create_engine(market_database_url()), clock()))
        return 0
    if args.request_engine_command == "resume":
        resume(engine or create_engine(market_database_url()))
        return 0
    try:
        if request_engine is None:
            request_engine = RequestEngine.from_env(engine or create_engine(market_database_url()))
        request_engine.acquire_lock()
    except RuntimeError as exc:
        print(f"market: {exc}", file=sys.stderr)
        return 1
    if args.once:
        request_engine.serve()
        request_engine.prune_responses()
        return 0
    last_prune = float("-inf")
    while True:
        if time.time() - last_prune >= PRUNE_INTERVAL:
            request_engine.prune_responses()
            last_prune = time.time()
        request_engine.serve(until=time.time() + IDLE_POLL_SECONDS)
        time.sleep(IDLE_POLL_SECONDS)


def _watchlist_offline(args: argparse.Namespace, engine: Engine | None) -> int:
    """`watchlist list` and `watchlist remove` need only the database, not the data source."""
    try:
        engine = engine or create_engine(market_database_url())
    except RuntimeError as exc:
        print(f"market: {exc}", file=sys.stderr)
        return 1
    if args.watchlist_command == "list":
        for security_id in watchlist(engine):
            with engine.connect() as conn:
                tickers = conn.execute(
                    text("SELECT ticker FROM ticker_periods WHERE security_id = :s ORDER BY valid_from DESC NULLS LAST LIMIT 1"),
                    {"s": security_id},
                ).scalar()
            print(f"{tickers or '?'}\t{security_id}")
        return 0
    security_id = security_on(engine, args.ticker, args.as_of or date.today())
    if security_id is None:
        print(f"market: unknown Ticker {args.ticker!r}", file=sys.stderr)
        return 1
    remove_from_watchlist(engine, security_id)
    print(f"removed {args.ticker} (security {security_id}) from the watchlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
