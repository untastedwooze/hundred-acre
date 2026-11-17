"""Trading Session tagging for Intraday Bars, decided at load time from the Trading Calendar.

New York clock: overnight 20:00-04:00, pre-market 04:00 to the open, regular from the open to the close
(13:00 on half days, as the calendar says), after-hours from the close to 20:00. A Bar starting at or after 20:00
leads into the next trading day, so it belongs to that day; so does any Bar on a non-trading day.
"""

import bisect
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl

NEW_YORK = ZoneInfo("America/New_York")
SESSIONS = ("overnight", "pre-market", "regular", "after-hours")
_PRE_MARKET_START = time(4, 0)
_AFTER_HOURS_END = time(20, 0)


def assign_sessions(starts: Sequence[datetime], calendar: pl.DataFrame) -> list[tuple[date, str] | None]:
    """(trading_date, Trading Session) for each Bar start (UTC), or None if the calendar has no day to put it in.

    `calendar` is `read_trading_calendar`'s output and must reach a few days past the last Bar.
    """
    days = calendar["trading_date"].to_list()
    opens = dict(zip(days, calendar["open"].to_list()))
    closes = dict(zip(days, calendar["close"].to_list()))
    out: list[tuple[date, str] | None] = []
    for start in starts:
        local = start.astimezone(NEW_YORK)
        candidate = local.date() + timedelta(days=1) if local.time() >= _AFTER_HOURS_END else local.date()
        i = bisect.bisect_left(days, candidate)
        if i == len(days):
            out.append(None)
            continue
        day = days[i]
        if day != local.date() or local.time() >= _AFTER_HOURS_END or local.time() < _PRE_MARKET_START:
            session = "overnight"
        elif start < opens[day]:
            session = "pre-market"
        elif start < closes[day]:
            session = "regular"
        else:
            session = "after-hours"
        out.append((day, session))
    return out
