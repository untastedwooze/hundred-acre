from datetime import date

import pytest
from sqlalchemy import text

from hundred_acre.market.delistings import (
    assumed_delistings, delisting, record_spinoff, set_delisting_outcome, spinoffs_of, sync_delistings,
)
from hundred_acre.market.jobs import load_daily_bars
from hundred_acre.market.lake import read_bars
from hundred_acre.market.reference import sync_reference
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord

D1, D2, D3 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)


def world(engine, lake):
    """ATVI delisted on D2 (last bar D2 closes 94.5), PXD delisted D1, XOM and MSFT still trading."""
    s = FakeMarketDataSource()
    s.tickers_ = [
        TickerRecord("ATVI", "Activision", "CS", "stocks", "F1", active=False, delisted_date=D2),
        TickerRecord("PXD", "Pioneer", "CS", "stocks", "F2", active=False, delisted_date=D1),
        TickerRecord("XOM", "Exxon", "CS", "stocks", "F3"),
    ]
    px = lambda t, c: BarData(t, c, c, c, c, 1.0, c, 1)
    s.grouped_daily_bars[D1] = [px("ATVI", 94.0), px("PXD", 250.0), px("XOM", 100.0)]
    s.grouped_daily_bars[D2] = [px("ATVI", 94.5), px("XOM", 101.0)]
    sync_reference(s, engine)
    for d in (D1, D2):
        load_daily_bars(s, d, engine=engine, lake=lake)
    with engine.connect() as c:
        ids = dict(c.execute(text("SELECT ticker, security_id FROM ticker_periods")).all())
    return ids


def test_delisted_security_gets_assumed_cash_at_last_close(market_engine, lake):
    ids = world(market_engine, lake)

    assert sorted(sync_delistings(market_engine, lake)) == sorted([ids["ATVI"], ids["PXD"]])

    d = delisting(market_engine, ids["ATVI"])
    assert (d.outcome, d.cash_per_share, d.assumed, d.delisted_on) == ("cash", 94.5, True, D2)
    assert delisting(market_engine, ids["PXD"]).cash_per_share == 250.0
    assert delisting(market_engine, ids["XOM"]) is None
    assert sync_delistings(market_engine, lake) == []  # idempotent
    assert read_bars(lake, [ids["ATVI"]], D1, D2).height == 2  # history kept


def test_cash_override_is_no_longer_assumed_and_survives_resync(market_engine, lake):
    ids = world(market_engine, lake)
    sync_delistings(market_engine, lake)

    set_delisting_outcome(market_engine, ids["ATVI"], cash_per_share=95.0)
    sync_delistings(market_engine, lake)

    d = delisting(market_engine, ids["ATVI"])
    assert (d.outcome, d.cash_per_share, d.assumed) == ("cash", 95.0, False)


def test_conversion_links_target_with_ratio(market_engine, lake):
    ids = world(market_engine, lake)
    sync_delistings(market_engine, lake)

    set_delisting_outcome(market_engine, ids["PXD"], convert_to=ids["XOM"], ratio=2.3234)

    d = delisting(market_engine, ids["PXD"])
    assert (d.outcome, d.target_security_id, d.ratio, d.assumed, d.cash_per_share) == ("conversion", ids["XOM"], 2.3234, False, None)


def test_worthless_override(market_engine, lake):
    ids = world(market_engine, lake)
    sync_delistings(market_engine, lake)

    set_delisting_outcome(market_engine, ids["PXD"], worthless=True)

    d = delisting(market_engine, ids["PXD"])
    assert (d.outcome, d.cash_per_share, d.assumed) == ("worthless", None, False)


def test_assumed_outcomes_can_be_listed(market_engine, lake):
    ids = world(market_engine, lake)
    sync_delistings(market_engine, lake)
    set_delisting_outcome(market_engine, ids["ATVI"], cash_per_share=95.0)

    assert [d.security_id for d in assumed_delistings(market_engine)] == [ids["PXD"]]


def test_override_needs_exactly_one_outcome_and_an_existing_delisting(market_engine, lake):
    ids = world(market_engine, lake)
    sync_delistings(market_engine, lake)
    with pytest.raises(ValueError):
        set_delisting_outcome(market_engine, ids["ATVI"], cash_per_share=1.0, worthless=True)
    with pytest.raises(ValueError):
        set_delisting_outcome(market_engine, ids["ATVI"], convert_to=ids["XOM"])  # ratio missing
    with pytest.raises(ValueError):
        set_delisting_outcome(market_engine, ids["XOM"], worthless=True)  # not delisted


def test_spinoff_links_child_to_parent_and_parent_continues(market_engine, lake):
    ids = world(market_engine, lake)
    record_spinoff(market_engine, parent_id=ids["XOM"], child_id=ids["ATVI"], ratio=0.25, spin_date=D2)

    [s] = spinoffs_of(market_engine, ids["XOM"])
    assert (s.parent_security_id, s.child_security_id, s.ratio, s.spin_date) == (ids["XOM"], ids["ATVI"], 0.25, D2)
    assert delisting(market_engine, ids["XOM"]) is None
    assert sync_delistings(market_engine, lake) and delisting(market_engine, ids["XOM"]) is None
