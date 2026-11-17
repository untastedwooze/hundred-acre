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
    share_class_figi: str
    issuer_cik: str | None


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
            security_id = conn.execute(
                text(
                    "INSERT INTO securities (share_class_figi, name, type, issuer_cik) VALUES (:figi, :name, :type, :cik) "
                    "ON CONFLICT (share_class_figi) DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type, "
                    "issuer_cik = EXCLUDED.issuer_cik RETURNING security_id"
                ),
                {"figi": figi, "name": head.name, "type": _security_type(head.type), "cik": head.cik},
            ).scalar_one()
            conn.execute(text("DELETE FROM ticker_periods WHERE security_id = :s"), {"s": security_id})
            for rec in recs:
                for ticker, start, end in _periods(rec, events[rec.ticker]):
                    conn.execute(
                        text("INSERT INTO ticker_periods (ticker, security_id, valid_from, valid_to) VALUES (:t, :s, :f, :e)"),
                        {"t": ticker, "s": security_id, "f": start, "e": end},
                    )


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
            text("SELECT security_id, type, name, share_class_figi, issuer_cik FROM securities WHERE security_id = :s"),
            {"s": security_id},
        ).one()
    return SecurityInfo(*row)
