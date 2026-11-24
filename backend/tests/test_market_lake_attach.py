"""A Lake attaches DuckLake once per process and hands out cursors, so per-Security loops don't pay an ATTACH each.

Regression: the production nightly spent ~6 hours in `refresh_statistics` because every `lake.connect()` started a new
DuckDB, re-ran INSTALL/LOAD/ATTACH and re-read the catalog (~0.4s), about 7 times per Security.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import duckdb
import polars as pl
import pytest

from hundred_acre.market import lake as lake_module
from hundred_acre.market.lake import BAR_SCHEMA, Lake, read_bars, write_bars
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord
from hundred_acre.market.statistics import refresh_statistics
from market_helpers import load_daily_bars

DAYS = [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)]
NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


@pytest.fixture
def count_attaches(monkeypatch):
    """Counts new DuckDB databases the lake module opens (each one is a full ATTACH + catalog read)."""
    calls = []
    real = duckdb.connect

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(lake_module.duckdb, "connect", counting)
    return calls


def bars(security_id: int, day: date, close: float) -> pl.DataFrame:
    return pl.DataFrame(
        [dict(security_id=security_id, trading_date=day, ticker=f"T{security_id}", open=close, high=close, low=close,
              close=close, volume=1.0, vwap=close, trade_count=1, source="test", fetched_at=NOW)],
        schema=BAR_SCHEMA,
    )


def test_refreshing_statistics_for_many_securities_attaches_the_lake_once(market_engine, lake, count_attaches):
    n = 4
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord(f"T{i}", f"T{i}", "CS", "stocks", f"BBG{i:09d}") for i in range(n)]
    s.grouped_daily_bars = {
        d: [BarData(f"T{i}", 10 + j, 10 + j, 10 + j, 10 + j, 1000, 10 + j, 3) for i in range(n)] for j, d in enumerate(DAYS)
    }
    for d in DAYS:
        load_daily_bars(s, d, engine=market_engine, lake=lake)

    assert len(refresh_statistics(lake).rebuilt) == n
    assert len(count_attaches) <= 1, f"{len(count_attaches)} lake ATTACHes for {n} Securities"


def test_an_attached_lake_sees_and_builds_on_bars_another_attachment_committed(market_db_url, tmp_path):
    # Two Lake objects stand in for two processes (e.g. the API and the nightly): each has its own attachment.
    reader = Lake(catalog_url=market_db_url, data_path=tmp_path / "ducklake")
    writer = Lake(catalog_url=market_db_url, data_path=tmp_path / "ducklake")
    try:
        assert read_bars(reader, [1, 2], date.min, date.max).is_empty()  # reader is now attached
        write_bars(writer, bars(1, DAYS[0], 10.0), DAYS[0])
        assert read_bars(reader, [1], date.min, date.max)["close"].to_list() == [10.0]

        write_bars(reader, bars(2, DAYS[1], 20.0), DAYS[1])  # commits on top of the writer's snapshot
        got = read_bars(writer, [1, 2], date.min, date.max)
        assert got.select("security_id", "close").rows() == [(1, 10.0), (2, 20.0)]
    finally:
        reader.close()
        writer.close()


def test_threads_can_read_through_one_lake_at_once(lake):
    write_bars(lake, pl.concat([bars(i, DAYS[0], float(i)) for i in range(1, 9)]), DAYS[0])

    def close_of(sid: int) -> float:
        return read_bars(lake, [sid], date.min, date.max)["close"].item()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(close_of, range(1, 9))) == [float(i) for i in range(1, 9)]
