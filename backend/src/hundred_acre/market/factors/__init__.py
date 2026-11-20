"""Factors: Reference Factor Returns (French's Data Library) and per-Security Factor Exposures (#39).

Public interface:
- import_reference_factor_returns(lake, source): store a new vintage (no-op when unchanged)
- read_reference_factor_returns(lake, vintage=None, start, end, frequency): daily or monthly, any vintage
- refresh_factor_exposures(engine, lake, ...): staleness-driven (see exposures.py)
- read_factor_exposures(lake, security_ids, start, end, model=None)
"""

from hundred_acre.market.factors.exposures import ExposureRefreshResult, read_factor_exposures, refresh_factor_exposures
from hundred_acre.market.factors.reference_returns import (
    ImportResult,
    VintageInfo,
    import_reference_factor_returns,
    latest_vintage,
    read_reference_factor_returns,
)
from hundred_acre.market.factors.source import (
    FACTOR_COLUMNS,
    FRENCH_ATTRIBUTION,
    FakeReferenceFactorSource,
    FrenchDataLibrarySource,
    ReferenceFactorError,
    ReferenceFactorSource,
)

__all__ = [
    "ExposureRefreshResult", "FACTOR_COLUMNS", "read_factor_exposures", "refresh_factor_exposures", "FRENCH_ATTRIBUTION", "FakeReferenceFactorSource", "FrenchDataLibrarySource", "ImportResult",
    "ReferenceFactorError", "ReferenceFactorSource", "VintageInfo", "import_reference_factor_returns",
    "latest_vintage", "read_reference_factor_returns",
]
