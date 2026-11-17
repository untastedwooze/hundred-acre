"""Coverage: one row per Security, interval and trading day recording what has been fetched."""

from collections.abc import Iterable, Sequence
from datetime import date, datetime

import polars as pl
from sqlalchemy import Connection, Engine, text

from hundred_acre.market.trading_calendar import NYSE

STATUSES = ("complete", "empty", "failed", "partial")  # "suspect" is also stored but summarised with its own queue


def record_coverage(
    conn: Connection,
    security_id: int,
    interval: str,
    days: Iterable[date],
    *,
    status: str,
    fetched_at: datetime,
    bar_counts: dict[date, int] | None = None,
) -> None:
    """Upsert Coverage for `days`; `bar_counts` gives each day's Bar count (default 0)."""
    rows = [
        {"s": security_id, "i": interval, "d": d, "f": fetched_at, "n": (bar_counts or {}).get(d, 0), "st": status}
        for d in days
    ]
    if not rows:
        return
    conn.execute(
        text(
            "INSERT INTO coverage (security_id, interval, trading_date, fetched_at, bar_count, status) "
            "VALUES (:s, :i, :d, :f, :n, :st) "
            "ON CONFLICT (security_id, interval, trading_date) DO UPDATE SET "
            "fetched_at = EXCLUDED.fetched_at, bar_count = EXCLUDED.bar_count, status = EXCLUDED.status"
        ),
        rows,
    )


def days_to_fetch(
    engine: Engine, security_id: int, interval: str, start: date, end: date, *, calendar_id: str = NYSE
) -> list[date]:
    """Trading Calendar days in [start, end] minus complete or empty Coverage."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT c.trading_date FROM trading_calendar c "
                    "LEFT JOIN coverage v ON v.security_id = :s AND v.interval = :i AND v.trading_date = c.trading_date "
                    "WHERE c.calendar_id = :cal AND c.trading_date BETWEEN :start AND :end "
                    "AND (v.status IS NULL OR v.status NOT IN ('complete', 'empty')) ORDER BY c.trading_date"
                ),
                {"s": security_id, "i": interval, "cal": calendar_id, "start": start, "end": end},
            ).scalars()
        )


def coverage_summary(
    engine: Engine,
    security_ids: Sequence[int],
    interval: str,
    start: date,
    end: date,
    *,
    calendar_id: str = NYSE,
) -> pl.DataFrame:
    """Per Security, how many Trading Calendar days in [start, end] are complete, empty, failed, partial or never fetched.

    Only Trading Calendar days count, so a holiday is never reported as missing.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT s.security_id, "
                "count(*) FILTER (WHERE v.status = 'complete'), count(*) FILTER (WHERE v.status = 'empty'), "
                "count(*) FILTER (WHERE v.status = 'failed'), count(*) FILTER (WHERE v.status = 'partial'), "
                "count(*) FILTER (WHERE v.status = 'suspect'), count(*) FILTER (WHERE v.status IS NULL) "
                "FROM unnest(CAST(:ids AS bigint[])) AS s(security_id) "
                "CROSS JOIN trading_calendar c "
                "LEFT JOIN coverage v ON v.security_id = s.security_id AND v.interval = :i "
                "AND v.trading_date = c.trading_date "
                "WHERE c.calendar_id = :cal AND c.trading_date BETWEEN :start AND :end "
                "GROUP BY s.security_id ORDER BY s.security_id"
            ),
            {"ids": list(security_ids), "i": interval, "cal": calendar_id, "start": start, "end": end},
        ).all()
    cols = ["security_id", "complete", "empty", "failed", "partial", "suspect", "never_fetched"]
    return pl.DataFrame([tuple(r) for r in rows], schema={c: pl.Int64 for c in cols}, orient="row")
