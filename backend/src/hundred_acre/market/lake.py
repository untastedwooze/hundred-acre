"""DuckLake store for Bars (ADR-0002): catalog in the market Postgres database, Parquet files under a data path."""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
from sqlalchemy.engine import make_url

BAR_COLUMNS = (
    "security_id", "trading_date", "open", "high", "low", "close", "volume", "vwap", "trade_count", "source", "fetched_at",
)

BAR_SCHEMA = {
    "security_id": pl.Int64, "trading_date": pl.Date, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
    "close": pl.Float64, "volume": pl.Float64, "vwap": pl.Float64, "trade_count": pl.Int64, "source": pl.String,
    "fetched_at": pl.Datetime("us", "UTC"),
}

INTRADAY_COLUMNS = (
    "security_id", "trading_date", "bar_start", "trading_session", "open", "high", "low", "close", "volume", "vwap",
    "trade_count", "source", "fetched_at",
)

INTRADAY_SCHEMA = {**BAR_SCHEMA, "bar_start": pl.Datetime("us", "UTC"), "trading_session": pl.String}

_CREATE_INTRADAY = """
CREATE TABLE IF NOT EXISTS lake.intraday_bars (
    security_id BIGINT, trading_date DATE, bar_start TIMESTAMPTZ, trading_session VARCHAR,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, vwap DOUBLE,
    trade_count BIGINT, source VARCHAR, fetched_at TIMESTAMPTZ
)"""

_CREATE_BARS = """
CREATE TABLE IF NOT EXISTS lake.daily_bars (
    security_id BIGINT, trading_date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, vwap DOUBLE,
    trade_count BIGINT, source VARCHAR, fetched_at TIMESTAMPTZ
)"""

_CREATE_FACTORS = """
CREATE TABLE IF NOT EXISTS lake.adjustment_factors (
    security_id BIGINT, trading_date DATE, split_factor DOUBLE, total_return_factor DOUBLE
)"""

_CREATE_STATISTICS = """
CREATE TABLE IF NOT EXISTS lake.daily_security_statistics (
    security_id BIGINT, trading_date DATE, realized_volatility DOUBLE, overnight_return DOUBLE, dollar_volume DOUBLE,
    avg_volume_20 DOUBLE, avg_volume_60 DOUBLE, method VARCHAR, parameters VARCHAR, source_fingerprint VARCHAR,
    source_snapshot BIGINT
)"""

BASES = ("none", "split", "total_return")


@dataclass(frozen=True)
class Lake:
    catalog_url: str  # SQLAlchemy URL of the hundred_acre_market database
    data_path: Path

    @classmethod
    def from_env(cls) -> "Lake":
        """Catalog from MARKET_DATABASE_URL (default: local hundred_acre_market); data path from MARKET_DATA_PATH."""
        from hundred_acre.market.db import market_database_url

        return cls(market_database_url(), Path(os.environ["MARKET_DATA_PATH"]))

    def _attach_target(self) -> str:
        u = make_url(self.catalog_url)
        parts = {"dbname": u.database, "host": u.host, "port": u.port, "user": u.username, "password": u.password}
        conninfo = " ".join(f"{k}={v}" for k, v in parts.items() if v is not None)
        return f"ducklake:postgres:{conninfo}"

    @contextmanager
    def connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        self.data_path.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        try:
            con.execute("INSTALL ducklake; INSTALL postgres; LOAD ducklake; LOAD postgres")
            con.execute("ATTACH ? AS lake (DATA_PATH ?, DATA_INLINING_ROW_LIMIT 0)", [self._attach_target(), str(self.data_path) + "/"])
            new = (
                con.execute(
                    "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' AND table_name = 'daily_bars'"
                ).fetchone()[0]
                == 0
            )
            con.execute(_CREATE_BARS)
            con.execute(_CREATE_FACTORS)
            con.execute(_CREATE_STATISTICS)
            if new:
                con.execute("ALTER TABLE lake.daily_bars SET PARTITIONED BY (year(trading_date))")
            new_intraday = (
                con.execute(
                    "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' AND table_name = 'intraday_bars'"
                ).fetchone()[0]
                == 0
            )
            con.execute(_CREATE_INTRADAY)
            if new_intraday:
                con.execute("ALTER TABLE lake.intraday_bars SET PARTITIONED BY (year(trading_date), month(trading_date))")
                con.execute("ALTER TABLE lake.intraday_bars SET SORTED BY (security_id, bar_start)")
            yield con
        finally:
            con.close()


