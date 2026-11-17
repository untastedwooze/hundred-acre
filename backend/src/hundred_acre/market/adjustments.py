"""Corporate Actions (splits, dividends) and Adjustment Factors (#23).

Corporate Actions are append-only revisions in Postgres. From the current ones, a DuckLake table holds a cumulative
split factor and total-return factor per Security per trading day, rebuilt per Security when its actions change.
Factors multiply Unadjusted prices at read time (`read_bars(basis=...)`); stored Bars are never rewritten.

Factor for day d covers every action *after* d (an action's own day is already on the new basis). Bars loaded after
a rebuild reuse the nearest earlier factor, so rebuild (or sync) after loading Bars for a Security with later actions.
"""

from dataclasses import dataclass, field
from datetime import date

import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.lake import Lake, read_bars, transaction
from hundred_acre.market.reference import security_on
from hundred_acre.market.source import MarketDataSource


@dataclass(frozen=True)
class CorporateAction:
    kind: str  # "split" | "dividend"
    source_key: str
    revision: int
    security_id: int
    action_date: date
    split_from: float | None
    split_to: float | None
    cash_amount: float | None
    current: bool
    dividend_type: str | None = None


@dataclass
class SyncResult:
    rebuilt: list[int] = field(default_factory=list)  # Securities whose current actions changed and factors were rebuilt


def corporate_action_history(engine: Engine, security_id: int) -> list[CorporateAction]:
    """Every revision of every split and dividend of a Security, oldest first; `current` marks the live revision."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT r.kind, r.source_key, r.revision, r.security_id, r.action_date, r.split_from, r.split_to, "
                "r.cash_amount, r.revision = (SELECT max(revision) FROM corporate_action_revisions x "
                "WHERE x.kind = r.kind AND x.source_key = r.source_key), r.dividend_type "
                "FROM corporate_action_revisions r WHERE r.security_id = :s ORDER BY r.action_date, r.kind, r.revision"
            ),
            {"s": security_id},
        ).all()
    return [CorporateAction(*r) for r in rows]


def sync_corporate_actions(source: MarketDataSource, since: date, *, engine: Engine, lake: Lake) -> SyncResult:
    """Record new or revised splits and dividends since `since`, then rebuild factors of the Securities they changed."""
    incoming = [
        ("split", s.id or f"{s.ticker}:{s.execution_date}", s.ticker, s.execution_date, s.split_from, s.split_to, None, None)
        for s in source.splits(since)
    ] + [
        ("dividend", d.id or f"{d.ticker}:{d.ex_dividend_date}", d.ticker, d.ex_dividend_date, None, None, d.cash_amount, d.dividend_type)
        for d in source.dividends(since)
    ]
    changed: set[int] = set()
    for kind, key, ticker, day, split_from, split_to, cash, dividend_type in incoming:
        security_id = security_on(engine, ticker, day)
        if security_id is None:
            continue
        with engine.begin() as conn:
            latest = conn.execute(
                text(
                    "SELECT revision, security_id, action_date, split_from, split_to, cash_amount, dividend_type "
                    "FROM corporate_action_revisions WHERE kind = :k AND source_key = :key ORDER BY revision DESC LIMIT 1"
                ),
                {"k": kind, "key": key},
            ).first()
            if latest is not None and tuple(latest[1:]) == (security_id, day, split_from, split_to, cash, dividend_type):
                continue
            conn.execute(
                text(
                    "INSERT INTO corporate_action_revisions "
                    "(kind, source_key, revision, security_id, action_date, split_from, split_to, cash_amount, dividend_type) "
                    "VALUES (:k, :key, :rev, :s, :d, :f, :t, :c, :dt)"
                ),
                {"k": kind, "key": key, "rev": (latest[0] + 1) if latest else 1, "s": security_id, "d": day,
                 "f": split_from, "t": split_to, "c": cash, "dt": dividend_type},
            )
        changed.add(security_id)
        if latest is not None:
            changed.add(latest[1])  # the Security an action moved away from
    for security_id in sorted(changed):
        rebuild_adjustment_factors(engine, lake, security_id)
    return SyncResult(sorted(changed))


def rebuild_adjustment_factors(engine: Engine, lake: Lake, security_id: int) -> None:
    """Rebuild one Security's whole factor series from its current Corporate Actions, in one DuckLake transaction."""
    bars = read_bars(lake, [security_id], date.min, date.max, columns=["trading_date", "close"])
    with engine.connect() as conn:
        actions = conn.execute(
            text(
                "SELECT kind, action_date, split_from, split_to, cash_amount FROM corporate_actions_current "
                "WHERE security_id = :s ORDER BY action_date"
            ),
            {"s": security_id},
        ).all()
    days, closes = bars["trading_date"].to_list(), bars["close"].to_list()
    multipliers = []  # (action date, split multiplier, dividend multiplier)
    for kind, day, split_from, split_to, cash in actions:
        if kind == "split":
            multipliers.append((day, split_from / split_to, 1.0))
            continue
        previous = [c for d, c in zip(days, closes) if d < day]
        multipliers.append((day, 1.0, 1.0 - cash / previous[-1] if previous else 1.0))
    rows = []
    for d in days:
        split = total = 1.0
        for day, s, dv in multipliers:
            if day > d:
                split *= s
                total *= s * dv
        rows.append((security_id, d, split, total))
    frame = pl.DataFrame(
        rows, schema={"security_id": pl.Int64, "trading_date": pl.Date, "split_factor": pl.Float64,
                      "total_return_factor": pl.Float64}, orient="row",
    )
    with lake.connect() as con:
        con.register("incoming", frame.to_arrow())
        with transaction(con):
            con.execute("DELETE FROM lake.adjustment_factors WHERE security_id = ?", [security_id])
            con.execute("INSERT INTO lake.adjustment_factors SELECT * FROM incoming")


_PRICE_COLUMNS = ("open", "high", "low", "close", "vwap")
FACTOR_COLUMN = {"split": "split_factor", "total_return": "total_return_factor"}


def apply_adjustment(lake: Lake, bars: pl.DataFrame, basis: str) -> pl.DataFrame:
    """Multiply prices by the basis' factor and divide volume by the split factor; needs security_id and trading_date."""
    with lake.connect() as con:
        factors = con.sql(
            "SELECT security_id, trading_date, split_factor, total_return_factor FROM lake.adjustment_factors "
            "WHERE list_contains(?, security_id)",
            params=[bars["security_id"].unique().to_list()],
        ).pl()
    joined = (
        bars.with_row_index("_row")
        .sort("security_id", "trading_date")
        .join_asof(factors.sort("security_id", "trading_date"), on="trading_date", by="security_id", strategy="backward", check_sortedness=False)
        .sort("_row")
        .with_columns(pl.col("split_factor").fill_null(1.0), pl.col("total_return_factor").fill_null(1.0))
    )
    price_factor = pl.col(FACTOR_COLUMN[basis])
    return joined.with_columns(
        [(pl.col(c) * price_factor).alias(c) for c in _PRICE_COLUMNS if c in joined.columns]
        + ([(pl.col("volume") / pl.col("split_factor")).alias("volume")] if "volume" in joined.columns else [])
    ).drop("_row", "split_factor", "total_return_factor")
