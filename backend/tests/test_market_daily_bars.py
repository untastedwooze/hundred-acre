from datetime import date

import polars as pl

from hundred_acre.market.jobs import load_daily_bars
from hundred_acre.market.lake import read_bars
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord

DAY = date(2026, 11, 13)
NEXT = date(2026, 11, 16)
ALL_COLUMNS = [
    "security_id", "trading_date", "open", "high", "low", "close", "volume", "vwap", "trade_count", "source", "fetched_at",
]


def aapl_source() -> FakeMarketDataSource:
    source = FakeMarketDataSource()
    source.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    source.grouped_daily_bars[DAY] = [BarData("AAPL", 100.0, 110.0, 99.0, 105.0, 1_000_000.0, 104.5, 5000)]
    return source


def test_loaded_daily_bar_is_readable(market_engine, lake):
    result = load_daily_bars(aapl_source(), DAY, engine=market_engine, lake=lake)

    df = read_bars(lake, security_ids=[result.security_ids["AAPL"]], start=DAY, end=DAY)

    assert isinstance(df, pl.DataFrame)
    row = df.row(0, named=True)
    assert (row["open"], row["high"], row["low"], row["close"]) == (100.0, 110.0, 99.0, 105.0)
    assert (row["volume"], row["vwap"], row["trade_count"]) == (1_000_000.0, 104.5, 5000)
    assert row["trading_date"] == DAY
    assert row["source"] == "massive"
    assert row["fetched_at"] is not None


def test_bars_are_requested_unadjusted(market_engine, lake):
    source = aapl_source()
    source.adjusted_grouped_daily_bars[DAY] = [BarData("AAPL", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1)]

    result = load_daily_bars(source, DAY, engine=market_engine, lake=lake)

    df = read_bars(lake, [result.security_ids["AAPL"]], DAY, DAY)
    assert df["close"].to_list() == [105.0]


def test_otc_bars_are_excluded(market_engine, lake):
    source = aapl_source()
    source.tickers_.append(TickerRecord("PINK", "Pink Co", "CS", "otc", "BBG000PINK01"))
    source.grouped_daily_bars[DAY].append(BarData("PINK", 1.0, 1.0, 1.0, 1.0, 10.0, 1.0, 1, otc=True))

    result = load_daily_bars(source, DAY, engine=market_engine, lake=lake)

    assert set(result.security_ids) == {"AAPL"}
    assert read_bars(lake, [1, 2, 3], DAY, DAY).height == 1


def test_one_security_per_share_class_figi(market_engine, lake):
    source = aapl_source()
    source.tickers_.append(TickerRecord("AAPL.OLD", "Apple", "CS", "stocks", "BBG001S5N8V8"))
    source.grouped_daily_bars[DAY].append(BarData("AAPL.OLD", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1))

    result = load_daily_bars(source, DAY, engine=market_engine, lake=lake)

    assert result.security_ids["AAPL"] == result.security_ids["AAPL.OLD"]


def test_security_ids_are_stable_across_runs(market_engine, lake):
    source = aapl_source()
    source.grouped_daily_bars[NEXT] = source.grouped_daily_bars[DAY]
    first = load_daily_bars(source, DAY, engine=market_engine, lake=lake)
    second = load_daily_bars(source, NEXT, engine=market_engine, lake=lake)

    assert first.security_ids == second.security_ids
    df = read_bars(lake, list(first.security_ids.values()), DAY, NEXT)
    assert df["trading_date"].to_list() == [DAY, NEXT]


def test_rerunning_a_date_does_not_duplicate_bars(market_engine, lake):
    source = aapl_source()
    result = load_daily_bars(source, DAY, engine=market_engine, lake=lake)
    load_daily_bars(source, DAY, engine=market_engine, lake=lake)

    assert read_bars(lake, list(result.security_ids.values()), DAY, DAY).height == 1


def test_read_returns_only_requested_securities_dates_and_columns(market_engine, lake):
    source = aapl_source()
    source.tickers_.append(TickerRecord("MSFT", "Microsoft", "CS", "stocks", "BBG001S5TD05"))
    source.grouped_daily_bars[DAY].append(BarData("MSFT", 50.0, 51.0, 49.0, 50.5, 10.0, 50.2, 7))
    source.grouped_daily_bars[NEXT] = [BarData("AAPL", 1.0, 2.0, 0.5, 1.5, 3.0, 1.2, 4)]
    ids = {}
    for d in (DAY, NEXT):
        ids |= load_daily_bars(source, d, engine=market_engine, lake=lake).security_ids

    df = read_bars(lake, [ids["AAPL"]], NEXT, NEXT, columns=["trading_date", "close"])

    assert df.columns == ["trading_date", "close"]
    assert df.rows() == [(NEXT, 1.5)]


def test_prices_and_volume_are_64_bit_floats_and_files_split_by_year(market_engine, lake):
    source = aapl_source()
    old = date(2025, 12, 31)
    source.grouped_daily_bars[old] = source.grouped_daily_bars[DAY]
    load_daily_bars(source, old, engine=market_engine, lake=lake)
    result = load_daily_bars(source, DAY, engine=market_engine, lake=lake)

    df = read_bars(lake, list(result.security_ids.values()), old, DAY)

    assert [df.schema[c] for c in ("open", "high", "low", "close", "volume")] == [pl.Float64] * 5
    assert df.columns == ALL_COLUMNS
    parquet_dirs = {p.parent.name for p in lake.data_path.rglob("*.parquet")}
    assert parquet_dirs >= {"year=2025", "year=2026"}
