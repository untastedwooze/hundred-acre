"""The History Limit: the earliest trading day Massive serves on our plan (ADR-0006, GLOSSARY).

One function computes it, for the Request Engine and for Coverage and the jobs.
"""

import os
import re
from datetime import UTC, date, datetime

DEFAULT_HISTORY_YEARS = 2


def history_years_from_env() -> int:
    """The configured span in years (`MASSIVE_HISTORY_YEARS`)."""
    return int(os.environ.get("MASSIVE_HISTORY_YEARS", DEFAULT_HISTORY_YEARS))


def history_limit(today: date, years: int | None = None) -> date:
    """`today` minus `years`, inclusive (that day itself is served). 29 February falls back to the 28th."""
    years = history_years_from_env() if years is None else years
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def utc_today(epoch_seconds: float) -> date:
    """The UTC date at a clock reading; never behind New York's date."""
    return datetime.fromtimestamp(epoch_seconds, UTC).date()


_RANGE = re.compile(r"^(?P<head>/v2/aggs/ticker/[^/]+/range/\d+/[a-z]+/)(?P<start>\d{4}-\d{2}-\d{2})/(?P<end>\d{4}-\d{2}-\d{2})$")
_GROUPED = re.compile(r"^/v2/aggs/grouped/locale/us/market/stocks/(?P<day>\d{4}-\d{2}-\d{2})$")


def is_bar_path(path: str) -> bool:
    return bool(_RANGE.match(path) or _GROUPED.match(path))


def clamp_bar_path(path: str, limit: date) -> str | None:
    """The path to send for a Bar endpoint with its window start moved up to `limit`; None when the whole window is
    before `limit`. Any other path is returned unchanged."""
    if m := _RANGE.match(path):
        if date.fromisoformat(m["end"]) < limit:
            return None
        if date.fromisoformat(m["start"]) < limit:
            return f"{m['head']}{limit.isoformat()}/{m['end']}"
    elif m := _GROUPED.match(path):
        if date.fromisoformat(m["day"]) < limit:
            return None
    return path
