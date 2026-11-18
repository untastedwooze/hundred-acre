"""`market` command line interface: run market-data jobs for an explicit as-of trading date."""

import argparse
import sys
import time
from datetime import date, timedelta

from sqlalchemy import Engine, create_engine, text

from hundred_acre.market.adjustments import rebuild_adjustment_factors, sync_corporate_actions
from hundred_acre.market.db import market_database_url, migrate
from hundred_acre.market.delistings import sync_delistings
from hundred_acre.market.jobs import nightly_daily_bars, nightly_intraday_bars
from hundred_acre.market.lake import Lake
from hundred_acre.market.statistics import refresh_statistics
from hundred_acre.market.ratelimit import Priority, RateLimiter, ThrottledSource
from hundred_acre.market.reference import security_on, sync_reference
from hundred_acre.market.source import MarketDataSource, MarketDataUnavailable
from hundred_acre.market.watchlist import add_to_watchlist, remove_from_watchlist, watchlist

IDLE_POLL_SECONDS = 1.0
JOBS = ("reference-sync", "nightly", "weekly", "backfill")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market", description="hundred-acre market data jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="migrate the hundred_acre_market database to the latest revision")
    for job in JOBS:
        p = sub.add_parser(job, help=f"run the {job} job")
        p.add_argument("--as-of", type=date.fromisoformat, required=True, help="as-of trading date, YYYY-MM-DD")
    re_ = sub.add_parser("request-engine", help="the Request Engine, the only process that calls Massive")
    re_sub = re_.add_subparsers(dest="request_engine_command", required=True)
    run = re_sub.add_parser("run", help="serve queued Requests (configured by environment variables)")
    run.add_argument("--once", action="store_true", help="serve until the queue is empty, then exit")
    wl = sub.add_parser("watchlist", help="manage the Watchlist (Securities with nightly Intraday refreshes)")
    wl_sub = wl.add_subparsers(dest="watchlist_command", required=True)
    for name, help_ in (("add", "add a Ticker and backfill its Intraday history"), ("remove", "remove a Ticker")):
        p = wl_sub.add_parser(name, help=help_)
        p.add_argument("ticker")
        p.add_argument("--as-of", type=date.fromisoformat, default=None, help="date the Ticker is looked up on (default: today)")
    wl_sub.add_parser("list", help="list Watchlist Securities")
    return parser


def _job_priority(job: str) -> Priority:
    return {
        "reference-sync": Priority.NIGHTLY_DAILY, "nightly": Priority.NIGHTLY_DAILY,
        "weekly": Priority.WEEKLY_UNIVERSE, "backfill": Priority.BACKFILL, "watchlist": Priority.BACKFILL,
    }[job]


def main(argv: list[str] | None = None, *, source: MarketDataSource | None = None,
         engine: Engine | None = None, lake: Lake | None = None, request_engine=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        migrate()
        return 0

    if args.command == "request-engine":
        return _request_engine_run(args, engine, request_engine)

    if args.command == "watchlist" and args.watchlist_command in ("list", "remove"):
        return _watchlist_offline(args, engine)

    try:
        if source is None:
            from hundred_acre.market.massive import MassiveSource

            source = ThrottledSource(MassiveSource.from_env(), RateLimiter(), _job_priority(args.command))
        engine = engine or create_engine(market_database_url())
        lake = lake or Lake.from_env()
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
            watchlist_source = source.with_priority(Priority.NIGHTLY_WATCHLIST) if isinstance(source, ThrottledSource) else source
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
            job(source, args.as_of, engine=engine, lake=lake)
    except MarketDataUnavailable as exc:
        print(f"market: data source unavailable: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # e.g. unknown Ticker
        print(f"market: {exc}", file=sys.stderr)
        return 1
    return 0


def _request_engine_run(args: argparse.Namespace, engine: Engine | None, request_engine) -> int:
    from hundred_acre.market.request_engine import RequestEngine

    try:
        if request_engine is None:
            request_engine = RequestEngine.from_env(engine or create_engine(market_database_url()))
    except RuntimeError as exc:
        print(f"market: {exc}", file=sys.stderr)
        return 1
    if args.once:
        request_engine.serve()
        return 0
    while True:
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
