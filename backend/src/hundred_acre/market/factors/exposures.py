"""Factor Exposures (#43): per-Security OLS fits of excess returns on Reference Factor Returns, a recomputable cache.

For each Security and model, the daily excess return (total return from `daily_total_returns`, minus the risk-free
rate) is regressed on the model's Factor Returns plus an intercept (alpha, daily). Windows are the `window_days`
Trading Calendar days ending at a month-end trading day; a window is estimated only when its end is on or before the
vintage's last Factor Return and at least `min_coverage` of its days have both a Security return and a Factor Return.

Every row records its method, parameters, a fingerprint of the Security's source data (Daily Bars, Adjustment Factors
and Delisting), the DuckLake Bar snapshot and the Factor Return vintage it was computed from. A Security whose
fingerprint or vintage no longer matches is stale and is rebuilt.

This tracer bullet estimates the single latest month-end window for FF6; `MODELS` and `_window_ends` are the seams
along which further models and rolling windows extend it.
"""

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl
from sqlalchemy import Engine, text

from hundred_acre.market.delistings import delisting
from hundred_acre.market.factors.reference_returns import VintageInfo, latest_vintage, read_reference_factor_returns
from hundred_acre.market.factors.source import FACTOR_COLUMNS
from hundred_acre.market.lake import Lake, current_snapshot, read_bars, transaction
from hundred_acre.market.returns import daily_total_returns

MODELS: dict[str, tuple[str, ...]] = {"ff6": FACTOR_COLUMNS}
METHOD = "OLS of daily excess total returns on daily Reference Factor Returns, with intercept (alpha, daily)"
DEFAULT_WINDOW_DAYS = 252
DEFAULT_MIN_COVERAGE = 0.8

EXPOSURE_COLUMNS = (
    "security_id", "model", "window_start", "window_end", "n_obs", "alpha",
    *(f"beta_{f}" for f in FACTOR_COLUMNS), "t_alpha", *(f"t_{f}" for f in FACTOR_COLUMNS), "r_squared",
    "method", "parameters", "source_fingerprint", "bar_snapshot", "factor_vintage",
)
_DDL = f"""
CREATE TABLE IF NOT EXISTS lake.factor_exposures (
    security_id BIGINT, model VARCHAR, window_start DATE, window_end DATE, n_obs BIGINT, alpha DOUBLE,
    {', '.join(f'beta_{f} DOUBLE' for f in FACTOR_COLUMNS)}, t_alpha DOUBLE, {', '.join(f't_{f} DOUBLE' for f in FACTOR_COLUMNS)},
    r_squared DOUBLE, method VARCHAR, parameters VARCHAR, source_fingerprint VARCHAR, bar_snapshot BIGINT,
    factor_vintage VARCHAR
)"""


@dataclass
class ExposureRefreshResult:
    rebuilt: list[int] = field(default_factory=list)  # Securities recomputed (with or without an estimable window)


def _window_ends(engine: Engine, vintage: VintageInfo, window_days: int) -> list[tuple[date, list[date]]]:
    """(window end, window's trading days) pairs to estimate: the latest month-end on or before the last Factor Return."""
    with engine.connect() as conn:
        days = [
            r[0] for r in conn.execute(
                text("SELECT trading_date FROM trading_calendar WHERE calendar_id = 'XNYS' AND trading_date <= :d ORDER BY trading_date"),
                {"d": vintage.last_date + timedelta(days=10)},
            )
        ]
    month_ends = [d for d, nxt in zip(days, days[1:]) if (d.year, d.month) != (nxt.year, nxt.month) and d <= vintage.last_date]
    if not month_ends:
        return []
    end = month_ends[-1]
    window = [d for d in days if d <= end][-window_days:]
    return [(end, window)] if len(window) == window_days else []


def _fingerprints(engine: Engine, con) -> dict[int, str]:
    parts: dict[int, list] = {}
    for table, agg in (
        ("daily_bars", "count(*), max(fetched_at)::VARCHAR"),
        ("adjustment_factors", "count(*), sum(split_factor), sum(total_return_factor)"),
    ):
        for sid, *vals in con.execute(f"SELECT security_id, {agg} FROM lake.{table} GROUP BY security_id").fetchall():
            parts.setdefault(sid, []).append((table, *map(str, vals)))
    out = {}
    for sid, p in parts.items():
        if any(t == "daily_bars" for t, *_ in p):
            out[sid] = json.dumps(sorted(p) + [("delisting", repr(delisting(engine, sid)))])
    return out


