from datetime import date

import pytest
from sqlalchemy import text

from hundred_acre.market.cli import main
from hundred_acre.market.reference import list_ticker_periods, sync_reference
from hundred_acre.market.source import FakeMarketDataSource, MarketDataUnavailable, TickerEvent, TickerRecord
from hundred_acre.market.ticker_history import fetch_ticker_history


def ev(ticker, d):
    return TickerEvent(ticker, d, "ticker_change")


def periods(engine):
    return sorted(((t, f, e) for t, _, f, e in list_ticker_periods(engine)), key=lambda p: (p[0], p[1] or date.min))


def rename_source():
    s = FakeMarketDataSource()
    s.tickers_ = [
        TickerRecord("PIRS", "Pieris", "CS", "stocks", "F1", active=False, delisted_date=date(2024, 12, 16)),
        TickerRecord("PVLA", "Palvella", "CS", "stocks", "F1"),
    ]
    return s


def synced(source, engine):
    sync_reference(source, engine)
    fetch_ticker_history(source, engine)


def test_history_supplies_first_start_and_earlier_tickers(market_engine):
    s = rename_source()
    s.events["PVLA"] = [ev("OLD", date(2015, 1, 5)), ev("PIRS", date(2016, 3, 1)), ev("PVLA", date(2024, 12, 17))]
    synced(s, market_engine)
    assert periods(market_engine) == [
        ("OLD", date(2015, 1, 5), date(2016, 2, 29)),
        ("PIRS", date(2016, 3, 1), date(2024, 12, 16)),
        ("PVLA", date(2024, 12, 17), None),
    ]


def test_later_sync_keeps_history_periods(market_engine):
    s = rename_source()
    s.events["PVLA"] = [ev("PIRS", date(2016, 3, 1)), ev("PVLA", date(2024, 12, 17))]
    synced(s, market_engine)
    sync_reference(s, market_engine)
    assert periods(market_engine)[0] == ("PIRS", date(2016, 3, 1), date(2024, 12, 16))


def test_events_contradicting_reference_records_are_stored_but_not_used(market_engine):
    s = rename_source()
    s.events["PVLA"] = [ev("PIRS", date(2016, 3, 1)), ev("PVLA", date(2024, 12, 17)), ev("ZZZ", date(2025, 6, 1))]
    synced(s, market_engine)
    assert [p[0] for p in periods(market_engine)] == ["PIRS", "PVLA"]
    with market_engine.connect() as c:
        stored = c.execute(text("SELECT events FROM ticker_history")).scalar_one()
    assert [e["ticker"] for e in stored] == ["PIRS", "PVLA", "ZZZ"]


def test_404_is_stored_as_no_history_and_not_refetched(market_engine):
    s = rename_source()
    synced(s, market_engine)
    assert s.events_calls == ["PVLA"]
    fetch_ticker_history(s, market_engine)
    assert s.events_calls == ["PVLA"]
    assert periods(market_engine) == [("PIRS", None, date(2024, 12, 16)), ("PVLA", date(2024, 12, 17), None)]


def test_refetched_only_when_reference_records_change_or_forced(market_engine):
    s = rename_source()
    synced(s, market_engine)
    sync_reference(s, market_engine)
    fetch_ticker_history(s, market_engine)
    assert s.events_calls == ["PVLA"]

    s.tickers_[1] = TickerRecord("PVL2", "Palvella", "CS", "stocks", "F1")
    sync_reference(s, market_engine)
    fetch_ticker_history(s, market_engine)
    assert s.events_calls == ["PVLA", "PVL2"]

    fetch_ticker_history(s, market_engine, force=True)
    assert s.events_calls == ["PVLA", "PVL2", "PVL2"]


def test_only_missing_skips_securities_that_have_history(market_engine):
    s = rename_source()
    synced(s, market_engine)
    s.tickers_[1] = TickerRecord("PVL2", "Palvella", "CS", "stocks", "F1")
    sync_reference(s, market_engine)
    fetch_ticker_history(s, market_engine, only_missing=True)
    assert s.events_calls == ["PVLA"]


class DiesOn(FakeMarketDataSource):
    def ticker_events(self, ticker):
        if ticker == "BBB":
            raise MarketDataUnavailable("down")
        return super().ticker_events(ticker)


def test_interrupted_fetch_resumes_without_refetching_finished_securities(market_engine):
    recs = [TickerRecord(t, t, "CS", "stocks", f"F-{t}") for t in ("AAA", "BBB", "CCC")]
    down = DiesOn(tickers_=recs)
    sync_reference(down, market_engine)
    with pytest.raises(MarketDataUnavailable):
        fetch_ticker_history(down, market_engine)
    assert down.events_calls == ["AAA"]

    up = FakeMarketDataSource(tickers_=recs)
    fetch_ticker_history(up, market_engine)
    assert up.events_calls == ["BBB", "CCC"]


def test_history_start_after_delisting_is_ignored(market_engine):
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("DEAD", "Dead", "CS", "stocks", "F9", active=False, delisted_date=date(2020, 1, 3))]
    s.events["DEAD"] = [ev("DEAD", date(2021, 1, 1))]
    synced(s, market_engine)
    assert periods(market_engine) == [("DEAD", None, date(2020, 1, 3))]


def test_cli_ticker_history_fetches_and_backfill_fetches_first(market_engine, lake):
    s = rename_source()
    s.events["PVLA"] = [ev("PIRS", date(2016, 3, 1)), ev("PVLA", date(2024, 12, 17))]
    sync_reference(s, market_engine)
    assert main(["ticker-history"], source=s, engine=market_engine, lake=lake) == 0
    assert s.events_calls == ["PVLA"]
    assert main(["ticker-history", "--force"], source=s, engine=market_engine, lake=lake) == 0
    assert s.events_calls == ["PVLA", "PVLA"]


def test_backfill_fetches_missing_history_before_bars(market_engine, lake):
    s = rename_source()
    s.events["PVLA"] = [ev("PIRS", date(2016, 3, 1)), ev("PVLA", date(2024, 12, 17))]
    assert main(["backfill", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake) == 0
    assert s.events_calls == ["PVLA"]
    assert periods(market_engine)[0] == ("PIRS", date(2016, 3, 1), date(2024, 12, 16))


def test_reused_ticker_keeps_the_earlier_holders_end_when_history_arrives(market_engine):
    s = FakeMarketDataSource()
    s.tickers_ = [
        TickerRecord("GM", "Old", "CS", "stocks", "F-OLD", active=False, delisted_date=date(2009, 6, 1)),
        TickerRecord("GM", "New", "CS", "stocks", "F-NEW"),
    ]
    s.events["GM"] = [ev("GM", date(2010, 11, 18))]
    synced(s, market_engine)
    assert periods(market_engine) == [("GM", None, date(2009, 6, 1)), ("GM", date(2009, 6, 2), None)]
