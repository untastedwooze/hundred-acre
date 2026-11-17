from datetime import date

import pytest
from sqlalchemy import text

from hundred_acre.market.adjustments import corporate_action_history, sync_corporate_actions
from hundred_acre.market.jobs import load_daily_bars
from hundred_acre.market.lake import read_bars
from hundred_acre.market.source import BarData, Dividend, FakeMarketDataSource, Split, TickerRecord

D1, D2, D3, D4, D5 = (date(2026, 11, d) for d in (2, 3, 4, 5, 6))
CLOSES = {D1: 600.0, D2: 150.0, D3: 152.0, D4: 15.2, D5: 15.5}


def nvda(engine, lake) -> tuple[FakeMarketDataSource, int]:
    source = FakeMarketDataSource()
    source.tickers_ = [TickerRecord("NVDA", "Nvidia", "CS", "stocks", "BBG000BBJQV0")]
    for d, c in CLOSES.items():
        source.grouped_daily_bars[d] = [BarData("NVDA", c, c, c, c, 1000.0, c, 10)]
        sid = load_daily_bars(source, d, engine=engine, lake=lake).security_ids["NVDA"]
    source.split_list = [Split("NVDA", D2, 1, 4, id="s1"), Split("NVDA", D4, 1, 10, id="s2")]
    return source, sid


def closes(lake, sid, basis):
    return read_bars(lake, [sid], D1, D5, columns=["close"], basis=basis)["close"].to_list()


def test_split_adjusted_closes_are_continuous_and_stored_bars_unchanged(market_engine, lake):
    source, sid = nvda(market_engine, lake)
    sync_corporate_actions(source, D1, engine=market_engine, lake=lake)

    assert closes(lake, sid, "split") == pytest.approx([15.0, 15.0, 15.2, 15.2, 15.5])
    assert closes(lake, sid, "none") == list(CLOSES.values())
    df = read_bars(lake, [sid], D1, D5, columns=["volume"], basis="split")
    assert df["volume"].to_list() == pytest.approx([40000.0, 10000.0, 10000.0, 1000.0, 1000.0])


def test_revised_split_rebuilds_only_that_securitys_factors(market_engine, lake):
    source, sid = nvda(market_engine, lake)
    source.tickers_.append(TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8"))
    for d in CLOSES:
        source.grouped_daily_bars[d].append(BarData("AAPL", 10.0, 10.0, 10.0, 10.0, 1.0, 10.0, 1))
    source.split_list.append(Split("AAPL", D3, 1, 2, id="a1"))
    for d in CLOSES:
        aapl = load_daily_bars(source, d, engine=market_engine, lake=lake).security_ids["AAPL"]
    first = sync_corporate_actions(source, D1, engine=market_engine, lake=lake)
    assert sorted(first.rebuilt) == sorted([sid, aapl])

    source.split_list[1] = Split("NVDA", D4, 1, 5, id="s2")  # revised 10:1 -> 5:1
    second = sync_corporate_actions(source, D1, engine=market_engine, lake=lake)

    assert second.rebuilt == [sid]
    assert closes(lake, sid, "split")[0] == pytest.approx(600.0 / 4 / 5)
    history = [(a.revision, a.split_to, a.current) for a in corporate_action_history(market_engine, sid) if a.action_date == D4]
    assert history == [(1, 10, False), (2, 5, True)]


def test_unchanged_resync_rebuilds_nothing(market_engine, lake):
    source, sid = nvda(market_engine, lake)
    sync_corporate_actions(source, D1, engine=market_engine, lake=lake)

    assert sync_corporate_actions(source, D1, engine=market_engine, lake=lake).rebuilt == []


def test_total_return_factor_differs_from_split_factor_with_dividends(market_engine, lake):
    source, sid = nvda(market_engine, lake)
    source.dividend_list = [Dividend("NVDA", D3, 1.52, id="d1")]  # 1% of the previous close (152 is D3's; prior is 150)
    sync_corporate_actions(source, D1, engine=market_engine, lake=lake)

    split, total = closes(lake, sid, "split"), closes(lake, sid, "total_return")
    assert total[-1] == split[-1]  # latest day carries no later actions
    assert total[0] < split[0]
    assert total[0] == pytest.approx(split[0] * (150.0 - 1.52) / 150.0)
    assert total[2] == split[2]  # ex-date itself is not adjusted for its own dividend


def test_unknown_basis_is_rejected(market_engine, lake):
    with pytest.raises(ValueError):
        read_bars(lake, [1], D1, D5, basis="bogus")