def write_bars(lake: Lake, bars: pl.DataFrame, trading_date: date) -> None:
    """Replace `trading_date`'s Daily Bars with `bars` in one transaction (re-runs are idempotent)."""
    with lake.connect() as con:
        con.register("incoming", bars.select(BAR_COLUMNS).to_arrow())
        con.execute("BEGIN")
        con.execute("DELETE FROM lake.daily_bars WHERE trading_date = ?", [trading_date])
        con.execute("INSERT INTO lake.daily_bars SELECT * FROM incoming")
        con.execute("COMMIT")


def replace_bars(lake: Lake, bars: pl.DataFrame, security_ids: Sequence[int], days: Sequence[date]) -> None:
    """Replace `security_ids`' Daily Bars on `days` with `bars` in one transaction."""
    with lake.connect() as con:
        con.register("incoming", bars.select(BAR_COLUMNS).to_arrow())
        con.execute("BEGIN")
        con.execute(
            "DELETE FROM lake.daily_bars WHERE list_contains(?, security_id) AND list_contains(?, trading_date)",
            [list(security_ids), list(days)],
        )
        con.execute("INSERT INTO lake.daily_bars SELECT * FROM incoming")
        con.execute("COMMIT")


def add_bars(lake: Lake, bars: pl.DataFrame, trading_date: date) -> None:
    """Add Bars to `trading_date`, replacing only those of the same Securities and leaving other Bars untouched."""
    with lake.connect() as con:
        con.register("incoming", bars.select(BAR_COLUMNS).to_arrow())
        con.execute("BEGIN")
        con.execute(
            "DELETE FROM lake.daily_bars WHERE trading_date = ? AND security_id IN (SELECT security_id FROM incoming)",
            [trading_date],
        )
        con.execute("INSERT INTO lake.daily_bars SELECT * FROM incoming")
        con.execute("COMMIT")


DEFAULT_SNAPSHOT_RETENTION_DAYS = 90


def snapshot_retention_days() -> int:
    """How long DuckLake snapshots (previous versions of re-fetched Bars) stay readable; MARKET_SNAPSHOT_RETENTION_DAYS."""
    return int(os.environ.get("MARKET_SNAPSHOT_RETENTION_DAYS", DEFAULT_SNAPSHOT_RETENTION_DAYS))


def current_snapshot(lake: Lake) -> int:
    """Id of the latest DuckLake snapshot; pass it to `read_bars(snapshot=...)` later to see Bars as they are now."""
    with lake.connect() as con:
        return con.execute("SELECT id FROM lake.current_snapshot()").fetchone()[0]


def expire_snapshots(lake: Lake, retention_days: int | None = None) -> list[int]:
    """Expire snapshots older than `retention_days` (default: configured retention); returns the expired ids."""
    days = snapshot_retention_days() if retention_days is None else retention_days
    with lake.connect() as con:
        before = {r[0] for r in con.execute("SELECT snapshot_id FROM lake.snapshots()").fetchall()}
        con.execute(
            "CALL ducklake_expire_snapshots('lake', older_than => now() - to_days(?))", [days]
        )
        after = {r[0] for r in con.execute("SELECT snapshot_id FROM lake.snapshots()").fetchall()}
    return sorted(before - after)


