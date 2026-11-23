"""Reference sync: Securities (one per share-class FIGI), Issuers (by CIK) and dated Ticker periods."""

import json
import logging
from dataclasses import dataclass
from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy import Engine, text

from hundred_acre.market.source import MarketDataSource, TickerEvent, TickerRecord

log = logging.getLogger(__name__)

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


def records_json(recs: list[TickerRecord]) -> list[dict]:
    """The Reference Records `(ticker, active, delisted_utc)` of one Security, in a canonical order, for storage and comparison."""
    items = [{"ticker": r.ticker, "active": r.active, "delisted_utc": r.delisted_date.isoformat() if r.delisted_date else None}
             for r in recs]
    return sorted(items, key=lambda i: (i["ticker"], i["delisted_utc"] or "", i["active"]))


def _records_from_json(figi: str, items: list[dict]) -> list[TickerRecord]:
    return [
        TickerRecord(i["ticker"], None, None, "stocks", figi, active=i["active"],
                     delisted_date=date.fromisoformat(i["delisted_utc"]) if i["delisted_utc"] else None)
        for i in items
    ]


def events_json(events: list[TickerEvent]) -> list[dict]:
    return [{"ticker": e.ticker, "date": e.date.isoformat(), "type": e.type} for e in events]


def _events_from_json(items: list[dict]) -> list[TickerEvent]:
    return [TickerEvent(i["ticker"], date.fromisoformat(i["date"]), i["type"]) for i in items]


def _build_periods(
    recs: list[TickerRecord], earlier_holder_end: date | None, events: list[TickerEvent]
) -> list[tuple[str, date | None, date | None]]:
    """(ticker, valid_from, valid_to) for one Security's kept Reference Records (ADR 0005).

    The Reference Records win: they give every record's Ticker and end. The first record starts the day after any earlier
    holder of its Ticker was delisted, else on the latest event naming it, else it is open-ended. Events before that
    start give earlier Tickers, each running until the next event. Events dated after it are logged and ignored.
    """
    first = recs[0]
    start = earlier_holder_end + timedelta(days=1) if earlier_holder_end else None
    earlier: list[tuple[str, date, date]] = []
    evs = sorted((e for e in events if e.ticker), key=lambda e: e.date)
    own = [i for i, e in enumerate(evs) if e.ticker == first.ticker]
    if own:
        named = evs[own[-1]].date
        if first.delisted_date and named > first.delisted_date:
            log.info("ignoring Ticker History for %s: it starts %s, after the delisting on %s", first.ticker, named, first.delisted_date)
        else:
            if start is None:
                start = named
            ignored = evs[own[-1] + 1:]
            if ignored:
                log.info("ignoring %d Ticker History event(s) for %s dated after %s", len(ignored), first.ticker, named)
            before = evs[: own[-1]]
            for i, e in enumerate(before):
                nxt = before[i + 1].date if i + 1 < len(before) else named
                end = min(nxt - timedelta(days=1), start - timedelta(days=1))
                if e.ticker != first.ticker and e.date <= end:
                    earlier.append((e.ticker, e.date, end))
    out: list[tuple[str, date | None, date | None]] = list(earlier)
    for rec in recs:
        out.append((rec.ticker, start, rec.delisted_date))
        start = rec.delisted_date + timedelta(days=1) if rec.delisted_date else None
    return out


def earlier_holder_end(first: TickerRecord, delistings: Iterable[date]) -> date | None:
    """The latest of `delistings` (of other Securities' holders of `first.ticker`) that precede `first`'s own delisting."""
    return max((d for d in delistings if first.delisted_date is None or d < first.delisted_date), default=None)


def earlier_holder_end_stored(conn, first: TickerRecord, *, excluding: int | None = None) -> date | None:
    """`earlier_holder_end` from the stored Reference Records of every Security but `excluding`."""
    probe = json.dumps([{"ticker": first.ticker, "active": False}])
    ends: list[date] = []
    for other_figi, other in conn.execute(
        text("SELECT share_class_figi, reference_records FROM securities "
             "WHERE security_id IS DISTINCT FROM :s AND reference_records @> CAST(:p AS jsonb)"),
        {"s": excluding, "p": probe},
    ):
        ends += [r.delisted_date for r in _one_per_delisting(_records_from_json(other_figi, other))
                 if r.ticker == first.ticker and not r.active and r.delisted_date]
    return earlier_holder_end(first, ends)


