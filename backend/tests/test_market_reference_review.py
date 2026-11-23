"""Review fixes for the reference sync: lookup activity, hand-added Security periods, one-Bar-per-day winner."""

from datetime import date, datetime, timezone

from sqlalchemy import text

from hundred_acre.market.lake import read_bars
from hundred_acre.market.massive import MassiveSource
from hundred_acre.market.quarantine import quarantine_bars, release_quarantine
from hundred_acre.market.reference import list_ticker_periods, sync_reference
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord
from hundred_acre.market.watchlist import add_to_watchlist

MON = date(2026, 11, 23)


def test_ticker_lookup_without_an_active_flag_reads_activity_from_the_delisting():
    responses = {"/v3/reference/tickers": {"results": [
        {"ticker": "OLD", "market": "stocks", "delisted_utc": "2020-05-01T00:00:00Z"},
        {"ticker": "NEW", "market": "stocks"}]}}
    source = MassiveSource("k", transport=lambda path, params, headers: responses[path])
    old, new = source.lookup_ticker("X")
    assert (old.active, new.active) == (False, True)


def test_a_hand_added_security_starts_the_day_after_the_earlier_holder_of_its_ticker_was_delisted(market_engine, lake):
    source = FakeMarketDataSource()
    source.tickers_ = [
        TickerRecord("OLDCO", "OldCo", "CS", "stocks", "F-OLD", active=False, delisted_date=date(2020, 5, 1)),
        TickerRecord("OLDCO", "NewCo", "CS", "otc", None),
    ]
    sync_reference(source, market_engine)

    sid = add_to_watchlist(source, market_engine, lake, "OLDCO", as_of=MON)

    assert [(f, e) for t, i, f, e in list_ticker_periods(market_engine, [sid])] == [(date(2020, 5, 2), None)]


def test_a_hand_added_security_stores_only_its_own_figis_reference_records(market_engine, lake):
    source = FakeMarketDataSource()
    source.tickers_ = [
        TickerRecord("OLDCO", "OldCo", "CS", "stocks", "F-OLD", active=False, delisted_date=date(2020, 5, 1)),
        TickerRecord("OLDCO", "NewCo", "ADRC", "otc", "F-NEW", active=False, delisted_date=date(2025, 1, 2)),
    ]

    sid = add_to_watchlist(source, market_engine, lake, "OLDCO", as_of=date(2024, 1, 2))

    with market_engine.connect() as conn:
        stored = conn.execute(text("SELECT reference_records FROM securities WHERE security_id = :s"), {"s": sid}).scalar()
    assert stored == [{"ticker": "OLDCO", "active": False, "delisted_utc": "2025-01-02"}]


def test_release_keeps_the_bar_of_the_latest_starting_ticker_whatever_the_order(market_engine, lake):
    day = date(2026, 11, 9)
    with market_engine.begin() as conn:
        sid = conn.execute(text("INSERT INTO securities (verified) VALUES (true) RETURNING security_id")).scalar_one()
        conn.execute(text("INSERT INTO ticker_periods (ticker, security_id, valid_from) VALUES ('AAA', :s, NULL), "
                          "('ZZZ', :s, :f)"), {"s": sid, "f": date(2026, 1, 1)})
    quarantine_bars(market_engine, [(BarData(t, c, c, c, c, 1.0, c, 1), day) for t, c in (("AAA", 1.0), ("ZZZ", 2.0))],
                    source="massive", fetched_at=datetime.now(timezone.utc))

    release_quarantine(market_engine, lake)

    assert read_bars(lake, [sid], day, day, columns=["ticker"])["ticker"].to_list() == ["ZZZ"]
