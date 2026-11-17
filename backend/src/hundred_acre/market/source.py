"""The market-data source boundary (Massive in production) and an in-memory fake for tests.

Tests fill `FakeMarketDataSource` with scenarios, like `FakeFeatureSource` for GitHub.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol


class MarketDataUnavailable(Exception):
    """The market-data source cannot be reached."""


@dataclass(frozen=True)
class BarData:
    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    trade_count: int | None
    otc: bool = False  # the source flags over-the-counter tickers


@dataclass(frozen=True)
class TickerRecord:
    ticker: str
    name: str | None
    type: str | None  # CS, ETF, ADRC, PFD, WARRANT, UNIT, RIGHT, ...
    market: str  # "stocks", "otc", ...
    share_class_figi: str | None
    active: bool = True
    cik: str | None = None  # Issuer
    list_date: date | None = None  # first day trading
    delisted_date: date | None = None  # last day trading


@dataclass(frozen=True)
class TickerEvent:
    ticker: str  # the Ticker before the change
    date: date  # first day the Security traded as new_ticker
    type: str  # e.g. "ticker_change"
    new_ticker: str | None = None


@dataclass(frozen=True)
class Split:
    ticker: str
    execution_date: date
    split_from: float
    split_to: float
    id: str | None = None  # Massive's record id; a revised record keeps its id


@dataclass(frozen=True)
class Dividend:
    ticker: str
    ex_dividend_date: date
    cash_amount: float
    id: str | None = None  # Massive's record id; a revised record keeps its id


@dataclass(frozen=True)
class MarketHoliday:
    date: date
    exchange: str
    name: str
    status: str  # "closed" | "early-close"


class MarketDataSource(Protocol):
    def grouped_daily(self, day: date, *, adjusted: bool, include_otc: bool) -> list[BarData]:
        """Every ticker's Bar for `day`. Raises MarketDataUnavailable if unreachable."""
        ...

    def aggregates(self, ticker: str, start: date, end: date, *, adjusted: bool) -> list[tuple[date, BarData]]:
        """One ticker's Daily Bars over a date range."""
        ...

    def tickers(self) -> list[TickerRecord]:
        """Tickers reference data."""
        ...

    def ticker_events(self, ticker: str) -> list[TickerEvent]: ...

    def splits(self, since: date) -> list[Split]: ...

    def dividends(self, since: date) -> list[Dividend]: ...

    def upcoming_market_holidays(self) -> list[MarketHoliday]: ...


@dataclass
class FakeMarketDataSource:
    """In-memory MarketDataSource for tests. Set `available = False` to simulate an outage.

    `grouped_daily_bars` holds the Unadjusted Bars per day; `adjusted_grouped_daily_bars` is what the
    source would answer if (wrongly) asked for adjusted data, so tests can tell which was requested.
    """

    tickers_: list[TickerRecord] = field(default_factory=list)
    grouped_daily_bars: dict[date, list[BarData]] = field(default_factory=dict)
    adjusted_grouped_daily_bars: dict[date, list[BarData]] = field(default_factory=dict)
    aggregate_bars: dict[str, list[tuple[date, BarData]]] = field(default_factory=dict)
    events: dict[str, list[TickerEvent]] = field(default_factory=dict)
    split_list: list[Split] = field(default_factory=list)
    dividend_list: list[Dividend] = field(default_factory=list)
    holidays: list[MarketHoliday] = field(default_factory=list)
    available: bool = True
    failing_tickers: set[str] = field(default_factory=set)  # `aggregates` raises MarketDataUnavailable for these
    crash_after_aggregate_calls: int | None = None  # `aggregates` raises RuntimeError once this many calls were made
    aggregate_calls: list[tuple[str, date, date]] = field(default_factory=list)  # (ticker, start, end) per call

    def _check(self) -> None:
        if not self.available:
            raise MarketDataUnavailable("fake source is unavailable")

    def grouped_daily(self, day: date, *, adjusted: bool, include_otc: bool) -> list[BarData]:
        self._check()
        table = self.adjusted_grouped_daily_bars if adjusted else self.grouped_daily_bars
        return [b for b in table.get(day, []) if include_otc or not b.otc]

    def aggregates(self, ticker: str, start: date, end: date, *, adjusted: bool) -> list[tuple[date, BarData]]:
        self._check()
        if self.crash_after_aggregate_calls is not None and len(self.aggregate_calls) >= self.crash_after_aggregate_calls:
            raise RuntimeError("fake source crashed")
        self.aggregate_calls.append((ticker, start, end))
        if ticker in self.failing_tickers:
            raise MarketDataUnavailable(f"fake source failed for {ticker}")
        return [(d, b) for d, b in self.aggregate_bars.get(ticker, []) if start <= d <= end]

    def tickers(self) -> list[TickerRecord]:
        self._check()
        return list(self.tickers_)

    def ticker_events(self, ticker: str) -> list[TickerEvent]:
        self._check()
        return list(self.events.get(ticker, []))

    def splits(self, since: date) -> list[Split]:
        self._check()
        return [s for s in self.split_list if s.execution_date >= since]

    def dividends(self, since: date) -> list[Dividend]:
        self._check()
        return [d for d in self.dividend_list if d.ex_dividend_date >= since]

    def upcoming_market_holidays(self) -> list[MarketHoliday]:
        self._check()
        return list(self.holidays)
