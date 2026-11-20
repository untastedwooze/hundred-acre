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
            if isinstance(r, list):
                r = r[n - 1]
                if isinstance(r, Exception):
                    raise r
            return r
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
        previous = getattr(self, "engine", None)
        if previous is not None:
            previous.release_lock()
        engine = RequestEngine(
            self.db, MassiveSource(KEY, transport=self.transport), self.dir, clock=self.clock, sleep=self.clock.sleep,
            calls_per_minute=self.per_minute, max_concurrent_calls=self.max_concurrent,
        )
        engine.acquire_lock()  # as the CLI does: the lock first, then the engine may write
        return engine


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
        w.engine.release_lock()


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


NOT_RUNNING = "market data unavailable: request engine is not running"


def _source(market_engine, tmp_path, clock, sleep=None):
    return EngineSource(market_engine, tmp_path / "r", clock=clock, sleep=sleep or clock.sleep, poll_interval=1.0)


def test_source_fails_immediately_with_no_heartbeat_or_a_stale_one(market_engine, tmp_path):
    clock = FakeClock()
    src = _source(market_engine, tmp_path, clock)
    with pytest.raises(MarketDataUnavailable, match=NOT_RUNNING):
        src.splits(date(2020, 1, 1))
    with market_engine.begin() as conn:
        conn.execute(text("UPDATE request_engine_state SET heartbeat_at = :t"), {"t": clock() - 61})
    with pytest.raises(MarketDataUnavailable, match=NOT_RUNNING):
        src.splits(date(2020, 1, 1))
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM requests")).scalar() == 0


def test_source_fails_immediately_when_the_engine_is_halted(market_engine, tmp_path):
    clock = FakeClock()
    with market_engine.begin() as conn:
        conn.execute(text(
            "UPDATE request_engine_state SET state = 'halted', halt_reason = 'HTTP 401', heartbeat_at = :t"), {"t": clock()})
    with pytest.raises(MarketDataUnavailable, match="market data unavailable: request engine halted: HTTP 401"):
        _source(market_engine, tmp_path, clock).splits(date(2020, 1, 1))


def test_a_waiting_caller_fails_when_the_heartbeat_goes_stale_mid_wait(world):
    w = world({"/v3/reference/splits": {"results": []}})
    start = w.clock()
    w.source = _source(w.db, w.dir.parent, w.clock)  # nobody serves while it waits: the engine is dead
    with pytest.raises(MarketDataUnavailable, match=NOT_RUNNING):
        w.source.splits(date(2020, 1, 1))
    assert w.clock() - start >= 60


def test_heartbeat_stays_fresh_during_a_long_rate_limit_wait(world, market_engine):
    from hundred_acre.market.request_engine import enqueue

    w = world({"/v3/reference/splits": {"results": []}}, per_minute=1)
    for i in range(2):  # two distinct Requests (identical ones would coalesce)
        enqueue(w.db, "/v3/reference/splits", {"i": i}, requester="t", now=w.clock())
    ages = []

    def spy(seconds):
        w.clock.sleep(seconds)
        with market_engine.connect() as conn:
            ages.append(w.clock() - conn.execute(text("SELECT heartbeat_at FROM request_engine_state")).scalar())

    w.engine._sleep = spy
    w.engine.serve()
    assert len(w.transport.calls) == 2 and max(ages) <= 15


def test_a_second_engine_exits_immediately_and_makes_no_calls(market_engine, tmp_path, capsys):
    from hundred_acre.market.cli import main
    from hundred_acre.market.request_engine import enqueue

    clock = FakeClock()
    transport = FakeTransport({GROUPED: {"results": [BAR]}})

    def make():
        return RequestEngine(market_engine, MassiveSource(KEY, transport=transport), tmp_path / "r", clock=clock)

    first = make()
    first.acquire_lock()
    enqueue(market_engine, GROUPED, {}, requester="t", now=clock())
    assert main(["request-engine", "run", "--once"], engine=market_engine, request_engine=make()) == 1
    assert "already running" in capsys.readouterr().err
    assert transport.calls == []
    first.release_lock()
    assert main(["request-engine", "run", "--once"], engine=market_engine, request_engine=make()) == 0
    assert len(transport.calls) == 1


def test_market_cli_job_with_the_engine_down_exits_1_with_the_not_running_message(market_engine, tmp_path, lake, capsys):
    from hundred_acre.market.cli import main

    src = _source(market_engine, tmp_path, FakeClock())
    assert main(["reference-sync", "--as-of", "2026-11-13"], source=src, engine=market_engine, lake=lake) == 1
    assert NOT_RUNNING in capsys.readouterr().err


