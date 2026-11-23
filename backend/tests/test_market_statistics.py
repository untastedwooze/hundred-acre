from datetime import date, datetime, timedelta, timezone

import pytest

from hundred_acre.market.adjustments import sync_corporate_actions
from hundred_acre.market.jobs import backfill_intraday_bars
from market_helpers import load_daily_bars
from hundred_acre.market.source import BarData, FakeMarketDataSource, Split, TickerRecord
from hundred_acre.market.statistics import read_security_statistics, refresh_statistics

MON, TUE, WED = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)
VOL_5M = 0.0140722  # sqrt of two squared log returns of +-1% (100 -> 101 -> 100)


def utc(day: date, h: int, m: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)  # 14:30 UTC = 9:30 ET in November


def bar(price: float, volume: float = 1000.0) -> BarData:
    return BarData("AAPL", price, price, price, price, volume, price, 3)


def make_source(prices: dict[date, float], volume: float = 1000.0) -> FakeMarketDataSource:
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    s.grouped_daily_bars = {d: [bar(p, volume)] for d, p in prices.items()}
    s.intraday_bars = {
        "AAPL": [
            (utc(d, 14, 30 + 5 * i), bar(p * m))
            for d, p in prices.items()
            for i, m in enumerate((1.0, 1.01, 1.0))
        ]
    }
    return s


def load_all(source, engine, lake):
    for d in source.grouped_daily_bars:
        sid = load_daily_bars(source, d, engine=engine, lake=lake).security_ids["AAPL"]
    backfill_intraday_bars(
        source, max(source.grouped_daily_bars), engine=engine, lake=lake, since=min(source.grouped_daily_bars),
        security_ids=[sid],
    )
    return sid


def stats(lake, sid, start=MON, end=WED):
    return read_security_statistics(lake, [sid], start, end)


def test_split_day_shows_no_fake_return_or_volatility_spike(market_engine, lake):
    source = make_source({MON: 400.0, TUE: 100.0, WED: 101.0})
    sid = load_all(source, market_engine, lake)
    source.split_list = [Split("AAPL", TUE, 1, 4, id="s1")]
    sync_corporate_actions(source, MON, engine=market_engine, lake=lake)
    refresh_statistics(lake)

    df = stats(lake, sid)
    assert df["trading_date"].to_list() == [MON, TUE, WED]
    assert df["overnight_return"][0] is None
    assert df["overnight_return"][1] == pytest.approx(0.0, abs=1e-9)
    assert df["overnight_return"][2] == pytest.approx(0.01)
    assert df["realized_volatility"].to_list() == pytest.approx([VOL_5M] * 3, rel=1e-3)
    assert df["dollar_volume"].to_list() == pytest.approx([400000.0, 100000.0, 101000.0])


def test_average_volume_needs_a_full_window(market_engine, lake):
    days = [d for i in range(30) if (d := date(2026, 10, 1) + timedelta(days=i)).weekday() < 5]  # 22 weekdays
    source = make_source({d: 10.0 for d in days})
    sid = load_all(source, market_engine, lake)
    refresh_statistics(lake)

    df = stats(lake, sid, days[0], days[-1])
    assert df["avg_volume_20"].to_list() == [None] * 19 + [1000.0] * (len(days) - 19)
    assert df["avg_volume_60"].to_list() == [None] * len(days)


def test_rows_record_method_parameters_and_source_snapshot(market_engine, lake):
    source = make_source({MON: 10.0, TUE: 11.0})
    sid = load_all(source, market_engine, lake)
    refresh_statistics(lake)

    df = stats(lake, sid)
    assert df["method"].n_unique() == 1 and df["method"][0]
    assert "5m" in df["parameters"][0] and "20" in df["parameters"][0]
    assert df["source_snapshot"].null_count() == 0


def test_bar_correction_rebuilds_only_affected_rows(market_engine, lake):
    source = make_source({MON: 10.0, TUE: 11.0})
    sid = load_all(source, market_engine, lake)
    assert refresh_statistics(lake).rebuilt == [sid]
    assert refresh_statistics(lake).rebuilt == []  # nothing changed
    before = stats(lake, sid)

    source.grouped_daily_bars[TUE] = [bar(11.0, 2000.0)]  # late correction
    load_daily_bars(source, TUE, engine=market_engine, lake=lake)
    assert refresh_statistics(lake).rebuilt == [sid]

    after = stats(lake, sid)
    assert after["dollar_volume"].to_list() == pytest.approx([10000.0, 22000.0])
    assert after["source_snapshot"][0] != before["source_snapshot"][0]


def test_corporate_action_change_rebuilds(market_engine, lake):
    source = make_source({MON: 400.0, TUE: 100.0})
    sid = load_all(source, market_engine, lake)
    refresh_statistics(lake)
    assert stats(lake, sid)["overnight_return"][1] == pytest.approx(-0.75)

    source.split_list = [Split("AAPL", TUE, 1, 4, id="s1")]
    sync_corporate_actions(source, MON, engine=market_engine, lake=lake)
    assert refresh_statistics(lake).rebuilt == [sid]
    assert stats(lake, sid)["overnight_return"][1] == pytest.approx(0.0, abs=1e-9)


def test_full_rebuild_matches_incremental(market_engine, lake):
    source = make_source({MON: 400.0, TUE: 100.0, WED: 101.0})
    sid = load_all(source, market_engine, lake)
    source.split_list = [Split("AAPL", TUE, 1, 4, id="s1")]
    sync_corporate_actions(source, MON, engine=market_engine, lake=lake)
    refresh_statistics(lake)
    incremental = stats(lake, sid).drop("source_snapshot")

    assert refresh_statistics(lake, full=True).rebuilt == [sid]
    assert stats(lake, sid).drop("source_snapshot").equals(incremental)
