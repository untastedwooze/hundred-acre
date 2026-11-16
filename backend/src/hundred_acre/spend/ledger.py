"""Usage Ledger: ingest Claude Code transcripts (`record`) and price them (`total_spend`).

No conversation text is read into storage: only ids, timestamps, model and usage numbers.
Spend is never stored; it is computed from tokens and the Price Schedule on every query.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from sqlalchemy import Engine, text

from hundred_acre.spend.attribution import extract_issue_refs


@dataclass(frozen=True)
class RecordResult:
    messages_seen: int  # distinct message ids found in the given transcripts (incl. subagents)
    messages_added: int  # of those, how many were new to the Usage Ledger


@dataclass(frozen=True)
class SpendTotal:
    dollars: Decimal  # full precision; round for display. Excludes unpriced messages.
    unpriced_messages: int  # messages whose model has no Price Schedule rate; NOT counted as $0 silently


def _transcript_files(path: Path) -> list[Path]:
    """A transcript plus the subagent transcripts Claude Code keeps beside it."""
    files = [path]
    files += sorted((path.parent / path.stem / "subagents").glob("*.jsonl"))
    return files


def _parse(path: Path, is_subagent_file: bool) -> dict[str, dict]:
    """Messages in one file keyed by message id; the last line for an id wins (final usage)."""
    found: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not usage or not message.get("id") or not message.get("model"):
            continue
        creation = usage.get("cache_creation") or {}
        write_1h = creation.get("ephemeral_1h_input_tokens", 0)
        write_5m = creation.get("ephemeral_5m_input_tokens", usage.get("cache_creation_input_tokens", 0) - write_1h)
        found[message["id"]] = {
            "id": message["id"],
            "api_request_id": entry.get("requestId"),
            "session_id": entry["sessionId"],
            "project_path": entry.get("cwd"),
            "is_subagent": bool(entry.get("isSidechain")) or is_subagent_file,
            "timestamp": datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")),
            "model": message["model"],
            "usage": json.dumps(usage),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_write_5m_tokens": write_5m,
            "cache_write_1h_tokens": write_1h,
            "web_search_requests": (usage.get("server_tool_use") or {}).get("web_search_requests", 0),
        }
    return found


def record(engine: Engine, transcripts: Iterable[Path]) -> RecordResult:
    """Idempotently ingest transcripts (and their subagent transcripts) into the Usage Ledger.

    Subagent usage is stored under the parent Session. Repeated lines for one message id count once.
    """
    messages: dict[str, dict] = {}
    files: list[Path] = []
    for transcript in transcripts:
        for file in _transcript_files(Path(transcript)):
            files.append(file)
            messages.update(_parse(file, is_subagent_file=file != Path(transcript)))
    if not messages:
        return RecordResult(0, 0)

    with engine.begin() as conn:
        existing = {
            r[0]
            for r in conn.execute(text("select id from messages where id = any(:ids)"), {"ids": list(messages)})
        }
        for m in messages.values():
            conn.execute(
                text(
                    """
                    insert into sessions (id, project_path, first_seen, last_seen)
                    values (:session_id, :project_path, :timestamp, :timestamp)
                    on conflict (id) do update set
                        first_seen = least(sessions.first_seen, excluded.first_seen),
                        last_seen = greatest(sessions.last_seen, excluded.last_seen),
                        project_path = coalesce(sessions.project_path, excluded.project_path)
                    """
                ),
                m,
            )
            conn.execute(
                text(
                    """
                    insert into messages (id, api_request_id, session_id, is_subagent, timestamp, model,
                        usage, input_tokens, output_tokens, cache_read_tokens, cache_write_5m_tokens,
                        cache_write_1h_tokens, web_search_requests)
                    values (:id, :api_request_id, :session_id, :is_subagent, :timestamp, :model,
                        cast(:usage as jsonb), :input_tokens, :output_tokens, :cache_read_tokens,
                        :cache_write_5m_tokens, :cache_write_1h_tokens, :web_search_requests)
                    on conflict (id) do update set
                        usage = excluded.usage, input_tokens = excluded.input_tokens,
                        output_tokens = excluded.output_tokens, cache_read_tokens = excluded.cache_read_tokens,
                        cache_write_5m_tokens = excluded.cache_write_5m_tokens,
                        cache_write_1h_tokens = excluded.cache_write_1h_tokens,
                        web_search_requests = excluded.web_search_requests
                    """
                ),
                m,
            )
        # Transcript text is parsed transiently; only issue references are stored.
        refs = extract_issue_refs(files)
        for session_id in {m["session_id"] for m in messages.values()}:
            conn.execute(
                text(
                    """
                    insert into attributions (session_id, source, refs)
                    values (:session_id, 'inferred', cast(:refs as jsonb))
                    on conflict (session_id) do update set refs = excluded.refs
                    where attributions.source = 'inferred'
                    """
                ),
                {"session_id": session_id, "refs": json.dumps({str(n): c for n, c in refs.get(session_id, {}).items()})},
            )
    return RecordResult(len(messages), len(messages) - len(existing))


# Token column -> Price Schedule dimension.
_DIMENSIONS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
    "cache_write_5m_tokens": "cache_write_5m",
    "cache_write_1h_tokens": "cache_write_1h",
    "web_search_requests": "web_search",
}


def _priced_messages_sql() -> str:
    """Per message: rate in effect at its timestamp for each dimension (null when no rate exists)."""
    rate_cols = ",\n".join(
        f"""(select p.rate_per_million from price_schedule p
             where p.model = m.model and p.dimension = '{dim}' and p.effective_from <= m.timestamp
             order by p.effective_from desc limit 1) as rate_{col}"""
        for col, dim in _DIMENSIONS.items()
    )
    return f"select m.*, {rate_cols} from messages m"


def session_spend(engine: Engine) -> dict[str, tuple[Decimal, int]]:
    """Per Session: (priced dollars, unpriced message count). Same pricing rules as `total_spend`."""
    cost = " + ".join(f"coalesce({c}, 0) * rate_{c}" for c in _DIMENSIONS)
    unpriced = " or ".join(f"({c} > 0 and rate_{c} is null)" for c in _DIMENSIONS)
    sql = f"""
        select session_id,
            coalesce(sum(({cost}) / 1000000) filter (where not ({unpriced})), 0),
            count(*) filter (where {unpriced})
        from ({_priced_messages_sql()}) priced
        group by session_id
    """
    with engine.connect() as conn:
        return {sid: (Decimal(d), n) for sid, d, n in conn.execute(text(sql))}


def total_spend(engine: Engine) -> SpendTotal:
    """Total Spend to date: tokens x the Price Schedule rate in effect at each message's time.

    A message that has tokens in a dimension with no rate is unpriced and excluded from dollars.
    """
    cost = " + ".join(f"coalesce({c}, 0) * rate_{c}" for c in _DIMENSIONS)
    unpriced = " or ".join(f"({c} > 0 and rate_{c} is null)" for c in _DIMENSIONS)
    sql = f"""
        select
            coalesce(sum(({cost}) / 1000000) filter (where not ({unpriced})), 0) as dollars,
            count(*) filter (where {unpriced}) as unpriced
        from ({_priced_messages_sql()}) priced
    """
    with engine.connect() as conn:
        dollars, unpriced_count = conn.execute(text(sql)).one()
    return SpendTotal(Decimal(dollars), unpriced_count)


@dataclass(frozen=True)
class SeriesPoint:
    timestamp: datetime  # UTC, start of the hour the Spend was incurred in
    dollars: Decimal  # cumulative Spend up to and including this hour


@dataclass(frozen=True)
class SpendSeries:
    points: list[SeriesPoint]  # ascending by time; empty for an empty Usage Ledger
    unpriced_messages: int  # excluded from the dollars, as in total_spend


def cumulative_spend(engine: Engine) -> SpendSeries:
    """Cumulative Spend over time, bucketed by UTC hour. Unpriced messages are excluded and counted."""
    cost = " + ".join(f"coalesce({c}, 0) * rate_{c}" for c in _DIMENSIONS)
    unpriced = " or ".join(f"({c} > 0 and rate_{c} is null)" for c in _DIMENSIONS)
    sql = f"""
        select bucket, sum(dollars) over (order by bucket) as cumulative
        from (
            select date_trunc('hour', timestamp at time zone 'UTC') as bucket,
                   sum(({cost}) / 1000000) as dollars
            from ({_priced_messages_sql()}) priced
            where not ({unpriced})
            group by 1
        ) hourly
        order by bucket
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).all()
    return SpendSeries(
        [SeriesPoint(b.replace(tzinfo=timezone.utc), Decimal(c)) for b, c in rows],
        total_spend(engine).unpriced_messages,
    )