def test_requests_are_served_in_priority_class_order_and_fifo_within_a_class(world):
    from hundred_acre.market.priority import PriorityClass as P
    from hundred_acre.market.request_engine import enqueue
    w = world({f"/p/{i}": {"results": []} for i in range(6)}, per_minute=100)
    queued = [(0, P.BACKFILL), (1, P.NIGHTLY_DAILY), (2, P.INTERACTIVE), (3, P.NIGHTLY_WATCHLIST),
              (4, P.WEEKLY_UNIVERSE), (5, P.INTERACTIVE)]
    for i, p in queued:
        enqueue(w.db, f"/p/{i}", {}, requester="t", now=w.clock(), priority=p)
    w.engine.serve()
    assert [c[1] for c in w.transport.calls] == ["/p/2", "/p/5", "/p/3", "/p/1", "/p/4", "/p/0"]


def test_a_higher_class_request_gets_in_between_the_pages_of_a_running_lower_one(world):
    from hundred_acre.market.priority import PriorityClass as P
    from hundred_acre.market.request_engine import enqueue
    pages = [{"results": [], "next_cursor": "c2"}, {"results": [], "next_cursor": "c3"}, {"results": []}]
    w = world({"/v3/reference/splits": pages, "/watch": {"results": []}}, per_minute=1)
    enqueue(w.db, "/v3/reference/splits", {}, requester="bf", now=w.clock(), priority=P.BACKFILL)
    w.engine.serve(until=w.clock() + 1)  # the backfill's first page only
    assert [c[1] for c in w.transport.calls] == ["/v3/reference/splits"]
    enqueue(w.db, "/watch", {}, requester="nw", now=w.clock(), priority=P.NIGHTLY_WATCHLIST)
    w.engine.serve()
    assert [c[1] for c in w.transport.calls] == [
        "/v3/reference/splits", "/watch", "/v3/reference/splits", "/v3/reference/splits"]


def test_priority_class_is_stored_by_name(world, market_engine):
    from hundred_acre.market.priority import PriorityClass as P
    from hundred_acre.market.request_engine import enqueue
    w = world({"/x": {"results": []}})
    enqueue(w.db, "/x", {}, requester="t", now=w.clock(), priority=P.WEEKLY_UNIVERSE)
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT priority FROM requests")).scalar() == "weekly_universe"


def test_engine_source_derives_a_copy_at_another_class_and_its_requests_carry_it(world, market_engine):
    from hundred_acre.market.priority import PriorityClass as P
    w = world({GROUPED: {"results": [BAR]}})
    derived = w.source.with_priority(P.NIGHTLY_WATCHLIST)
    derived.grouped_daily(date(2026, 11, 13), adjusted=False, include_otc=False)
    assert derived is not w.source
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT priority FROM requests")).scalar() == "nightly_watchlist"

SPLITS = "/v3/reference/splits"
DIVS = "/v3/reference/dividends"


def _split(t):
    return {"ticker": t, "execution_date": "2020-01-02", "split_from": 1, "split_to": 2}


def _enqueue(w, path):
    from hundred_acre.market.request_engine import enqueue
    return enqueue(w.db, path, {}, requester="t", now=w.clock())


def _state(db, request_id):
    with db.connect() as conn:
        return tuple(conn.execute(text("SELECT state, last_status FROM requests WHERE id = :i"), {"i": request_id}).one())


def test_5xx_on_page_two_is_retried_with_backoff_without_refetching_page_one(world):
    w = world({SPLITS: [{"results": [_split("A")], "next_cursor": "c2"}, MassiveError("boom", 503),
                        {"results": [_split("B")]}]}, per_minute=100)
    assert [s.ticker for s in w.source.splits(date(2020, 1, 1))] == ["A", "B"]
    assert [c[2].get("cursor") for c in w.transport.calls] == [None, "c2", "c2"]
    assert w.transport.calls[2][0] - w.transport.calls[1][0] >= 1


def test_network_errors_and_timeouts_are_retried_too(world):
    w = world({SPLITS: [MassiveError("timed out"), MassiveError("reset"), {"results": [_split("A")]}]}, per_minute=100)
    assert [s.ticker for s in w.source.splits(date(2020, 1, 1))] == ["A"]
    assert len(w.transport.calls) == 3


