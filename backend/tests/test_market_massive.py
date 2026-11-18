import logging
from datetime import date

import pytest

from hundred_acre.market.massive import MassiveError, MassiveSource
from hundred_acre.market.source import BarData, MarketDataUnavailable

KEY = "sekrit-key-123"


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses  # path -> json, a list of pages, or an exception
        self.calls = []

    def __call__(self, path, params, headers):
        self.calls.append((path, dict(params), dict(headers)))
        r = self.responses[path]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, list):
            return r[len([c for c in self.calls if c[0] == path]) - 1]
        return r


def test_grouped_daily_requests_unadjusted_and_parses_bars():
    t = FakeTransport({"/v2/aggs/grouped/locale/us/market/stocks/2026-11-13": {"results": [
        {"T": "AAPL", "o": 100, "h": 110, "l": 99, "c": 105, "v": 1000, "vw": 104.5, "n": 50}]}})
    bars = MassiveSource(KEY, transport=t).grouped_daily(date(2026, 11, 13), adjusted=False, include_otc=False)
    assert bars == [BarData("AAPL", 100, 110, 99, 105, 1000, 104.5, 50)]
    assert t.calls[0][1]["adjusted"] == "false"
    assert t.calls[0][1]["include_otc"] == "false"


def test_tickers_follow_pagination():
    t = FakeTransport({"/v3/reference/tickers": [
        {"results": [{"ticker": "A", "name": "A Co", "type": "CS", "market": "stocks",
                      "share_class_figi": "F1", "active": True}], "next_cursor": "c2"},
        {"results": [{"ticker": "B", "market": "stocks", "active": True}]},
        {"results": [{"ticker": "C", "market": "stocks", "active": False}]},
    ]})
    recs = MassiveSource(KEY, transport=t).tickers()
    assert sorted(r.ticker for r in recs) == ["A", "B", "C"]
    assert [r for r in recs if r.ticker == "C"][0].active is False


def test_api_key_never_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    t = FakeTransport({"/v3/reference/splits": MassiveError(f"HTTP 403 for https://x?apiKey={KEY}")})
    source = MassiveSource(KEY, transport=t)
    with pytest.raises(MarketDataUnavailable) as e:
        source.splits(date(2020, 1, 1))
    assert KEY not in str(e.value) and KEY not in repr(e.value)
    assert KEY not in caplog.text
    assert KEY not in repr(source)


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MASSIVE_API_KEY"):
        MassiveSource.from_env()


def test_call_makes_exactly_one_call_and_returns_body_and_next_cursor():
    t = FakeTransport({"/v3/reference/splits": [
        {"results": [{"ticker": "A"}], "next_cursor": "c2"}, {"results": []}]})
    page = MassiveSource(KEY, transport=t).call("/v3/reference/splits", {"limit": 1000})
    assert len(t.calls) == 1
    assert page.body == {"results": [{"ticker": "A"}], "next_cursor": "c2"}
    assert page.next_cursor == "c2"
    last = MassiveSource(KEY, transport=FakeTransport({"/p": {"results": []}})).call("/p")
    assert last.next_cursor is None


def test_call_redacts_api_key_in_errors():
    t = FakeTransport({"/p": MassiveError(f"boom {KEY}")})
    with pytest.raises(MarketDataUnavailable) as e:
        MassiveSource(KEY, transport=t).call("/p")
    assert KEY not in str(e.value)


def test_parsing_works_on_raw_pages_without_http():
    from hundred_acre.market import massive as m

    bar = {"o": 1, "h": 2, "l": 1, "c": 2, "v": 3}
    assert m.parse_bars([{"results": [{"T": "AAPL", **bar}]}]) == [BarData("AAPL", 1, 2, 1, 2, 3, None, None)]
    daily = m.parse_aggregates(
        [{"results": [{"t": 1700000000000, **bar}]}, {"results": [{"t": 1700086400000, **bar}]}], "X")
    assert [d for d, _ in daily] == [date(2023, 11, 14), date(2023, 11, 15)]
    assert daily[0][1].ticker == "X"
    intra = m.parse_intraday_aggregates([{"results": [{"t": 1700000000000, **bar}]}], "X")
    assert intra[0][0].isoformat() == "2023-11-14T22:13:20+00:00"
    recs = m.parse_tickers([{"results": [{"ticker": "A", "active": True}]}], active=True)
    assert recs[0].ticker == "A" and recs[0].active is True
    ev = m.parse_ticker_events([{"results": {"events": [
        {"date": "2020-01-02", "type": "ticker_change", "ticker_change": {"ticker": "B"}}]}}], "A")
    assert ev[0].ticker == "A" and ev[0].date == date(2020, 1, 2)
    sp = m.parse_splits([{"results": [
        {"ticker": "A", "execution_date": "2020-01-02", "split_from": 1, "split_to": 2, "id": "s"}]}])
    assert sp[0].split_to == 2
    dv = m.parse_dividends([{"results": [{"ticker": "A", "ex_dividend_date": "2020-01-02", "cash_amount": 0.5}]}])
    assert dv[0].cash_amount == 0.5
    hol = m.parse_market_holidays([{"results": [
        {"date": "2026-12-25", "exchange": "NYSE", "name": "Xmas", "status": "closed"}]}])
    assert hol[0].name == "Xmas"
    assert m.parse_market_holidays([[{"date": "2026-12-25"}]])[0].date == date(2026, 12, 25)
