from datetime import date

from hundred_acre.market.ratelimit import Priority, RateLimiter, ThrottledSource
from hundred_acre.market.source import FakeMarketDataSource


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(per_minute=5):
    clock = FakeClock()
    return clock, RateLimiter(per_minute=per_minute, clock=clock, sleep=clock.sleep)


def test_calls_are_limited_to_five_per_minute():
    clock, limiter = make()
    source = ThrottledSource(FakeMarketDataSource(), limiter, Priority.NIGHTLY_DAILY)
    stamps = []
    for _ in range(11):
        source.tickers()
        stamps.append(clock.now)
    assert stamps[4] - stamps[0] < 60
    assert stamps[5] - stamps[0] >= 60
    assert stamps[10] - stamps[0] >= 120


def test_higher_priority_queued_requests_are_served_first():
    clock, limiter = make(per_minute=1)
    order = []
    limiter.call(Priority.BACKFILL, lambda: order.append("warmup"))  # uses the one slot
    futures = [
        limiter.submit(Priority.BACKFILL, lambda: order.append("backfill")),
        limiter.submit(Priority.WEEKLY_UNIVERSE, lambda: order.append("weekly")),
        limiter.submit(Priority.NIGHTLY_DAILY, lambda: order.append("daily")),
        limiter.submit(Priority.NIGHTLY_WATCHLIST, lambda: order.append("watchlist")),
    ]
    limiter.drain()
    assert all(f.done() for f in futures)
    assert order == ["warmup", "watchlist", "daily", "weekly", "backfill"]


def test_throttled_source_returns_inner_results():
    source = ThrottledSource(
        FakeMarketDataSource(), RateLimiter(clock=FakeClock(), sleep=lambda s: None), Priority.BACKFILL
    )
    assert source.splits(date(2020, 1, 1)) == []
