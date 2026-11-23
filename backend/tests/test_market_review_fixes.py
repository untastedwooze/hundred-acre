"""Behaviour added by the integration/market-data review fixes, tested through jobs, CLI and read interfaces."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from hundred_acre.market.adjustments import corporate_action_history, sync_corporate_actions
from hundred_acre.market.cli import main
from hundred_acre.market.jobs import nightly_daily_bars, nightly_intraday_bars, run_weekly
from market_helpers import load_daily_bars
from hundred_acre.market.lake import read_bars, read_intraday_bars
from hundred_acre.market.quarantine import list_provisional_securities
from hundred_acre.market.source import (
    BarData, Dividend, FakeMarketDataSource, MarketDataUnavailable, MarketHoliday, Split, TickerEvent, TickerRecord,
)
from hundred_acre.market.reference import sync_reference
from hundred_acre.market.statistics import read_security_statistics
from hundred_acre.market.ticker_history import fetch_ticker_history
from hundred_acre.market.watchlist import add_to_watchlist

MON, TUE = date(2026, 11, 23), date(2026, 11, 24)


def utc(day: date, h: int, m: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def bar(ticker="AAA", close=10.0, volume=100.0) -> BarData:
    return BarData(ticker, close, close, close, close, volume, close, 3)


def sid_of(engine, ticker):
    with engine.connect() as c:
        return c.execute(text("SELECT security_id FROM ticker_periods WHERE ticker = :t"), {"t": ticker}).scalar_one()


def cov(engine, sid, day, interval="1d"):
    with engine.connect() as c:
        return c.execute(
            text("SELECT status FROM coverage WHERE security_id = :s AND interval = :i AND trading_date = :d"),
            {"s": sid, "i": interval, "d": day},
        ).scalar_one_or_none()


def source(*tickers: str, day: date = MON) -> FakeMarketDataSource:
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord(t, t, "CS", "stocks", f"BBG{t:0<9}", list_date=date(2020, 1, 2)) for t in tickers]
    s.grouped_daily_bars[day] = [bar(t) for t in tickers]
    for t in tickers:
        s.intraday_bars[t] = [(utc(day, 14, 30), bar(t, volume=50.0)), (utc(day, 14, 35), bar(t, volume=50.0))]
    return s


def test_nightly_command_refetches_the_previous_day_and_reports_calendar_disagreements(market_engine, lake, capsys):
    s = source("AAA")
    s.grouped_daily_bars[TUE] = [bar("AAA", 11.0)]
    s.holidays = [MarketHoliday(date(2026, 12, 1), "NYSE", "Surprise closure", "closed")]
    main(["nightly", "--as-of", "2026-11-23"], source=s, engine=market_engine, lake=lake)
    s.grouped_daily_bars[MON] = [bar("AAA", 10.5)]  # late correction

    assert main(["nightly", "--as-of", "2026-11-24"], source=s, engine=market_engine, lake=lake) == 0

    sid = sid_of(market_engine, "AAA")
    assert read_bars(lake, [sid], MON, MON, columns=["close"]).rows() == [(10.5,)]
    assert "2026-12-01" in capsys.readouterr().out


def test_calendar_covers_the_following_year(market_engine, lake):
    result = nightly_daily_bars(source("AAA"), MON, engine=market_engine, lake=lake)
    with market_engine.connect() as c:
        last = c.execute(text("SELECT max(trading_date) FROM trading_calendar")).scalar_one()
    assert last >= date(2027, 6, 1)  # next year, as far as exchange_calendars goes
    assert result.calendar_disagreements == []


def test_nightly_watchlist_intraday_suspect_day_is_refetched(market_engine, lake):
    s = source("AAA")
    s.grouped_daily_bars[MON] = [bar("AAA", volume=200.0)]  # intraday sums to 100: mismatch
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    sid = add_to_watchlist(s, market_engine, lake, "AAA", as_of=MON)
    s.intraday_bars["AAA"] = [(utc(MON, 14, 30), bar(volume=50.0)), (utc(MON, 14, 35), bar(volume=150.0))]  # corrected

    nightly_intraday_bars(s, MON, engine=market_engine, lake=lake)

    assert read_intraday_bars(lake, [sid], MON, MON, columns=["volume"]).rows() == [(50.0,), (150.0,)]
    assert cov(market_engine, sid, MON, "5m") == "complete"


def test_intraday_bars_can_be_read_on_a_split_basis(market_engine, lake):
    s = source("AAA")
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    sid = add_to_watchlist(s, market_engine, lake, "AAA", as_of=MON)
    s.split_list = [Split("AAA", TUE, 1, 2, id="s1")]  # after the Bars: they are on the pre-split basis
    sync_corporate_actions(s, MON, engine=market_engine, lake=lake)

    none = read_intraday_bars(lake, [sid], MON, MON, columns=["close", "volume"])
    split = read_intraday_bars(lake, [sid], MON, MON, columns=["close", "volume"], basis="split")

    assert none.rows() == [(10.0, 50.0)] * 2
    assert split.rows() == [(5.0, 100.0)] * 2
    with pytest.raises(ValueError):
        read_intraday_bars(lake, [sid], MON, MON, basis="bogus")


def test_weekly_refreshes_statistics(market_engine, lake):
    s = source("AAA")
    load_daily_bars(s, MON, engine=market_engine, lake=lake)

    run_weekly(s, MON, engine=market_engine, lake=lake)

    sid = sid_of(market_engine, "AAA")
    assert read_security_statistics(lake, [sid], MON, MON, columns=["dollar_volume"]).height == 1


def test_watchlist_commands_add_list_and_remove(market_engine, lake, capsys):
    s = source("AAA")
    load_daily_bars(s, MON, engine=market_engine, lake=lake)

    assert main(["watchlist", "add", "AAA", "--as-of", "2026-11-23"], source=s, engine=market_engine, lake=lake) == 0
    sid = sid_of(market_engine, "AAA")
    assert read_intraday_bars(lake, [sid], MON, MON).height == 2
    capsys.readouterr()

    assert main(["watchlist", "list"], engine=market_engine, lake=lake) == 0
    assert capsys.readouterr().out.split() == ["AAA", str(sid)]

    assert main(["watchlist", "remove", "AAA", "--as-of", "2026-11-23"], engine=market_engine, lake=lake) == 0
    capsys.readouterr()
    main(["watchlist", "list"], engine=market_engine, lake=lake)
    assert capsys.readouterr().out == ""


def test_watchlist_add_unknown_ticker_fails_cleanly(market_engine, lake, capsys):
    assert main(["watchlist", "add", "NOPE", "--as-of", "2026-11-23"], source=source("AAA"), engine=market_engine, lake=lake) == 1
    assert "NOPE" in capsys.readouterr().err


def test_otc_watchlist_securities_get_no_empty_coverage(market_engine, lake):
    s = source("AAA")
    s.tickers_.append(TickerRecord("OTCX", "Otc", "CS", "otc", "BBGOTC0000"))
    s.tickers_.append(TickerRecord("LATE", "Late", "CS", "stocks", "BBGLATE000"))
    s.events["LATE"] = [TickerEvent("LATE", date(2026, 12, 1), "ticker_change")]  # Ticker History: not yet listed
    otc = add_to_watchlist(s, market_engine, lake, "OTCX", as_of=MON)
    sync_reference(s, market_engine)
    fetch_ticker_history(s, market_engine)
    load_daily_bars(s, MON, engine=market_engine, lake=lake)

    assert cov(market_engine, otc, MON) is None
    assert cov(market_engine, sid_of(market_engine, "LATE"), MON) is None
    assert cov(market_engine, sid_of(market_engine, "AAA"), MON) == "complete"


def test_security_listed_but_silent_is_empty(market_engine, lake):
    s = source("AAA", "BBB")
    s.grouped_daily_bars[MON] = [bar("AAA")]
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    assert cov(market_engine, sid_of(market_engine, "BBB"), MON) == "empty"


class GroupedDailyDown(FakeMarketDataSource):
    def grouped_daily(self, day, *, adjusted, include_otc):
        raise MarketDataUnavailable("grouped daily is down")


def test_unavailable_grouped_daily_records_failed_coverage(market_engine, lake):
    s = source("AAA")
    load_daily_bars(s, MON, engine=market_engine, lake=lake)  # reference data exists now
    down = GroupedDailyDown(tickers_=s.tickers_)

    with pytest.raises(MarketDataUnavailable):
        load_daily_bars(down, TUE, engine=market_engine, lake=lake)

    assert cov(market_engine, sid_of(market_engine, "AAA"), TUE) == "failed"


def test_provisional_rule_counts_trading_days_not_quarantined_rows(market_engine, lake):
    s = FakeMarketDataSource()
    days = [date(2026, 11, 9), date(2026, 11, 13)]  # Mon and Fri: five trading days apart, two Bars
    for d in days:
        s.grouped_daily_bars[d] = [bar("MYST")]
        load_daily_bars(s, d, engine=market_engine, lake=lake)
    assert [p.ticker for p in list_provisional_securities(market_engine)] == ["MYST"]


def test_provisional_rule_does_not_fire_before_five_trading_days(market_engine, lake):
    s = FakeMarketDataSource()
    for d in (date(2026, 11, 9), date(2026, 11, 12)):  # four trading days
        s.grouped_daily_bars[d] = [bar("MYST")]
        load_daily_bars(s, d, engine=market_engine, lake=lake)
    assert list_provisional_securities(market_engine) == []


def test_dividend_type_is_stored_with_the_corporate_action(market_engine, lake):
    s = source("AAA")
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    s.dividend_list = [Dividend("AAA", MON, 0.5, id="d1", dividend_type="SC")]
    sync_corporate_actions(s, MON, engine=market_engine, lake=lake)

    [action] = corporate_action_history(market_engine, sid_of(market_engine, "AAA"))
    assert action.dividend_type == "SC"
