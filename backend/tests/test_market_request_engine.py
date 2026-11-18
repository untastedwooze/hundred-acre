import gzip
import json
import threading
import time
from datetime import date

import pytest
from sqlalchemy import text

from hundred_acre.market.engine_source import EngineSource
from hundred_acre.market.massive import MassiveError, MassiveSource
from hundred_acre.market.request_engine import RequestEngine
from hundred_acre.market.source import BarData, MarketDataUnavailable

KEY = "sekrit-key-123"
GROUPED = "/v2/aggs/grouped/locale/us/market/stocks/2026-11-13"
BAR = {"T": "AAPL", "o": 100, "h": 110, "l": 99, "c": 105, "v": 1000, "vw": 104.5, "n": 50}


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0)


class FakeTransport:
    """path -> json | list of pages (by call order) | Exception. Records (clock time, path, params)."""

    def __init__(self, responses, clock=None, duration=0.0):
        self.responses, self.clock, self.duration = responses, clock, duration
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, path, params, headers):
        with self._lock:
            self.calls.append((self.clock() if self.clock else None, path, dict(params)))
            n = len([c for c in self.calls if c[1] == path])
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.duration)
            r = self.responses[path]
            if isinstance(r, Exception):
                raise r
            return r[n - 1] if isinstance(r, list) else r
        finally:
            with self._lock:
                self.in_flight -= 1


class World:
    def __init__(self, market_engine, tmp_path, responses, *, per_minute=5, max_concurrent=1, duration=0.0):
        self.db, self.dir, self.per_minute, self.max_concurrent = market_engine, tmp_path / "responses", per_minute, max_concurrent
        self.clock = FakeClock()
        self.transport = FakeTransport(responses, self.clock, duration)
        self.engine = self.new_engine()
        self.source = EngineSource(
            market_engine, self.dir, clock=self.clock,
            sleep=lambda s: self.engine.serve(until=self.clock() + s),
            poll_interval=1.0,
        )

    def new_engine(self):
        return RequestEngine(
            self.db, MassiveSource(KEY, transport=self.transport), self.dir, clock=self.clock, sleep=self.clock.sleep,
            calls_per_minute=self.per_minute, max_concurrent_calls=self.max_concurrent,
        )


@pytest.fixture
def world(market_engine, tmp_path):
    def make(responses, **kw):
        return World(market_engine, tmp_path, responses, **kw)
    return make


def test_grouped_daily_answered_through_the_engine(world):
    w = world({GROUPED: {"results": [BAR]}})
    bars = w.source.grouped_daily(date(2026, 11, 13), adjusted=False, include_otc=False)
    assert bars == [BarData("AAPL", 100, 110, 99, 105, 1000, 104.5, 50)]
    assert w.transport.calls[0][2] == {"adjusted": "false", "include_otc": "false"}


def _direct(responses):
    t = FakeTransport(responses)
    return MassiveSource(KEY, transport=t)


def test_every_source_method_returns_what_the_massive_client_returns(world):
    bar = {"o": 1, "h": 2, "l": 1, "c": 2, "v": 3, "t": 1700000000000}
    responses = {
        GROUPED: {"results": [BAR]},
        "/v2/aggs/ticker/AAPL/range/1/day/2026-01-01/2026-01-31": {"results": [bar]},
        "/v2/aggs/ticker/AAPL/range/5/minute/2026-01-01/2026-04-30": {"results": [bar]},
        "/v2/aggs/ticker/AAPL/range/5/minute/2026-05-01/2026-05-10": {"results": [{**bar, "t": 1700000300000}]},
        "/v3/reference/tickers": [
            {"results": [{"ticker": "A", "active": True}]}, {"results": [{"ticker": "B", "active": False}]}],
        "/vX/reference/tickers/AAPL/events": {"results": {"events": [
            {"date": "2020-01-02", "type": "ticker_change", "ticker_change": {"ticker": "B"}}]}},
        "/v3/reference/splits": {"results": [
            {"ticker": "A", "execution_date": "2020-01-02", "split_from": 1, "split_to": 2, "id": "s"}]},
        "/v3/reference/dividends": {"results": [
            {"ticker": "A", "ex_dividend_date": "2020-01-02", "cash_amount": 0.5}]},
        "/v1/marketstatus/upcoming": [{"date": "2026-12-25", "exchange": "NYSE", "name": "X", "status": "closed"}],
    }
    w, d = world(responses, per_minute=100), _direct(responses)
    day, since = date(2026, 11, 13), date(2020, 1, 1)
    start, end = date(2026, 1, 1), date(2026, 1, 31)
    assert w.source.grouped_daily(day, adjusted=False, include_otc=True) == d.grouped_daily(day, adjusted=False, include_otc=True)
    assert w.source.aggregates("AAPL", start, end, adjusted=False) == d.aggregates("AAPL", start, end, adjusted=False)
    i_end = date(2026, 5, 10)
    got = w.source.intraday_aggregates("AAPL", start, i_end, adjusted=False)
    assert len(got) == 2 and got == d.intraday_aggregates("AAPL", start, i_end, adjusted=False)
    assert w.source.tickers() == d.tickers()
    assert w.source.ticker_events("AAPL") == d.ticker_events("AAPL")
    assert w.source.splits(since) == d.splits(since)
    assert w.source.dividends(since) == d.dividends(since)
    assert w.source.upcoming_market_holidays() == d.upcoming_market_holidays()