def test_after_five_transient_failures_the_request_fails_as_market_data_unavailable(world):
    w = world({SPLITS: MassiveError("boom", 500)}, per_minute=100)
    with pytest.raises(MarketDataUnavailable):
        w.source.splits(date(2020, 1, 1))
    assert len(w.transport.calls) == 5
    gaps = [b[0] - a[0] for a, b in zip(w.transport.calls, w.transport.calls[1:])]
    assert gaps == sorted(gaps) and gaps[-1] > gaps[0]  # exponential


def test_a_429_stops_all_calls_for_60_seconds_and_is_not_an_attempt(world):
    w = world({SPLITS: [MassiveError("slow down", 429)] + [MassiveError("boom", 500)] * 4 + [{"results": []}],
               DIVS: {"results": []}}, per_minute=100)
    a, b = _enqueue(w, SPLITS), _enqueue(w, DIVS)
    w.engine.serve()
    t429 = w.transport.calls[0][0]
    assert all(c[0] >= t429 + 60 for c in w.transport.calls[1:])
    # 4 transient failures after the 429 still leave a 5th attempt: the 429 was not counted
    assert _state(w.db, a) == ("complete", None) and _state(w.db, b) == ("complete", None)


def test_a_404_fails_that_request_immediately_and_others_continue(world):
    w = world({SPLITS: MassiveError("nope", 404), DIVS: {"results": []}}, per_minute=100)
    a, b = _enqueue(w, SPLITS), _enqueue(w, DIVS)
    w.engine.serve()
    assert _state(w.db, a) == ("failed", 404) and _state(w.db, b) == ("complete", None)
    assert len([c for c in w.transport.calls if c[1] == SPLITS]) == 1


def test_a_401_halts_the_engine_across_restarts_until_resume(world, market_engine):
    from hundred_acre.market.cli import main

    w = world({SPLITS: [MassiveError("bad key", 401)], DIVS: {"results": []}}, per_minute=100)
    a, b = _enqueue(w, SPLITS), _enqueue(w, DIVS)
    w.engine.serve()
    assert _state(w.db, a) == ("failed", 401) and _state(w.db, b) == ("queued", None)
    assert len(w.transport.calls) == 1
    with market_engine.connect() as conn:
        state, reason = conn.execute(text("SELECT state, halt_reason FROM request_engine_state")).one()
    assert state == "halted" and "401" in reason
    w.engine = w.new_engine()
    w.engine.serve()
    assert len(w.transport.calls) == 1
    assert main(["request-engine", "resume"], engine=market_engine) == 0  # database only: no env needed
    w.engine = w.new_engine()
    w.engine.serve()
    assert _state(w.db, b) == ("complete", None)


def test_a_403_halts_too(world, market_engine):
    w = world({SPLITS: MassiveError("plan", 403), DIVS: {"results": []}}, per_minute=100)
    _enqueue(w, SPLITS), _enqueue(w, DIVS)
    w.engine.serve()
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM request_engine_state")).scalar() == "halted"


# --- waiters, coalescing and abandonment ---

def _wait_for(w, path, priority=None, params=None):
    from hundred_acre.market.priority import PriorityClass as P
    from hundred_acre.market.request_engine import enqueue_as_waiter
    return enqueue_as_waiter(w.db, path, params or {}, requester="t", now=w.clock(), priority=priority or P.INTERACTIVE)


def test_identical_simultaneous_requests_cause_one_set_of_calls_and_both_get_the_answer(world):
    from hundred_acre.market.request_engine import read_pages
    w = world({"/q": {"results": [1]}})
    (a, _), (b, _) = _wait_for(w, "/q"), _wait_for(w, "/q")
    assert a == b
    w.engine.serve()
    assert len(w.transport.calls) == 1
    assert read_pages(w.db, w.dir, a) == [{"results": [1]}]


def test_a_backfill_request_joined_by_a_nightly_waiter_is_served_at_nightly_priority(world):
    from hundred_acre.market.priority import PriorityClass as P
    w = world({"/shared": {"results": []}, "/other": {"results": []}}, per_minute=100)
    _wait_for(w, "/shared", P.BACKFILL)
    _wait_for(w, "/other", P.NIGHTLY_DAILY)
    _wait_for(w, "/shared", P.NIGHTLY_WATCHLIST)
    w.engine.serve()
    assert [c[1] for c in w.transport.calls] == ["/shared", "/other"]
    with w.db.connect() as conn:
        assert conn.execute(text("SELECT priority FROM requests WHERE path = '/shared'")).scalar() == "nightly_watchlist"


