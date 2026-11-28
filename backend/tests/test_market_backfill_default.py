"""Backfill starts at the History Limit by default and clamps an earlier --since (ADR-0006)."""

from datetime import date

import pytest
from sqlalchemy import text

from hundred_acre.market.cli import build_parser
from hundred_acre.market.history_limit import history_limit
from hundred_acre.market.jobs import backfill_intraday_bars
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord
from hundred_acre.market.watchlist import add_to_watchlist
from market_helpers import backfill_daily_bars

# On 2026-11-27 the old default (two years back, plus one day) from an as_of of the 25th started a day
# before the History Limit, which Massive answers with a 403.
TODAY = date(2026, 11, 27)
AS_OF = date(2026, 11, 25)
LIMIT = date(2024, 11, 27)  # a Wednesday, a trading day
assert history_limit(TODAY, 2) == LIMIT


def source() -> FakeMarketDataSource:
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    s.aggregate_bars = {"AAPL": [(LIMIT, BarData("AAPL", 1, 1, 1, 1, 5.0, 1, 1))]}
    return s


def starts(s) -> set[date]:
    return {start for _, start, _ in s.aggregate_calls}


def test_default_starts_at_the_history_limit(market_engine, lake):
    s = source()
    backfill_daily_bars(s, AS_OF, engine=market_engine, lake=lake, today=TODAY)
    assert starts(s) == {LIMIT}


def test_since_earlier_than_the_limit_is_clamped(market_engine, lake):
    s = source()
    backfill_daily_bars(s, AS_OF, engine=market_engine, lake=lake, since=date(2020, 1, 2), today=TODAY)
    assert starts(s) == {LIMIT}


def test_since_later_than_the_limit_starts_there(market_engine, lake):
    s = source()
    later = date(2026, 11, 2)
    backfill_daily_bars(s, AS_OF, engine=market_engine, lake=lake, since=later, today=TODAY)
    assert starts(s) == {later}


def test_intraday_default_starts_at_the_limit_including_its_lead_in(market_engine, lake):
    s = source()
    backfill_daily_bars(s, AS_OF, engine=market_engine, lake=lake, today=TODAY)  # reference data and Ticker periods
    with market_engine.begin() as conn:
        sid = conn.execute(text("SELECT security_id FROM securities")).scalar_one()
    backfill_intraday_bars(s, AS_OF, engine=market_engine, lake=lake, security_ids=[sid], today=TODAY)
    assert {start for _, start, _ in s.intraday_calls} == {LIMIT}


def test_watchlist_add_backfills_from_the_limit(market_engine, lake):
    s = source()
    add_to_watchlist(s, market_engine, lake, "AAPL", as_of=AS_OF, today=TODAY)
    assert {start for _, start, _ in s.intraday_calls} == {LIMIT}


def test_no_day_before_the_limit_is_recorded_failed(market_engine, lake):
    s = source()
    s.failing_tickers = {"AAPL"}
    backfill_daily_bars(s, AS_OF, engine=market_engine, lake=lake, since=date(2020, 1, 2), today=TODAY)
    with market_engine.connect() as conn:
        early = conn.execute(
            text("SELECT count(*) FROM coverage WHERE status = 'failed' AND trading_date < :l"), {"l": LIMIT}
        ).scalar_one()
        failed = conn.execute(text("SELECT count(*) FROM coverage WHERE status = 'failed'")).scalar_one()
    assert failed > 0 and early == 0


def test_backfill_help_describes_the_default_as_the_history_limit(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backfill", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "History Limit" in out and "two years" not in out
