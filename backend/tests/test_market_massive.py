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
