from datetime import date, timedelta

from market_helpers import load_daily_bars
from hundred_acre.market.lake import read_bars
from hundred_acre.market.quarantine import list_provisional_securities, list_quarantined_bars
from hundred_acre.market.reference import security_info, security_on
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord

DAYS = [date(2026, 11, 9) + timedelta(days=i) for i in range(5)]  # Mon-Fri


def bar(ticker: str, close: float) -> BarData:
    return BarData(ticker, close, close, close, close, 100.0, close, 1)


def test_bars_before_reference_data_are_quarantined_then_matched(market_engine, lake):
    source = FakeMarketDataSource()
    source.grouped_daily_bars[DAYS[0]] = [bar("NEWCO", 10.0)]

    first = load_daily_bars(source, DAYS[0], engine=market_engine, lake=lake)

    assert first.bars_written == 0
    assert [(q.ticker, q.trading_date, q.close) for q in list_quarantined_bars(market_engine)] == [("NEWCO", DAYS[0], 10.0)]
    assert list_provisional_securities(market_engine) == []

    source.tickers_ = [TickerRecord("NEWCO", "NewCo", "CS", "stocks", "FIGI-NEWCO", list_date=DAYS[0])]
    source.grouped_daily_bars[DAYS[1]] = [bar("NEWCO", 11.0)]
    second = load_daily_bars(source, DAYS[1], engine=market_engine, lake=lake)

    sid = second.security_ids["NEWCO"]
    assert list_quarantined_bars(market_engine) == []
    assert read_bars(lake, [sid], DAYS[0], DAYS[1])["close"].to_list() == [10.0, 11.0]


def test_figi_less_ticker_becomes_provisional_after_five_trading_days(market_engine, lake):
    source = FakeMarketDataSource()
    for i, d in enumerate(DAYS):
        source.grouped_daily_bars[d] = [bar("MYST", 1.0 + i)]

    for d in DAYS[:4]:
        load_daily_bars(source, d, engine=market_engine, lake=lake)
    assert list_provisional_securities(market_engine) == []
    assert len(list_quarantined_bars(market_engine)) == 4

    load_daily_bars(source, DAYS[4], engine=market_engine, lake=lake)

    [prov] = list_provisional_securities(market_engine)
    assert prov.ticker == "MYST"
    assert list_quarantined_bars(market_engine) == []
    assert security_info(market_engine, prov.security_id).verified is False
    assert security_on(market_engine, "MYST", DAYS[0]) == prov.security_id
    assert read_bars(lake, [prov.security_id], DAYS[0], DAYS[4])["close"].to_list() == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_later_figi_attaches_to_the_same_provisional_security(market_engine, lake):
    source = FakeMarketDataSource()
    for i, d in enumerate(DAYS):
        source.grouped_daily_bars[d] = [bar("MYST", 1.0 + i)]
        load_daily_bars(source, d, engine=market_engine, lake=lake)
    [prov] = list_provisional_securities(market_engine)
    before = read_bars(lake, [prov.security_id], DAYS[0], DAYS[4])

    nxt = DAYS[4] + timedelta(days=3)
    source.tickers_ = [TickerRecord("MYST", "Mystery Inc", "CS", "stocks", "FIGI-MYST", list_date=DAYS[0])]
    source.grouped_daily_bars[nxt] = [bar("MYST", 9.0)]
    result = load_daily_bars(source, nxt, engine=market_engine, lake=lake)

    assert result.security_ids["MYST"] == prov.security_id
    info = security_info(market_engine, prov.security_id)
    assert (info.share_class_figi, info.verified, info.name) == ("FIGI-MYST", True, "Mystery Inc")
    assert list_provisional_securities(market_engine) == []
    after = read_bars(lake, [prov.security_id], DAYS[0], nxt)
    assert after["close"].to_list() == before["close"].to_list() + [9.0]
    assert security_on(market_engine, "MYST", DAYS[2]) == prov.security_id
