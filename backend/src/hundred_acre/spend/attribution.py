"""Find the GitHub issue references in a Claude Code transcript.

The transcript is parsed transiently. Only issue numbers and per-signal counts leave this module;
no conversation text is ever returned or stored.
"""

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterator, Literal, get_args

Signal = Literal["created", "closed", "branch", "mentioned"]
Stage = Literal["grill", "spec", "implement", "review", "fix", "other"]
AttributionSource = Literal["inferred", "manual"]

# Strongest first. Ranking between Features uses this order, then the reference count.
SIGNALS: tuple[Signal, ...] = get_args(Signal)

# issue number -> signal -> count
IssueRefs = dict[int, dict[str, int]]

_ISSUE_URL = re.compile(r"/issues/(\d+)")
_CLOSE_COMMAND = re.compile(r"\bgh\s+issue\s+close\s+#?(\d+)")
_VIEW_COMMAND = re.compile(r"\bgh\s+issue\s+view\s+#?(\d+)")
_CLOSING_KEYWORD = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w&/])#(\d+)\b")
_BRANCH = re.compile(r"(?:^|/)(?:(?:ticket|issue)-(\d+)|(\d+)-[\w-]+)$")


# The one Stage mapping table: pipeline skill name (plugin prefix stripped) -> Stage.
STAGE_BY_SKILL: dict[str, Stage] = {
    "grilling": "grill",
    "grill-me": "grill",
    "grill-with-docs": "grill",
    "to-spec": "spec",
    "write-spec": "spec",
    "to-prd": "spec",
    "to-issues": "spec",
    "to-tickets": "spec",
    "triage": "spec",
    "tdd": "implement",
    "implement-spec": "implement",
    "implement": "implement",
    "code-review": "review",
    "review": "review",
    "diagnosing-bugs": "fix",
    "diagnose": "fix",
}
STAGES: tuple[Stage, ...] = get_args(Stage)  # pipeline order
DEFAULT_STAGE: Stage = "other"

_COMMAND_NAME = re.compile(r"<command-name>/?([^<\s]+)</command-name>")


def stage_for_skill(skill: str) -> Stage | None:
    """The Stage a skill belongs to, or None if it is not a pipeline skill."""
    return STAGE_BY_SKILL.get(skill.rsplit(":", 1)[-1].strip().lower())


def _entries(path: Path) -> Iterator[tuple[dict, str, dict]]:
    """(entry, session id, message) for each transcript line that belongs to a Session and has a message."""
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        session = entry.get("sessionId")
        message = entry.get("message")
        if session and isinstance(message, dict):
            yield entry, session, message


def _skill_names(entry: dict, content) -> list[str]:
    names: list[str] = []
    if entry.get("type") == "user":
        names += [n for t in _text_blocks(content) for n in _COMMAND_NAME.findall(t)]
    if not isinstance(content, str):
        names += [
            str((b.get("input") or {}).get("skill", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"
        ]
    return names


def extract_stages(files: list[Path]) -> dict[str, Stage]:
    """Stage per Session id: that of the first pipeline skill run (Skill tool or slash command).

    A Session that runs no pipeline skill is 'other'. Only skill names are read, never other text.
    """
    stages: dict[str, Stage] = {}
    for path in files:
        for entry, session, message in _entries(path):
            if stages.setdefault(session, DEFAULT_STAGE) != DEFAULT_STAGE:
                continue
            for name in _skill_names(entry, message.get("content") or []):
                stage = stage_for_skill(name)
                if stage:
                    stages[session] = stage
                    break
    return stages


def _add(refs: dict[str, dict[int, dict[str, int]]], session: str, number: int, signal: str, times: int = 1) -> None:
    counts = refs[session].setdefault(number, {})
    counts[signal] = counts.get(signal, 0) + times


def _text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]


def _result_text(content) -> str:
    return "\n".join(_text_blocks(content))


def extract_issue_refs(files: list[Path]) -> dict[str, IssueRefs]:
    """Issue references per Session id found in the given transcript files."""
    refs: dict[str, IssueRefs] = defaultdict(dict)
    branches: dict[str, set[str]] = defaultdict(set)
    for path in files:
        commands: dict[str, str] = {}  # tool_use id -> Bash command
        for entry, session, message in _entries(path):
            if entry.get("gitBranch"):
                branches[session].add(entry["gitBranch"])
            content = message.get("content") or []
            for text in _text_blocks(content):
                for n in _MENTION.findall(text):
                    _add(refs, session, int(n), "mentioned")
            if isinstance(content, str):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    command = (block.get("input") or {}).get("command", "")
                    commands[block["id"]] = command
                    for n in _CLOSE_COMMAND.findall(command):
                        _add(refs, session, int(n), "closed")
                    for n in _VIEW_COMMAND.findall(command):
                        _add(refs, session, int(n), "mentioned")
                    if re.search(r"\bgh\s+pr\b|\bgit\s+commit\b", command):
                        for n in _CLOSING_KEYWORD.findall(command):
                            _add(refs, session, int(n), "closed")
                elif block.get("type") == "tool_result":
                    command = commands.get(block.get("tool_use_id"), "")
                    if re.search(r"\bgh\s+issue\s+create\b", command):
                        for n in _ISSUE_URL.findall(_result_text(block.get("content"))):
                            _add(refs, session, int(n), "created")
    for session, names in branches.items():
        for name in names:
            match = _BRANCH.search(name)
            if match:
                _add(refs, session, int(match.group(1) or match.group(2)), "branch")
    return dict(refs)