def test_a_paged_query_is_one_request_answered_by_several_calls(world, market_engine):
    def split(t):
        return {"ticker": t, "execution_date": "2020-01-02", "split_from": 1, "split_to": 2}

    pages = [{"results": [split("A")], "next_cursor": "c2"},
             {"results": [split("B")], "next_cursor": "c3"},
             {"results": [split("C")]}]
    w = world({"/v3/reference/splits": pages}, per_minute=100)
    assert [s.ticker for s in w.source.splits(date(2020, 1, 1))] == ["A", "B", "C"]
    assert [c[2].get("cursor") for c in w.transport.calls] == [None, "c2", "c3"]
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM requests")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM calls")).scalar() == 3


def test_no_more_than_the_configured_calls_in_any_60_seconds_even_across_a_restart(world):
    w = world({"/v3/reference/splits": [
        {"results": [], "next_cursor": f"c{i}"} for i in range(1, 7)] + [{"results": []}]}, per_minute=3)
    from hundred_acre.market.request_engine import enqueue
    enqueue(w.db, "/v3/reference/splits", {}, requester="t", now=w.clock())
    w.engine.serve(until=w.clock() + 1)
    assert len(w.transport.calls) == 3
    w.engine = w.new_engine()  # a restart: same database, new instance
    w.engine.serve()
    times = sorted(c[0] for c in w.transport.calls)
    assert len(times) == 7
    for t in times:
        assert len([x for x in times if t - 60 < x <= t]) <= 3, times


def test_max_concurrent_calls_is_honoured(world):
    from hundred_acre.market.request_engine import enqueue

    paths = ["/v3/reference/splits", "/v3/reference/dividends", "/v3/reference/tickers", "/v1/marketstatus/upcoming"]
    for limit in (1, 2):
        w = world({p: {"results": []} for p in paths}, per_minute=100, max_concurrent=limit, duration=0.05)
        for path in paths:
            enqueue(w.db, path, {}, requester="t", now=w.clock())
        w.engine.serve()
        assert w.transport.max_in_flight == limit


def test_response_bodies_are_gzip_files_and_the_call_row_records_status_duration_size(world, market_engine):
    w = world({GROUPED: {"results": [BAR]}})
    w.source.grouped_daily(date(2026, 11, 13), adjusted=False, include_otc=False)
    with market_engine.connect() as conn:
        call = conn.execute(text("SELECT * FROM calls")).one()
    assert call.http_status == 200 and call.duration is not None and call.response_bytes > 0
    body = json.loads(gzip.decompress((w.dir / call.body_path).read_bytes()))
    assert body == {"results": [BAR]}
    assert call.response_bytes == len(json.dumps(body).encode())


def test_failed_request_reaches_the_caller_as_market_data_unavailable_without_the_key(world, market_engine, caplog):
    caplog.set_level("DEBUG")
    w = world({"/v3/reference/splits": MassiveError(f"HTTP 404 from Massive {KEY}", 404)})
    with pytest.raises(MarketDataUnavailable) as e:
        w.source.splits(date(2020, 1, 1))
    assert KEY not in str(e.value) and KEY not in repr(e.value) and KEY not in caplog.text
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT http_status FROM calls")).scalar() == 404
        assert tuple(conn.execute(text("SELECT state, last_status FROM requests")).one()) == ("failed", 404)
        dump = str(conn.execute(text("SELECT * FROM requests")).all() + conn.execute(text("SELECT * FROM calls")).all())
    assert KEY not in dump and KEY not in repr(w.engine) and KEY not in repr(w.source)
    assert not any(KEY in p.read_text(errors="ignore") for p in w.dir.rglob("*") if p.is_file())


def test_cli_request_engine_run_uses_environment_configuration_only(market_engine, tmp_path, monkeypatch, capsys):
    from hundred_acre.market.cli import main
    from hundred_acre.market.request_engine import enqueue

    monkeypatch.delenv("MARKET_RESPONSES_DIR", raising=False)
    assert main(["request-engine", "run", "--once"], engine=market_engine) == 1
    assert "MARKET_RESPONSES_DIR" in capsys.readouterr().err

    transport = FakeTransport({GROUPED: {"results": [BAR]}})
    monkeypatch.setenv("MARKET_RESPONSES_DIR", str(tmp_path / "r"))
    monkeypatch.setenv("MASSIVE_API_KEY", KEY)
    monkeypatch.setenv("MASSIVE_CALLS_PER_MINUTE", "2")
    monkeypatch.setattr(MassiveSource, "from_env", classmethod(lambda cls: cls(KEY, transport=transport)))
    enqueue(market_engine, GROUPED, {}, requester="t", now=time.time())
    assert main(["request-engine", "run", "--once"], engine=market_engine) == 0
    assert len(transport.calls) == 1
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM requests")).scalar() == "complete"