def test_asking_again_after_completion_makes_new_calls(world):
    w = world({"/q": {"results": []}})
    first, _ = _wait_for(w, "/q")
    w.engine.serve()
    second, _ = _wait_for(w, "/q")
    w.engine.serve()
    assert first != second and len(w.transport.calls) == 2


PAGES3 = [{"results": [], "next_cursor": "c2"}, {"results": [], "next_cursor": "c3"}, {"results": []}]


def test_a_request_whose_waiter_stops_renewing_is_abandoned_before_its_next_call(world):
    w = world({"/p": PAGES3}, per_minute=1)
    rid, _ = _wait_for(w, "/p")
    w.engine.serve(until=w.clock() + 1)  # page one
    w.clock.now += 61  # the lease lapses
    w.engine.serve()
    assert len(w.transport.calls) == 1
    assert _state(w.db, rid)[0] == "abandoned"


def test_an_identical_request_after_abandonment_resumes_from_the_stored_cursor(world):
    w = world({"/p": PAGES3}, per_minute=1)
    rid, _ = _wait_for(w, "/p")
    w.engine.serve(until=w.clock() + 1)
    w.clock.now += 61
    w.engine.serve()
    again, wid = _wait_for(w, "/p")
    assert again == rid
    from hundred_acre.market.request_engine import renew_leases

    def sleep_and_renew(seconds):  # the live caller keeps renewing while the rate limit makes it wait
        w.clock.sleep(seconds)
        renew_leases(w.db, [wid], w.clock())

    w.engine._sleep = sleep_and_renew
    w.engine.serve()
    assert _state(w.db, rid)[0] == "complete"
    assert [c[2].get("cursor") for c in w.transport.calls] == [None, "c2", "c3"]


def test_renewed_leases_keep_a_request_alive(world):
    from hundred_acre.market.request_engine import renew_leases
    w = world({"/q": {"results": []}})
    rid, wid = _wait_for(w, "/q")
    w.clock.now += 50
    renew_leases(w.db, [wid], w.clock())
    w.clock.now += 50
    w.engine.serve()
    assert _state(w.db, rid)[0] == "complete"


def test_a_waiter_is_woken_by_notification_well_before_its_timeout(world, market_engine):
    from hundred_acre.market.request_engine import wait_for_notification
    w = world({"/q": {"results": []}})
    _wait_for(w, "/q")
    timer = threading.Timer(0.5, w.engine.serve)
    timer.start()
    began = time.monotonic()
    wait_for_notification(market_engine, 20)
    timer.join()
    assert time.monotonic() - began < 10


def test_polling_alone_completes_a_wait_when_notifications_are_lost(world):
    w = world({GROUPED: {"results": [BAR]}})  # World's source waits by sleeping and polling: no notification used
    bars = w.source.grouped_daily(date(2026, 11, 13), adjusted=False, include_otc=False)
    assert bars[0].ticker == "AAPL"


def test_response_bodies_older_than_90_days_are_pruned_but_call_rows_stay(world):
    w = world({GROUPED: {"results": [BAR]}})
    w.source.grouped_daily(date(2026, 11, 13), adjusted=False, include_otc=False)
    files = list(w.dir.rglob("*.json.gz"))
    assert len(files) == 1
    w.clock.now += 89 * 86400
    w.engine.prune_responses()
    assert files[0].exists()
    w.clock.now += 2 * 86400
    w.engine.prune_responses()
    assert not files[0].exists()
    with w.db.connect() as conn:
        rows = conn.execute(text("SELECT http_status, body_path FROM calls")).all()
        assert conn.execute(text("SELECT count(*) FROM requests")).scalar_one() == 1
    assert [(r.http_status, r.body_path) for r in rows] == [(200, None)]


# ---- review fixes ----


def _requests(db):
    with db.connect() as conn:
        return conn.execute(text("SELECT id, state, priority, attempts, last_error FROM requests ORDER BY id")).all()


def test_a_request_whose_only_caller_died_by_exception_is_abandoned_not_served(world):
    w = world({SPLITS: {"results": []}}, per_minute=100)

    def boom(_):
        raise RuntimeError("caller died")

    source = EngineSource(w.db, w.dir, clock=w.clock, sleep=boom, poll_interval=1.0)
    with pytest.raises(RuntimeError):
        source.splits(date(2020, 1, 1))
    w.engine.serve()
    assert w.transport.calls == []
    assert [r.state for r in _requests(w.db)] == ["abandoned"]