def period_preference(ticker: str, valid_from: date | None) -> tuple[int, str]:
    """Sort key of a Ticker period for choosing the one Daily Bar a Security keeps on a day: the lowest key wins.

    Of the Ticker periods covering the day, the one that started latest wins (an open start counts as oldest); a tie goes
    to the alphabetically first Ticker, so the choice never depends on the order the Bars arrived in.
    """
    return (-(valid_from.toordinal() if valid_from else 0), ticker)


def period_preference_on(engine: Engine, ticker: str, security_id: int, on: date) -> tuple[int, str] | None:
    """`period_preference` of the period of `ticker` on `security_id` covering `on`, or None if there is none."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT valid_from FROM ticker_periods WHERE ticker = :t AND security_id = :s "
                 "AND (valid_from IS NULL OR valid_from <= :d) AND (valid_to IS NULL OR valid_to >= :d)"),
            {"t": ticker, "s": security_id, "d": on},
        ).all()
    return min((period_preference(ticker, r[0]) for r in rows), default=None)


def _write_periods(conn, security_id: int, built: list[tuple[str, date | None, date | None]]) -> None:
    conn.execute(text("DELETE FROM ticker_periods WHERE security_id = :s"), {"s": security_id})
    for ticker, p_start, p_end in built:
        conn.execute(
            text("INSERT INTO ticker_periods (ticker, security_id, valid_from, valid_to) VALUES (:t, :s, :f, :e)"),
            {"t": ticker, "s": security_id, "f": p_start, "e": p_end},
        )


def sync_reference(source: MarketDataSource, engine: Engine) -> None:
    """Create a Security per exchange-listed share-class FIGI and rebuild its Ticker periods from Reference Records.

    An inactive record's period ends on its delisting; the Security's next record starts the day after. The first record
    starts the day after any earlier holder of that Ticker was delisted, else where stored Ticker History says, else it is
    open-ended. Over-the-counter Tickers and Tickers without a FIGI are outside the Universe and skipped. The whole sync
    is one transaction.
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
        history = {
            figi: _events_from_json(events)
            for figi, events in conn.execute(
                text("SELECT s.share_class_figi, h.events FROM ticker_history h JOIN securities s USING (security_id)")
            )
        }
        for figi, recs in kept.items():
            head = recs[-1]
            if head.cik:
                conn.execute(
                    text("INSERT INTO issuers (cik, name) VALUES (:c, :n) ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name"),
                    {"c": head.cik, "n": head.name},
                )
            first = recs[0]
            ends = earlier_holder_end(first, (d for d, other in delistings.get(first.ticker, []) if other != figi))
            built = _build_periods(recs, ends, history.get(figi, []))
            _attach_to_provisional(conn, figi, built)
            security_id = conn.execute(
                text(
                    "INSERT INTO securities (share_class_figi, name, type, issuer_cik, market, reference_records) "
                    "VALUES (:figi, :name, :type, :cik, :market, CAST(:records AS jsonb)) "
                    "ON CONFLICT (share_class_figi) DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type, "
                    "issuer_cik = EXCLUDED.issuer_cik, market = EXCLUDED.market, "
                    "reference_records = EXCLUDED.reference_records RETURNING security_id"
                ),
                {"figi": figi, "name": head.name, "type": _security_type(head.type), "cik": head.cik,
                 "market": head.market, "records": json.dumps(records_json(by_figi[figi]))},
            ).scalar_one()
            _write_periods(conn, security_id, built)


def rebuild_periods(conn, security_id: int) -> None:
    """Rebuild one Security's Ticker periods from its stored Reference Records and Ticker History, without calling the source."""
    figi, stored, events = conn.execute(
        text("SELECT s.share_class_figi, s.reference_records, h.events FROM securities s "
             "LEFT JOIN ticker_history h USING (security_id) WHERE s.security_id = :s"),
        {"s": security_id},
    ).one()
    if not stored:
        return
    recs = _one_per_delisting(_records_from_json(figi, stored))
    _write_periods(conn, security_id, _build_periods(recs, earlier_holder_end_stored(conn, recs[0], excluding=security_id),
                                                     _events_from_json(events or [])))


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
