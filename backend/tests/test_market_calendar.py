from datetime import date, datetime, timezone

from hundred_acre.market.trading_calendar import generate_calendar, read_trading_calendar
from hundred_acre.market.source import FakeMarketDataSource, MarketHoliday

START, END = date(2026, 11, 20), date(2026, 12, 31)


def test_calendar_has_trading_days_with_open_close_and_half_days(market_engine):
    generate_calendar(market_engine, START, END)

    df = read_trading_calendar(market_engine, START, END)

    days = df["trading_date"].to_list()
    assert date(2026, 11, 26) not in days  # Thanksgiving
    assert date(2026, 11, 28) not in days  # Saturday
    assert date(2026, 12, 25) not in days  # Christmas
    regular = df.filter(df["trading_date"] == date(2026, 11, 20)).row(0, named=True)
    assert regular["open"] == datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)  # 09:30 ET
    assert regular["close"] == datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)  # 16:00 ET
    assert regular["half_day"] is False
    half = df.filter(df["trading_date"] == date(2026, 11, 27)).row(0, named=True)
    assert half["close"] == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)  # 13:00 ET
    assert half["half_day"] is True


def test_calendar_is_keyed_by_calendar_id_and_generation_is_repeatable(market_engine):
    generate_calendar(market_engine, START, END)
    generate_calendar(market_engine, START, END)

    nyse = read_trading_calendar(market_engine, START, END)
    assert nyse["trading_date"].n_unique() == nyse.height
    assert read_trading_calendar(market_engine, START, END, calendar_id="CRYPTO").height == 0


def test_agreeing_massive_holidays_report_nothing(market_engine):
    source = FakeMarketDataSource(
        holidays=[
            MarketHoliday(date(2026, 11, 26), "NYSE", "Thanksgiving", "closed"),
            MarketHoliday(date(2026, 11, 27), "NYSE", "Thanksgiving", "early-close"),
        ]
    )

    assert generate_calendar(market_engine, START, END, source=source) == []


def test_disagreements_with_massive_holidays_are_reported(market_engine):
    source = FakeMarketDataSource(
        holidays=[
            MarketHoliday(date(2026, 12, 1), "NYSE", "Surprise closure", "closed"),  # we think it trades
            MarketHoliday(date(2026, 12, 2), "NYSE", "Surprise early close", "early-close"),  # we think full day
            MarketHoliday(date(2026, 11, 20), "NASDAQ", "Other exchange", "closed"),  # ignored
        ]
    )

    found = generate_calendar(market_engine, START, END, source=source)

    assert sorted((d.date, d.massive_status) for d in found) == [
        (date(2026, 12, 1), "closed"),
        (date(2026, 12, 2), "early-close"),
    ]
