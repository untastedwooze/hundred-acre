"""The Request Engine: the only code that makes Calls to Massive (ADR-0003).

Callers enqueue Requests in Postgres (`enqueue`); the engine serves them first-come first-served, one Call per
page, inside a rate window computed from the persisted Calls log. It knows nothing about Bars or Coverage.
"""

import gzip
import hashlib
import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import Engine, text

from hundred_acre.market.massive import MassiveSource
from hundred_acre.market.priority import PRIORITY_ORDER, PriorityClass
from hundred_acre.market.source import MarketDataUnavailable

DEFAULT_CALLS_PER_MINUTE = 5
DEFAULT_MAX_CONCURRENT_CALLS = 1
WINDOW_SECONDS = 60.0
_BLOCKED_BY_OTHERS_RETRY = 1.0  # seconds to wait when Calls made elsewhere hold every concurrency slot


def identity_key(path: str, params: dict) -> str:
    return hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()


def enqueue(
    engine: Engine, path: str, params: dict, *, requester: str, now: float,
    priority: PriorityClass = PriorityClass.INTERACTIVE,
) -> int:
    """Put one Request on the queue at a Priority Class and return its id."""
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO requests (path, params, identity_key, requester, created_at, priority) "
                "VALUES (:path, CAST(:params AS jsonb), :key, :requester, :now, :priority) RETURNING id"
            ),
            {"path": path, "params": json.dumps(params), "key": identity_key(path, params),
             "requester": requester, "now": now, "priority": str(priority)},
        ).scalar_one()


def response_file(responses_dir: Path, request_id: int, page: int) -> Path:
    return responses_dir / str(request_id) / f"{page}.json.gz"


def read_pages(engine: Engine, responses_dir: Path, request_id: int) -> list:
    """The stored response bodies of a Request, in page order."""
    with engine.connect() as conn:
        paths = conn.execute(
            text("SELECT body_path FROM calls WHERE request_id = :r AND http_status = 200 ORDER BY page"),
            {"r": request_id},
        ).scalars().all()
    return [json.loads(gzip.decompress((responses_dir / p).read_bytes())) for p in paths]


