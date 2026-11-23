"""Test helpers: the jobs no longer sync reference data themselves (nightly, weekly and backfill do, once)."""

from hundred_acre.market import jobs
from hundred_acre.market.reference import sync_reference
from hundred_acre.market.source import MarketDataUnavailable


def _sync(source, engine) -> None:
    try:
        sync_reference(source, engine)
    except MarketDataUnavailable:  # an outage test: the job under test must see the outage itself
        pass


def load_daily_bars(source, as_of, *, engine, lake):
    _sync(source, engine)
    return jobs.load_daily_bars(source, as_of, engine=engine, lake=lake)


def backfill_daily_bars(source, as_of, *, engine, lake, **kwargs):
    _sync(source, engine)
    return jobs.backfill_daily_bars(source, as_of, engine=engine, lake=lake, **kwargs)
