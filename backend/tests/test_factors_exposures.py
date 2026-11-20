import numpy as np
import pytest

from factors_world import (
    T1, T2, bars_frame, WINDOW_DAYS, WINDOW_END, FACTORS_THROUGH, factor_matrix, factor_source, load_world, prices_from, trading_days,
)
from hundred_acre.market.factors import (
    FACTOR_COLUMNS,
    import_reference_factor_returns,
    read_factor_exposures,
    refresh_factor_exposures,
)
from hundred_acre.market.lake import replace_bars

BETAS = [1.1, 0.4, -0.3, 0.2, 0.15, -0.25]
ALPHA = 0.0003


def refresh(engine, lake, **kw):
    return refresh_factor_exposures(engine, lake, window_days=WINDOW_DAYS, **kw)


@pytest.fixture
def setup(market_engine, lake):
    days = trading_days(market_engine)
    matrix = factor_matrix(len(days))
    import_reference_factor_returns(lake, factor_source(days, matrix), now=T1)
    return days, matrix


def test_exact_linear_returns_recover_coefficients_and_alpha(market_engine, lake, setup):
    days, matrix = setup
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, ALPHA, BETAS)})

    result = refresh(market_engine, lake)
    df = read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1], model="ff6")

    assert result.rebuilt == [ids["AAA"]] and df.height == 1
    row = df.row(0, named=True)
    assert row["window_end"] == WINDOW_END and row["n_obs"] == WINDOW_DAYS
    assert row["window_start"] == days[days.index(WINDOW_END) - WINDOW_DAYS + 1]
    assert row["alpha"] == pytest.approx(ALPHA, abs=1e-9)
    assert [row[f"beta_{f}"] for f in FACTOR_COLUMNS] == pytest.approx(BETAS, abs=1e-7)
    assert row["r_squared"] == pytest.approx(1.0, abs=1e-9)
    assert row["method"] and row["parameters"] and row["bar_snapshot"] > 0
    assert row["factor_vintage"].startswith("2026-11-01-")


def test_rows_record_the_estimation_parameters(market_engine, lake, setup):
    days, matrix = setup
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, ALPHA, BETAS)})
    refresh(market_engine, lake)

    row = read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1]).row(0, named=True)

    assert f'"window_days": {WINDOW_DAYS}' in row["parameters"] and "min_coverage" in row["parameters"]
    assert "OLS" in row["method"]


def test_nothing_is_computed_before_the_first_factor_import(market_engine, lake):
    days = trading_days(market_engine)
    matrix = factor_matrix(len(days))
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, ALPHA, BETAS)})

    assert refresh(market_engine, lake).rebuilt == []
    assert read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1]).height == 0


def test_windows_after_the_latest_factor_return_are_not_computed(market_engine, lake):
    days = trading_days(market_engine)
    matrix = factor_matrix(len(days))
    import_reference_factor_returns(lake, factor_source(days, matrix, through=WINDOW_END.replace(day=29)), now=T1)
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, ALPHA, BETAS)})
    refresh(market_engine, lake)

    df = read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1])

    assert df["window_end"].to_list() == [WINDOW_END.replace(month=8, day=31)]


def test_a_window_with_under_80_percent_coverage_is_skipped(market_engine, lake, setup):
    days, matrix = setup
    end = days.index(WINDOW_END)
    thin = [None if end - 25 < i <= end else c for i, c in enumerate(prices_from(matrix, ALPHA, BETAS))]  # 25 of 60 missing
    mild = [None if i in (end - 3, end - 10, end - 17) else c for i, c in enumerate(prices_from(matrix, ALPHA, BETAS))]
    _, ids = load_world(market_engine, lake, days, {"THIN": thin, "MILD": mild})

    refresh(market_engine, lake)

    df = read_factor_exposures(lake, [ids["THIN"], ids["MILD"]], days[0], days[-1])
    assert df["security_id"].to_list() == [ids["MILD"]]
    row = df.row(0, named=True)
    assert row["n_obs"] == WINDOW_DAYS - 6  # a missing Bar loses that day's return and the next one
    assert row["alpha"] == pytest.approx(ALPHA, abs=1e-9)  # the multi-day return across the gap is not used


def test_a_split_day_does_not_distort_exposures(market_engine, lake, setup):
    from hundred_acre.market.adjustments import sync_corporate_actions
    from hundred_acre.market.source import Split

    days, matrix = setup
    closes = prices_from(matrix, ALPHA, BETAS)
    split_day = days.index(WINDOW_END) - 20
    shown = [c / 4 if i >= split_day else c for i, c in enumerate(closes)]
    source, ids = load_world(market_engine, lake, days, {"AAA": shown})
    source.split_list = [Split("AAA", days[split_day], 1, 4, id="s1")]
    sync_corporate_actions(source, days[0], engine=market_engine, lake=lake)

    refresh(market_engine, lake)

    row = read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1]).row(0, named=True)
    assert row["alpha"] == pytest.approx(ALPHA, abs=1e-9) and row["r_squared"] == pytest.approx(1.0, abs=1e-9)


def test_refresh_only_recomputes_stale_securities(market_engine, lake, setup):
    days, matrix = setup
    closes = prices_from(matrix, ALPHA, BETAS)
    source, ids = load_world(market_engine, lake, days, {"AAA": closes, "BBB": prices_from(matrix, 0.0, BETAS[::-1])})
    refresh(market_engine, lake)
    assert refresh(market_engine, lake).rebuilt == []

    corrected = {"AAA": [c * 1.001 if k == 5 else c for k, c in enumerate(closes)]}
    replace_bars(lake, bars_frame(ids, days, corrected), [ids["AAA"]], days)  # late correction of one Security

    assert refresh(market_engine, lake).rebuilt == [ids["AAA"]]
    assert sorted(refresh(market_engine, lake, full=True).rebuilt) == sorted(ids.values())


def test_a_restated_vintage_makes_every_security_stale_and_is_recorded(market_engine, lake, setup):
    days, matrix = setup
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, ALPHA, BETAS)})
    refresh(market_engine, lake)
    old = read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1]).row(0, named=True)

    restated = matrix.copy()
    restated[0, 0] += 0.001  # French restates an early day outside the window
    import_reference_factor_returns(lake, factor_source(days, restated), now=T2)

    assert refresh(market_engine, lake).rebuilt == [ids["AAA"]]
    new = read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1])
    assert new.height == 1 and new["factor_vintage"][0] != old["factor_vintage"] and new["factor_vintage"][0].startswith("2026-11-08-")


def test_a_malformed_reimport_leaves_exposures_on_the_previous_vintage(market_engine, lake, setup):
    from hundred_acre.market.factors import FakeReferenceFactorSource, ReferenceFactorError

    days, matrix = setup
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, ALPHA, BETAS)})
    refresh(market_engine, lake)
    with pytest.raises(ReferenceFactorError):
        import_reference_factor_returns(lake, FakeReferenceFactorSource("junk", "junk"), now=T2)

    assert refresh(market_engine, lake).rebuilt == []
    assert read_factor_exposures(lake, [ids["AAA"]], days[0], days[-1]).height == 1