class RequestEngine:
    def __init__(
        self, engine: Engine, massive: MassiveSource, responses_dir: Path, *,
        calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE, max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
    ):
        self._db, self._massive, self._dir = engine, massive, Path(responses_dir)
        self._per_minute, self._max_concurrent = calls_per_minute, max_concurrent_calls
        self._clock, self._sleep = clock, sleep
        with self._db.begin() as conn:  # Calls left in flight by a dead predecessor will never finish
            conn.execute(text(
                "UPDATE calls SET duration = 0, http_status = 0 WHERE duration IS NULL"))

    def __repr__(self) -> str:
        return "RequestEngine()"

    @classmethod
    def from_env(cls, engine: Engine, **kwargs) -> "RequestEngine":
        """Build from environment configuration only; raises RuntimeError naming what is missing."""
        responses = os.environ.get("MARKET_RESPONSES_DIR")
        if not responses:
            raise RuntimeError("MARKET_RESPONSES_DIR is not set")
        return cls(
            engine, MassiveSource.from_env(), Path(responses),
            calls_per_minute=int(os.environ.get("MASSIVE_CALLS_PER_MINUTE", DEFAULT_CALLS_PER_MINUTE)),
            max_concurrent_calls=int(os.environ.get("MASSIVE_MAX_CONCURRENT_CALLS", DEFAULT_MAX_CONCURRENT_CALLS)),
            **kwargs,
        )

    def serve(self, until: float | None = None) -> int:
        """Serve queued Requests until none are left, or until the clock reaches `until`. Returns Calls made."""
        made = 0
        while True:
            now = self._clock()
            self._heartbeat(now)
            if until is not None and now >= until:
                return made
            with self._db.connect() as conn:
                pending = conn.execute(text(
                    "SELECT id, path, params, next_cursor FROM requests "
                    "WHERE state IN ('queued', 'in_progress') "
                    "ORDER BY array_position(CAST(:order AS text[]), priority), id LIMIT :n"),
                    {"n": self._max_concurrent, "order": [str(p) for p in PRIORITY_ORDER]}).all()
                if not pending:
                    return made
                starts = conn.execute(text(
                    "SELECT started_at FROM calls WHERE started_at > :since ORDER BY started_at"),
                    {"since": now - WINDOW_SECONDS}).scalars().all()
                in_flight = conn.execute(text("SELECT count(*) FROM calls WHERE duration IS NULL")).scalar_one()
            slots = min(self._max_concurrent - in_flight, self._per_minute - len(starts))
            if slots <= 0:
                wait = (starts[0] + WINDOW_SECONDS - now) if len(starts) >= self._per_minute else _BLOCKED_BY_OTHERS_RETRY
                if until is not None and now + wait > until:
                    self._sleep(until - now)
                    return made
                self._sleep(wait)
                continue
            batch = pending[:slots]
            self._serve_batch(batch, now)
            made += len(batch)

    def _serve_batch(self, batch: list, now: float) -> None:
        call_ids = {}
        with self._db.begin() as conn:
            for r in batch:
                page = conn.execute(text(
                    "SELECT count(*) + 1 FROM calls WHERE request_id = :r AND http_status = 200"), {"r": r.id}).scalar_one()
                call_ids[r.id] = (conn.execute(text(
                    "INSERT INTO calls (request_id, page, started_at) VALUES (:r, :p, :now) RETURNING id"),
                    {"r": r.id, "p": page, "now": now}).scalar_one(), page)
                conn.execute(text("UPDATE requests SET state = 'in_progress' WHERE id = :r"), {"r": r.id})

        def one(r):
            params = dict(r.params)
            if r.next_cursor:
                params["cursor"] = r.next_cursor
            began = self._clock()
            try:
                return self._massive.call(r.path, params), None, None, self._clock() - began
            except MarketDataUnavailable as exc:
                return None, str(exc), getattr(exc, "status", None), self._clock() - began

        if len(batch) == 1:
            results = [one(batch[0])]
        else:
            with ThreadPoolExecutor(len(batch)) as pool:
                results = list(pool.map(one, batch))
        for r, (page_result, error, status, duration) in zip(batch, results):
            call_id, page = call_ids[r.id]
            self._record(r.id, call_id, page, page_result, error, status, duration)

    def _record(self, request_id, call_id, page, result, error, status, duration) -> None:
        body_path = size = None
        if result is not None:
            raw = json.dumps(result.body).encode()
            size = len(raw)
            file = response_file(self._dir, request_id, page)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(gzip.compress(raw))
            body_path = str(file.relative_to(self._dir))
        with self._db.begin() as conn:
            conn.execute(text(
                "UPDATE calls SET duration = :d, http_status = :s, response_bytes = :size, body_path = :p WHERE id = :id"),
                {"d": duration, "s": 200 if result is not None else (status or 0), "size": size, "p": body_path, "id": call_id})
            if result is None:
                conn.execute(text(
                    "UPDATE requests SET state = 'failed', last_error = :e, last_status = :s, finished_at = :now WHERE id = :r"),
                    {"e": error, "s": status, "now": self._clock(), "r": request_id})
            elif result.next_cursor:
                conn.execute(text("UPDATE requests SET next_cursor = :c WHERE id = :r"),
                             {"c": result.next_cursor, "r": request_id})
            else:
                conn.execute(text(
                    "UPDATE requests SET state = 'complete', next_cursor = NULL, finished_at = :now WHERE id = :r"),
                    {"now": self._clock(), "r": request_id})

    def _heartbeat(self, now: float) -> None:
        with self._db.begin() as conn:
            conn.execute(text("UPDATE request_engine_state SET heartbeat_at = :now WHERE id = 1"), {"now": now})
