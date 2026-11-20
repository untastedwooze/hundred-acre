import io
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from hundred_acre.market.factors import (
    FRENCH_ATTRIBUTION,
    FakeReferenceFactorSource,
    FrenchDataLibrarySource,
    ReferenceFactorError,
    import_reference_factor_returns,
    read_reference_factor_returns,
)
from hundred_acre.market.source import FakeMarketDataSource

FIXTURES = Path(__file__).parent / "fixtures"
FIVE = "x\n\n,Mkt-RF,SMB,HML,RMW,CMA,RF\n20250102, 1.0, 2.0, 3.0, 4.0, 5.0, 0.5\n20250103, -1.0, 0.0, 0.0, 0.0, 0.0, 0.5\n"
MOM = "x\n\n,Mom\n20250102, 6.0\n20250103, -2.0\n"
T1 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 8, tzinfo=timezone.utc)


def test_import_converts_percents_to_decimals(lake):
    result = import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM), now=T1)

    assert result.created and (result.first_date, result.last_date, result.rows) == (date(2025, 1, 2), date(2025, 1, 3), 2)
    df = read_reference_factor_returns(lake)
    assert df.columns[:8] == ["trading_date", "mkt_rf", "smb", "hml", "rmw", "cma", "umd", "rf"]
    assert df.row(0)[:8] == (date(2025, 1, 2), pytest.approx(0.01), pytest.approx(0.02), pytest.approx(0.03),
                             pytest.approx(0.04), pytest.approx(0.05), pytest.approx(0.06), pytest.approx(0.005))
    assert df["vintage"][0] == result.vintage


def test_identical_reimport_creates_no_vintage(lake):
    first = import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM), now=T1)
    again = import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM), now=T2)

    assert not again.created and again.vintage == first.vintage


def test_restatement_keeps_old_vintage_readable(lake):
    first = import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM), now=T1)
    restated = import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE.replace("1.0, 2.0", "1.5, 2.0"), MOM), now=T2)

    assert restated.created and restated.vintage != first.vintage
    assert read_reference_factor_returns(lake)["mkt_rf"][0] == pytest.approx(0.015)
    assert read_reference_factor_returns(lake, vintage=first.vintage)["mkt_rf"][0] == pytest.approx(0.01)


@pytest.mark.parametrize("five", [
    FIVE.replace("Mkt-RF,SMB", "Mkt,SMB"),
    FIVE + "2025abc, 1, 1, 1, 1, 1, 1\n",
    FIVE.replace("5.0, 0.5\n20250103", "5.0\n20250103"),
])
def test_malformed_file_fails_loudly_and_keeps_previous_vintage(lake, five):
    good = import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM), now=T1)

    with pytest.raises(ReferenceFactorError):
        import_reference_factor_returns(lake, FakeReferenceFactorSource(five, MOM), now=T2)

    assert set(read_reference_factor_returns(lake)["vintage"]) == {good.vintage}


def test_unreachable_source_fails_loudly(lake):
    with pytest.raises(ReferenceFactorError):
        import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM, available=False), now=T1)


def test_import_makes_no_massive_calls(lake):
    massive = FakeMarketDataSource()
    import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM), now=T1)
    assert massive.aggregate_calls == [] and massive.intraday_calls == []


def test_monthly_returns_compound_the_daily_ones(lake):
    import_reference_factor_returns(lake, FakeReferenceFactorSource(FIVE, MOM), now=T1)

    df = read_reference_factor_returns(lake, frequency="monthly")

    assert df["month"].to_list() == [date(2025, 1, 31)]
    assert df["mkt_rf"][0] == pytest.approx(1.01 * 0.99 - 1)
    assert df["rf"][0] == pytest.approx(1.005**2 - 1)


def test_real_parser_reads_fixture_in_frenchs_layout():
    five = (FIXTURES / "french_5_factors_daily.csv").read_text()
    mom = (FIXTURES / "french_momentum_daily.csv").read_text()

    df = FakeReferenceFactorSource(five, mom).parsed()

    assert df["trading_date"].to_list() == [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    assert df["umd"].to_list() == pytest.approx([0.004, -0.006, 0.009])
    assert df["rf"][0] == pytest.approx(0.00017)


def test_french_source_downloads_and_unzips(monkeypatch):
    def zipped(name, text):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name, text)
        return buf.getvalue()

    urls = []

    def fake_get(url):
        urls.append(url)
        return zipped("f.csv", FIVE if "5_Factors" in url else MOM)

    five, mom = FrenchDataLibrarySource(http_get=fake_get).fetch()

    assert five == FIVE and mom == MOM
    assert all(u.startswith("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/") for u in urls)


def test_attribution_names_the_data_library():
    assert "Kenneth R. French" in FRENCH_ATTRIBUTION and "Dartmouth" in FRENCH_ATTRIBUTION
