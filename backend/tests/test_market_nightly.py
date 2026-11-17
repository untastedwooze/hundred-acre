from datetime import date

from hundred_acre.market.jobs import nightly_daily_bars
from hundred_acre.market.lake import current_snapshot, expire_snapshots, read_bars, snapshot_retention_days
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord

MON, TUE = date(2026, 11, 23), date(2026, 11, 24)
AFTER_WEEKEND = date(2026, 11, 30)
BEFORE_WEEKEND = date(2026, 11, 27)


def bar(close):
    return BarData("AAPL", close, close, close, close, 100.0, close, 5)


def source_with(day_bars: dict[date, float]) -> FakeMarketDataSource:
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    s.grouped_daily_bars = {d: [bar(c)] for d, c in day_bars.items()}
    return s


def aapl_id(engine):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text("SELECT security_id FROM tickers WHERE ticker = 'AAPL'")).scalar_one()


def test_nightly_refetches_the_previous_trading_day_and_replaces_it_atomically(market_engine, lake):
    source = source_with({MON: 10.0, TUE: 11.0})
    nightly_daily_bars(source, MON, engine=market_engine, lake=lake)
    before = current_snapshot(lake)

    source.grouped_daily_bars[MON] = [bar(10.5)]  # late correction
    nightly_daily_bars(source, TUE, engine=market_engine, lake=lake)

    sid = aapl_id(market_engine)
    df = read_bars(lake, [sid], MON, TUE, columns=["trading_date", "close"])
    assert df.rows() == [(MON, 10.5), (TUE, 11.0)]
    old = read_bars(lake, [sid], MON, TUE, columns=["trading_date", "close"], snapshot=before)
    assert old.rows() == [(MON, 10.0)]


def test_previous_trading_day_skips_weekends_and_holidays(market_engine, lake):
    source = source_with({BEFORE_WEEKEND: 5.0, AFTER_WEEKEND: 6.0})
    nightly_daily_bars(source, BEFORE_WEEKEND, engine=market_engine, lake=lake)
    source.grouped_daily_bars[BEFORE_WEEKEND] = [bar(5.5)]

    nightly_daily_bars(source, AFTER_WEEKEND, engine=market_engine, lake=lake)

    sid = aapl_id(market_engine)
    assert read_bars(lake, [sid], BEFORE_WEEKEND, AFTER_WEEKEND, columns=["close"])["close"].to_list() == [5.5, 6.0]


def test_snapshot_retention_defaults_to_90_days_and_is_configurable(monkeypatch):
    monkeypatch.delenv("MARKET_SNAPSHOT_RETENTION_DAYS", raising=False)
    assert snapshot_retention_days() == 90
    monkeypatch.setenv("MARKET_SNAPSHOT_RETENTION_DAYS", "30")
    assert snapshot_retention_days() == 30


def test_recent_snapshots_survive_default_expiry_and_expired_ones_go(market_engine, lake):
    source = source_with({MON: 10.0})
    nightly_daily_bars(source, MON, engine=market_engine, lake=lake)
    old_snapshot = current_snapshot(lake)
    source.grouped_daily_bars[MON] = [bar(10.5)]
    nightly_daily_bars(source, MON, engine=market_engine, lake=lake)
    sid = aapl_id(market_engine)

    assert expire_snapshots(lake) == []
    assert read_bars(lake, [sid], MON, MON, snapshot=old_snapshot)["close"].to_list() == [10.0]

    assert old_snapshot in expire_snapshots(lake, retention_days=0)
