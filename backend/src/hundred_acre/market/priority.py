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


def at_priority(source, priority: PriorityClass):
    """`source` at another Priority Class, if it has Priority Classes (the in-memory fake does not)."""
    return source.with_priority(priority) if hasattr(source, "with_priority") else source