def test_a_second_engine_instance_does_not_touch_a_live_engines_state(world, market_engine):
    from hundred_acre.market.request_engine import EngineAlreadyRunning

    w = world({SPLITS: {"results": []}})
    with market_engine.begin() as conn:
        conn.execute(text("INSERT INTO requests (id, path, params, identity_key, requester, created_at) "
                          "VALUES (1, '/x', '{}', 'k', 't', 0)"))
        conn.execute(text("INSERT INTO calls (request_id, page, started_at) VALUES (1, 1, 5)"))
        beat = conn.execute(text("SELECT heartbeat_at FROM request_engine_state")).scalar()
    w.clock.now += 1000
    other = RequestEngine(w.db, MassiveSource(KEY, transport=w.transport), w.dir, clock=w.clock)
    with pytest.raises(EngineAlreadyRunning):
        other.acquire_lock()
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT duration FROM calls")).scalar() is None
        assert conn.execute(text("SELECT heartbeat_at FROM request_engine_state")).scalar() == beat


def test_a_completion_racing_a_join_is_not_resurrected(world, market_engine):
    from hundred_acre.market.request_engine import enqueue_as_waiter

    w = world({SPLITS: {"results": []}})
    first, _ = enqueue_as_waiter(w.db, SPLITS, {}, requester="t", now=w.clock())
    result = {}
    holder = market_engine.connect()
    tx = holder.begin()
    holder.execute(text("UPDATE requests SET state = 'complete' WHERE id = :i"), {"i": first})

    t = threading.Thread(target=lambda: result.update(
        got=enqueue_as_waiter(w.db, SPLITS, {}, requester="t", now=w.clock())))
    t.start()
    time.sleep(0.5)  # the join has read 'queued' and is blocked on the row
    tx.commit()
    holder.close()
    t.join(10)
    assert result["got"][0] != first
    assert dict((r.id, r.state) for r in _requests(w.db))[first] == "complete"


def test_an_unexpected_exception_in_a_call_is_a_failed_attempt_not_an_engine_crash(world):
    w = world({SPLITS: ValueError("not json")}, per_minute=100)
    rid = _enqueue(w, SPLITS)
    w.engine.serve(until=w.clock() + 1)
    (row,) = _requests(w.db)
    assert row.attempts >= 1 and "not json" in row.last_error and row.state != "complete"


def test_wait_for_notification_returns_the_connection_to_the_pool_clean(market_engine):
    from hundred_acre.market.request_engine import wait_for_notification

    wait_for_notification(market_engine, 0.05)
    raw = market_engine.raw_connection()
    try:
        assert raw.driver_connection.autocommit is False
    finally:
        raw.close()


def test_only_one_non_terminal_request_per_identity_even_for_plain_enqueues(world, market_engine):
    from sqlalchemy.exc import IntegrityError

    w = world({SPLITS: {"results": []}})
    a, b = _enqueue(w, SPLITS), _enqueue(w, SPLITS)
    assert a == b
    with pytest.raises(IntegrityError), market_engine.begin() as conn:
        conn.execute(text("INSERT INTO requests (path, params, identity_key, requester, created_at) "
                          "SELECT path, params, identity_key, requester, created_at FROM requests"))


def test_a_disk_error_recording_a_response_fails_the_attempt_and_closes_the_call(world, market_engine, tmp_path):
    w = world({SPLITS: {"results": []}}, per_minute=100)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so the responses directory cannot be created under it")
    w.engine._dir = blocker
    _enqueue(w, SPLITS)
    w.engine.serve(until=w.clock() + 1)
    with market_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM calls WHERE duration IS NULL")).scalar() == 0
    (row,) = _requests(w.db)
    assert row.attempts >= 1 and row.state != "complete"


def test_priority_follows_live_waiters_not_the_highest_ever(world):
    from hundred_acre.market.priority import PriorityClass as P
    from hundred_acre.market.request_engine import enqueue, enqueue_as_waiter, release_waiters

    w = world({SPLITS: {"results": []}, DIVS: {"results": []}}, per_minute=100)
    _, urgent = enqueue_as_waiter(w.db, SPLITS, {}, requester="t", now=w.clock(), priority=P.INTERACTIVE)
    enqueue_as_waiter(w.db, SPLITS, {}, requester="t", now=w.clock(), priority=P.BACKFILL)
    release_waiters(w.db, [urgent])
    enqueue(w.db, DIVS, {}, requester="t", now=w.clock(), priority=P.NIGHTLY_DAILY)
    w.engine.serve()
    assert [c[1] for c in w.transport.calls] == [DIVS, SPLITS]
