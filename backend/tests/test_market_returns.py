from datetime import date

import logging

import pytest

from hundred_acre.market.adjustments import sync_corporate_actions
from hundred_acre.market.delistings import set_delisting_outcome, sync_delistings
from hundred_acre.market.jobs import load_daily_bars
from hundred_acre.market.reference import sync_reference
from hundred_acre.market.returns import daily_total_returns
from hundred_acre.market.source import BarData, Dividend, FakeMarketDataSource, Split, TickerRecord

D1, D2, D3, D4 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)
D5 = date(2026, 11, 6)


def px(ticker: str, close: float) -> BarData:
    return BarData(ticker, close, close, close, close, 1000.0, close, 1)


def world(engine, lake, prices: dict[str, dict[date, float]], delisted: dict[str, date] | None = None):
    """Securities by ticker with the given closes per day; `delisted` marks tickers whose Ticker period ends."""
    source = FakeMarketDataSource()
    source.tickers_ = [
        TickerRecord(t, t, "CS", "stocks", f"F{t}", active=t not in (delisted or {}), delisted_date=(delisted or {}).get(t))
        for t in prices
    ]
    days = sorted({d for series in prices.values() for d in series})
    for d in days:
        source.grouped_daily_bars[d] = [px(t, series[d]) for t, series in prices.items() if d in series]
    sync_reference(source, engine)
    ids = {}
    for d in days:
        ids.update(load_daily_bars(source, d, engine=engine, lake=lake).security_ids)
    return source, ids


def returns(engine, lake, ids, *tickers, start=D1, end=D5) -> dict[date, float]:
    df = daily_total_returns(engine, lake, [ids[t] for t in tickers], start, end)
    return dict(zip(df["trading_date"].to_list(), df["total_return"].to_list()))


def test_close_to_close_returns_for_several_securities(market_engine, lake):
    source, ids = world(market_engine, lake, {"AAA": {D1: 100.0, D2: 110.0, D3: 99.0}, "BBB": {D1: 50.0, D2: 50.0, D3: 55.0}})

    df = daily_total_returns(market_engine, lake, [ids["AAA"], ids["BBB"]], D1, D3)

    assert df.columns == ["security_id", "trading_date", "total_return"]
    assert df.select("security_id", "trading_date", "total_return").rows() == [
        (ids["AAA"], D2, pytest.approx(0.10)), (ids["AAA"], D3, pytest.approx(-0.10)),
        (ids["BBB"], D2, pytest.approx(0.0)), (ids["BBB"], D3, pytest.approx(0.10)),
    ]


def test_range_start_uses_the_previous_close_outside_the_range(market_engine, lake):
    source, ids = world(market_engine, lake, {"AAA": {D1: 100.0, D2: 110.0, D3: 121.0}})

    assert returns(market_engine, lake, ids, "AAA", start=D3, end=D3) == {D3: pytest.approx(0.10)}


def test_split_day_produces_no_return(market_engine, lake):
    source, ids = world(market_engine, lake, {"AAA": {D1: 400.0, D2: 100.0, D3: 101.0}})
    source.split_list = [Split("AAA", D2, 1, 4, id="s1")]
    sync_corporate_actions(source, D1, engine=market_engine, lake=lake)

    assert returns(market_engine, lake, ids, "AAA") == {D2: pytest.approx(0.0), D3: pytest.approx(0.01)}


def test_dividend_day_includes_the_dividend(market_engine, lake):
    source, ids = world(market_engine, lake, {"AAA": {D1: 100.0, D2: 99.0, D3: 99.0}})
    source.dividend_list = [Dividend("AAA", D2, 2.0, id="d1")]
    sync_corporate_actions(source, D1, engine=market_engine, lake=lake)

    got = returns(market_engine, lake, ids, "AAA")
    assert got[D2] == pytest.approx(99.0 / 98.0 - 1)  # total-return factor basis; price fell 1% but the 2 dividend is reinvested
    assert got[D2] > 0
    assert got[D3] == pytest.approx(0.0)


def delisted_world(engine, lake, last_close=100.0):
    source, ids = world(
        engine, lake,
        {"GONE": {D1: 90.0, D2: last_close}, "TGT": {D1: 20.0, D2: 21.0, D3: 22.0}},
        delisted={"GONE": D2},
    )
    sync_delistings(engine, lake)
    return ids


def test_security_without_delisting_gets_no_extra_row(market_engine, lake):
    ids = delisted_world(market_engine, lake)

    assert list(returns(market_engine, lake, ids, "TGT")) == [D2, D3]


def test_assumed_cash_delisting_gives_final_return_of_about_zero(market_engine, lake):
    ids = delisted_world(market_engine, lake)

    got = returns(market_engine, lake, ids, "GONE")

    assert list(got) == [D2, D3]  # ordinary D2 return, then the final return on the next trading day
    assert got[D2] == pytest.approx(100.0 / 90.0 - 1)
    assert got[D3] == pytest.approx(0.0)


def test_confirmed_cash_delisting_is_cash_over_previous_close_minus_one(market_engine, lake):
    ids = delisted_world(market_engine, lake)
    set_delisting_outcome(market_engine, ids["GONE"], cash_per_share=105.0)

    assert returns(market_engine, lake, ids, "GONE")[D3] == pytest.approx(0.05)


def test_conversion_delisting_is_ratio_times_target_close_over_previous_close_minus_one(market_engine, lake):
    ids = delisted_world(market_engine, lake)
    set_delisting_outcome(market_engine, ids["GONE"], convert_to=ids["TGT"], ratio=5.0)

    assert returns(market_engine, lake, ids, "GONE")[D3] == pytest.approx(5.0 * 21.0 / 100.0 - 1)


def test_worthless_delisting_is_minus_one(market_engine, lake):
    ids = delisted_world(market_engine, lake)
    set_delisting_outcome(market_engine, ids["GONE"], worthless=True)

    assert returns(market_engine, lake, ids, "GONE")[D3] == -1.0


def test_final_return_is_outside_a_range_that_ends_before_it(market_engine, lake):
    ids = delisted_world(market_engine, lake)
    set_delisting_outcome(market_engine, ids["GONE"], worthless=True)

    assert returns(market_engine, lake, ids, "GONE", end=D2) == {D2: pytest.approx(100.0 / 90.0 - 1)}


def test_conversion_whose_target_has_no_close_is_logged_not_silently_dropped(market_engine, lake, caplog):
    source, ids = world(
        market_engine, lake, {"GONE": {D1: 90.0, D2: 100.0}, "TGT": {D3: 22.0}}, delisted={"GONE": D2},
    )
    sync_delistings(market_engine, lake)
    set_delisting_outcome(market_engine, ids["GONE"], convert_to=ids["TGT"], ratio=5.0)

    with caplog.at_level(logging.WARNING):
        got = returns(market_engine, lake, ids, "GONE")

    assert D3 not in got
    assert any(f"{ids['GONE']}" in r.message and f"{ids['TGT']}" in r.message for r in caplog.records)
