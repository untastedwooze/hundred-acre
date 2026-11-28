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
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import Engine, text

from hundred_acre.market.history_limit import (
    DEFAULT_HISTORY_YEARS, clamp_bar_path, history_limit, history_years_from_env, is_bar_path, utc_today,
)
from hundred_acre.market.massive import MassiveSource
from hundred_acre.market.priority import PRIORITY_ORDER, PriorityClass
from hundred_acre.market.source import MarketDataUnavailable

DEFAULT_CALLS_PER_MINUTE = 5
DEFAULT_MAX_CONCURRENT_CALLS = 1
WINDOW_SECONDS = 60.0
HEARTBEAT_INTERVAL = 10.0
STALE_AFTER = 60.0  # a heartbeat older than this means the engine is not running
ADVISORY_LOCK_KEY = 0x48554E44  # "HUND": held for as long as a Request Engine runs
RESPONSE_RETENTION_DAYS = 90
PRUNE_INTERVAL = 3600.0
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0  # a Request's nth failed attempt waits base * 2**(n-1) before the next
RATE_LIMITED_PAUSE_SECONDS = 60.0
_BLOCKED_BY_OTHERS_RETRY = 1.0  # seconds to wait when Calls made elsewhere hold every concurrency slot


class EngineAlreadyRunning(RuntimeError):
    pass


def check_engine(engine: Engine, now: float) -> None:
    """Raise MarketDataUnavailable unless a Request Engine is alive and not halted."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT state, halt_reason, heartbeat_at FROM request_engine_state WHERE id = 1")).one()
    if row.state == "halted":
        raise MarketDataUnavailable(f"market data unavailable: request engine halted: {row.halt_reason}")
    if row.heartbeat_at is None or now - row.heartbeat_at > STALE_AFTER:
        raise MarketDataUnavailable("market data unavailable: request engine is not running")


def identity_key(path: str, params: dict) -> str:
    return hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()


LEASE_SECONDS = 60.0  # a waiter's lease; it must be renewed before it lapses
LEASE_RENEW_INTERVAL = 30.0
NOTIFY_CHANNEL = "request_done"


def enqueue_as_waiter(
    engine: Engine, path: str, params: dict, *, requester: str, now: float,
    priority: PriorityClass = PriorityClass.INTERACTIVE, wait: bool = True,
) -> tuple[int, int | None]:
    """Ask for a Request; returns (request id, waiter id).

    An identical Request that is queued or in progress is joined instead of duplicated, and an abandoned one is
    revived (queued, or in progress from its stored cursor). The Request runs at the highest Priority Class among
    its live waiters. With `wait=False` no waiter is registered: the Request is pinned at that Priority Class and is
    never abandoned. At most one non-terminal Request exists per identity (a unique index), so a plain enqueue joins too.
    """
    key = identity_key(path, params)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
        while True:
            row = conn.execute(text(
                "SELECT id, priority, state, next_cursor FROM requests "
                "WHERE identity_key = :k AND state IN ('queued', 'in_progress', 'abandoned') "
                "ORDER BY id LIMIT 1"), {"k": key}).one_or_none()
            if row is None:
                request_id = conn.execute(
                    text(
                        "INSERT INTO requests (path, params, identity_key, requester, created_at, priority, pinned_priority) "
                        "VALUES (:path, CAST(:params AS jsonb), :key, :requester, :now, :priority, :pinned) RETURNING id"
                    ),
                    {"path": path, "params": json.dumps(params), "key": key, "requester": requester, "now": now,
                     "priority": str(priority), "pinned": None if wait else str(priority)},
                ).scalar_one()
                break
            best = min(PriorityClass(row.priority), priority, key=PRIORITY_ORDER.index)
            state = row.state
            if state == "abandoned":
                state = "in_progress" if row.next_cursor else "queued"
                best = priority  # the old waiters are gone; only the reviver's urgency counts
            # Guarded: an engine completion that landed since the read must not be resurrected.
            joined = conn.execute(text(
                "UPDATE requests SET priority = :p, state = :s, retry_at = NULL, "
                "pinned_priority = CASE WHEN :pin THEN "
                "  (SELECT p FROM unnest(ARRAY[pinned_priority, CAST(:mine AS text)]) AS p WHERE p IS NOT NULL "
                "   ORDER BY array_position(CAST(:order AS text[]), p) LIMIT 1) ELSE pinned_priority END "
                "WHERE id = :r AND state IN ('queued', 'in_progress', 'abandoned')"),
                {"p": str(best), "s": state, "r": row.id, "pin": not wait, "mine": str(priority),
                 "order": [str(p) for p in PRIORITY_ORDER]}).rowcount
            if joined:
                request_id = row.id
                break
        waiter_id = None
        if wait:
            waiter_id = conn.execute(text(
                "INSERT INTO request_waiters (request_id, priority, lease_expires_at) "
                "VALUES (:r, :p, :exp) RETURNING id"),
                {"r": request_id, "p": str(priority), "exp": now + LEASE_SECONDS}).scalar_one()
        return request_id, waiter_id


def enqueue(
    engine: Engine, path: str, params: dict, *, requester: str, now: float,
    priority: PriorityClass = PriorityClass.INTERACTIVE, wait: bool = False,
) -> int:
    """Put one Request on the queue at a Priority Class (joining an identical pending one) and return its id."""
    return enqueue_as_waiter(engine, path, params, requester=requester, now=now, priority=priority, wait=wait)[0]


def renew_leases(engine: Engine, waiter_ids: list[int], now: float) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE request_waiters SET lease_expires_at = :e WHERE id = ANY(:ids)"),
                     {"e": now + LEASE_SECONDS, "ids": waiter_ids})


def release_waiters(engine: Engine, waiter_ids: list[int]) -> None:
    """A waiter gives up: its lease expires now. The row stays, so the engine sees a Request that lost all its waiters."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE request_waiters SET lease_expires_at = 0 WHERE id = ANY(:ids)"), {"ids": waiter_ids})


