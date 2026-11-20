"""Opt-in performance check: run with `pytest -m perf` (excluded from the default run)."""

import time
from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from hundred_acre.market.lake import INTRADAY_SCHEMA, read_intraday_bars, replace_intraday_bars

pytestmark = pytest.mark.perf

SECURITIES = 500
BARS_PER_DAY = 78  # regular session, five-minute Bars
READ_BUDGET_SECONDS = 2.5  # "about 2 seconds"


def _weekdays(start: date, count: int) -> list[date]:
    days, d = [], start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _synthetic(security_ids: list[int], days: list[date]) -> pl.DataFrame:
    base = pl.DataFrame({"security_id": security_ids}).join(
        pl.DataFrame({"trading_date": days}), how="cross"
    ).join(pl.DataFrame({"k": list(range(BARS_PER_DAY))}), how="cross")
    return base.with_columns(
        bar_start=(
            pl.col("trading_date").cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
            + pl.duration(hours=14, minutes=30) + pl.duration(minutes=5) * pl.col("k")
        ),
        trading_session=pl.lit("regular"),
        open=pl.col("k").cast(pl.Float64) + 100, high=pl.col("k").cast(pl.Float64) + 101,
        low=pl.col("k").cast(pl.Float64) + 99, close=pl.col("k").cast(pl.Float64) + 100.5,
        volume=pl.lit(1000.0), vwap=pl.col("k").cast(pl.Float64) + 100.2, trade_count=pl.lit(10, pl.Int64),
        source=pl.lit("synthetic"), fetched_at=pl.lit(datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ).drop("k").cast(INTRADAY_SCHEMA)


def test_year_of_five_minute_bars_for_500_securities_reads_in_about_two_seconds(lake):
    days = _weekdays(date(2025, 1, 2), 252)
    ids = list(range(1, SECURITIES + 1))
    for i in range(0, SECURITIES, 50):  # load in chunks to bound memory
        replace_intraday_bars(lake, _synthetic(ids[i : i + 50], days), ids[i : i + 50], days)

    started = time.perf_counter()
    df = read_intraday_bars(lake, ids, days[0], days[-1], columns=["security_id", "bar_start", "close"])
    elapsed = time.perf_counter() - started

    assert df.height == SECURITIES * len(days) * BARS_PER_DAY
    assert elapsed < READ_BUDGET_SECONDS, f"read took {elapsed:.2f}s"
