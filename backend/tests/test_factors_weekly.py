from datetime import date, timedelta

import pytest

from factors_world import T1, T2, WINDOW_DAYS, factor_matrix, factor_source, load_world, prices_from, trading_days
from hundred_acre.market import jobs
from hundred_acre.market.cli import main
from hundred_acre.market.factors import (
    FakeReferenceFactorSource, import_reference_factor_returns, read_factor_exposures, read_reference_factor_returns,
)
from hundred_acre.market.source import FakeMarketDataSource

MON = date(2026, 11, 23)


def test_weekly_runs_import_then_statistics_then_exposures(market_engine, lake, monkeypatch):
    order = []
    for name in ("import_reference_factor_returns", "refresh_statistics", "refresh_factor_exposures"):
        real = getattr(jobs, name, None)
        monkeypatch.setattr(jobs, name, lambda *a, _n=name, _r=real, **k: (order.append(_n), _r(*a, **k) if _r else None)[1], raising=False)
    days = trading_days(market_engine)

    jobs.run_weekly(
        FakeMarketDataSource(), MON, engine=market_engine, lake=lake, factor_source=factor_source(days, factor_matrix(len(days)))
    )

    assert order == ["import_reference_factor_returns", "refresh_statistics", "refresh_factor_exposures"]


def test_failed_import_still_refreshes_exposures_on_the_last_good_vintage(market_engine, lake):
    days = trading_days(market_engine)
    matrix = factor_matrix(len(days))
    import_reference_factor_returns(lake, factor_source(days, matrix), now=T1)
    good = read_reference_factor_returns(lake)["vintage"][0]
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, 0.0003, [1.1, 0.4, -0.3, 0.2, 0.15, -0.25])})

    jobs.run_weekly(
        FakeMarketDataSource(), MON, engine=market_engine, lake=lake,
        factor_source=FakeReferenceFactorSource("junk", "junk"), exposure_window_days=WINDOW_DAYS,
    )

    df = read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1], model="ff6")
    assert df.height > 0 and set(df["factor_vintage"]) == {good}


def test_weekly_command_imports_factors_with_the_injected_source(market_engine, lake):
    days = trading_days(market_engine)
    code = main(
        ["weekly", "--as-of", "2026-11-23"], source=FakeMarketDataSource(), engine=market_engine, lake=lake,
        factor_source=factor_source(days, factor_matrix(len(days))),
    )
    assert code == 0 and read_reference_factor_returns(lake).height > 0


def test_same_day_a_b_a_reimport_does_not_duplicate_vintage_rows(market_engine, lake):
    days = trading_days(market_engine)
    a = factor_matrix(len(days))
    b = a.copy()
    b[0, 0] += 0.001
    first = import_reference_factor_returns(lake, factor_source(days, a), now=T1)
    import_reference_factor_returns(lake, factor_source(days, b), now=T1 + timedelta(hours=1))
    third = import_reference_factor_returns(lake, factor_source(days, a), now=T1 + timedelta(hours=2))

    assert third.created and third.vintage != first.vintage
    n = read_reference_factor_returns(lake, vintage=first.vintage).height
    assert read_reference_factor_returns(lake, vintage=third.vintage).height == n == first.rows
