import json
from datetime import date
from pathlib import Path

from hundred_acre.market import massive as m

FIX = Path(__file__).parent / "fixtures" / "massive"


def load(name):
    return json.loads((FIX / name).read_text())


def test_inactive_ticker_keeps_cik_and_delisted_date():
    rec = m.parse_tickers([load("tickers_pirs_inactive.json")], active=False)[0]
    assert (rec.ticker, rec.active, rec.cik) == ("PIRS", False, "0001583648")
    assert rec.delisted_date == date(2024, 12, 16)


def test_active_ticker_has_cik_and_no_delisted_date():
    rec = m.parse_tickers([load("tickers_pvla_active.json")], active=True)[0]
    assert (rec.ticker, rec.cik, rec.delisted_date) == ("PVLA", "0001583648", None)


def test_rename_event_carries_the_ticker_in_use_from_its_date():
    ev = m.parse_ticker_events([load("events_rename_ibim.json")], "queried")
    assert [(e.ticker, e.date, e.type) for e in ev] == [("IBIM", date(2026, 3, 26), "ticker_change")]


def test_multi_event_response_is_recorded_as_massive_says():
    ev = m.parse_ticker_events([load("events_multi_agilent.json")], "A")
    assert [(e.ticker, e.date) for e in ev] == [("AWD", date(2005, 11, 23)), ("A", date(2003, 9, 10))]


def test_blank_ticker_is_kept_as_is():
    ev = m.parse_ticker_events([load("events_blank_gbtc.json")], "GBTC")
    assert [e.ticker for e in ev] == ["", "GBTC"]
