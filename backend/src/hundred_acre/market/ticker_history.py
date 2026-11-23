"""Ticker History: each Security's ticker events, fetched once and again only when its Reference Records change (ADR 0005)."""

import json

from sqlalchemy import Engine, text

from hundred_acre.market.reference import events_json, rebuild_periods
from hundred_acre.market.source import MarketDataSource


def fetch_ticker_history(source: MarketDataSource, engine: Engine, *, force: bool = False, only_missing: bool = False) -> int:
    """Fetch Ticker History for the Securities that need it; returns how many were fetched.

    A Security needs it if it has none, or (unless `only_missing`) its Reference Records differ from the ones its History
    was fetched against; `force` refetches every Security. One Security per write, with its Ticker periods rebuilt in the
    same transaction, so an interrupted fetch resumes without refetching finished Securities. A source outage stops the
    fetch. A Security the source has no events for is stored with an empty History.
    """
    if force:
        condition = "TRUE"
    elif only_missing:
        condition = "h.security_id IS NULL"
    else:
        condition = "h.security_id IS NULL OR h.fetched_against IS DISTINCT FROM s.reference_records"
    with engine.connect() as conn:
        todo = [
            tuple(r) for r in conn.execute(text(
                "SELECT s.security_id, p.ticker FROM securities s "
                "LEFT JOIN ticker_history h USING (security_id) "
                "JOIN LATERAL (SELECT ticker FROM ticker_periods WHERE security_id = s.security_id "
                "              ORDER BY valid_to DESC NULLS FIRST, valid_from DESC NULLS LAST LIMIT 1) p ON TRUE "
                f"WHERE s.reference_records IS NOT NULL AND ({condition}) ORDER BY s.security_id"
            ))
        ]
    for security_id, ticker in todo:
        events = source.ticker_events(ticker)
        with engine.begin() as conn:
            fetched_against = conn.execute(
                text("SELECT reference_records FROM securities WHERE security_id = :s"), {"s": security_id}
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO ticker_history (security_id, events, fetched_against) "
                    "VALUES (:s, CAST(:e AS jsonb), CAST(:r AS jsonb)) "
                    "ON CONFLICT (security_id) DO UPDATE SET events = EXCLUDED.events, "
                    "fetched_against = EXCLUDED.fetched_against, fetched_at = now()"
                ),
                {"s": security_id, "e": json.dumps(events_json(events)), "r": json.dumps(fetched_against)},
            )
            rebuild_periods(conn, security_id)
    return len(todo)
