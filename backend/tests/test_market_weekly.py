from datetime import date, datetime, timezone

from sqlalchemy import text

from hundred_acre.market.cli import main
from hundred_acre.market.jobs import load_daily_bars, run_weekly
from hundred_acre.market.lake import read_bars, read_intraday_bars
from hundred_acre.market.source import BarData, FakeMarketDataSource, TickerRecord
from hundred_acre.market.watchlist import add_to_watchlist

MON = date(2026, 11, 23)


def utc(day: date, h: int, m: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def bar(volume: float) -> BarData:
    return BarData("X", 10, 10, 10, 10, volume, 10, 3)


class Source(FakeMarketDataSource):
    """Returns `corrected` Intraday Bars for a Ticker from its second call onward, when given."""

    corrected: dict = {}

    def intraday_aggregates(self, ticker, start, end, *, adjusted=False):
        n = sum(1 for c in self.intraday_calls if c[0] == ticker)
        out = super().intraday_aggregates(ticker, start, end, adjusted=adjusted)
        return self.corrected[ticker] if n >= 1 and ticker in self.corrected else out


def make_source(tickers: dict[str, str], daily_volume: float = 200.0) -> Source:
    s = Source()
    s.tickers_ = [
        TickerRecord(t, t, code, "stocks", f"BBG{t:0<9}", list_date=date(2020, 1, 2)) for t, code in tickers.items()
    ]
    for t in tickers:
        s.intraday_bars[t] = [(utc(MON, 14, 30), bar(100.0)), (utc(MON, 14, 35), bar(100.0))]
    s.grouped_daily_bars[MON] = [BarData(t, 10, 10, 10, 10, daily_volume, 10, 5) for t in tickers]
    return s


def sid_of(engine, ticker):
    with engine.connect() as c:
        return c.execute(text("SELECT security_id FROM ticker_periods WHERE ticker = :t"), {"t": ticker}).scalar_one()


def coverage(engine, sid, day=MON):
    with engine.connect() as c:
        return c.execute(
            text("SELECT status FROM coverage WHERE security_id = :s AND interval = '5m' AND trading_date = :d"),
            {"s": sid, "d": day},
        ).scalar_one_or_none()


def test_weekly_loads_one_call_per_non_watchlist_security(market_engine, lake):
    s = make_source({"AAA": "CS", "BBB": "ETF", "WL": "CS"})
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    add_to_watchlist(s, market_engine, lake, "WL", as_of=MON)
    s.intraday_calls.clear()

    run_weekly(s, MON, engine=market_engine, lake=lake)

    assert sorted(t for t, _, _ in s.intraday_calls) == ["AAA", "BBB"]
    for t in ("AAA", "BBB"):
        assert read_intraday_bars(lake, [sid_of(market_engine, t)], MON, MON).height == 2


def test_warrants_units_and_rights_are_skipped_but_keep_daily_bars(market_engine, lake):
    s = make_source({"AAA": "CS", "AAAW": "WARRANT", "AAAU": "UNIT", "AAAR": "RIGHT"})
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    s.intraday_calls.clear()

    run_weekly(s, MON, engine=market_engine, lake=lake)

    assert [t for t, _, _ in s.intraday_calls] == ["AAA"]
    assert read_bars(lake, [sid_of(market_engine, "AAAW")], MON, MON).height == 1


def test_mismatched_volume_is_marked_suspect_and_refetched(market_engine, lake):
    s = make_source({"AAA": "CS"}, daily_volume=200.0)
    s.intraday_bars["AAA"] = [(utc(MON, 14, 30), bar(50.0))]  # first fetch only sums to 50
    s.corrected = {"AAA": [(utc(MON, 14, 30), bar(50.0)), (utc(MON, 14, 35), bar(150.0))]}
    load_daily_bars(s, MON, engine=market_engine, lake=lake)

    run_weekly(s, MON, engine=market_engine, lake=lake)

    sid = sid_of(market_engine, "AAA")
    assert read_intraday_bars(lake, [sid], MON, MON, columns=["volume"]).rows() == [(50.0,), (150.0,)]
    assert coverage(market_engine, sid) == "complete"


def test_unresolved_mismatch_stays_suspect(market_engine, lake):
    s = make_source({"AAA": "CS"}, daily_volume=1000.0)
    load_daily_bars(s, MON, engine=market_engine, lake=lake)

    run_weekly(s, MON, engine=market_engine, lake=lake)

    assert coverage(market_engine, sid_of(market_engine, "AAA")) == "suspect"


def test_volume_within_threshold_is_not_suspect(market_engine, lake):
    s = make_source({"AAA": "CS"}, daily_volume=204.0)  # 2% off
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    run_weekly(s, MON, engine=market_engine, lake=lake)
    assert coverage(market_engine, sid_of(market_engine, "AAA")) == "complete"
    assert len(s.intraday_calls) == 1


def test_threshold_defaults_to_five_percent_and_is_configurable(market_engine, lake, monkeypatch):
    s = make_source({"AAA": "CS"}, daily_volume=210.0)  # ~4.8% off: within the default
    load_daily_bars(s, MON, engine=market_engine, lake=lake)
    run_weekly(s, MON, engine=market_engine, lake=lake)
    sid = sid_of(market_engine, "AAA")
    assert coverage(market_engine, sid) == "complete"

    monkeypatch.setenv("MARKET_VOLUME_MISMATCH_THRESHOLD", "0.01")
    run_weekly(s, MON, engine=market_engine, lake=lake)
    assert coverage(market_engine, sid) == "suspect"


def test_weekly_command_runs(market_engine, lake):
    from hundred_acre.market.factors import FakeReferenceFactorSource

    s = make_source({"AAA": "CS"})
    code = main(
        ["weekly", "--as-of", "2026-11-23"], source=s, engine=market_engine, lake=lake,
        factor_source=FakeReferenceFactorSource("junk", "junk"),  # a failed import must not fail the job
    )
    assert code == 0
    assert len(s.intraday_calls) == 1
