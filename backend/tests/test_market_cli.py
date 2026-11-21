from datetime import date

import pytest
from sqlalchemy import text

from hundred_acre.market.cli import main
from hundred_acre.market.lake import read_bars
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord

DAY = date(2026, 11, 13)


def src():
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    s.grouped_daily_bars[DAY] = [BarData("AAPL", 100.0, 110.0, 99.0, 105.0, 1e6, 104.5, 5000)]
    return s


def test_nightly_command_loads_bars_for_as_of(market_engine, lake):
    code = main(["nightly", "--as-of", "2026-11-13"], source=src(), engine=market_engine, lake=lake)
    assert code == 0
    with market_engine.connect() as c:
        sid = c.execute(text("SELECT security_id FROM securities")).scalar_one()
    assert read_bars(lake, [sid], DAY, DAY).height == 1


def test_nightly_syncs_corporate_actions_and_delistings_after_loading_bars(market_engine, lake):
    from hundred_acre.market.adjustments import corporate_action_history
    from hundred_acre.market.delistings import delisting
    from hundred_acre.market.source import Split

    s = src()
    s.tickers_.append(TickerRecord("OLD", "Old Co", "CS", "stocks", "BBG000OLD", active=False, delisted_date=DAY))
    s.grouped_daily_bars[DAY].append(BarData("OLD", 5.0, 5.0, 5.0, 5.0, 10.0, 5.0, 1))
    s.split_list = [Split("AAPL", DAY, 1, 2, id="s1")]
    main(["reference-sync", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake)

    assert main(["nightly", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake) == 0

    with market_engine.connect() as c:
        ids = dict(c.execute(text("SELECT ticker, security_id FROM ticker_periods")).all())
    assert [a.kind for a in corporate_action_history(market_engine, ids["AAPL"])] == ["split"]
    assert delisting(market_engine, ids["OLD"]).cash_per_share == 5.0


def test_nightly_command_also_loads_watchlist_intraday_bars(market_engine, lake):
    from datetime import datetime, timezone

    from hundred_acre.market.lake import read_intraday_bars
    from hundred_acre.market.watchlist import add_to_watchlist

    s = src()
    s.intraday_bars["AAPL"] = [(datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc), BarData("AAPL", 1, 1, 1, 1, 5.0, 1, 1))]
    sid = add_to_watchlist(s, market_engine, lake, "AAPL", as_of=date(2026, 11, 12))
    assert main(["nightly", "--as-of", "2026-11-13"], source=s, engine=market_engine, lake=lake) == 0
    assert read_intraday_bars(lake, [sid], DAY, DAY).height == 1


def test_reference_sync_command_creates_securities(market_engine, lake):
    assert main(["reference-sync", "--as-of", "2026-11-13"], source=src(), engine=market_engine, lake=lake) == 0
    with market_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM securities")).scalar_one() == 1


def test_as_of_is_required(market_engine, lake):
    with pytest.raises(SystemExit):
        main(["nightly"], source=src(), engine=market_engine, lake=lake)


@pytest.mark.parametrize("job", ["weekly", "backfill"])
def test_weekly_and_backfill_are_commands(job, market_engine, lake):
    # Their jobs land in tickets #17/#18; until then the command reports unavailability (exit 2).
    assert main([job, "--as-of", "2026-11-13"], source=src(), engine=market_engine, lake=lake) in (0, 2)


# --- `--as-of latest` -------------------------------------------------------------------------------------------

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _latest_run_loads(market_engine, lake, now_et, trading_days):
    """Run `nightly --as-of latest` at `now_et`; return the set of days (of `trading_days`) that got Bars."""
    s = FakeMarketDataSource()
    s.tickers_ = [TickerRecord("AAPL", "Apple", "CS", "stocks", "BBG001S5N8V8")]
    for d in trading_days:
        s.grouped_daily_bars[d] = [BarData("AAPL", 100.0, 110.0, 99.0, 105.0, 1e6, 104.5, 5000)]
    clock = lambda: now_et.timestamp()  # noqa: E731
    assert main(["nightly", "--as-of", "latest"], source=s, engine=market_engine, lake=lake, clock=clock) == 0
    with market_engine.connect() as c:
        sid = c.execute(text("SELECT security_id FROM securities")).scalar_one()
    return {d for d in trading_days if read_bars(lake, [sid], d, d).height == 1}


FRI, THU, SAT = date(2026, 11, 13), date(2026, 11, 12), date(2026, 11, 14)
THANKSGIVING, WED = date(2026, 11, 26), date(2026, 11, 25)
HALF_DAY = date(2026, 11, 27)  # day after Thanksgiving, closes 13:00 ET


@pytest.mark.parametrize(
    "now_et, expected",
    [
        (datetime(2026, 11, 13, 21, 0, tzinfo=ET), FRI),
        (datetime(2026, 11, 13, 19, 0, tzinfo=ET), THU),
        (datetime(2026, 11, 14, 3, 0, tzinfo=ET), FRI),
        (datetime(2026, 11, 14, 15, 0, tzinfo=ET), FRI),
        (datetime(2026, 11, 26, 12, 0, tzinfo=ET), WED),  # market holiday
        (datetime(2026, 11, 27, 12, 0, tzinfo=ET), WED),  # half day, before its early close
        (datetime(2026, 11, 27, 14, 0, tzinfo=ET), HALF_DAY),  # half day, after its early close
    ],
)
def test_as_of_latest_resolves_to_last_trading_day_whose_session_has_closed(market_engine, lake, now_et, expected):
    days = [THU, FRI, WED, HALF_DAY]
    loaded = _latest_run_loads(market_engine, lake, now_et, days)
    # The job ran for the resolved date (it may also fill earlier gaps, but never a later day).
    assert expected in loaded
    assert max(loaded) == expected


def test_explicit_as_of_ignores_the_clock(market_engine, lake):
    clock = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=ET).timestamp()  # noqa: E731
    assert main(["nightly", "--as-of", "2026-11-13"], source=src(), engine=market_engine, lake=lake, clock=clock) == 0
    with market_engine.connect() as c:
        sid = c.execute(text("SELECT security_id FROM securities")).scalar_one()
    assert read_bars(lake, [sid], DAY, DAY).height == 1


def test_as_of_rejects_garbage(market_engine, lake):
    with pytest.raises(SystemExit):
        main(["nightly", "--as-of", "tomorrow"], source=src(), engine=market_engine, lake=lake)
