"""Real Massive (free REST tier) implementation of the MarketDataSource boundary.

The API key is sent in a header, read from configuration, and never appears in URLs, logs, reprs or errors.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from hundred_acre.market.source import (
    BarData, Dividend, MarketDataUnavailable, MarketHoliday, Split, TickerEvent, TickerRecord,
)

BASE_URL = "https://api.massive.com"

_INTRADAY_CHUNK_DAYS = 120  # ~85 trading days x <= 288 five-minute Bars stays under the 50000 limit

Transport = Callable[[str, dict, dict], dict]  # (path, params, headers) -> parsed JSON


class MassiveError(Exception):
    """A failed Massive request."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status, when Massive answered


def _http_transport(path: str, params: dict, headers: dict) -> dict:
    url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise MassiveError(f"HTTP {exc.code} from Massive {path}", exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MassiveError(f"Massive unreachable for {path}: {type(exc).__name__}") from None


def _bar(r: dict) -> BarData:
    return BarData(
        ticker=r.get("T", ""), open=r["o"], high=r["h"], low=r["l"], close=r["c"], volume=r["v"],
        vwap=r.get("vw"), trade_count=r.get("n"),
    )


def _date(value: str) -> date:
    return date.fromisoformat(value[:10])


Pages = list  # raw response bodies (parsed JSON), in page order


@dataclass(frozen=True)
class Page:
    """One Massive response: its body and the cursor for the next page, if any."""

    body: dict | list
    next_cursor: str | None = None


def _results(pages: Pages) -> list[dict]:
    out: list[dict] = []
    for body in pages:
        out.extend(body.get("results") or [])
    return out


def parse_bars(pages: Pages) -> list[BarData]:
    return [_bar(r) for r in _results(pages)]


def parse_aggregates(pages: Pages, ticker: str) -> list[tuple[date, BarData]]:
    return [
        (datetime.fromtimestamp(r["t"] / 1000, timezone.utc).date(), _bar({**r, "T": ticker}))
        for r in _results(pages)
    ]


def parse_intraday_aggregates(pages: Pages, ticker: str) -> list[tuple[datetime, BarData]]:
    return [
        (datetime.fromtimestamp(r["t"] / 1000, timezone.utc), _bar({**r, "T": ticker}))
        for r in _results(pages)
    ]


def parse_tickers(pages: Pages, *, active: bool) -> list[TickerRecord]:
    return [
        TickerRecord(
            ticker=r["ticker"], name=r.get("name"), type=r.get("type"), market=r.get("market", "stocks"),
            share_class_figi=r.get("share_class_figi"), active=r.get("active", active),
            cik=r.get("cik"), delisted_date=_date(r["delisted_utc"]) if r.get("delisted_utc") else None,
        )
        for r in _results(pages)
    ]


def parse_ticker_events(pages: Pages, ticker: str) -> list[TickerEvent]:
    events: list[dict] = []
    for body in pages:
        events.extend((body.get("results") or {}).get("events") or [])
    return [
        TickerEvent((e.get("ticker_change") or {}).get("ticker") or "", _date(e["date"]), e.get("type", ""))
        for e in events
    ]


def parse_splits(pages: Pages) -> list[Split]:
    return [
        Split(r["ticker"], _date(r["execution_date"]), r["split_from"], r["split_to"], r.get("id"))
        for r in _results(pages)
    ]


def parse_dividends(pages: Pages) -> list[Dividend]:
    return [
        Dividend(r["ticker"], _date(r["ex_dividend_date"]), r["cash_amount"], r.get("id"), r.get("dividend_type"))
        for r in _results(pages)
    ]


def parse_market_holidays(pages: Pages) -> list[MarketHoliday]:
    rows: list[dict] = []
    for body in pages:
        rows.extend(body if isinstance(body, list) else body.get("results") or [])
    return [MarketHoliday(_date(r["date"]), r.get("exchange", ""), r.get("name", ""), r.get("status", "")) for r in rows]


def _flag(value: bool) -> str:
    return str(value).lower()


def grouped_daily_request(day: date, *, adjusted: bool, include_otc: bool) -> tuple[str, dict]:
    return (f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            {"adjusted": _flag(adjusted), "include_otc": _flag(include_otc)})


def aggregates_request(ticker: str, start: date, end: date, *, adjusted: bool) -> tuple[str, dict]:
    return (f"/v2/aggs/ticker/{urllib.parse.quote(ticker)}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": _flag(adjusted), "sort": "asc", "limit": 50000})


