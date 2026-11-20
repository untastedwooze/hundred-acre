"""Each market CLI job sends its Requests at the Priority Class ADR-0003 assigns it."""

from datetime import date, datetime, timezone

import pytest

from hundred_acre.market.cli import main
from hundred_acre.market.priority import PriorityClass as P
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord

DAY = date(2026, 11, 13)


class SpySource:
    """A source that records (method, Priority Class) per call; `with_priority` derives a sibling like EngineSource."""

    def __init__(self, inner=None, priority=P.INTERACTIVE, log=None):
        self.inner = inner or FakeMarketDataSource()
        self.priority = priority
        self.log = log if log is not None else []

    def with_priority(self, priority):
        return SpySource(self.inner, priority, self.log)

    def __getattr__(self, name):
        target = getattr(self.inner, name)

        def call(*args, **kwargs):
            self.log.append((name, self.priority))
            return target(*args, **kwargs)

        return call


def spy():
    inner = FakeMarketDataSource()
    inner.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    inner.grouped_daily_bars[DAY] = [BarData("AAPL", 100.0, 110.0, 99.0, 105.0, 1e6, 104.5, 5000)]
    inner.intraday_bars["AAPL"] = [(datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc), BarData("AAPL", 1, 1, 1, 1, 5.0, 1, 1))]
    return SpySource(inner)


def classes(source, method):
    return {p for name, p in source.log if name == method}


def test_reference_sync_runs_at_nightly_daily(market_engine, lake):
    s = spy()
    main(["reference-sync", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake)
    assert classes(s, "tickers") == {P.NIGHTLY_DAILY}


def test_nightly_runs_at_nightly_daily_with_watchlist_step_at_nightly_watchlist(market_engine, lake):
    from hundred_acre.market.watchlist import add_to_watchlist

    s = spy()
    add_to_watchlist(s.inner, market_engine, lake, "AAPL", as_of=date(2026, 11, 12))
    assert main(["nightly", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake) == 0
    assert classes(s, "grouped_daily") == {P.NIGHTLY_DAILY}
    assert classes(s, "intraday_aggregates") == {P.NIGHTLY_WATCHLIST}


@pytest.mark.parametrize("job,expected", [("weekly", P.WEEKLY_UNIVERSE), ("backfill", P.BACKFILL)])
def test_weekly_and_backfill_use_their_own_class(job, expected, market_engine, lake):
    s = spy()
    main(["reference-sync", "--as-of", "2026-11-13"], source=s.inner, engine=market_engine, lake=lake)
    main([job, "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake)
    assert s.log, "the job made no source calls"
    assert {p for _, p in s.log} == {expected}


def test_watchlist_add_looks_up_interactively_and_backfills_intraday_at_backfill(market_engine, lake):
    s = spy()
    assert main(["watchlist", "add", "AAPL", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake) == 0
    assert classes(s, "tickers") == {P.INTERACTIVE}
    assert classes(s, "intraday_aggregates") == {P.BACKFILL}
