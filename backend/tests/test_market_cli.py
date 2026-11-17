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
