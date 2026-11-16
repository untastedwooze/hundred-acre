"""Find the GitHub issue references in a Claude Code transcript.

The transcript is parsed transiently. Only issue numbers and per-signal counts leave this module;
no conversation text is ever returned or stored.
"""

import json
import re
from collections import defaultdict
from pathlib import Path

# Strongest first. Ranking between Features uses this order, then the reference count.
SIGNALS = ("created", "closed", "branch", "mentioned")

# issue number -> signal -> count
IssueRefs = dict[int, dict[str, int]]

_ISSUE_URL = re.compile(r"/issues/(\d+)")
_CLOSE_COMMAND = re.compile(r"\bgh\s+issue\s+close\s+#?(\d+)")
_VIEW_COMMAND = re.compile(r"\bgh\s+issue\s+view\s+#?(\d+)")
_CLOSING_KEYWORD = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w&/])#(\d+)\b")
_BRANCH = re.compile(r"(?:^|/)(?:(?:ticket|issue)-(\d+)|(\d+)-[\w-]+)$")


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
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            session = entry.get("sessionId")
            message = entry.get("message")
            if not session or not isinstance(message, dict):
                continue
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
