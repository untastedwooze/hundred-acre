from sqlalchemy import text

from hundred_acre.market.cli import main
from hundred_acre.market.priority import PriorityClass
from hundred_acre.market.request_engine import enqueue

NOW = 2_000_000.0


def status(market_engine, capsys, monkeypatch, cpm=None):
    if cpm is not None:
        monkeypatch.setenv("MASSIVE_CALLS_PER_MINUTE", str(cpm))
    assert main(["request-engine", "status"], engine=market_engine, clock=lambda: NOW) == 0
    return capsys.readouterr().out


def set_engine(market_engine, **cols):
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with market_engine.begin() as conn:
        conn.execute(text(f"UPDATE request_engine_state SET {sets} WHERE id = 1"), cols)


def add_call(market_engine, request_id, started_at, status_=200):
    with market_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO calls (request_id, page, started_at, duration, http_status) "
            "VALUES (:r, 1, :t, 0.1, :s)"), {"r": request_id, "t": started_at, "s": status_})


def test_fresh_heartbeat_shows_running_with_age(market_engine, capsys, monkeypatch):
    set_engine(market_engine, heartbeat_at=NOW - 7)
    out = status(market_engine, capsys, monkeypatch)
    assert "engine: running" in out
    assert "heartbeat 7s ago" in out


def test_old_or_missing_heartbeat_shows_stale(market_engine, capsys, monkeypatch):
    set_engine(market_engine, heartbeat_at=NOW - 600)
    assert "engine: stale" in status(market_engine, capsys, monkeypatch)
    set_engine(market_engine, heartbeat_at=None)
    assert "engine: stale" in status(market_engine, capsys, monkeypatch)


def test_halted_shows_reason(market_engine, capsys, monkeypatch):
    set_engine(market_engine, state="halted", halt_reason="massive rejected the API key (401)", heartbeat_at=NOW - 1)
    out = status(market_engine, capsys, monkeypatch)
    assert "engine: halted: massive rejected the API key (401)" in out


def test_queue_depth_and_drain_time_per_priority_class(market_engine, capsys, monkeypatch):
    for i in range(3):
        enqueue(market_engine, "/a", {"i": i}, requester="t", now=NOW, priority=PriorityClass.BACKFILL)
    enqueue(market_engine, "/b", {}, requester="t", now=NOW, priority=PriorityClass.NIGHTLY_DAILY)
    out = status(market_engine, capsys, monkeypatch, cpm=5)
    lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.split() and ln.split()[0] in
             {c.value for c in PriorityClass}}
    assert "3 queued" in lines["backfill"] and "~36s" in lines["backfill"]
    assert "1 queued" in lines["nightly_daily"] and "~12s" in lines["nightly_daily"]
    assert "0 queued" in lines["interactive"]
    out = status(market_engine, capsys, monkeypatch, cpm=10)
    assert "~18s" in out


def test_calls_in_last_hour_day_and_window(market_engine, capsys, monkeypatch):
    rid = enqueue(market_engine, "/a", {}, requester="t", now=NOW)
    for age in (10, 30, 120, 7200):
        add_call(market_engine, rid, NOW - age)
    add_call(market_engine, rid, NOW - 200_000)
    out = status(market_engine, capsys, monkeypatch, cpm=5)
    assert "last hour: 3" in out
    assert "last day: 4" in out
    assert "window: 2/5 calls in the last 60s" in out


def test_recent_failed_requests_with_http_status(market_engine, capsys, monkeypatch):
    rid = enqueue(market_engine, "/v3/reference/tickers/ZZZZ", {}, requester="t", now=NOW)
    with market_engine.begin() as conn:
        conn.execute(text("UPDATE requests SET state='failed', last_status=404, last_error='not found', "
                          "finished_at=:n WHERE id=:r"), {"n": NOW - 5, "r": rid})
    out = status(market_engine, capsys, monkeypatch)
    assert "/v3/reference/tickers/ZZZZ" in out and "404" in out and "not found" in out


def test_no_failures_says_none(market_engine, capsys, monkeypatch):
    assert "failed requests: none" in status(market_engine, capsys, monkeypatch)
