from datetime import date, datetime, timezone

from sqlalchemy import text

from hundred_acre.market.jobs import nightly_intraday_bars
from hundred_acre.market.lake import read_intraday_bars
from hundred_acre.market.reference import security_info, security_on, sync_reference
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord
from hundred_acre.market.watchlist import add_to_watchlist, remove_from_watchlist, watchlist

MON, TUE = date(2026, 11, 23), date(2026, 11, 24)
FRI_BEFORE = date(2026, 11, 20)
# November: ET is UTC-5, so 9:30 ET = 14:30 UTC and 16:00 ET = 21:00 UTC.


def utc(day: date, h: int, m: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def bar(close: float = 10.0, volume: float = 100.0) -> BarData:
    return BarData("AAPL", close, close, close, close, volume, close, 3)


def make_source(*bars: tuple[datetime, BarData]) -> FakeMarketDataSource:
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    s.intraday_bars = {"AAPL": list(bars)}
    return s


def watch(source, engine, lake, as_of=FRI_BEFORE) -> int:
    return add_to_watchlist(source, engine, lake, "AAPL", as_of=as_of)


def test_nightly_loads_watchlist_bars_tagged_by_trading_session(market_engine, lake):
    source = make_source(
        (utc(MON, 9, 0), bar()),  # 04:00 ET
        (utc(MON, 14, 30), bar()),  # 09:30 ET
        (utc(MON, 20, 55), bar()),  # 15:55 ET, last regular bar
        (utc(MON, 21, 0), bar()),  # 16:00 ET
    )
    sid = watch(source, market_engine, lake)
    nightly_intraday_bars(source, MON, engine=market_engine, lake=lake)

    df = read_intraday_bars(lake, [sid], MON, MON, columns=["bar_start", "trading_session"], sessions=None)
    assert df.rows() == [
        (utc(MON, 9, 0), "pre-market"),
        (utc(MON, 14, 30), "regular"),
        (utc(MON, 20, 55), "regular"),
        (utc(MON, 21, 0), "after-hours"),
    ]


def sessions_of(lake, sid, day, **kw):
    return read_intraday_bars(lake, [sid], day, day, columns=["bar_start", "trading_session"], **kw).rows()


def coverage_of(engine, sid):
    with engine.connect() as conn:
        return {
            d: (st, n)
            for d, st, n in conn.execute(
                text("SELECT trading_date, status, bar_count FROM coverage WHERE security_id = :s AND interval = '5m'"),
                {"s": sid},
            )
        }


def test_half_day_regular_session_ends_at_one_pm(market_engine, lake):
    half_day = date(2026, 11, 27)  # day after Thanksgiving, closes 13:00 ET = 18:00 UTC
    source = make_source((utc(half_day, 17, 55), bar()), (utc(half_day, 18, 0), bar()))
    sid = watch(source, market_engine, lake)
    nightly_intraday_bars(source, half_day, engine=market_engine, lake=lake)

    assert sessions_of(lake, sid, half_day, sessions=None) == [
        (utc(half_day, 17, 55), "regular"),
        (utc(half_day, 18, 0), "after-hours"),
    ]


def test_overnight_bar_belongs_to_the_trading_day_it_leads_into(market_engine, lake):
    friday = date(2026, 11, 20)
    source = make_source(
        (utc(date(2026, 11, 21), 3, 0), bar()),  # Friday 22:00 ET
        (utc(MON, 14, 30), bar()),
    )
    sid = watch(source, market_engine, lake)
    nightly_intraday_bars(source, MON, engine=market_engine, lake=lake)

    assert sessions_of(lake, sid, friday, sessions=None) == []
    assert sessions_of(lake, sid, MON, sessions=None) == [
        (utc(date(2026, 11, 21), 3, 0), "overnight"),
        (utc(MON, 14, 30), "regular"),
    ]


def test_reads_default_to_the_regular_session_and_can_filter_by_session(market_engine, lake):
    source = make_source(
        (utc(MON, 9, 0), bar()), (utc(MON, 14, 30), bar()), (utc(MON, 21, 0), bar()), (utc(MON, 2, 0), bar()),
    )
    sid = watch(source, market_engine, lake)
    nightly_intraday_bars(source, MON, engine=market_engine, lake=lake)

    assert sessions_of(lake, sid, MON) == [(utc(MON, 14, 30), "regular")]
    assert sessions_of(lake, sid, MON, sessions="pre-market") == [(utc(MON, 9, 0), "pre-market")]
    both = sessions_of(lake, sid, MON, sessions=["pre-market", "after-hours"])
    assert both == [(utc(MON, 9, 0), "pre-market"), (utc(MON, 21, 0), "after-hours")]
    assert [r[1] for r in sessions_of(lake, sid, MON, sessions=None)] == [
        "overnight", "pre-market", "regular", "after-hours",
    ]


def test_gaps_stay_gaps_thin_days_are_complete_and_barless_days_are_empty(market_engine, lake):
    source = make_source((utc(MON, 14, 30), bar()), (utc(MON, 15, 30), bar()))  # nothing between; nothing on Tuesday
    sid = watch(source, market_engine, lake)
    nightly_intraday_bars(source, TUE, engine=market_engine, lake=lake)

    assert [r[0] for r in sessions_of(lake, sid, MON)] == [utc(MON, 14, 30), utc(MON, 15, 30)]
    cov = coverage_of(market_engine, sid)
    assert cov[MON] == ("complete", 2)
    assert cov[TUE] == ("empty", 0)


def test_failed_fetch_is_recorded_and_retried_next_night(market_engine, lake):
    source = make_source((utc(MON, 14, 30), bar()))
    sid = watch(source, market_engine, lake)
    source.failing_tickers = {"AAPL"}
    nightly_intraday_bars(source, MON, engine=market_engine, lake=lake)
    assert coverage_of(market_engine, sid)[MON][0] == "failed"

    source.failing_tickers = set()
    nightly_intraday_bars(source, TUE, engine=market_engine, lake=lake)
    assert coverage_of(market_engine, sid)[MON] == ("complete", 1)
    assert sessions_of(lake, sid, MON) == [(utc(MON, 14, 30), "regular")]


def test_covered_days_are_not_fetched_again(market_engine, lake):
    source = make_source((utc(MON, 14, 30), bar()))
    watch(source, market_engine, lake)
    nightly_intraday_bars(source, MON, engine=market_engine, lake=lake)
    calls = len(source.intraday_calls)

    nightly_intraday_bars(source, MON, engine=market_engine, lake=lake)
    assert len(source.intraday_calls) == calls


def test_adding_to_the_watchlist_backfills_missing_intraday_history(market_engine, lake):
    source = make_source((utc(FRI_BEFORE, 14, 30), bar(1.0)), (utc(MON, 14, 30), bar(2.0)))
    sid = add_to_watchlist(source, market_engine, lake, "AAPL", as_of=MON)

    df = read_intraday_bars(lake, [sid], FRI_BEFORE, MON, columns=["trading_date", "close"])
    assert df.rows() == [(FRI_BEFORE, 1.0), (MON, 2.0)]


def test_removed_securities_leave_the_nightly_set_but_keep_their_bars(market_engine, lake):
    source = make_source((utc(MON, 14, 30), bar()), (utc(TUE, 14, 30), bar()))
    sid = watch(source, market_engine, lake, as_of=MON)
    remove_from_watchlist(market_engine, sid)
    assert watchlist(market_engine) == []

    nightly_intraday_bars(source, TUE, engine=market_engine, lake=lake)
    assert sessions_of(lake, sid, TUE) == []
    assert sessions_of(lake, sid, MON) == [(utc(MON, 14, 30), "regular")]


def test_a_hand_added_otc_security_joins_the_universe(market_engine, lake):
    source = make_source()
    source.tickers_.append(TickerRecord("NSRGY", "Nestle ADR", "ADRC", "otc", "BBG000OTC001", list_date=date(2020, 1, 2)))
    source.intraday_bars["NSRGY"] = [(utc(MON, 14, 30), BarData("NSRGY", 1, 1, 1, 1, 5.0, 1, 1, otc=True))]

    sid = add_to_watchlist(source, market_engine, lake, "NSRGY", as_of=MON)

    assert security_on(market_engine, "NSRGY", MON) == sid
    assert security_info(market_engine, sid).type == "adr"
    assert watchlist(market_engine) == [sid]
    assert sessions_of(lake, sid, MON) == [(utc(MON, 14, 30), "regular")]
    sync_reference(source, market_engine)  # a later sync keeps it
    assert security_on(market_engine, "NSRGY", MON) == sid