def intraday_requests(ticker: str, start: date, end: date, *, adjusted: bool) -> list[tuple[str, dict]]:
    """Five-minute Bars; ranges are split so each Request stays under Massive's 50000-result limit."""
    out: list[tuple[str, dict]] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=_INTRADAY_CHUNK_DAYS - 1), end)
        out.append((
            f"/v2/aggs/ticker/{urllib.parse.quote(ticker)}/range/5/minute/{chunk_start.isoformat()}/{chunk_end.isoformat()}",
            {"adjusted": _flag(adjusted), "sort": "asc", "limit": 50000},
        ))
        chunk_start = chunk_end + timedelta(days=1)
    return out


def tickers_request(*, active: bool) -> tuple[str, dict]:
    return "/v3/reference/tickers", {"active": _flag(active), "market": "stocks", "limit": 1000}


def ticker_events_request(ticker: str) -> tuple[str, dict]:
    return f"/vX/reference/tickers/{urllib.parse.quote(ticker)}/events", {}


def splits_request(since: date) -> tuple[str, dict]:
    return "/v3/reference/splits", {"execution_date.gte": since.isoformat(), "limit": 1000}


def dividends_request(since: date) -> tuple[str, dict]:
    return "/v3/reference/dividends", {"ex_dividend_date.gte": since.isoformat(), "limit": 1000}


def market_holidays_request() -> tuple[str, dict]:
    return "/v1/marketstatus/upcoming", {}


def _next_cursor(body: object) -> str | None:
    """The cursor in a response's top-level `next_url`, or None when there is no further page."""
    url = body.get("next_url") if isinstance(body, dict) else None
    if not isinstance(url, str):
        return None
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("cursor", [None])[0]


class MassiveSource:
    def __init__(self, api_key: str, *, transport: Transport = _http_transport):
        self._api_key = api_key
        self._transport = transport

    @classmethod
    def from_env(cls) -> "MassiveSource":
        key = os.environ.get("MASSIVE_API_KEY")
        if not key:
            raise RuntimeError("MASSIVE_API_KEY is not set")
        return cls(key)

    def __repr__(self) -> str:
        return "MassiveSource()"

    def call(self, path: str, params: dict | None = None) -> Page:
        """Make exactly one Call and return its page, including the next cursor (if any)."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            body = self._transport(path, params or {}, headers)
        except MassiveError as exc:
            unavailable = MarketDataUnavailable(str(exc).replace(self._api_key, "***"))
            unavailable.status = exc.status  # type: ignore[attr-defined]
            raise unavailable from None
        return Page(body, _next_cursor(body))

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self.call(path, params).body

    def _paged(self, path: str, params: dict) -> Pages:
        pages: Pages = []
        params = dict(params)
        while True:
            page = self.call(path, params)
            pages.append(page.body)
            if not page.next_cursor:
                return pages
            params["cursor"] = page.next_cursor

    def grouped_daily(self, day: date, *, adjusted: bool, include_otc: bool) -> list[BarData]:
        return parse_bars([self._get(*grouped_daily_request(day, adjusted=adjusted, include_otc=include_otc))])

    def aggregates(self, ticker: str, start: date, end: date, *, adjusted: bool) -> list[tuple[date, BarData]]:
        return parse_aggregates([self._get(*aggregates_request(ticker, start, end, adjusted=adjusted))], ticker)

    def intraday_aggregates(
        self, ticker: str, start: date, end: date, *, adjusted: bool
    ) -> list[tuple[datetime, BarData]]:
        pages = [self._get(path, params) for path, params in intraday_requests(ticker, start, end, adjusted=adjusted)]
        return parse_intraday_aggregates(pages, ticker)

    def tickers(self) -> list[TickerRecord]:
        out: list[TickerRecord] = []
        for active in (True, False):
            out.extend(parse_tickers(self._paged(*tickers_request(active=active)), active=active))
        return out

    def ticker_events(self, ticker: str) -> list[TickerEvent]:
        try:
            return parse_ticker_events([self._get(*ticker_events_request(ticker))], ticker)
        except MarketDataUnavailable as exc:
            if getattr(exc, "status", None) == 404:  # no events record for this ticker
                return []
            raise

    def splits(self, since: date) -> list[Split]:
        return parse_splits(self._paged(*splits_request(since)))

    def dividends(self, since: date) -> list[Dividend]:
        return parse_dividends(self._paged(*dividends_request(since)))

    def upcoming_market_holidays(self) -> list[MarketHoliday]:
        return parse_market_holidays([self._get(*market_holidays_request())])
