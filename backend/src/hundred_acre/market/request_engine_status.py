"""`market request-engine status`: a read-only report built from the database alone (works with no engine running)."""

import os

from sqlalchemy import Engine, text

from hundred_acre.market.priority import PRIORITY_ORDER
from hundred_acre.market.request_engine import DEFAULT_CALLS_PER_MINUTE, STALE_AFTER, WINDOW_SECONDS

RECENT_FAILURES = 10


def _duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


def render_status(engine: Engine, now: float) -> str:
    per_minute = int(os.environ.get("MASSIVE_CALLS_PER_MINUTE", DEFAULT_CALLS_PER_MINUTE))
    with engine.connect() as conn:
        eng = conn.execute(text("SELECT state, halt_reason, heartbeat_at FROM request_engine_state WHERE id = 1")).one()
        depth = dict(conn.execute(text(
            "SELECT priority, count(*) FROM requests WHERE state IN ('queued', 'in_progress') GROUP BY priority"
        )).all())
        calls = conn.execute(text(
            "SELECT count(*) FILTER (WHERE started_at > :hour), count(*), "
            "count(*) FILTER (WHERE started_at > :window) FROM calls WHERE started_at > :day"),
            {"hour": now - 3600, "day": now - 86400, "window": now - WINDOW_SECONDS}).one()
        failed = conn.execute(text(
            "SELECT path, last_status, last_error, finished_at FROM requests WHERE state = 'failed' "
            "ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT :n"), {"n": RECENT_FAILURES}).all()

    age = None if eng.heartbeat_at is None else now - eng.heartbeat_at
    beat = "no heartbeat" if age is None else f"heartbeat {_duration(age)} ago"
    if eng.state == "halted":
        head = f"engine: halted: {eng.halt_reason} ({beat})"
    elif age is None or age > STALE_AFTER:
        head = f"engine: stale ({beat})"
    else:
        head = f"engine: running ({beat})"

    lines = [head, "", f"queue (drain estimated at {per_minute} calls/min, one call per queued request):"]
    for cls in PRIORITY_ORDER:
        n = depth.get(str(cls), 0)
        lines.append(f"  {cls:<18}{n} queued, drain ~{_duration(n * 60 / per_minute)}")
    lines += ["", "calls:", f"  last hour: {calls[0]}", f"  last day: {calls[1]}",
              f"  window: {calls[2]}/{per_minute} calls in the last {int(WINDOW_SECONDS)}s", ""]
    if not failed:
        lines.append("failed requests: none")
    else:
        lines.append("recent failed requests:")
        for f in failed:
            when = "" if f.finished_at is None else f"{_duration(now - f.finished_at)} ago, "
            lines.append(f"  {f.path} HTTP {f.last_status or '-'} ({when}{f.last_error or 'no error recorded'})")
    return "\n".join(lines)
