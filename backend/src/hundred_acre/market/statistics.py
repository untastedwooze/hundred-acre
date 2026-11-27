"""Daily Security Statistics (#25): a recomputable DuckLake cache derived from Bars and Adjustment Factors.

Per Security per trading day: realized volatility (regular-session five-minute split-adjusted log returns, root of
their summed squares), overnight return (previous Daily Bar close to this day's open, split-adjusted), dollar volume
(vwap x volume, as traded) and trailing 20- and 60-day average split-adjusted volume (null until the window is full).

Every row records its method, parameters, a fingerprint of the source data (Daily Bars, Intraday Bars and factors) and
the DuckLake snapshot it was computed from. A Security whose fingerprint no longer matches is stale and is rebuilt.
"""

import json
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from hundred_acre.market.lake import Lake, commit, read_bars, read_intraday_bars

METHOD = "regular-session 5m split-adjusted log-return realized volatility; close-to-open overnight return; vwap dollar volume"
PARAMETERS = {"interval": "5m", "session": "regular", "adjustment": "split", "avg_volume_windows": [20, 60]}
STATISTICS_COLUMNS = (
    "security_id", "trading_date", "realized_volatility", "overnight_return", "dollar_volume", "avg_volume_20",
    "avg_volume_60", "method", "parameters", "source_fingerprint", "source_snapshot",
)
_SCHEMA = {
    "security_id": pl.Int64, "trading_date": pl.Date, "realized_volatility": pl.Float64, "overnight_return": pl.Float64,
    "dollar_volume": pl.Float64, "avg_volume_20": pl.Float64, "avg_volume_60": pl.Float64, "method": pl.String,
    "parameters": pl.String, "source_fingerprint": pl.String, "source_snapshot": pl.Int64,
}


@dataclass
class StatisticsResult:
    rebuilt: list[int] = field(default_factory=list)  # Securities whose statistics were (re)computed


def _fingerprints(con) -> dict[int, str]:
    parts: dict[int, list] = {}
    for table, agg in (
        ("daily_bars", "count(*), max(fetched_at)::VARCHAR"),
        ("intraday_bars", "count(*), max(fetched_at)::VARCHAR"),
        ("adjustment_factors", "count(*), sum(split_factor)"),
    ):
        for sid, *vals in con.execute(f"SELECT security_id, {agg} FROM lake.{table} GROUP BY security_id").fetchall():
            parts.setdefault(sid, []).append((table, *map(str, vals)))
    return {sid: json.dumps(sorted(p)) for sid, p in parts.items() if any(t == "daily_bars" for t, *_ in p)}


def _compute(lake: Lake, security_id: int) -> pl.DataFrame:
    daily = read_bars(lake, [security_id], date.min, date.max, basis="split").sort("trading_date")
    unadjusted = read_bars(lake, [security_id], date.min, date.max, columns=["trading_date", "vwap", "volume"])
    intraday = read_intraday_bars(
        lake, [security_id], date.min, date.max, columns=["trading_date", "bar_start", "close"], basis="split"
    )
    vol = pl.DataFrame(schema={"trading_date": pl.Date, "realized_volatility": pl.Float64})
    if intraday.height:
        adjusted = intraday.sort("bar_start")
        vol = (
            adjusted.with_columns(r=pl.col("close").log().diff().over("trading_date"))
            .group_by("trading_date")
            .agg(n=pl.col("r").drop_nulls().len(), ss=(pl.col("r") ** 2).sum())
            .select("trading_date", realized_volatility=pl.when(pl.col("n") > 0).then(pl.col("ss").sqrt()))
        )
    return (
        daily.select(
            "security_id", "trading_date",
            overnight_return=pl.col("open") / pl.col("close").shift(1) - 1,
            avg_volume_20=pl.col("volume").rolling_mean(20, min_samples=20),
            avg_volume_60=pl.col("volume").rolling_mean(60, min_samples=60),
        )
        .join(unadjusted.select("trading_date", dollar_volume=pl.col("vwap") * pl.col("volume")), on="trading_date")
        .join(vol, on="trading_date", how="left")
    )


def refresh_statistics(lake: Lake, *, security_ids: list[int] | None = None, full: bool = False) -> StatisticsResult:
    """Recompute statistics for Securities whose source data changed since they were computed.

    `full=True` recomputes every Security (or `security_ids`) from scratch regardless of staleness.
    """
    with lake.connect() as con:
        current = _fingerprints(con)
        stored = dict(con.execute(
            "SELECT security_id, any_value(source_fingerprint) FROM lake.daily_security_statistics GROUP BY security_id"
        ).fetchall())
    wanted = sorted(current if security_ids is None else set(security_ids) & set(current))
    todo = [s for s in wanted if full or stored.get(s) != current[s]]
    for sid in todo:
        with lake.connect() as con:
            snapshot = con.execute("SELECT id FROM lake.current_snapshot()").fetchone()[0]
        frame = (
            _compute(lake, sid)
            .with_columns(
                method=pl.lit(METHOD), parameters=pl.lit(json.dumps(PARAMETERS)),
                source_fingerprint=pl.lit(current[sid]), source_snapshot=pl.lit(snapshot, dtype=pl.Int64),
            )
            .select(STATISTICS_COLUMNS)
            .cast(_SCHEMA)
        )
        with lake.connect() as con:
            con.register("incoming", frame.to_arrow())
            commit(
                con,
                ("DELETE FROM lake.daily_security_statistics WHERE security_id = ?", [sid]),
                "INSERT INTO lake.daily_security_statistics SELECT * FROM incoming",
            )
    return StatisticsResult(todo)


def read_security_statistics(
    lake: Lake, security_ids: list[int], start: date, end: date, columns: list[str] | None = None
) -> pl.DataFrame:
    """Daily Security Statistics for `security_ids` with `start <= trading_date <= end`, by Security then date."""
    wanted = list(columns) if columns is not None else list(STATISTICS_COLUMNS)
    unknown = set(wanted) - set(STATISTICS_COLUMNS)
    if unknown:
        raise ValueError(f"unknown statistics columns: {sorted(unknown)}")
    with lake.connect() as con:
        return con.sql(
            f"SELECT {', '.join(wanted)} FROM lake.daily_security_statistics "
            "WHERE list_contains(?, security_id) AND trading_date BETWEEN ? AND ? ORDER BY security_id, trading_date",
            params=[list(security_ids), start, end],
        ).pl()
