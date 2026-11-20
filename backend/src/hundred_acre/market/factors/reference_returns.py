"""Reference Factor Returns in DuckLake (#43, ADR-0002/0004): imported as dated, immutable vintages.

Each import that differs from the latest vintage writes a complete new vintage (id = import date + content
fingerprint, plus a counter if that id is already taken, e.g. A, B, A re-imported on one day). Older vintages are never modified. Monthly returns are compounded from the daily ones at read time.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone

import polars as pl

from hundred_acre.market.factors.source import RETURN_COLUMNS, ReferenceFactorSource, parse_reference_factors
from hundred_acre.market.lake import Lake, transaction

_DDL = """
CREATE TABLE IF NOT EXISTS lake.reference_factor_returns (
    vintage VARCHAR, imported_at TIMESTAMPTZ, fingerprint VARCHAR, trading_date DATE,
    mkt_rf DOUBLE, smb DOUBLE, hml DOUBLE, rmw DOUBLE, cma DOUBLE, umd DOUBLE, rf DOUBLE
)"""


@dataclass(frozen=True)
class ImportResult:
    created: bool  # False when the published data matched the latest vintage
    vintage: str
    first_date: date
    last_date: date
    rows: int


@dataclass(frozen=True)
class VintageInfo:
    vintage: str  # id, also the fingerprint the exposures record
    imported_at: datetime
    first_date: date
    last_date: date


def _fingerprint(df: pl.DataFrame) -> str:
    return hashlib.sha256(df.write_csv().encode()).hexdigest()


def latest_vintage(lake: Lake) -> VintageInfo | None:
    """The most recently imported vintage, or None before the first import."""
    with lake.connect() as con:
        con.execute(_DDL)
        df = con.sql(
            "SELECT vintage, any_value(imported_at) AS imported_at, min(trading_date) AS first_date, "
            "max(trading_date) AS last_date FROM lake.reference_factor_returns "
            "GROUP BY vintage ORDER BY imported_at DESC LIMIT 1"
        ).pl()
    return VintageInfo(**df.row(0, named=True)) if df.height else None


def import_reference_factor_returns(
    lake: Lake, source: ReferenceFactorSource, *, now: datetime | None = None
) -> ImportResult:
    """Fetch and parse the published files; store a new vintage unless they match the latest one.

    Raises ReferenceFactorError (storing nothing) when the files are unreachable or malformed.
    """
    df = parse_reference_factors(*source.fetch())
    fingerprint = _fingerprint(df)
    now = now or datetime.now(timezone.utc)
    vintage = f"{now:%Y-%m-%d}-{fingerprint[:12]}"
    first, last = df["trading_date"].min(), df["trading_date"].max()
    with lake.connect() as con:
        con.execute(_DDL)
        found = con.execute(
            "SELECT vintage, any_value(fingerprint) FROM lake.reference_factor_returns "
            "GROUP BY vintage ORDER BY any_value(imported_at) DESC LIMIT 1"
        ).fetchall()
        latest = found[0] if found else None
        if latest and latest[1] == fingerprint:
            return ImportResult(False, latest[0], first, last, df.height)
        taken = {r[0] for r in con.execute("SELECT DISTINCT vintage FROM lake.reference_factor_returns").fetchall()}
        base, n = vintage, 1
        while vintage in taken:  # same-day A -> B -> A: a new vintage must not reuse (and duplicate) A's id
            n += 1
            vintage = f"{base}-{n}"
        incoming = df.with_columns(
            vintage=pl.lit(vintage), imported_at=pl.lit(now).dt.cast_time_unit("us").dt.replace_time_zone("UTC"),
            fingerprint=pl.lit(fingerprint),
        ).select("vintage", "imported_at", "fingerprint", "trading_date", *RETURN_COLUMNS)
        con.register("incoming", incoming.to_arrow())
        with transaction(con):
            con.execute("INSERT INTO lake.reference_factor_returns SELECT * FROM incoming")
    return ImportResult(True, vintage, first, last, df.height)


def read_reference_factor_returns(
    lake: Lake, *, vintage: str | None = None, start: date | None = None, end: date | None = None,
    frequency: str = "daily",
) -> pl.DataFrame:
    """Reference Factor Returns as decimals, for `vintage` (default: the latest) within `start..end`.

    Daily: trading_date, mkt_rf, smb, hml, rmw, cma, umd, rf, vintage. Monthly (`frequency="monthly"`): the same with
    `month` (the month's last calendar day) in place of trading_date, compounded from the daily returns in range.
    Empty before the first import.
    """
    if frequency not in ("daily", "monthly"):
        raise ValueError(f"unknown frequency {frequency!r}")
    if vintage is None:
        info = latest_vintage(lake)
        vintage = info.vintage if info else None
    with lake.connect() as con:
        con.execute(_DDL)
        daily = con.sql(
            f"SELECT trading_date, {', '.join(RETURN_COLUMNS)}, vintage FROM lake.reference_factor_returns "
            "WHERE vintage = ? AND trading_date BETWEEN ? AND ? ORDER BY trading_date",
            params=[vintage, start or date.min, end or date.max],
        ).pl()
    if frequency == "daily":
        return daily
    return (
        daily.with_columns(month=pl.col("trading_date").dt.month_end())
        .group_by("month", "vintage", maintain_order=True)
        .agg(((pl.col(c) + 1).product() - 1).alias(c) for c in RETURN_COLUMNS)
        .select("month", *RETURN_COLUMNS, "vintage")
    )
