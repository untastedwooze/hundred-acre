from datetime import date

from hundred_acre.market.jobs import load_daily_bars
from hundred_acre.market.lake import read_bars
from hundred_acre.market.reference import security_info, security_on
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerEvent, TickerRecord


def bar(ticker: str, close: float) -> BarData:
    return BarData(ticker, close, close, close, close, 100.0, close, 1)


def test_rename_keeps_one_continuous_security(market_engine, lake):
    before, after = date(2022, 10, 26), date(2022, 10, 28)
    source = FakeMarketDataSource()
    source.tickers_ = [TickerRecord("META", "Meta", "CS", "stocks", "FIGI-META", cik="0001326801", list_date=date(2012, 5, 18))]
    source.events["META"] = [TickerEvent("FB", date(2022, 10, 28), "ticker_change", new_ticker="META")]
    source.grouped_daily_bars[before] = [bar("FB", 100.0)]
    source.grouped_daily_bars[after] = [bar("META", 101.0)]

    first = load_daily_bars(source, before, engine=market_engine, lake=lake)
    second = load_daily_bars(source, after, engine=market_engine, lake=lake)

    sid = first.security_ids["FB"]
    assert second.security_ids["META"] == sid
    assert read_bars(lake, [sid], before, after)["close"].to_list() == [100.0, 101.0]
    assert security_on(market_engine, "FB", before) == sid
    assert security_on(market_engine, "FB", after) is None
    assert security_on(market_engine, "META", before) is None
    assert security_on(market_engine, "META", after) == sid


def test_reused_ticker_is_two_securities(market_engine, lake):
    old_day, new_day = date(2009, 5, 29), date(2026, 11, 13)
    source = FakeMarketDataSource()
    source.tickers_ = [
        TickerRecord("GM", "Old GM", "CS", "stocks", "FIGI-OLDGM", active=False, delisted_date=date(2009, 6, 1)),
        TickerRecord("GM", "General Motors", "CS", "stocks", "FIGI-NEWGM", list_date=date(2010, 11, 18)),
    ]
    source.grouped_daily_bars[old_day] = [bar("GM", 1.0)]
    source.grouped_daily_bars[new_day] = [bar("GM", 50.0)]

    old = load_daily_bars(source, old_day, engine=market_engine, lake=lake).security_ids["GM"]
    new = load_daily_bars(source, new_day, engine=market_engine, lake=lake).security_ids["GM"]

    assert old != new
    assert read_bars(lake, [old], old_day, new_day)["close"].to_list() == [1.0]
    assert read_bars(lake, [new], old_day, new_day)["close"].to_list() == [50.0]
    assert security_on(market_engine, "GM", date(2009, 12, 1)) is None
    assert security_on(market_engine, "GM", date(2012, 1, 3)) == new


def test_share_classes_are_two_securities_with_one_issuer(market_engine, lake):
    day = date(2026, 11, 13)
    source = FakeMarketDataSource()
    source.tickers_ = [
        TickerRecord("GOOGL", "Alphabet A", "CS", "stocks", "FIGI-A", cik="0001652044"),
        TickerRecord("GOOG", "Alphabet C", "CS", "stocks", "FIGI-C", cik="0001652044"),
    ]
    source.grouped_daily_bars[day] = [bar("GOOGL", 1.0), bar("GOOG", 2.0)]

    ids = load_daily_bars(source, day, engine=market_engine, lake=lake).security_ids

    assert ids["GOOGL"] != ids["GOOG"]
    a, c = security_info(market_engine, ids["GOOGL"]), security_info(market_engine, ids["GOOG"])
    assert a.issuer_cik == c.issuer_cik == "0001652044"


def test_securities_carry_a_normalized_type(market_engine, lake):
    day = date(2026, 11, 13)
    codes = {"CS": "common_stock", "ETF": "etf", "ADRC": "adr", "PFD": "preferred", "WARRANT": "warrant", "UNIT": "unit", "RIGHT": "right"}
    source = FakeMarketDataSource()
    source.tickers_ = [TickerRecord(f"T{i}", None, code, "stocks", f"FIGI-{i}") for i, code in enumerate(codes)]
    source.grouped_daily_bars[day] = [bar(f"T{i}", 1.0) for i in range(len(codes))]

    ids = load_daily_bars(source, day, engine=market_engine, lake=lake).security_ids

    got = [security_info(market_engine, ids[f"T{i}"]).type for i in range(len(codes))]
    assert got == list(codes.values())


def test_resync_is_idempotent(market_engine, lake):
    day = date(2022, 10, 28)
    source = FakeMarketDataSource()
    source.tickers_ = [TickerRecord("META", "Meta", "CS", "stocks", "FIGI-META")]
    source.events["META"] = [TickerEvent("FB", day, "ticker_change", new_ticker="META")]
    source.grouped_daily_bars[day] = [bar("META", 1.0)]

    a = load_daily_bars(source, day, engine=market_engine, lake=lake).security_ids
    b = load_daily_bars(source, day, engine=market_engine, lake=lake).security_ids

    assert a == b
    assert security_on(market_engine, "FB", date(2022, 10, 27)) == a["META"]
