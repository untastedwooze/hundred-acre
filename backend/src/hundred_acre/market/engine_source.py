"""MarketDataSource that answers every method through the Request Engine's Postgres queue (ADR-0003).

Each method enqueues one or more Requests, polls until they are complete, and parses the stored pages with the
same parsing the Massive client uses.
"""

import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Engine, text

from hundred_acre.market import massive as m
from hundred_acre.market.request_engine import check_engine, enqueue, read_pages
from hundred_acre.market.source import (
    BarData, Dividend, MarketDataUnavailable, MarketHoliday, Split, TickerEvent, TickerRecord,
)

DEFAULT_POLL_INTERVAL = 3.0


class EngineSource:
    def __init__(
        self, engine: Engine, responses_dir: Path, *, requester: str = "market",
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        self._db, self._dir, self._requester = engine, Path(responses_dir), requester
        self._clock, self._sleep, self._poll = clock, sleep, poll_interval

    def __repr__(self) -> str:
        return "EngineSource()"

    def _fetch(self, requests: list[tuple[str, dict]]) -> list[list]:
        """Enqueue the Requests, wait for all of them, and return each one's stored pages."""
        check_engine(self._db, self._clock())
        ids = [enqueue(self._db, path, params, requester=self._requester, now=self._clock()) for path, params in requests]
        while True:
            with self._db.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, state, last_error FROM requests WHERE id = ANY(:ids)"), {"ids": ids}).all()
            for row in rows:
                if row.state == "failed":
                    raise MarketDataUnavailable(row.last_error or "request failed")
            if all(row.state == "complete" for row in rows):
                return [read_pages(self._db, self._dir, i) for i in ids]
            check_engine(self._db, self._clock())
            self._sleep(self._poll)

    def _one(self, request: tuple[str, dict]) -> list:
        return self._fetch([request])[0]

    def grouped_daily(self, day: date, *, adjusted: bool, include_otc: bool) -> list[BarData]:
        return m.parse_bars(self._one(m.grouped_daily_request(day, adjusted=adjusted, include_otc=include_otc)))

    def aggregates(self, ticker: str, start: date, end: date, *, adjusted: bool) -> list[tuple[date, BarData]]:
        return m.parse_aggregates(self._one(m.aggregates_request(ticker, start, end, adjusted=adjusted)), ticker)

    def intraday_aggregates(
        self, ticker: str, start: date, end: date, *, adjusted: bool
    ) -> list[tuple[datetime, BarData]]:
        per_request = self._fetch(m.intraday_requests(ticker, start, end, adjusted=adjusted))
        return m.parse_intraday_aggregates([p for pages in per_request for p in pages], ticker)

    def tickers(self) -> list[TickerRecord]:
        active, inactive = self._fetch([m.tickers_request(active=True), m.tickers_request(active=False)])
        return m.parse_tickers(active, active=True) + m.parse_tickers(inactive, active=False)

    def ticker_events(self, ticker: str) -> list[TickerEvent]:
        return m.parse_ticker_events(self._one(m.ticker_events_request(ticker)), ticker)

    def splits(self, since: date) -> list[Split]:
        return m.parse_splits(self._one(m.splits_request(since)))

    def dividends(self, since: date) -> list[Dividend]:
        return m.parse_dividends(self._one(m.dividends_request(since)))

    def upcoming_market_holidays(self) -> list[MarketHoliday]:
        return m.parse_market_holidays(self._one(m.market_holidays_request()))
