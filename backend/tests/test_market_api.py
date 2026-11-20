from datetime import date

import pytest
from fastapi.testclient import TestClient

from factors_world import T1, WINDOW_DAYS, WINDOW_END, factor_matrix, factor_source, load_world, prices_from, trading_days
from hundred_acre.main import app
from hundred_acre.market.api import get_lake, get_market_engine
from hundred_acre.market.factors import import_reference_factor_returns, refresh_factor_exposures

BETAS = [1.1, 0.4, -0.3, 0.2, 0.15, -0.25]


@pytest.fixture
def client(market_engine, lake):
    app.dependency_overrides[get_market_engine] = lambda: market_engine
    app.dependency_overrides[get_lake] = lambda: lake
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def world(market_engine, lake):
    days = trading_days(market_engine)
    matrix = factor_matrix(len(days))
    load_world(market_engine, lake, days, {"AAA": prices_from(matrix, 0.0003, BETAS), "THIN": [None] * len(days)})
    return days, matrix


def refreshed(market_engine, lake, world):
    days, matrix = world
    import_reference_factor_returns(lake, factor_source(days, matrix), now=T1)
    refresh_factor_exposures(market_engine, lake, window_days=WINDOW_DAYS)


def test_resolves_a_ticker_to_its_security_as_of_a_date(client, world):
    body = client.get("/api/market/securities/AAA", params={"on": "2026-10-01"}).json()
    assert body["ticker"] == "AAA" and isinstance(body["security_id"], int)


def test_unknown_ticker_is_a_clear_404(client, world):
    r = client.get("/api/market/securities/NOPE")
    assert r.status_code == 404 and "NOPE" in r.json()["detail"]


def test_factor_vintage_and_through_date(client, market_engine, lake, world):
    assert client.get("/api/market/factors/vintage").json()["vintage"] is None
    refreshed(market_engine, lake, world)
    body = client.get("/api/market/factors/vintage").json()
    assert body["vintage"].startswith("2026-11-01-")
    assert body["through_date"] == "2026-10-15"
    assert "Kenneth R. French" in body["attribution"]


def test_exposure_series_filtered_by_model(client, market_engine, lake, world):
    refreshed(market_engine, lake, world)
    ff6 = client.get("/api/market/factors/exposures", params={"ticker": "AAA", "model": "ff6"}).json()
    assert ff6["empty_reason"] is None and ff6["model"] == "ff6"
    last = ff6["series"][-1]
    assert last["window_end"] == str(WINDOW_END)
    assert last["alpha"] == pytest.approx(0.0003) and last["r_squared"] == pytest.approx(1.0)
    assert len(last["betas"]) == 6 and last["betas"][0]["factor"] == "Mkt-RF"
    assert last["betas"][0]["value"] == pytest.approx(1.1)
    assert ff6["vintage"]["through_date"] == "2026-10-15"

    capm = client.get("/api/market/factors/exposures", params={"ticker": "AAA", "model": "capm"}).json()
    assert [b["factor"] for b in capm["series"][-1]["betas"]] == ["Mkt-RF"]


@pytest.mark.parametrize(
    "ticker,reason",
    [("NOPE", "unknown_ticker"), ("THIN", "too_little_history")],
)
def test_empty_responses_say_why(client, market_engine, lake, world, ticker, reason):
    refreshed(market_engine, lake, world)
    body = client.get("/api/market/factors/exposures", params={"ticker": ticker}).json()
    assert body["series"] == [] and body["empty_reason"] == reason and body["message"]


def test_no_factor_returns_yet(client, world):
    body = client.get("/api/market/factors/exposures", params={"ticker": "AAA"}).json()
    assert body["empty_reason"] == "no_factor_returns" and body["vintage"] is None


def test_rejects_an_unknown_model(client, world):
    assert client.get("/api/market/factors/exposures", params={"ticker": "AAA", "model": "x"}).status_code == 422


def test_factor_returns_short_of_a_month_end_is_distinct_from_too_little_history(client, market_engine, lake, world):
    days, matrix = world
    import_reference_factor_returns(lake, factor_source(days, matrix, through=date(2026, 6, 20)), now=T1)
    refresh_factor_exposures(market_engine, lake, window_days=WINDOW_DAYS)

    body = client.get("/api/market/factors/exposures", params={"ticker": "AAA"}).json()
    thin = client.get("/api/market/factors/exposures", params={"ticker": "THIN"}).json()

    assert body["series"] == [] and body["empty_reason"] == "factor_returns_not_at_month_end"
    assert thin["empty_reason"] == "too_little_history"
    assert body["message"] != thin["message"] and "month-end" in body["message"] and "month-end" not in thin["message"]
