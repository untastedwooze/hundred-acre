"""In-process rate limiter with request priority, placed in front of any MarketDataSource.

A future request engine replaces this behind the same `MarketDataSource` boundary; loaders are unaffected.
"""

import heapq
import itertools
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from datetime import date, datetime
from enum import IntEnum

from hundred_acre.market.source import (
    BarData, Dividend, MarketDataSource, MarketHoliday, Split, TickerEvent, TickerRecord,
)


class Priority(IntEnum):
    """Lower value is served first."""

    NIGHTLY_WATCHLIST = 0
    NIGHTLY_DAILY = 1
    WEEKLY_UNIVERSE = 2
    BACKFILL = 3


class RateLimiter:
    """Allows `per_minute` calls in any sliding 60-second window; queued calls run in priority order (FIFO within one)."""

    def __init__(
        self, per_minute: int = 5, *, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._queue: list[tuple[int, int, Callable, Future]] = []
        self._seq = itertools.count()
        self._queue_lock = threading.Lock()
        self._run_lock = threading.Lock()

    def submit(self, priority: Priority, fn: Callable) -> Future:
        future: Future = Future()
        with self._queue_lock:
            heapq.heappush(self._queue, (int(priority), next(self._seq), fn, future))
        return future

    def drain(self) -> None:
        """Run queued calls, highest priority first, waiting as needed for the rate limit."""
        with self._run_lock:
            while True:
                with self._queue_lock:
                    if not self._queue:
                        return
                self._wait_for_slot()
                with self._queue_lock:  # re-pick: a higher-priority call may have arrived while waiting
                    _, _, fn, future = heapq.heappop(self._queue)
                self._starts.append(self._clock())
                try:
                    future.set_result(fn())
                except BaseException as exc:
                    future.set_exception(exc)

    def call(self, priority: Priority, fn: Callable):
        future = self.submit(priority, fn)
        while not future.done():
            self.drain()
        return future.result()

    def _wait_for_slot(self) -> None:
        while True:
            now = self._clock()
            while self._starts and now - self._starts[0] >= 60:
                self._starts.popleft()
            if len(self._starts) < self._per_minute:
                return
            self._sleep(60 - (now - self._starts[0]))


class ThrottledSource:
    """A MarketDataSource whose every call goes through the limiter at a fixed priority."""

    def __init__(self, inner: MarketDataSource, limiter: RateLimiter, priority: Priority):
        self._inner, self._limiter, self._priority = inner, limiter, priority

    def with_priority(self, priority: Priority) -> "ThrottledSource":
        return ThrottledSource(self._inner, self._limiter, priority)

    def _do(self, fn: Callable, *args, **kwargs):
        return self._limiter.call(self._priority, lambda: fn(*args, **kwargs))

    def grouped_daily(self, day: date, *, adjusted: bool, include_otc: bool) -> list[BarData]:
        return self._do(self._inner.grouped_daily, day, adjusted=adjusted, include_otc=include_otc)

    def aggregates(self, ticker: str, start: date, end: date, *, adjusted: bool) -> list[tuple[date, BarData]]:
        return self._do(self._inner.aggregates, ticker, start, end, adjusted=adjusted)

    def intraday_aggregates(
        self, ticker: str, start: date, end: date, *, adjusted: bool
    ) -> list[tuple[datetime, BarData]]:
        return self._do(self._inner.intraday_aggregates, ticker, start, end, adjusted=adjusted)

    def tickers(self) -> list[TickerRecord]:
        return self._do(self._inner.tickers)

    def ticker_events(self, ticker: str) -> list[TickerEvent]:
        return self._do(self._inner.ticker_events, ticker)

    def splits(self, since: date) -> list[Split]:
        return self._do(self._inner.splits, since)

    def dividends(self, since: date) -> list[Dividend]:
        return self._do(self._inner.dividends, since)

    def upcoming_market_holidays(self) -> list[MarketHoliday]:
        return self._do(self._inner.upcoming_market_holidays)