def read_bars(
    lake: Lake,
    security_ids: Sequence[int],
    start: date,
    end: date,
    columns: Sequence[str] | None = None,
    snapshot: int | None = None,
    basis: str = "none",
) -> pl.DataFrame:
    """Unadjusted Daily Bars for `security_ids` with `start <= trading_date <= end`, as a Polars DataFrame.

    `columns` limits the columns returned (default: all), in the order asked. `snapshot` reads the Bars as they were
    at that DuckLake snapshot (see `current_snapshot`), e.g. before a late correction replaced them.
    `basis` is "none" (Unadjusted), "split" or "total_return": prices are multiplied by the Adjustment Factor at read
    time and volume divided by the split factor; stored Bars are untouched.
    """
    if basis not in BASES:
        raise ValueError(f"unknown adjustment basis: {basis!r}; expected one of {BASES}")
    wanted = list(columns) if columns is not None else list(BAR_COLUMNS)
    unknown = set(wanted) - set(BAR_COLUMNS)
    if unknown:
        raise ValueError(f"unknown Bar columns: {sorted(unknown)}")
    fetch = wanted if basis == "none" else wanted + [c for c in ("security_id", "trading_date") if c not in wanted]
    select = ", ".join(fetch)
    at = f" AT (VERSION => {int(snapshot)})" if snapshot is not None else ""
    with lake.connect() as con:
        df = con.sql(
            f"SELECT {select} FROM lake.daily_bars{at} WHERE list_contains(?, security_id) AND trading_date BETWEEN ? AND ? "
            "ORDER BY security_id, trading_date",
            params=[list(security_ids), start, end],
        ).pl()
    if basis == "none":
        return df
    from hundred_acre.market.adjustments import apply_adjustment

    return apply_adjustment(lake, df, basis).select(wanted)


def replace_intraday_bars(lake: Lake, bars: pl.DataFrame, security_ids: Sequence[int], days: Sequence[date]) -> None:
    """Replace `security_ids`' Intraday Bars on trading `days` with `bars` in one transaction, sorted by Security then bar_start."""
    with lake.connect() as con:
        con.register("incoming", bars.select(INTRADAY_COLUMNS).sort("security_id", "bar_start").to_arrow())
        con.execute("BEGIN")
        con.execute(
            "DELETE FROM lake.intraday_bars WHERE list_contains(?, security_id) AND list_contains(?, trading_date)",
            [list(security_ids), list(days)],
        )
        con.execute("INSERT INTO lake.intraday_bars SELECT * FROM incoming ORDER BY security_id, bar_start")
        con.execute("COMMIT")


def read_intraday_bars(
    lake: Lake,
    security_ids: Sequence[int],
    start: date,
    end: date,
    columns: Sequence[str] | None = None,
    sessions: str | Sequence[str] | None = "regular",
    snapshot: int | None = None,
) -> pl.DataFrame:
    """Unadjusted five-minute Bars for `security_ids` with `start <= trading_date <= end`, by Security then bar_start.

    Reads the regular Trading Session by default; `sessions` names one or several sessions, or None for all of them.
    """
    wanted = list(columns) if columns is not None else list(INTRADAY_COLUMNS)
    unknown = set(wanted) - set(INTRADAY_COLUMNS)
    if unknown:
        raise ValueError(f"unknown Bar columns: {sorted(unknown)}")
    chosen = [sessions] if isinstance(sessions, str) else None if sessions is None else list(sessions)
    at = f" AT (VERSION => {int(snapshot)})" if snapshot is not None else ""
    session_filter = "" if chosen is None else " AND list_contains(?, trading_session)"
    params = [list(security_ids), start, end] + ([chosen] if chosen is not None else [])
    with lake.connect() as con:
        return con.sql(
            f"SELECT {', '.join(wanted)} FROM lake.intraday_bars{at} "
            f"WHERE list_contains(?, security_id) AND trading_date BETWEEN ? AND ?{session_filter} "
            "ORDER BY security_id, bar_start",
            params=params,
        ).pl()
