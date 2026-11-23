from datetime import date
from pathlib import Path

from sqlalchemy import text

from hundred_acre.market.cli import main
from hundred_acre.market.reference import list_ticker_periods, sync_reference
from hundred_acre.market.source import FakeMarketDataSource, TickerRecord

CRONTAB = Path(__file__).resolve().parents[2] / "ops" / "scheduler" / "crontab"


def periods(engine):
    return sorted(((t, f, e) for t, _, f, e in list_ticker_periods(engine)), key=lambda p: (p[0], p[1] or date.min))


def test_rename_ends_old_ticker_on_delisting_and_starts_new_the_next_day(market_engine):
    s = FakeMarketDataSource()
    s.tickers_ = [
        TickerRecord("PIRS", "Pieris", "CS", "stocks", "F1", active=False, delisted_date=date(2024, 12, 16)),
        TickerRecord("PVLA", "Palvella", "CS", "stocks", "F1"),
    ]
    sync_reference(s, market_engine)
    assert periods(market_engine) == [
        ("PIRS", None, date(2024, 12, 16)), ("PVLA", date(2024, 12, 17), None),
    ]
    assert s.events_calls == []


def test_reused_ticker_starts_the_day_after_the_earlier_holder_was_delisted(market_engine):
    s = FakeMarketDataSource()
    s.tickers_ = [
        TickerRecord("GM", "Old", "CS", "stocks", "F-OLD", active=False, delisted_date=date(2009, 6, 1)),
        TickerRecord("GM", "New", "CS", "stocks", "F-NEW"),
    ]
    sync_reference(s, market_engine)
    assert periods(market_engine) == [("GM", None, date(2009, 6, 1)), ("GM", date(2009, 6, 2), None)]


def test_shared_delisting_date_gives_one_period_for_the_main_ticker(market_engine):
    d = date(2023, 5, 1)
    s = FakeMarketDataSource()
    s.tickers_ = [
        TickerRecord("RGw", "RG", "CS", "stocks", "F2", active=False, delisted_date=d),
        TickerRecord("RG", "RG", "CS", "stocks", "F2", active=False, delisted_date=d),
        TickerRecord("UHAL.Q", "U", "CS", "stocks", "F3", active=False, delisted_date=d),
        TickerRecord("UHAE.Q", "U", "CS", "stocks", "F3", active=False, delisted_date=d),
    ]
    sync_reference(s, market_engine)
    assert periods(market_engine) == [("RG", None, d), ("UHAE.Q", None, d)]


def test_issuer_comes_from_cik(market_engine):
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("A", "Agilent", "CS", "stocks", "F4", cik="0001090872")]
    sync_reference(s, market_engine)
    with market_engine.connect() as c:
        assert c.execute(text("SELECT cik FROM issuers")).scalars().all() == ["0001090872"]


def test_nightly_syncs_once_and_makes_no_events_calls(market_engine, lake):
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "F5")]
    assert main(["nightly", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake) == 0
    assert s.tickers_calls == 1
    assert s.events_calls == []


def test_crontab_has_no_reference_sync_and_nightly_and_weekly_are_active():
    lines = [l for l in CRONTAB.read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert not any("reference-sync" in l for l in lines)
    assert any("market nightly" in l for l in lines)
    assert any("market weekly" in l for l in lines)
