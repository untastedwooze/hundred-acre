"""Shared builders for Factor Exposure tests: synthetic Factor Returns and Securities whose returns are exact."""

from datetime import date, datetime, timezone

import numpy as np

from hundred_acre.market.factors import FACTOR_COLUMNS, FakeReferenceFactorSource
import polars as pl

from hundred_acre.market.lake import BAR_SCHEMA, bar_row, replace_bars
from hundred_acre.market.reference import security_on, sync_reference
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord
from hundred_acre.market.trading_calendar import read_trading_calendar, generate_calendar

FIRST, LAST_BAR = date(2026, 6, 1), date(2026, 10, 30)
FACTORS_THROUGH = date(2026, 10, 15)  # so the latest month-end window ends 2026-09-30
WINDOW_END = date(2026, 9, 30)
WINDOW_DAYS = 60
RF = 0.0001
T1 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 8, tzinfo=timezone.utc)


def trading_days(engine) -> list[date]:
    generate_calendar(engine, FIRST, LAST_BAR)
    return read_trading_calendar(engine, FIRST, LAST_BAR)["trading_date"].to_list()


def factor_matrix(n: int, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 0.01, size=(n, len(FACTOR_COLUMNS)))


def factor_source(days: list[date], matrix: np.ndarray, *, through: date = FACTORS_THROUGH) -> FakeReferenceFactorSource:
    """French-format files (percent) for `days` up to `through`."""
    five, mom = ["x", "", ",Mkt-RF,SMB,HML,RMW,CMA,RF"], ["x", "", ",Mom"]
    for d, row in zip(days, matrix):
        if d > through:
            break
        pct = [repr(float(v) * 100) for v in row]
        five.append(f"{d:%Y%m%d}," + ",".join(pct[:5] + [repr(RF * 100)]))
        mom.append(f"{d:%Y%m%d}," + pct[5])
    return FakeReferenceFactorSource("\n".join(five) + "\n", "\n".join(mom) + "\n")


def prices_from(matrix: np.ndarray, alpha: float, betas: list[float], start: float = 100.0) -> list[float]:
    """Closes whose daily excess returns are exactly alpha + betas . factors (first day has no return)."""
    returns = RF + alpha + matrix @ np.array(betas)
    closes = [start]
    for r in returns[1:]:
        closes.append(closes[-1] * (1 + r))
    return closes


def bars_frame(ids: dict[str, int], days: list[date], closes: dict[str, list[float | None]], only: list[int] | None = None) -> pl.DataFrame:
    rows = [
        bar_row(ids[t], d, BarData(t, c[i], c[i], c[i], c[i], 1000.0, c[i], 1), source="test", fetched_at=datetime.now(timezone.utc))
        for t, c in closes.items() if only is None or ids[t] in only
        for i, d in enumerate(days) if c[i] is not None
    ]
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


def load_world(engine, lake, days: list[date], closes: dict[str, list[float | None]]):
    """Securities by ticker with the given closes per day (None: no Bar that day), written in one batch."""
    source = FakeMarketDataSource()
    source.tickers_ = [TickerRecord(t, t, "CS", "stocks", f"F{t}") for t in closes]
    sync_reference(source, engine)
    ids = {t: security_on(engine, t, days[0]) for t in closes}
    replace_bars(lake, bars_frame(ids, days, closes), list(ids.values()), days)
    return source, ids