def wait_for_notification(engine: Engine, timeout: float) -> None:
    """Block until a Request finishes (Postgres NOTIFY) or `timeout` seconds pass, whichever is first."""
    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        conn.autocommit = True
        conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
        for _ in conn.notifies(timeout=timeout, stop_after=1):
            break
        conn.execute("UNLISTEN *")
        conn.autocommit = False
    except BaseException:
        raw.invalidate()  # its state is unknown: do not hand it back to the pool
        raise
    finally:
        raw.close()


def resume(engine: Engine) -> None:
    """Clear a halt. Needs only the database; queued and in-progress Requests continue when an engine serves."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE request_engine_state SET state = 'running', halt_reason = NULL WHERE id = 1"))


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
        history_years: int = DEFAULT_HISTORY_YEARS,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
    ):
        self._db, self._massive, self._dir = engine, massive, Path(responses_dir)
        self._per_minute, self._max_concurrent = calls_per_minute, max_concurrent_calls
        self._history_years = history_years
        self._clock, self._sleep = clock, sleep
        self._lock_conn = None
        self._last_beat: float | None = None

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
            history_years=history_years_from_env(),
            **kwargs,
        )

    def acquire_lock(self) -> None:
        """Take the Postgres advisory lock that makes this the only running engine; raises EngineAlreadyRunning."""
        conn = self._db.connect()
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}).scalar_one():
            conn.close()
            raise EngineAlreadyRunning("a Request Engine is already running against this database")
        self._lock_conn = conn
        self.start()

    def start(self) -> None:
        """Take over from a dead predecessor. Only the lock holder may call this: it writes to shared state."""
        with self._db.begin() as conn:  # Calls left in flight by a dead predecessor will never finish
            conn.execute(text("UPDATE calls SET duration = 0, http_status = 0 WHERE duration IS NULL"))
        self._heartbeat(self._clock())

    def release_lock(self) -> None:
        if self._lock_conn is not None:
            self._lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            self._lock_conn.close()
            self._lock_conn = None

    def _sleep_beating(self, seconds: float) -> None:
        """Sleep, but never so long that the heartbeat goes quiet."""
        while seconds > 0:
            step = min(seconds, HEARTBEAT_INTERVAL)
            self._sleep(step)
            seconds -= step
            self._heartbeat(self._clock())

    def serve(self, until: float | None = None) -> int:
        """Serve queued Requests until none are left, or until the clock reaches `until`. Returns Calls made."""
        made = 0
        while True:
            now = self._clock()
            self._heartbeat(now)
            if until is not None and now >= until:
                return made
            self._abandon_unwanted(now)
            with self._db.connect() as conn:
                state = conn.execute(text("SELECT state, paused_until FROM request_engine_state WHERE id = 1")).one()
                if state.state == "halted":
                    return made
                if state.paused_until is not None and state.paused_until > now:
                    if self._wait(state.paused_until - now, now, until):
                        return made
                    continue
                pending = conn.execute(text(
                    "SELECT id, path, params, next_cursor FROM requests "
                    "WHERE state IN ('queued', 'in_progress') AND (retry_at IS NULL OR retry_at <= :now) "
                    "ORDER BY array_position(CAST(:order AS text[]), priority), id LIMIT :n"),
                    {"n": self._max_concurrent, "now": now, "order": [str(p) for p in PRIORITY_ORDER]}).all()
                if not pending:
                    next_retry = conn.execute(text(
                        "SELECT min(retry_at) FROM requests WHERE state IN ('queued', 'in_progress')")).scalar()
                    if next_retry is None:
                        return made
                    if self._wait(next_retry - now, now, until):
                        return made
                    continue
                skipped = [r for r in pending if self._sent_path(r.path, now) is None]
                if skipped:
                    self._complete_without_call(skipped, now)
                    continue
                starts = conn.execute(text(
                    "SELECT started_at FROM calls WHERE started_at > :since ORDER BY started_at"),
                    {"since": now - WINDOW_SECONDS}).scalars().all()
                in_flight = conn.execute(text("SELECT count(*) FROM calls WHERE duration IS NULL")).scalar_one()
            slots = min(self._max_concurrent - in_flight, self._per_minute - len(starts))
            if slots <= 0:
                wait = (starts[0] + WINDOW_SECONDS - now) if len(starts) >= self._per_minute else _BLOCKED_BY_OTHERS_RETRY
                if until is not None and now + wait > until:
                    self._sleep_beating(until - now)
                    return made
                self._sleep_beating(wait)
                continue
            batch = pending[:slots]
            self._serve_batch(batch, now)
            made += len(batch)

    def _abandon_unwanted(self, now: float) -> None:
        """Abandon unfinished Requests whose waiters all let their leases lapse (and that nothing pins), and set every
        other unfinished Request to the highest Priority Class among its live waiters and its pin."""
        order = [str(p) for p in PRIORITY_ORDER]
        with self._db.begin() as conn:
            conn.execute(text(
                "UPDATE requests r SET state = 'abandoned' WHERE r.state IN ('queued', 'in_progress') "
                "AND r.pinned_priority IS NULL "
                "AND EXISTS (SELECT 1 FROM request_waiters w WHERE w.request_id = r.id) "
                "AND NOT EXISTS (SELECT 1 FROM request_waiters w WHERE w.request_id = r.id AND w.lease_expires_at > :now)"),
                {"now": now})
            conn.execute(text(
                "UPDATE requests r SET priority = best.p FROM ("
                "  SELECT q.id, (SELECT c FROM ("
                "      SELECT w.priority AS c FROM request_waiters w WHERE w.request_id = q.id AND w.lease_expires_at > :now"
                "      UNION ALL SELECT q.pinned_priority WHERE q.pinned_priority IS NOT NULL) x"
                "    ORDER BY array_position(CAST(:order AS text[]), c) LIMIT 1) AS p"
                "  FROM requests q WHERE q.state IN ('queued', 'in_progress')) best "
                "WHERE r.id = best.id AND best.p IS NOT NULL AND r.priority <> best.p"),
                {"now": now, "order": order})

    def _wait(self, seconds: float, now: float, until: float | None) -> bool:
        """Sleep `seconds` (at most until `until`); True when `until` was reached and serving should stop."""
        if until is not None and now + seconds > until:
            self._sleep_beating(max(until - now, 0))
            return True
        self._sleep_beating(seconds)
        return False

    def _limit(self, now: float) -> date:
        return history_limit(utc_today(now), self._history_years)

    def _sent_path(self, path: str, now: float) -> str | None:
        """The path to send as of `now` (a Bar window moved up to the History Limit); None when there is nothing to ask."""
        return clamp_bar_path(path, self._limit(now))

    def _complete_without_call(self, requests: list, now: float) -> None:
        """A Bar window wholly before the History Limit has nothing to fetch: complete it empty, with no Call."""
        note = f"made no Call: the whole window is before the History Limit {self._limit(now).isoformat()}"
        with self._db.begin() as conn:
            for r in requests:
                conn.execute(text(
                    "UPDATE requests SET state = 'complete', next_cursor = NULL, note = :n, finished_at = :now WHERE id = :r"),
                    {"n": note, "now": now, "r": r.id})
                conn.execute(text("SELECT pg_notify(:c, :r)"), {"c": NOTIFY_CHANNEL, "r": str(r.id)})

    def _serve_batch(self, batch: list, now: float) -> None:
        call_ids = {}
        limit = self._limit(now)
        sent = {r.id: self._sent_path(r.path, now) for r in batch}
        with self._db.begin() as conn:
            for r in batch:
                page = conn.execute(text(
                    "SELECT count(*) + 1 FROM calls WHERE request_id = :r AND http_status = 200"), {"r": r.id}).scalar_one()
                call_ids[r.id] = (conn.execute(text(
                    "INSERT INTO calls (request_id, page, started_at, sent_path, history_limit) "
                    "VALUES (:r, :p, :now, :sp, :hl) RETURNING id"),
                    {"r": r.id, "p": page, "now": now, "sp": sent[r.id],
                     "hl": limit if is_bar_path(r.path) else None}).scalar_one(), page)
                conn.execute(text("UPDATE requests SET state = 'in_progress' WHERE id = :r"), {"r": r.id})

        def one(r):
            params = dict(r.params)
            if r.next_cursor:
                params["cursor"] = r.next_cursor
            began = self._clock()
            try:
                return self._massive.call(sent[r.id], params), None, None, self._clock() - began
            except MarketDataUnavailable as exc:
                return None, str(exc), getattr(exc, "status", None), self._clock() - began
            except Exception as exc:  # e.g. a 200 with a body that is not JSON: a failed attempt, never an engine crash
                return None, f"{type(exc).__name__}: {exc}", None, self._clock() - began

        if len(batch) == 1:
            results = [one(batch[0])]
        else:
            with ThreadPoolExecutor(len(batch)) as pool:
                results = list(pool.map(one, batch))
        for r, (page_result, error, status, duration) in zip(batch, results):
            call_id, page = call_ids[r.id]
            self._record(r.id, call_id, page, page_result, error, status, duration,
                         window=(sent[r.id], limit) if is_bar_path(r.path) else None)

    def _record(self, request_id, call_id, page, result, error, status, duration, window=None) -> None:
        body_path = size = None
        if result is not None:
            try:
                raw = json.dumps(result.body).encode()
                file = response_file(self._dir, request_id, page)
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(gzip.compress(raw))
                body_path, size = str(file.relative_to(self._dir)), len(raw)
            except (OSError, TypeError, ValueError) as exc:  # e.g. disk full: this attempt failed, the engine lives
                result, error, status = None, f"could not store the response: {type(exc).__name__}: {exc}", None
        with self._db.begin() as conn:
            conn.execute(text(
                "UPDATE calls SET duration = :d, http_status = :s, response_bytes = :size, body_path = :p WHERE id = :id"),
                {"d": duration, "s": 200 if result is not None else (status or 0), "size": size, "p": body_path, "id": call_id})
            if result is None:
                self._record_failure(conn, request_id, error, status, window)
                conn.execute(text("SELECT pg_notify(:c, :r)"), {"c": NOTIFY_CHANNEL, "r": str(request_id)})
            elif result.next_cursor:
                conn.execute(text("UPDATE requests SET next_cursor = :c, retry_at = NULL WHERE id = :r"),
                             {"c": result.next_cursor, "r": request_id})
            else:
                conn.execute(text(
                    "UPDATE requests SET state = 'complete', next_cursor = NULL, last_error = NULL, last_status = NULL, finished_at = :now WHERE id = :r"),
                    {"now": self._clock(), "r": request_id})
                conn.execute(text("SELECT pg_notify(:c, :r)"), {"c": NOTIFY_CHANNEL, "r": str(request_id)})

    def _record_failure(self, conn, request_id: int, error: str | None, status: int | None, window=None) -> None:
        now = self._clock()
        if status == 429:  # global pause; the Request is retried and this is not an attempt
            conn.execute(text("UPDATE request_engine_state SET paused_until = :u WHERE id = 1"),
                         {"u": now + RATE_LIMITED_PAUSE_SECONDS})
            return
        if status is None or status >= 500:
            attempts = conn.execute(text(
                "UPDATE requests SET attempts = attempts + 1, last_error = :e, last_status = :s WHERE id = :r "
                "RETURNING attempts"), {"e": error, "s": status, "r": request_id}).scalar_one()
            if attempts < MAX_ATTEMPTS:
                conn.execute(text("UPDATE requests SET retry_at = :t WHERE id = :r"),
                             {"t": now + BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), "r": request_id})
                return
        conn.execute(text(
            "UPDATE requests SET state = 'failed', last_error = :e, last_status = :s, finished_at = :now WHERE id = :r"),
            {"e": error, "s": status, "now": now, "r": request_id})
        if status in (401, 403):
            conn.execute(text("UPDATE request_engine_state SET state = 'halted', halt_reason = :why WHERE id = 1"),
                         {"why": f"Massive answered HTTP {status} for request {request_id}: {error}" + (
                             f" (window sent {window[0]}; History Limit applied {window[1].isoformat()})" if window else "")})

    def _heartbeat(self, now: float) -> None:
        if self._last_beat is not None and 0 <= now - self._last_beat < HEARTBEAT_INTERVAL:
            return
        self._last_beat = now
        with self._db.begin() as conn:
            conn.execute(text("UPDATE request_engine_state SET heartbeat_at = :now WHERE id = 1"), {"now": now})

    def prune_responses(self) -> int:
        """Delete response bodies older than the retention period and clear their location on the Call rows.
        Request and Call rows are kept. Returns the number of bodies removed."""
        cutoff = self._clock() - RESPONSE_RETENTION_DAYS * 86400
        with self._db.begin() as conn:
            old = conn.execute(text(
                "SELECT id, body_path FROM calls WHERE body_path IS NOT NULL AND started_at < :c"), {"c": cutoff}).all()
            for call in old:
                (self._dir / call.body_path).unlink(missing_ok=True)
            if old:
                conn.execute(text("UPDATE calls SET body_path = NULL WHERE id = ANY(:ids)"),
                             {"ids": [c.id for c in old]})
        for d in {(self._dir / c.body_path).parent for c in old}:
            try:
                d.rmdir()
            except OSError:
                pass
        return len(old)
