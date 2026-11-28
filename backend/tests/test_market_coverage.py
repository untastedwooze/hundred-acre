from datetime import date

import pytest

from hundred_acre.market.coverage import coverage_summary
from market_helpers import backfill_daily_bars, load_daily_bars
from hundred_acre.market.lake import read_bars
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord

# Trading days 23, 24, 25, 27 (half day), 30 Nov 2026; the 26th is Thanksgiving and 28/29 a weekend.
FIRST, LAST = date(2026, 11, 23), date(2026, 11, 30)
TRADING_DAYS = [date(2026, 11, d) for d in (23, 24, 25, 27, 30)]


def bar(ticker, close=10.0):
    return BarData(ticker, close, close, close, close, 100.0, close, 5)


def two_security_source(**bars_by_ticker) -> FakeMarketDataSource:
    source = FakeMarketDataSource()
    source.tickers_ = [
        TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8"),
        TickerRecord("MSFT", "Microsoft", "CS", "stocks", "BBG001S5TD05"),
    ]
    source.aggregate_bars = {
        "AAPL": [(d, bar("AAPL")) for d in TRADING_DAYS if d != date(2026, 11, 24)],  # no trades on the 24th
        "MSFT": [(d, bar("MSFT")) for d in TRADING_DAYS],
    }
    source.aggregate_bars.update(bars_by_ticker)
    return source


def summary_for(engine, ids, **kw):
    return {row["security_id"]: row for row in coverage_summary(engine, ids, "1d", FIRST, LAST).iter_rows(named=True)}


def ids_of(engine):
    from sqlalchemy import text

    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT ticker, security_id FROM ticker_periods")).all())


def backfill(source, engine, lake, *, as_of=LAST):
    return backfill_daily_bars(source, as_of, engine=engine, lake=lake, since=FIRST)


def test_never_fetched_days_are_counted_separately(market_engine, lake):
    source = two_security_source()
    from hundred_acre.market.reference import sync_reference
    from hundred_acre.market.trading_calendar import generate_calendar

    sync_reference(source, market_engine)
    generate_calendar(market_engine, FIRST, LAST)
    ids = ids_of(market_engine)

    row = summary_for(market_engine, [ids["AAPL"]])[ids["AAPL"]]

    assert row["never_fetched"] == 5
    assert (row["complete"], row["empty"], row["failed"], row["partial"]) == (0, 0, 0, 0)


def test_backfill_separates_complete_from_empty_and_ignores_the_holiday(market_engine, lake):
    source = two_security_source()

    backfill(source, market_engine, lake)

    ids = ids_of(market_engine)
    aapl = summary_for(market_engine, list(ids.values()))[ids["AAPL"]]
    assert (aapl["complete"], aapl["empty"], aapl["never_fetched"]) == (4, 1, 0)  # holiday not counted as missing
    df = read_bars(lake, [ids["AAPL"]], FIRST, LAST)
    assert date(2026, 11, 26) not in df["trading_date"].to_list()
    assert date(2026, 11, 24) not in df["trading_date"].to_list()  # gaps stay gaps


def test_failed_fetches_are_recorded_and_retried_on_the_next_run(market_engine, lake):
    source = two_security_source()
    source.failing_tickers = {"MSFT"}
    backfill(source, market_engine, lake)
    ids = ids_of(market_engine)
    summary = summary_for(market_engine, list(ids.values()))
    assert summary[ids["MSFT"]]["failed"] == 5
    assert summary[ids["AAPL"]]["failed"] == 0

    source.failing_tickers = set()
    source.aggregate_calls.clear()
    backfill(source, market_engine, lake)

    assert [c[0] for c in source.aggregate_calls] == ["MSFT"]
    assert summary_for(market_engine, [ids["MSFT"]])[ids["MSFT"]]["complete"] == 5


def test_interrupted_backfill_resumes_without_refetching_completed_days(market_engine, lake):
    source = two_security_source()
    source.crash_after_aggregate_calls = 1
    with pytest.raises(RuntimeError):
        backfill(source, market_engine, lake)

    source.crash_after_aggregate_calls = None
    source.aggregate_calls.clear()
    backfill(source, market_engine, lake)

    assert len(source.aggregate_calls) == 1  # only the Security the crash prevented
    ids = ids_of(market_engine)
    for s in summary_for(market_engine, list(ids.values())).values():
        assert s["never_fetched"] == 0 and s["failed"] == 0
    assert read_bars(lake, list(ids.values()), FIRST, LAST).height == 9


def test_a_completed_backfill_fetches_nothing_when_repeated(market_engine, lake):
    source = two_security_source()
    backfill(source, market_engine, lake)
    source.aggregate_calls.clear()

    backfill(source, market_engine, lake)

    assert source.aggregate_calls == []


def test_grouped_daily_load_marks_absent_securities_empty(market_engine, lake):
    source = two_security_source()
    source.grouped_daily_bars[FIRST] = [bar("AAPL")]

    load_daily_bars(source, FIRST, engine=market_engine, lake=lake)

    ids = ids_of(market_engine)
    from hundred_acre.market.trading_calendar import generate_calendar

    generate_calendar(market_engine, FIRST, LAST)
    s = summary_for(market_engine, list(ids.values()))
    assert (s[ids["AAPL"]]["complete"], s[ids["MSFT"]]["empty"]) == (1, 1)


def test_backfilled_bars_record_the_ticker_they_were_fetched_under(market_engine, lake):
    source = two_security_source()
    backfill(source, market_engine, lake)

    ids = ids_of(market_engine)
    df = read_bars(lake, [ids["MSFT"]], FIRST, LAST, columns=["ticker"])

    assert df["ticker"].to_list() == ["MSFT"] * len(TRADING_DAYS)


def test_backfill_command_since_starts_the_window_later(market_engine, lake):
    """`market backfill --since D` backfills from D, e.g. when the plan's history no longer reaches the default start (#71)."""
    from hundred_acre.market.cli import main

    since = date(2026, 11, 25)
    assert main(["backfill", "--as-of", "2026-11-30", "--since", since.isoformat()],
                source=two_security_source(), engine=market_engine, lake=lake) == 0

    ids = ids_of(market_engine)
    msft = summary_for(market_engine, [ids["MSFT"]])[ids["MSFT"]]
    assert (msft["complete"], msft["never_fetched"]) == (3, 2)  # 25, 27, 30 fetched; 23 and 24 left alone
    assert read_bars(lake, [ids["MSFT"]], FIRST, LAST)["trading_date"].min() == since
