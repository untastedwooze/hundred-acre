"""Real Massive (free REST tier) implementation of the MarketDataSource boundary.

The API key is sent in a header, read from configuration, and never appears in URLs, logs, reprs or errors.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timezone

from hundred_acre.market.source import (
    BarData, Dividend, MarketDataUnavailable, MarketHoliday, Split, TickerEvent, TickerRecord,
)

BASE_URL = "https://api.massive.com"

Transport = Callable[[str, dict, dict], dict]  # (path, params, headers) -> parsed JSON


class MassiveError(Exception):
    """A failed Massive request."""


def _http_transport(path: str, params: dict, headers: dict) -> dict:
    url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise MassiveError(f"HTTP {exc.code} from Massive {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MassiveError(f"Massive unreachable for {path}: {type(exc).__name__}") from None


def _bar(r: dict) -> BarData:
    return BarData(
        ticker=r.get("T", ""), open=r["o"], high=r["h"], low=r["l"], close=r["c"], volume=r["v"],
        vwap=r.get("vw"), trade_count=r.get("n"),
    )


def _date(value: str) -> date:
    return date.fromisoformat(value[:10])


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

    def _get(self, path: str, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            return self._transport(path, params or {}, headers)
        except MassiveError as exc:
            raise MarketDataUnavailable(str(exc).replace(self._api_key, "***")) from None

    def _paged(self, path: str, params: dict) -> list[dict]:
        out: list[dict] = []
        params = dict(params)
        while True:
            body = self._get(path, params)
            out.extend(body.get("results") or [])
            cursor = body.get("next_cursor")
            if not cursor:
                return out
            params["cursor"] = cursor

    def grouped_daily(self, day: date, *, adjusted: bool, include_otc: bool) -> list[BarData]:
        body = self._get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            {"adjusted": str(adjusted).lower(), "include_otc": str(include_otc).lower()},
        )
        return [_bar(r) for r in body.get("results") or []]

    def aggregates(self, ticker: str, start: date, end: date, *, adjusted: bool) -> list[tuple[date, BarData]]:
        body = self._get(
            f"/v2/aggs/ticker/{urllib.parse.quote(ticker)}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": str(adjusted).lower(), "sort": "asc", "limit": 50000},
        )
        return [
            (datetime.fromtimestamp(r["t"] / 1000, timezone.utc).date(), _bar({**r, "T": ticker}))
            for r in body.get("results") or []
        ]

    def tickers(self) -> list[TickerRecord]:
        out = []
        for active in ("true", "false"):
            for r in self._paged("/v3/reference/tickers", {"active": active, "limit": 1000}):
                out.append(TickerRecord(
                    ticker=r["ticker"], name=r.get("name"), type=r.get("type"), market=r.get("market", "stocks"),
                    share_class_figi=r.get("share_class_figi"), active=r.get("active", active == "true"),
                ))
        return out

    def ticker_events(self, ticker: str) -> list[TickerEvent]:
        body = self._get(f"/vX/reference/tickers/{urllib.parse.quote(ticker)}/events")
        events = (body.get("results") or {}).get("events") or []
        return [
            TickerEvent(ticker, _date(e["date"]), e.get("type", ""), (e.get("ticker_change") or {}).get("ticker"))
            for e in events
        ]

    def splits(self, since: date) -> list[Split]:
        rows = self._paged("/v3/reference/splits", {"execution_date.gte": since.isoformat(), "limit": 1000})
        return [Split(r["ticker"], _date(r["execution_date"]), r["split_from"], r["split_to"], r.get("id")) for r in rows]

    def dividends(self, since: date) -> list[Dividend]:
        rows = self._paged("/v3/reference/dividends", {"ex_dividend_date.gte": since.isoformat(), "limit": 1000})
        return [Dividend(r["ticker"], _date(r["ex_dividend_date"]), r["cash_amount"], r.get("id")) for r in rows]

    def upcoming_market_holidays(self) -> list[MarketHoliday]:
        body = self._get("/v1/marketstatus/upcoming")
        rows = body if isinstance(body, list) else body.get("results") or []
        return [MarketHoliday(_date(r["date"]), r.get("exchange", ""), r.get("name", ""), r.get("status", "")) for r in rows]
