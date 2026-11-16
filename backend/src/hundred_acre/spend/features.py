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

from hundred_acre.spend.attribution import SIGNALS, AttributionSource
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

    def issues_declaring_surface(self, name: str) -> list[IssueInfo]:
        """Top-level issues (Features) that declare UI Surface `name`. Raises FeatureSourceUnavailable if unreachable."""
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

    def issues_declaring_surface(self, name: str) -> list[IssueInfo]:
        if not self.available:
            raise FeatureSourceUnavailable("fake source is unavailable")
        return [i for i in self.issues.values() if i.parent is None and i.ui_surface == name]


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

    def issues_declaring_surface(self, name: str) -> list[IssueInfo]:
        # One listing call narrows to issues whose body mentions a ui-surface line; only those are
        # fetched in full, so the cost does not grow with every issue's parent/timeline lookups.
        numbers = self._get(
            "issues?state=all&per_page=100",
            "--paginate",
            "--jq",
            '.[] | select(.pull_request == null) | select((.body // "") | test("ui-surface:"; "i")) | .number',
        )
        found = (self.issue(int(n)) for n in (numbers or "").split())
        return [i for i in found if i and i.parent is None and i.ui_surface == name]


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
    github_available: bool  # False: a lookup failed, so `features` may be incomplete; the Spend of the Sessions that could not be resolved is in unresolved_dollars
    features: list[FeatureSpend]  # highest Spend first
    overhead_dollars: Decimal
    unresolved_dollars: Decimal  # Spend of Sessions with issue references that could not be resolved
    unpriced_messages: int


@dataclass(frozen=True)
class Resolution:
    """Where one Session's Attribution lands."""

    feature: IssueInfo | None  # the Feature (top-level issue); None = Overhead
    sub_issue: int | None  # the issue within the Feature the Session worked on, when known
    low_confidence: bool  # inferred from references to more than one Feature


def root_issue(source: FeatureSource, number: int, cache: dict[int, IssueInfo | None]) -> IssueInfo | None:
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


@dataclass
class _Candidate:
    root: IssueInfo
    rank: int = len(SIGNALS)  # strongest signal referencing this Feature (lower is stronger)
    references: int = 0
    leaf: tuple[int, int, int] = (len(SIGNALS), 0, 0)  # (rank, -references, issue) of the strongest-referenced issue


def infer_issue(source: FeatureSource, refs: dict[str, dict[str, int]], cache) -> Resolution:
    """Feature for stored issue references: strongest signal, then reference count."""
    candidates: dict[int, _Candidate] = {}
    for number, counts in refs.items():
        root = root_issue(source, int(number), cache)
        if root is None:
            continue
        rank = min(SIGNALS.index(s) for s in counts)
        total = sum(counts.values())
        candidate = candidates.setdefault(root.number, _Candidate(root))
        candidate.rank = min(candidate.rank, rank)
        candidate.references += total
        candidate.leaf = min(candidate.leaf, (rank, -total, int(number)))
    if not candidates:
        return Resolution(None, None, False)
    winner = min(candidates.values(), key=lambda c: (c.rank, -c.references))
    return Resolution(winner.root, winner.leaf[2], len(candidates) > 1)


def resolve_attribution(
    source: FeatureSource,
    attribution_source: AttributionSource,
    target: int | None,
    refs: dict[str, dict[str, int]],
    cache: dict[int, IssueInfo | None],
) -> Resolution:
    """A Session's Feature: its manual Attribution if set (target None = Overhead), else inferred from its references."""
    if attribution_source == "manual":
        feature = root_issue(source, target, cache) if target is not None else None
        return Resolution(feature, target, False)
    return infer_issue(source, refs, cache)


@dataclass
class _FeatureTotals:
    info: IssueInfo
    build: Decimal = Decimal(0)
    maintenance: Decimal = Decimal(0)
    sessions: int = 0
    low_confidence_sessions: int = 0


def split_build_maintenance(
    first_closed_at: datetime | None, messages: list[tuple[datetime, Decimal]]
) -> tuple[Decimal, Decimal]:
    """(Build, Maintenance) Spend of messages, split per message, not per Session: a Session can straddle
    the close. A message sent at the closing instant is Build; only later messages are Maintenance."""
    build = maintenance = Decimal(0)
    for ts, amount in messages:
        if first_closed_at is not None and ts > first_closed_at:
            maintenance += amount
        else:
            build += amount
    return build, maintenance


def feature_spend(engine: Engine, source: FeatureSource) -> FeatureSpendReport:
    """Spend per Feature (sub-issues rolled up), plus Overhead.

    Inferred Attributions are resolved here from the stored issue references, so Feature metadata is
    always live. A Session whose references cannot be resolved because the source is unavailable goes
    to `unresolved_dollars` and clears `github_available`; Features resolved from other Sessions keep
    their Spend.
    """
    spend = session_spend(engine)
    messages = message_spend(engine)
    with engine.connect() as conn:
        rows = conn.execute(text("select session_id, source, target_issue, refs from attributions")).all()
    attribution = {sid: (src, target, refs) for sid, src, target, refs in rows}
    unpriced = sum(n for _, n in spend.values())

    cache: dict[int, IssueInfo | None] = {}
    totals: dict[int, _FeatureTotals] = {}
    overhead = Decimal(0)
    unresolved = Decimal(0)
    available = True
    for sid, (dollars, _) in spend.items():
        src, target, refs = attribution.get(sid, ("inferred", None, {}))
        try:
            resolution = resolve_attribution(source, src, target, refs, cache)
        except FeatureSourceUnavailable:
            available = False
            if src == "manual" and target is None or src == "inferred" and not refs:
                overhead += dollars
            else:
                unresolved += dollars
            continue
        feature = resolution.feature
        if feature is None:
            overhead += dollars
            continue
        entry = totals.setdefault(feature.number, _FeatureTotals(feature))
        build, maintenance = split_build_maintenance(feature.first_closed_at, messages.get(sid, []))
        entry.build += build
        entry.maintenance += maintenance
        entry.sessions += 1
        entry.low_confidence_sessions += resolution.low_confidence

    features = sorted(
        (
            FeatureSpend(
                t.info.number,
                t.info.title,
                t.info.state,
                t.build + t.maintenance,
                t.build,
                t.maintenance,
                t.info.first_closed_at,
                t.sessions,
                t.low_confidence_sessions,
                t.info.ui_surface,
            )
            for t in totals.values()
        ),
        key=lambda f: f.dollars,
        reverse=True,
    )
    return FeatureSpendReport(available, features, overhead, unresolved, unpriced)