def _ols(y: np.ndarray, x: np.ndarray) -> dict | None:
    n, p = len(y), x.shape[1] + 1
    if n <= p:
        return None
    design = np.column_stack([np.ones(n), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    rss, tss = float(resid @ resid), float(((y - y.mean()) ** 2).sum())
    cov = rss / (n - p) * np.linalg.pinv(design.T @ design)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = coef / np.sqrt(np.diag(cov))
    return {"coef": coef, "t": t, "r_squared": 1 - rss / tss if tss > 0 else None}


def _estimate(
    engine: Engine, lake: Lake, security_id: int, factors: pl.DataFrame, end: date, window: list[date], min_coverage: float,
) -> list[dict]:
    rets = daily_total_returns(engine, lake, [security_id], window[0], end)
    bar_dates = set(read_bars(lake, [security_id], date.min, end, columns=["trading_date"])["trading_date"])
    previous = dict(zip(window[1:], window))
    # A return across a missing Bar spans several days, so it is not paired with a one-day Factor Return.
    # The window's first day has no calendar predecessor here, so it is judged on its own Bar.
    joined = rets.join(factors, on="trading_date").filter(
        pl.col("trading_date").map_elements(lambda d: previous.get(d, d) in bar_dates, return_dtype=pl.Boolean)
    ).sort("trading_date")
    rows = []
    for model, columns in MODELS.items():
        if joined.height < min_coverage * len(window):
            continue
        fit = _ols((joined["total_return"] - joined["rf"]).to_numpy(), joined.select(columns).to_numpy())
        if fit is None:
            continue
        row = {"security_id": security_id, "model": model, "window_start": window[0], "window_end": end,
               "n_obs": joined.height, "alpha": float(fit["coef"][0]), "t_alpha": float(fit["t"][0]),
               "r_squared": fit["r_squared"]}
        for f in FACTOR_COLUMNS:
            row[f"beta_{f}"] = row[f"t_{f}"] = None
        for i, f in enumerate(columns, start=1):
            row[f"beta_{f}"], row[f"t_{f}"] = float(fit["coef"][i]), float(fit["t"][i])
        rows.append(row)
    return rows


def refresh_factor_exposures(
    engine: Engine, lake: Lake, *, security_ids: list[int] | None = None, full: bool = False,
    window_days: int = DEFAULT_WINDOW_DAYS, min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> ExposureRefreshResult:
    """Recompute exposures for Securities whose source data or Factor Return vintage changed since computed.

    `full=True` recomputes every Security (or `security_ids`) regardless of staleness. Does nothing before the first
    Factor Return import.
    """
    vintage = latest_vintage(lake)
    if vintage is None:
        return ExposureRefreshResult()
    windows = _window_ends(engine, vintage, window_days)
    parameters = json.dumps({"window_days": window_days, "step": "month-end", "min_coverage": min_coverage})
    with lake.connect() as con:
        con.execute(_DDL)
        current = _fingerprints(engine, con)
        stored = {
            sid: (fp, fv) for sid, fp, fv in con.execute(
                "SELECT security_id, any_value(source_fingerprint), any_value(factor_vintage) "
                "FROM lake.factor_exposures GROUP BY security_id"
            ).fetchall()
        }
    wanted = sorted(current if security_ids is None else set(security_ids) & set(current))
    todo = [s for s in wanted if full or stored.get(s) != (current[s], vintage.vintage)]
    for sid in todo:
        snapshot = current_snapshot(lake)
        rows = []
        for end, window in windows:
            factors = read_reference_factor_returns(lake, vintage=vintage.vintage, start=window[0], end=end).drop("vintage")
            rows += _estimate(engine, lake, sid, factors, end, window, min_coverage)
        frame = pl.DataFrame(
            [{**r, "method": METHOD, "parameters": parameters, "source_fingerprint": current[sid], "bar_snapshot": snapshot,
              "factor_vintage": vintage.vintage} for r in rows],
            schema={"security_id": pl.Int64, "model": pl.String, "window_start": pl.Date, "window_end": pl.Date,
                    "n_obs": pl.Int64, "alpha": pl.Float64, **{c: pl.Float64 for c in EXPOSURE_COLUMNS[6:-5]},
                    "method": pl.String, "parameters": pl.String, "source_fingerprint": pl.String,
                    "bar_snapshot": pl.Int64, "factor_vintage": pl.String},
        ).select(EXPOSURE_COLUMNS)
        with lake.connect() as con:
            con.register("incoming", frame.to_arrow())
            with transaction(con):
                con.execute("DELETE FROM lake.factor_exposures WHERE security_id = ?", [sid])
                con.execute("INSERT INTO lake.factor_exposures SELECT * FROM incoming")
    return ExposureRefreshResult(todo)


def read_factor_exposures(
    lake: Lake, security_ids: list[int], start: date, end: date, model: str | None = None
) -> pl.DataFrame:
    """Factor Exposures for `security_ids` with `start <= window_end <= end`, optionally one `model`, by Security,
    model then window. One row per estimate with alpha, betas, t-statistics, R^2, n_obs and provenance columns."""
    with lake.connect() as con:
        con.execute(_DDL)
        return con.sql(
            f"SELECT {', '.join(EXPOSURE_COLUMNS)} FROM lake.factor_exposures "
            "WHERE list_contains(?, security_id) AND window_end BETWEEN ? AND ? AND (? IS NULL OR model = ?) "
            "ORDER BY security_id, model, window_end",
            params=[list(security_ids), start, end, model, model],
        ).pl()
