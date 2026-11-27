"""DuckLake writes retry a transaction conflict with a concurrent writer instead of crashing the job (#71).

Regression: a `market backfill` died after ~20 hours when its `replace_bars` COMMIT met a delete that `nightly` had just
committed to the same table: `Transaction conflict - attempting to insert into table with index "1" - but another
transaction has deleted from it`. DuckLake conflicts at table level, so Bars for different days still collide.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone

import duckdb
import polars as pl
import pytest

from hundred_acre.market import lake as lake_module
from hundred_acre.market.lake import BAR_SCHEMA, Lake, read_bars, replace_bars, write_bars
from test_market_coverage import FIRST, LAST, TRADING_DAYS, backfill, ids_of, two_security_source

NIGHTLY_DAY = date(2026, 11, 2)  # far from the backfill's days: the conflict is per table, not per row


class _Cursor:
    """A DuckDB cursor that lets `before_commit` run just before each COMMIT, when the transaction can no longer change."""

    def __init__(self, cur, before_commit):
        self._cur, self._before_commit = cur, before_commit

    def execute(self, sql, *args):
        if sql.strip().upper() == "COMMIT":
            self._before_commit()
        return self._cur.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class OverlappedLake(Lake):
    """A Lake whose COMMITs race a separate process's `nightly` write, `conflicts` times (None: every time)."""

    def __init__(self, catalog_url, data_path, conflicts):
        super().__init__(catalog_url, data_path)
        object.__setattr__(self, "nightly", Lake(catalog_url, data_path))  # its own DuckDB, like the nightly process
        object.__setattr__(self, "remaining", conflicts)
        object.__setattr__(self, "commits", 0)

    def _nightly_commits_first(self):
        object.__setattr__(self, "commits", self.commits + 1)
        if self.remaining is None or self.remaining > 0:
            if self.remaining is not None:
                object.__setattr__(self, "remaining", self.remaining - 1)
            write_bars(self.nightly, _bar(NIGHTLY_DAY, 2.0), NIGHTLY_DAY)  # replaces a Bar: deletes from the table

    @contextmanager
    def connect(self):
        with super().connect() as cur:
            yield _Cursor(cur, self._nightly_commits_first)

    def close(self):
        self.nightly.close()
        super().close()


def _bar(day: date, close: float, security_id: int = 999) -> pl.DataFrame:
    return pl.DataFrame(
        [dict(security_id=security_id, trading_date=day, ticker="ZZZ", open=close, high=close, low=close, close=close,
              volume=1.0, vwap=close, trade_count=1, source="seed", fetched_at=datetime.now(timezone.utc))],
        schema=BAR_SCHEMA,
    )


@pytest.fixture
def overlapped(market_db_url, tmp_path, monkeypatch):
    """Make an OverlappedLake with a Bar on NIGHTLY_DAY already written; retry waits are skipped."""
    monkeypatch.setattr(lake_module.time, "sleep", lambda s: None)
    made = []

    def make(conflicts):
        lake = OverlappedLake(market_db_url, tmp_path / "ducklake", conflicts)
        write_bars(lake.nightly, _bar(NIGHTLY_DAY, 1.0), NIGHTLY_DAY)
        made.append(lake)
        return lake

    yield make
    for lake in made:
        lake.close()


def test_backfill_overlapping_nightly_finishes(market_engine, overlapped):
    lake = overlapped(conflicts=2)

    backfill(two_security_source(), market_engine, lake)

    ids = ids_of(market_engine)
    bars = read_bars(lake, sorted(ids.values()), FIRST, LAST)
    assert bars.height == 4 + 5  # AAPL has no trades on the 24th
    assert read_bars(lake, [999], NIGHTLY_DAY, NIGHTLY_DAY)["close"].to_list() == [2.0]  # nightly's write kept
    assert lake.remaining == 0


def test_a_conflict_that_keeps_happening_fails_loudly(overlapped):
    lake = overlapped(conflicts=None)

    with pytest.raises(duckdb.TransactionException, match="Transaction conflict"):
        replace_bars(lake, _bar(TRADING_DAYS[0], 5.0, security_id=1), [1], [TRADING_DAYS[0]])

    assert lake.commits == lake_module.COMMIT_ATTEMPTS
    assert read_bars(lake, [1], TRADING_DAYS[0], TRADING_DAYS[0]).height == 0
