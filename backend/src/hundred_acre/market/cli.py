"""`market` command line interface: run market-data jobs for an explicit as-of trading date."""

import argparse
import sys
from datetime import date

from sqlalchemy import Engine, create_engine

from hundred_acre.market.db import market_database_url, migrate
from hundred_acre.market.jobs import load_daily_bars
from hundred_acre.market.lake import Lake
from hundred_acre.market.ratelimit import Priority, RateLimiter, ThrottledSource
from hundred_acre.market.reference import sync_reference
from hundred_acre.market.source import MarketDataSource, MarketDataUnavailable

JOBS = ("reference-sync", "nightly", "weekly", "backfill")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market", description="hundred-acre market data jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="migrate the hundred_acre_market database to the latest revision")
    for job in JOBS:
        p = sub.add_parser(job, help=f"run the {job} job")
        p.add_argument("--as-of", type=date.fromisoformat, required=True, help="as-of trading date, YYYY-MM-DD")
    return parser


def _job_priority(job: str) -> Priority:
    return {
        "reference-sync": Priority.NIGHTLY_DAILY, "nightly": Priority.NIGHTLY_DAILY,
        "weekly": Priority.WEEKLY_UNIVERSE, "backfill": Priority.BACKFILL,
    }[job]


def main(argv: list[str] | None = None, *, source: MarketDataSource | None = None,
         engine: Engine | None = None, lake: Lake | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        migrate()
        return 0

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
            result = load_daily_bars(source, args.as_of, engine=engine, lake=lake)
            print(f"loaded {result.bars_written} daily bars for {args.as_of}")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
