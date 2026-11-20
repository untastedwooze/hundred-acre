"""The Watchlist: Securities whose Intraday Bars are refreshed nightly. Adding one backfills its Intraday history."""

from datetime import date

from sqlalchemy import Engine, text

from hundred_acre.market.lake import Lake
from hundred_acre.market.priority import PriorityClass, at_priority
from hundred_acre.market.reference import _periods, _security_type, security_on, sync_reference
from hundred_acre.market.source import MarketDataSource


def add_to_watchlist(source: MarketDataSource, engine: Engine, lake: Lake, ticker: str, *, as_of: date) -> int:
    """Put the Security `ticker` referred to on `as_of` on the Watchlist and backfill its missing Intraday history.

    An over-the-counter Ticker the Universe does not hold yet is added to it by hand here. Returns the Security id.
    Raises ValueError if the source knows no such Ticker.
    """
    security_id = security_on(engine, ticker, as_of)
    source = at_priority(source, PriorityClass.INTERACTIVE)  # a person is waiting on the lookup
    if security_id is None:
        sync_reference(source, engine)
        security_id = security_on(engine, ticker, as_of) or _create_security(source, engine, ticker)
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
    records = [r for r in source.tickers() if r.ticker == ticker]
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
        if security_id is None:  # no FIGI: unverified until one is attached, like a Provisional Security
            security_id = conn.execute(
                text(
                    "INSERT INTO securities (share_class_figi, name, type, issuer_cik, verified, market) "
                    "VALUES (:f, :n, :t, :c, :v, :m) RETURNING security_id"
                ),
                {"f": rec.share_class_figi, "n": rec.name, "t": _security_type(rec.type), "c": rec.cik,
                 "v": rec.share_class_figi is not None, "m": rec.market},
            ).scalar_one()
        conn.execute(text("DELETE FROM ticker_periods WHERE security_id = :s"), {"s": security_id})
        for t, start, end in _periods(rec, source.ticker_events(rec.ticker)):
            conn.execute(
                text("INSERT INTO ticker_periods (ticker, security_id, valid_from, valid_to) VALUES (:t, :s, :f, :e)"),
                {"t": t, "s": security_id, "f": start, "e": end},
            )
    return security_id
