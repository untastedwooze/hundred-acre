"""Features: the Feature-source interface (GitHub, live) and per-Feature Spend (`feature_spend`).

A Feature is a top-level GitHub issue plus its sub-issues at any depth; a standalone issue is its own
Feature. Feature metadata is never stored: it is read through a `FeatureSource` on every query.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable, Protocol

from sqlalchemy import Engine, text

from hundred_acre.spend.attribution import SIGNALS
from hundred_acre.spend.ledger import message_spend, session_spend


class FeatureSourceUnavailable(Exception):
    """The Feature source (GitHub) cannot be reached."""


@dataclass(frozen=True)
class IssueInfo:
    number: int
    title: str
    parent: int | None  # parent issue number; None for a top-level issue
    state: str  # "open" | "closed"
    first_closed_at: datetime | None  # None while the issue has never been closed
    ui_surface: str | None = None  # from a `ui-surface: <name>` line in the body; None if undeclared


_UI_SURFACE_LINE = re.compile(r"^[ \t]*ui-surface:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def parse_ui_surface(body: str | None) -> str | None:
    """The UI Surface named by the first whole-line `ui-surface: <name>` in an issue body."""
    match = _UI_SURFACE_LINE.search((body or "").replace("\r", ""))
    return match.group(1) if match else None


class FeatureSource(Protocol):
    def issue(self, number: int) -> IssueInfo | None:
        """The issue, or None if no such issue exists. Raises FeatureSourceUnavailable if unreachable."""
        ...


@dataclass
class FakeFeatureSource:
    """In-memory FeatureSource for tests. Set `available = False` to simulate GitHub being unreachable."""

    issues: dict[int, IssueInfo] = field(default_factory=dict)
    available: bool = True

    def issue(self, number: int) -> IssueInfo | None:
        if not self.available:
            raise FeatureSourceUnavailable("fake source is unavailable")
        return self.issues.get(number)


Runner = Callable[[list[str]], str]


def _run_gh(args: list[str]) -> str:
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FeatureSourceUnavailable(str(e)) from e
    if done.returncode != 0:
        if "404" in done.stderr or "Not Found" in done.stderr:
            raise LookupError(done.stderr)
        raise FeatureSourceUnavailable(done.stderr.strip())
    return done.stdout


class GhFeatureSource:
    """FeatureSource backed by the `gh` CLI. Repo defaults to gh's current-directory repo
    (override with HUNDRED_ACRE_GITHUB_REPO=owner/name)."""

    def __init__(self, repo: str | None = None, run: Runner = _run_gh):
        self._repo = repo or os.environ.get("HUNDRED_ACRE_GITHUB_REPO") or "{owner}/{repo}"
        self._run = run

    def _get(self, path: str, *extra: str) -> str | None:
        try:
            return self._run(["api", f"repos/{self._repo}/{path}", *extra])
        except LookupError:
            return None

    def issue(self, number: int) -> IssueInfo | None:
        body = self._get(f"issues/{number}")
        if body is None:
            return None
        data = json.loads(body)
        parent = self._get(f"issues/{number}/parent")
        first_closed = None
        if data["state"] == "closed":
            events = self._get(
                f"issues/{number}/timeline", "--paginate", "--jq", '.[] | select(.event == "closed") | .created_at'
            )
            times = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in (events or "").split()]
            first_closed = min(times) if times else None
        return IssueInfo(
            number=number,
            title=data["title"],
            parent=json.loads(parent)["number"] if parent else None,
            state=data["state"],
            first_closed_at=first_closed,
            ui_surface=parse_ui_surface(data.get("body")),
        )


@dataclass(frozen=True)
class FeatureSpend:
    issue: int  # the top-level issue
    title: str
    state: str  # "open" | "closed"; open Features show their live Spend so far
    dollars: Decimal  # full precision, includes all sub-issues; build_dollars + maintenance_dollars
    build_dollars: Decimal  # Build Spend: messages at or before the top-level issue first closed (all, if never closed)
    maintenance_dollars: Decimal  # Maintenance Spend: messages after the top-level issue first closed
    first_closed_at: datetime | None  # Feature-completion date; reopening and re-closing never moves it
    sessions: int
    low_confidence_sessions: int  # Sessions that referenced more than one Feature
    ui_surface: str | None = None  # the UI Surface the Feature declares, if any


@dataclass(frozen=True)
class FeatureSpendReport:
    github_available: bool  # False: features is empty and unresolved_dollars holds the unattributable rest
    features: list[FeatureSpend]  # highest Spend first
    overhead_dollars: Decimal
    unresolved_dollars: Decimal  # Spend of Sessions with issue references that could not be resolved
    unpriced_messages: int


def _root(source: FeatureSource, number: int, cache: dict[int, IssueInfo | None]) -> IssueInfo | None:
    """The top-level issue above `number`, or None if `number` is not an issue."""
    seen: set[int] = set()
    current: IssueInfo | None = None
    while number not in seen:
        seen.add(number)
        if number not in cache:
            cache[number] = source.issue(number)
        info = cache[number]
        if info is None:
            return current
        current = info
        if info.parent is None:
            return info
        number = info.parent
    return current


def _infer(source, refs: dict[str, dict[str, int]], cache) -> tuple[IssueInfo | None, bool]:
    """(Feature, low_confidence) for inferred references: strongest signal, then reference count."""
    best: dict[int, list] = {}  # root number -> [strongest signal rank, reference count, root info]
    for number, counts in refs.items():
        root = _root(source, int(number), cache)
        if root is None:
            continue
        entry = best.setdefault(root.number, [len(SIGNALS), 0, root])
        entry[0] = min(entry[0], *(SIGNALS.index(s) for s in counts))
        entry[1] += sum(counts.values())
    if not best:
        return None, False
    winner = min(best.values(), key=lambda b: (b[0], -b[1]))
    return winner[2], len(best) > 1


def feature_spend(engine: Engine, source: FeatureSource) -> FeatureSpendReport:
    """Spend per Feature (sub-issues rolled up), plus Overhead.

    Inferred Attributions are resolved here from the stored issue references, so Feature metadata is
    always live. If the source is unavailable, no Feature is resolved and the report says so.
    """
    spend = session_spend(engine)
    messages = message_spend(engine)
    with engine.connect() as conn:
        rows = conn.execute(text("select session_id, source, target_issue, refs from attributions")).all()
    attribution = {sid: (src, target, refs) for sid, src, target, refs in rows}
    unpriced = sum(n for _, n in spend.values())

    cache: dict[int, IssueInfo | None] = {}
    totals: dict[int, list] = {}  # root -> [info, build, maintenance, sessions, low confidence sessions]
    overhead = Decimal(0)
    unresolved = Decimal(0)
    available = True
    for sid, (dollars, _) in spend.items():
        src, target, refs = attribution.get(sid, ("inferred", None, {}))
        try:
            if src == "manual":
                feature = _root(source, target, cache) if target is not None else None
                low = False
            else:
                feature, low = _infer(source, refs, cache)
        except FeatureSourceUnavailable:
            available = False
            if src == "manual" and target is None or src == "inferred" and not refs:
                overhead += dollars
            else:
                unresolved += dollars
            continue
        if feature is None:
            overhead += dollars
            continue
        entry = totals.setdefault(feature.number, [feature, Decimal(0), Decimal(0), 0, 0])
        # Split per message, not per Session: a Session can straddle the close. A message sent at the
        # closing instant is Build; only later messages are Maintenance.
        closed = feature.first_closed_at
        for ts, amount in messages.get(sid, []):
            entry[2 if closed is not None and ts > closed else 1] += amount
        entry[3] += 1
        entry[4] += low

    if not available:
        # Sessions resolved before the source failed must not be reported as a partial Feature table.
        for _, build, maintenance, _, _ in totals.values():
            unresolved += build + maintenance
        totals = {}
    features = sorted(
        (
            FeatureSpend(i.number, i.title, i.state, b + m, b, m, i.first_closed_at, n, lc, i.ui_surface)
            for i, b, m, n, lc in totals.values()
        ),
        key=lambda f: f.dollars,
        reverse=True,
    )
    return FeatureSpendReport(available, features, overhead, unresolved, unpriced)
