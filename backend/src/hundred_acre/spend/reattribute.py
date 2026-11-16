"""Session-level Attributions: list them (optionally only those needing review) and correct them.

Low confidence is computed at query time from the stored issue references, never stored, so it
stays consistent with live Feature metadata. A manual Attribution is never low-confidence and
`record` never overwrites it.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Engine, text

from hundred_acre.spend.features import FeatureSource, _infer, _root
from hundred_acre.spend.ledger import session_spend


@dataclass(frozen=True)
class SessionAttribution:
    session_id: str
    dollars: Decimal
    last_seen: datetime
    source: str  # "inferred" | "manual"
    target_issue: int | None  # the Feature (top-level issue); None = Overhead
    target_title: str | None
    low_confidence: bool  # inferred from references to more than one Feature


def reattribute(engine: Engine, session_id: str, target_issue: int | None, source: FeatureSource | None = None) -> None:
    """Manually attribute a Session to the Feature containing `target_issue` (None = Overhead).

    If `source` is given the issue is checked to exist. Raises LookupError for an unknown Session or issue.
    """
    if target_issue is not None and source is not None and source.issue(target_issue) is None:
        raise LookupError(f"no such issue: #{target_issue}")
    with engine.begin() as conn:
        found = conn.execute(text("select 1 from sessions where id = :id"), {"id": session_id}).scalar()
        if not found:
            raise LookupError(f"no such session: {session_id}")
        conn.execute(
            text(
                """
                insert into attributions (session_id, source, target_issue, refs)
                values (:id, 'manual', :target, '{}'::jsonb)
                on conflict (session_id) do update set source = 'manual', target_issue = excluded.target_issue
                """
            ),
            {"id": session_id, "target": target_issue},
        )


def list_sessions(engine: Engine, source: FeatureSource, needs_review: bool = False) -> list[SessionAttribution]:
    """Sessions with their resolved Attribution, newest first. Raises FeatureSourceUnavailable
    if references must be resolved and the source is unreachable."""
    spend = session_spend(engine)
    with engine.connect() as conn:
        seen = {sid: last for sid, last in conn.execute(text("select id, last_seen from sessions"))}
        rows = conn.execute(text("select session_id, source, target_issue, refs from attributions")).all()
    attribution = {sid: (src, target, refs) for sid, src, target, refs in rows}
    cache: dict = {}
    result = []
    for sid, last_seen in seen.items():
        src, target, refs = attribution.get(sid, ("inferred", None, {}))
        if src == "manual":
            feature = _root(source, target, cache) if target is not None else None
            low = False
        else:
            feature, low = _infer(source, refs, cache)
        if needs_review and not low:
            continue
        result.append(
            SessionAttribution(
                sid,
                spend.get(sid, (Decimal(0), 0))[0],
                last_seen,
                src,
                feature.number if feature else None,
                feature.title if feature else None,
                low,
            )
        )
    return sorted(result, key=lambda r: r.last_seen, reverse=True)
