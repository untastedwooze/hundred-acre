"""The Request Engine clamps Bar Calls to the History Limit at Call time (ADR-0006)."""

import calendar
from datetime import date

from sqlalchemy import text

from hundred_acre.market.history_limit import history_limit
from hundred_acre.market.massive import MassiveError
from hundred_acre.market.request_engine import enqueue
from test_market_request_engine import BAR, SPLITS, world  # noqa: F401  (world is a fixture)

NOON_2026_11_28 = calendar.timegm((2026, 11, 28, 12, 0, 0))  # History Limit 2024-11-28
JUST_AFTER_MIDNIGHT_UTC = calendar.timegm((2026, 11, 28, 0, 30, 0))  # still 2026-11-27 19:30 in New York
DAILY = "/v2/aggs/ticker/AAPL/range/1/day/{}/{}"
FIVE_MIN = "/v2/aggs/ticker/AAPL/range/5/minute/{}/{}"
GROUPED = "/v2/aggs/grouped/locale/us/market/stocks/{}"
DAY_BAR = {"o": 1, "h": 2, "l": 1, "c": 2, "v": 3, "t": 1_732_800_000_000}  # 2024-11-28


def _at(w, now):
    w.clock.now = now
    w.engine.start()  # a fresh heartbeat at the new time


def _ask(w, path, params=None):
    return enqueue(w.db, path, params or {"adjusted": "false"}, requester="t", now=w.clock())


def _calls(db):
    with db.connect() as conn:
        return conn.execute(text("SELECT sent_path, history_limit FROM calls ORDER BY id")).all()


def _request(db, rid):
    with db.connect() as conn:
        return conn.execute(text("SELECT state, params, note FROM requests WHERE id = :i"), {"i": rid}).one()


def test_history_limit_is_the_utc_date_minus_the_span_inclusive():
    assert history_limit(date(2026, 11, 28), 2) == date(2024, 11, 28)
    assert history_limit(date(2026, 11, 28), 5) == date(2021, 11, 28)


def test_history_limit_on_a_leap_day_falls_on_the_28th():
    assert history_limit(date(2028, 2, 29), 2) == date(2026, 2, 28)
    assert history_limit(date(2028, 2, 29), 4) == date(2024, 2, 29)


def test_daily_aggregates_starting_before_the_limit_are_called_from_the_limit(world):
    clipped, full = DAILY.format("2024-11-28", "2025-01-31"), DAILY.format("2020-01-01", "2025-01-31")
    w = world({clipped: {"results": [DAY_BAR]}})
    _at(w, NOON_2026_11_28)
    rid = _ask(w, full)
    w.engine.serve()
    assert [c[1] for c in w.transport.calls] == [clipped]
    assert _calls(w.db) == [(clipped, date(2024, 11, 28))]
    state, params, _ = _request(w.db, rid)
    assert state == "complete" and params == {"adjusted": "false"}


def test_an_intraday_request_spanning_the_limit_is_called_from_the_limit(world):
    clipped = FIVE_MIN.format("2024-11-28", "2024-12-20")
    w = world({clipped: {"results": [DAY_BAR]}})
    _at(w, NOON_2026_11_28)
    _ask(w, FIVE_MIN.format("2024-11-10", "2024-12-20"))
    w.engine.serve()
    assert _calls(w.db) == [(clipped, date(2024, 11, 28))]


def test_a_window_wholly_before_the_limit_completes_empty_with_no_call_and_says_why(world):
    w = world({})
    _at(w, NOON_2026_11_28)
    ids = [_ask(w, DAILY.format("2020-01-01", "2024-11-27")),
           _ask(w, GROUPED.format("2024-11-27"), {"adjusted": "false", "include_otc": "false"})]
    assert w.engine.serve() == 0
    assert w.transport.calls == [] and _calls(w.db) == []
    for rid in ids:
        state, _, note = _request(w.db, rid)
        assert state == "complete" and "2024-11-28" in note
    assert w.source.grouped_daily(date(2024, 11, 27), adjusted=False, include_otc=False) == []


def test_the_limit_turns_over_at_midnight_utc_not_new_york(world):
    w = world({GROUPED.format("2024-11-28"): {"results": [BAR]}})
    _at(w, JUST_AFTER_MIDNIGHT_UTC)
    assert len(w.source.grouped_daily(date(2024, 11, 28), adjusted=False, include_otc=False)) == 1
    assert w.source.grouped_daily(date(2024, 11, 27), adjusted=False, include_otc=False) == []
    assert len(w.transport.calls) == 1


def test_reference_requests_are_sent_unchanged(world):
    w = world({SPLITS: {"results": []}})
    _at(w, NOON_2026_11_28)
    w.source.splits(date(2020, 1, 1))
    assert w.transport.calls[0][1:] == (SPLITS, {"execution_date.gte": "2020-01-01", "limit": 1000})


def test_a_403_on_a_bar_endpoint_halts_and_names_the_window_and_the_limit(world, market_engine):
    clipped = DAILY.format("2024-11-28", "2025-01-31")
    w = world({clipped: MassiveError("plan", 403)})
    _at(w, NOON_2026_11_28)
    _ask(w, DAILY.format("2020-01-01", "2025-01-31"))
    w.engine.serve()
    with market_engine.connect() as conn:
        state, why = conn.execute(text("SELECT state, halt_reason FROM request_engine_state")).one()
    assert state == "halted" and clipped in why and "2024-11-28" in why


def test_the_history_span_is_configurable(world):
    w = world({DAILY.format("2025-11-28", "2026-01-31"): {"results": []}})
    w.engine._history_years = 1
    _at(w, NOON_2026_11_28)
    _ask(w, DAILY.format("2020-01-01", "2026-01-31"))
    w.engine.serve()
    assert _calls(w.db)[0][1] == date(2025, 11, 28)
