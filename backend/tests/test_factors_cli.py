from datetime import date

import pytest
from sqlalchemy import text

from factors_world import (
    T1, WINDOW_DAYS, WINDOW_END, factor_matrix, factor_source, load_world, prices_from, trading_days,
)
from hundred_acre.market.cli import main
from hundred_acre.market.factors import (
    FakeReferenceFactorSource, import_reference_factor_returns, refresh_factor_exposures,
)

BETAS = [1.1, 0.4, -0.3, 0.2, 0.15, -0.25]


def run(argv, engine, lake, **kw):
    return main(argv, engine=engine, lake=lake, **kw)


def test_import_reports_the_vintage_and_date_range_without_massive(market_engine, lake, capsys):
    days = trading_days(market_engine)
    source = factor_source(days, factor_matrix(len(days)))

    assert run(["factors", "import"], market_engine, lake, factor_source=source) == 0
    out = capsys.readouterr().out
    assert "created vintage" in out and str(days[0]) in out and "Kenneth R. French" in out

    assert run(["factors", "import"], market_engine, lake, factor_source=source) == 0
    assert "no change" in capsys.readouterr().out


def test_failed_import_exits_nonzero(market_engine, lake, capsys):
    code = run(["factors", "import"], market_engine, lake, factor_source=FakeReferenceFactorSource("junk", "junk"))
    assert code == 1 and "import failed" in capsys.readouterr().err


@pytest.fixture
def world(market_engine, lake):
    days = trading_days(market_engine)
    matrix = factor_matrix(len(days))
    _, ids = load_world(market_engine, lake, days, {"AAA": prices_from(matrix, 0.0003, BETAS), "THIN": [None] * len(days)})
    return days, matrix, ids


def refreshed(market_engine, lake, world):
    days, matrix, _ = world
    import_reference_factor_returns(lake, factor_source(days, matrix), now=T1)
    refresh_factor_exposures(market_engine, lake, window_days=WINDOW_DAYS)


def test_exposures_prints_the_latest_ff6_window(market_engine, lake, world, capsys):
    refreshed(market_engine, lake, world)

    assert run(["factors", "exposures", "AAA"], market_engine, lake) == 0

    out = capsys.readouterr().out
    assert f"to {WINDOW_END}" in out and "vintage 2026-11-01-" in out
    assert "alpha (daily)\t+0.000300" in out and "Mkt-RF\t+1.1000" in out and "UMD\t-0.2500" in out
    assert "R-squared\t1.0000" in out and "Kenneth R. French" in out


def test_exposures_date_selects_the_window_ending_on_or_before_it(market_engine, lake, world, capsys):
    refreshed(market_engine, lake, world)

    assert run(["factors", "exposures", "AAA", "--date", "2026-08-30"], market_engine, lake) == 1
    assert "no FF6 exposures" in capsys.readouterr().err
    assert run(["factors", "exposures", "AAA", "--date", "2026-09-29"], market_engine, lake) == 0
    assert "to 2026-08-31" in capsys.readouterr().out
    assert run(["factors", "exposures", "AAA", "--date", "2026-10-05"], market_engine, lake) == 0
    assert f"to {WINDOW_END}" in capsys.readouterr().out


def test_ticker_is_resolved_as_of_the_requested_date(market_engine, lake, world, capsys):
    _, _, ids = world
    with market_engine.begin() as conn:
        conn.execute(text("UPDATE ticker_periods SET ticker = 'OLDA', valid_to = '2026-09-30' WHERE security_id = :s"), {"s": ids["AAA"]})
        conn.execute(text("INSERT INTO ticker_periods (ticker, security_id, valid_from) VALUES ('NEWA', :s, '2026-10-01')"), {"s": ids["AAA"]})
    refreshed(market_engine, lake, world)

    assert run(["factors", "exposures", "OLDA", "--date", "2026-10-05"], market_engine, lake) == 1  # not its Ticker then
    assert "unknown Ticker" in capsys.readouterr().err
    assert run(["factors", "exposures", "OLDA", "--date", "2026-09-30"], market_engine, lake) == 0
    assert run(["factors", "exposures", "NEWA", "--date", "2026-10-05"], market_engine, lake) == 0


def test_each_no_exposures_message(market_engine, lake, world, capsys):
    assert run(["factors", "exposures", "AAA"], market_engine, lake) == 1
    assert "no Reference Factor Returns yet" in capsys.readouterr().err
    refreshed(market_engine, lake, world)
    assert run(["factors", "exposures", "THIN"], market_engine, lake) == 1
    assert "too little history" in capsys.readouterr().err
    assert run(["factors", "exposures", "ZZZ"], market_engine, lake) == 1
    assert "unknown Ticker" in capsys.readouterr().err


def test_exposures_model_selects_ff3_and_capm(market_engine, lake, world, capsys):
    refreshed(market_engine, lake, world)

    assert run(["factors", "exposures", "AAA", "--model", "capm"], market_engine, lake) == 0
    out = capsys.readouterr().out
    assert "CAPM exposure" in out and "Mkt-RF" in out and "SMB" not in out
    assert run(["factors", "exposures", "AAA", "--model", "ff3"], market_engine, lake) == 0
    out = capsys.readouterr().out
    assert "FF3 exposure" in out and "HML" in out and "RMW" not in out
