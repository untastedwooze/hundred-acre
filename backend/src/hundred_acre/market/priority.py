"""Priority Classes: the one place their serving order is defined. Stored by name, so reordering needs no migration."""

from enum import StrEnum


class PriorityClass(StrEnum):
    INTERACTIVE = "interactive"
    NIGHTLY_WATCHLIST = "nightly_watchlist"
    NIGHTLY_DAILY = "nightly_daily"
    WEEKLY_UNIVERSE = "weekly_universe"
    BACKFILL = "backfill"


# Served first to last.
PRIORITY_ORDER: tuple[PriorityClass, ...] = (
    PriorityClass.INTERACTIVE,
    PriorityClass.NIGHTLY_WATCHLIST,
    PriorityClass.NIGHTLY_DAILY,
    PriorityClass.WEEKLY_UNIVERSE,
    PriorityClass.BACKFILL,
)
