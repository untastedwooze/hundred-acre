"""The Watchlist: Securities whose Intraday Bars are refreshed nightly. Adding one backfills its Intraday history."""

import json
from datetime import date

from sqlalchemy import Engine, text

from hundred_acre.market.lake import Lake
from hundred_acre.market.priority import PriorityClass, at_priority
from hundred_acre.market.reference import _build_periods, _security_type, _write_periods, records_json, security_on
from hundred_acre.market.source import MarketDataSource


def add_to_watchlist(source: MarketDataSource, engine: Engine, lake: Lake, ticker: str, *, as_of: date) -> int:
    """Put the Security `ticker` referred to on `as_of` on the Watchlist and backfill its missing Intraday history.

    An over-the-counter Ticker the Universe does not hold yet is added to it by hand here. Returns the Security id.
    Raises ValueError if the source knows no such Ticker.
    """
    security_id = security_on(engine, ticker, as_of)
    source = at_priority(source, PriorityClass.INTERACTIVE)  # a person is waiting on the lookup
    if security_id is None:
        security_id = _create_security(source, engine, ticker)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO watchlist (security_id) VALUES (:s) ON CONFLICT DO NOTHING"), {"s": security_id}
        )
    from hundred_acre.market.jobs import backfill_intraday_bars  # jobs imports this module

    backfill = at_priority(source, PriorityClass.BACKFILL)
    backfill_intraday_bars(backfill, as_of, engine=engine, lake=lake, security_ids=[security_id])
    return security_id


def remove_from_watchlist(engine: Engine, security_id: int) -> None:
    """Stop nightly Intraday refreshes for the Security; its stored Bars and Coverage stay."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM watchlist WHERE security_id = :s"), {"s": security_id})


def watchlist(engine: Engine) -> list[int]:
    """Security ids on the Watchlist."""
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT security_id FROM watchlist ORDER BY security_id")).scalars())


def non_watchlist_security_ids(engine: Engine, *, skipped_types: tuple[str, ...] = ()) -> list[int]:
    """Securities off the Watchlist whose type is not in `skipped_types` (an unknown type is kept)."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT security_id FROM securities WHERE security_id NOT IN (SELECT security_id FROM watchlist) "
                    "AND (type IS NULL OR type <> ALL(:skipped)) ORDER BY security_id"
                ),
                {"skipped": list(skipped_types)},
            ).scalars()
        )


def _create_security(source: MarketDataSource, engine: Engine, ticker: str) -> int:
    records = source.lookup_ticker(ticker)
    if not records:
        raise ValueError(f"unknown Ticker {ticker!r}")
    rec = records[-1]
    with engine.begin() as conn:
        if rec.cik:
            conn.execute(
                text("INSERT INTO issuers (cik, name) VALUES (:c, :n) ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name"),
                {"c": rec.cik, "n": rec.name},
            )
        security_id = None
        if rec.share_class_figi:
            security_id = conn.execute(
                text("SELECT security_id FROM securities WHERE share_class_figi = :f"), {"f": rec.share_class_figi}
            ).scalar()
        if security_id is not None:  # the FIGI is already in the Universe: its periods came from the reference sync
            return security_id
        if security_id is None:  # no FIGI: unverified until one is attached, like a Provisional Security
            security_id = conn.execute(
                text(
                    "INSERT INTO securities (share_class_figi, name, type, issuer_cik, verified, market, reference_records) "
                    "VALUES (:f, :n, :t, :c, :v, :m, CAST(:r AS jsonb)) RETURNING security_id"
                ),
                {"f": rec.share_class_figi, "n": rec.name, "t": _security_type(rec.type), "c": rec.cik,
                 "v": rec.share_class_figi is not None, "m": rec.market,
                 "r": json.dumps(records_json(records)) if rec.share_class_figi else None},
            ).scalar_one()
        # Its Ticker History is not fetched here: with no ticker_history row, `market ticker-history` and `backfill` pick it up.
        _write_periods(conn, security_id, [(rec.ticker, rec.list_date, rec.delisted_date)])
    return security_id
