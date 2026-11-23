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


def _history_periods(rec: TickerRecord, events: list[TickerEvent]) -> list[tuple[str, date | None, date | None]]:
    """(ticker, valid_from, valid_to) for one Reference Record using Ticker History for its start and earlier Tickers.

    The record's Ticker starts at the latest event naming it (else its list date) and ends on its delisting; each earlier
    Ticker runs until the next event. Events with a blank Ticker are ignored.
    """
    evs = sorted((e for e in events if e.ticker), key=lambda e: e.date)
    own = [i for i, e in enumerate(evs) if e.ticker == rec.ticker]
    if not own:
        return [(rec.ticker, rec.list_date, rec.delisted_date)]
    start = evs[own[-1]].date
    earlier = evs[: own[-1]]
    out = []
    for i, e in enumerate(earlier):
        nxt = earlier[i + 1].date if i + 1 < len(earlier) else start
        if e.ticker != rec.ticker and e.date < nxt:
            out.append((e.ticker, e.date, nxt - timedelta(days=1)))
    out.append((rec.ticker, start, rec.delisted_date))
    return out


def _main_ticker(records: list[TickerRecord]) -> TickerRecord:
    """Of inactive records sharing a delisting date, the one without a `w`, `.Q` or `E`-style suffix, else the first alphabetically."""
    tickers = {r.ticker for r in records}

    def is_variant(t: str) -> bool:
        return "." in t or t[-1:].islower() or (t.endswith("E") and t[:-1] in tickers)

    plain = [r for r in records if not is_variant(r.ticker)]
    return min(plain or records, key=lambda r: r.ticker)


def _one_per_delisting(recs: list[TickerRecord]) -> list[TickerRecord]:
    """A Security's records ordered by delisting date (active last), one per shared delisting date."""
    inactive: dict[date | None, list[TickerRecord]] = {}
    for r in recs:
        if not r.active:
            inactive.setdefault(r.delisted_date, []).append(r)
    kept = [_main_ticker(group) for _, group in sorted(inactive.items(), key=lambda kv: (kv[0] is None, kv[0] or date.min))]
    return kept + [r for r in recs if r.active]


def sync_reference(source: MarketDataSource, engine: Engine) -> None:
    """Create a Security per exchange-listed share-class FIGI and rebuild its Ticker periods from Reference Records.

    An inactive record's period ends on its delisting; the Security's next record starts the day after. The first record
    starts the day after any earlier holder of that Ticker was delisted, else it is open-ended. Over-the-counter Tickers
    and Tickers without a FIGI are outside the Universe and skipped. The whole sync is one transaction.
    """
    by_figi: dict[str, list[TickerRecord]] = {}
    for rec in source.tickers():
        if rec.market != "otc" and rec.share_class_figi:
            by_figi.setdefault(rec.share_class_figi, []).append(rec)
    kept = {figi: _one_per_delisting(recs) for figi, recs in by_figi.items()}
    delistings: dict[str, list[tuple[date, str]]] = {}  # ticker -> (delisted, figi) of every inactive holder
    for figi, recs in kept.items():
        for r in recs:
            if not r.active and r.delisted_date:
                delistings.setdefault(r.ticker, []).append((r.delisted_date, figi))

    with engine.begin() as conn:
        for figi, recs in kept.items():
            head = recs[-1]
            if head.cik:
                conn.execute(
                    text("INSERT INTO issuers (cik, name) VALUES (:c, :n) ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name"),
                    {"c": head.cik, "n": head.name},
                )
            first = recs[0]
            earlier = [d for d, other in delistings.get(first.ticker, []) if other != figi
                       and (first.delisted_date is None or d < first.delisted_date)]
            start = max(earlier) + timedelta(days=1) if earlier else None
            built = []
            for rec in recs:
                built.append((rec.ticker, start, rec.delisted_date))
                start = rec.delisted_date + timedelta(days=1) if rec.delisted_date else None
            _attach_to_provisional(conn, figi, built)
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
            for ticker, p_start, p_end in built:
                conn.execute(
                    text("INSERT INTO ticker_periods (ticker, security_id, valid_from, valid_to) VALUES (:t, :s, :f, :e)"),
                    {"t": ticker, "s": security_id, "f": p_start, "e": p_end},
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
