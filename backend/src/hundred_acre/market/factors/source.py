"""Reference Factor source boundary (#43): where Kenneth French's published files come from.

Separate from Massive's `MarketDataSource` (ADR-0004): French's Data Library is not Massive and has no rate limit to
share, so nothing here goes through the Request Engine. A source returns the raw CSV text of the daily five-factor (2x3)
file and the daily momentum file; `parse_french_csv` reads them strictly into decimal returns.
"""

import io
import re
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import polars as pl

FRENCH_ATTRIBUTION = (
    "Factor Returns: Kenneth R. French Data Library, Tuck School of Business at Dartmouth "
    "(https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)."
)
FACTOR_COLUMNS = ("mkt_rf", "smb", "hml", "rmw", "cma", "umd")
RETURN_COLUMNS = (*FACTOR_COLUMNS, "rf")
BASE_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FIVE_FACTOR_FILE = "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
MOMENTUM_FILE = "F-F_Momentum_Factor_daily_CSV.zip"
_FIVE_HEADER = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
_MOM_HEADER = ["Mom"]
_DATE = re.compile(r"^\d{8}$")
_MISSING = (-99.99, -999.0)


class ReferenceFactorError(Exception):
    """The published files were unreachable, malformed or changed format. Nothing was stored."""


class ReferenceFactorSource(Protocol):
    def fetch(self) -> tuple[str, str]:
        """(daily five-factor CSV text, daily momentum CSV text)."""
        ...


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URLs
        return response.read()


@dataclass
class FrenchDataLibrarySource:
    """Downloads French's zipped CSV files over HTTP. `http_get` is injectable for tests."""

    http_get: Callable[[str], bytes] = _http_get

    def _csv(self, name: str) -> str:
        try:
            payload = self.http_get(BASE_URL + name)
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                return z.read(z.namelist()[0]).decode("utf-8", errors="replace")
        except (OSError, zipfile.BadZipFile, IndexError) as exc:
            raise ReferenceFactorError(f"could not download {name}: {exc}") from exc

    def fetch(self) -> tuple[str, str]:
        return self._csv(FIVE_FACTOR_FILE), self._csv(MOMENTUM_FILE)


@dataclass
class FakeReferenceFactorSource:
    """Serves canned CSV text. Set `available = False` to simulate an outage."""

    five_factor_csv: str
    momentum_csv: str
    available: bool = True

    def fetch(self) -> tuple[str, str]:
        if not self.available:
            raise ReferenceFactorError("fake reference factor source is unavailable")
        return self.five_factor_csv, self.momentum_csv

    def parsed(self) -> pl.DataFrame:
        return parse_reference_factors(*self.fetch())


def parse_french_csv(text: str, expected: list[str]) -> pl.DataFrame:
    """Daily data section of one French file: a `date` column plus `expected` columns, in percent.

    The data section starts at the line after the header (`,<expected columns>`) and ends at the first blank line; the
    annual section and copyright footer after it are ignored. Any unparseable row inside it is an error.
    """
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.startswith(",")), None)
    if header is None:
        raise ReferenceFactorError("no header line found")
    if [c.strip() for c in lines[header].split(",")] != ["", *expected]:
        raise ReferenceFactorError(f"unexpected header {lines[header]!r}, expected {expected}")
    rows = []
    for number, line in enumerate(lines[header + 1 :], start=header + 2):
        if not line.strip():
            break
        fields = [f.strip() for f in line.split(",")]
        try:
            if not _DATE.match(fields[0]) or len(fields) != len(expected) + 1:
                raise ValueError("bad shape")
            values = [float(f) for f in fields[1:]]
        except ValueError as exc:
            raise ReferenceFactorError(f"unparseable row at line {number}: {line!r}") from exc
        if any(v in _MISSING for v in values):
            raise ReferenceFactorError(f"missing-value marker at line {number}: {line!r}")
        rows.append((fields[0], *values))
    if not rows:
        raise ReferenceFactorError("data section is empty")
    df = pl.DataFrame(rows, schema=["date", *expected], orient="row")
    if df["date"].n_unique() != df.height:
        raise ReferenceFactorError("duplicate dates in data section")
    return df.with_columns(pl.col("date").str.to_date("%Y%m%d"))


def parse_reference_factors(five_factor_csv: str, momentum_csv: str) -> pl.DataFrame:
    """Both files joined on date, percents converted to decimals: trading_date, mkt_rf, smb, hml, rmw, cma, umd, rf."""
    five = parse_french_csv(five_factor_csv, _FIVE_HEADER)
    mom = parse_french_csv(momentum_csv, _MOM_HEADER)
    joined = five.join(mom, on="date", how="inner")
    if joined.height == 0:
        raise ReferenceFactorError("the five-factor and momentum files share no dates")
    renamed = {"date": "trading_date", "Mkt-RF": "mkt_rf", "SMB": "smb", "HML": "hml", "RMW": "rmw", "CMA": "cma",
               "Mom": "umd", "RF": "rf"}
    return (
        joined.rename(renamed)
        .select("trading_date", *RETURN_COLUMNS)
        .with_columns(pl.col(c) / 100 for c in RETURN_COLUMNS)
        .sort("trading_date")
    )
