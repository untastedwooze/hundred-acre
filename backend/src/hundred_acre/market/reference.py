"""Reference sync: Securities (one per share-class FIGI), Issuers (by CIK) and dated Ticker periods."""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Engine, text

from hundred_acre.market.source import MarketDataSource, TickerEvent, TickerRecord

_TYPES = {
    "CS": "common_stock", "ETF": "etf", "ETN": "etf", "ETV": "etf", "ADRC": "adr", "ADRP": "adr", "ADRR": "adr",
    "PFD": "preferred", "WARRANT": "warrant", "UNIT": "unit", "RIGHT": "right",
}


@dataclass(frozen=True)
class SecurityInfo:
    security_id: int
    type: str | None
    name: str | None
    share_class_figi: str | None
    issuer_cik: str | None
    verified: bool = True


def _security_type(code: str | None) -> str | None:
    return _TYPES.get(code, code.lower() if code else None)


def _periods(rec: TickerRecord, changes: list[TickerEvent]) -> list[tuple[str, date | None, date | None]]:
    """(ticker, valid_from, valid_to) for one reference record, split at its rename events."""
    out, start = [], rec.list_date
    for e in sorted(changes, key=lambda e: e.date):
        out.append((e.ticker, start, e.date - timedelta(days=1)))
        start = e.date
    out.append((rec.ticker, start, rec.delisted_date))
    return out


def sync_reference(source: MarketDataSource, engine: Engine) -> None:
    """Create a Security per exchange-listed share-class FIGI and rebuild its Ticker periods.

    Over-the-counter Tickers and Tickers without a FIGI are outside the Universe and skipped.
    """
    by_figi: dict[str, list[TickerRecord]] = {}
    for rec in source.tickers():
        if rec.market != "otc" and rec.share_class_figi:
            by_figi.setdefault(rec.share_class_figi, []).append(rec)
    events = {rec.ticker: source.ticker_events(rec.ticker) for recs in by_figi.values() for rec in recs}

    with engine.begin() as conn:
        for figi, recs in by_figi.items():
            head = recs[-1]
            if head.cik:
                conn.execute(
                    text("INSERT INTO issuers (cik, name) VALUES (:c, :n) ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name"),
                    {"c": head.cik, "n": head.name},
                )
            _attach_to_provisional(conn, figi, [p for rec in recs for p in _periods(rec, events[rec.ticker])])
            security_id = conn.execute(
                text(
                    "INSERT INTO securities (share_class_figi, name, type, issuer_cik, market) "
                    "VALUES (:figi, :name, :type, :cik, :market) "
                    "ON CONFLICT (share_class_figi) DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type, "
                    "issuer_cik = EXCLUDED.issuer_cik, market = EXCLUDED.market RETURNING security_id"
                ),
                {"figi": figi, "name": head.name, "type": _security_type(head.type), "cik": head.cik,
                 "market": head.market},
            ).scalar_one()
            conn.execute(text("DELETE FROM ticker_periods WHERE security_id = :s"), {"s": security_id})
            for rec in recs:
                for ticker, start, end in _periods(rec, events[rec.ticker]):
                    conn.execute(
                        text("INSERT INTO ticker_periods (ticker, security_id, valid_from, valid_to) VALUES (:t, :s, :f, :e)"),
                        {"t": ticker, "s": security_id, "f": start, "e": end},
                    )


def _attach_to_provisional(conn, figi: str, periods: list[tuple[str, date | None, date | None]]) -> None:
    """Give an unverified Provisional Security this FIGI if it held one of the Tickers in an overlapping date range."""
    if conn.execute(text("SELECT 1 FROM securities WHERE share_class_figi = :f"), {"f": figi}).first():
        return
    for ticker, start, end in periods:
        found = conn.execute(
            text(
                "SELECT s.security_id FROM securities s JOIN ticker_periods p USING (security_id) "
                "WHERE s.share_class_figi IS NULL AND p.ticker = :t "
                "AND (CAST(:e AS date) IS NULL OR p.valid_from IS NULL OR p.valid_from <= :e) "
                "AND (CAST(:s AS date) IS NULL OR p.valid_to IS NULL OR p.valid_to >= :s) ORDER BY s.security_id LIMIT 1"
            ),
            {"t": ticker, "s": start, "e": end},
        ).scalar()
        if found is not None:
            conn.execute(
                text("UPDATE securities SET share_class_figi = :f, verified = true WHERE security_id = :i"),
                {"f": figi, "i": found},
            )
            return


def security_on(engine: Engine, ticker: str, on: date) -> int | None:
    """The Security `ticker` referred to on `on`, or None if no Security held it that day."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT security_id FROM ticker_periods WHERE ticker = :t "
                "AND (valid_from IS NULL OR valid_from <= :d) AND (valid_to IS NULL OR valid_to >= :d) "
                "ORDER BY valid_from DESC NULLS LAST LIMIT 1"
            ),
            {"t": ticker, "d": on},
        ).scalar()


def security_info(engine: Engine, security_id: int) -> SecurityInfo:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT security_id, type, name, share_class_figi, issuer_cik, verified FROM securities WHERE security_id = :s"),
            {"s": security_id},
        ).one()
    return SecurityInfo(*row)


def list_ticker_periods(engine: Engine, security_ids: list[int] | None = None) -> list[tuple[str, int, date | None, date | None]]:
    """(ticker, security_id, valid_from, valid_to) for `security_ids` (default: every Security), by Security then start."""
    query = "SELECT ticker, security_id, valid_from, valid_to FROM ticker_periods "
    if security_ids is None:
        query += "ORDER BY ticker, valid_from"
        params: dict = {}
    else:
        query += "WHERE security_id = ANY(:ids) ORDER BY security_id, valid_from"
        params = {"ids": security_ids}
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(query), params).all()]


def securities_expected_to_trade(engine: Engine, on: date) -> list[int]:
    """Exchange-listed Securities (not over-the-counter) with a Ticker period covering `on`."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT s.security_id FROM securities s WHERE s.market IS DISTINCT FROM 'otc' AND EXISTS ("
                    "SELECT 1 FROM ticker_periods p WHERE p.security_id = s.security_id "
                    "AND (p.valid_from IS NULL OR p.valid_from <= :d) AND (p.valid_to IS NULL OR p.valid_to >= :d)) "
                    "ORDER BY s.security_id"
                ),
                {"d": on},
            ).scalars()
        )
