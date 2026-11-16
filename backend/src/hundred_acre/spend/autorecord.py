"""Automatic recording: the Claude Code hook entry point, the retry queue, and the transcript sweep."""

import json
import os
import re
from pathlib import Path
from typing import Iterable

from sqlalchemy import Engine

from hundred_acre.spend.ledger import RecordResult, record


def default_queue_file() -> Path:
    base = os.environ.get("SPEND_QUEUE_FILE")
    if base:
        return Path(base)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state / "hundred-acre" / "spend-retry-queue"


def _read_queue(queue_file: Path) -> list[Path]:
    try:
        return [Path(p) for p in queue_file.read_text().splitlines() if p.strip()]
    except OSError:
        return []


def _write_queue(queue_file: Path, paths: Iterable[Path]) -> None:
    unique = list(dict.fromkeys(str(p) for p in paths))
    if not unique:
        queue_file.unlink(missing_ok=True)
        return
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text("\n".join(unique) + "\n")


def record_from_hook(payload: str, engine: Engine, queue_file: Path | None = None) -> None:
    """Record the transcript named in a Claude Code hook payload (plus any queued retries).

    Never raises. A failed recording leaves its transcript in the retry queue for the next run.
    """
    try:
        queue_file = queue_file or default_queue_file()
        pending = _read_queue(queue_file)
        try:
            current = json.loads(payload).get("transcript_path")
        except (ValueError, AttributeError):
            current = None
        if current:
            pending.append(Path(current))
        pending = [p for p in dict.fromkeys(pending) if p.exists()]
        if not pending:
            _write_queue(queue_file, [])
            return
        try:
            record(engine, pending)
        except Exception:
            _write_queue(queue_file, pending)
            return
        _write_queue(queue_file, [])
    except Exception:
        pass


def _encode_project_dir(path: str) -> str:
    """Claude Code names a project's transcript directory after its path: non-alphanumerics become '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def sweep(engine: Engine, projects_dir: Path, repo_root: str, worktrees: Iterable[str] = ()) -> RecordResult:
    """Record every transcript for the repo and its git worktrees found under `projects_dir`."""
    exact = {_encode_project_dir(p) for p in [repo_root, *worktrees]}
    worktree_prefix = _encode_project_dir(repo_root) + "--claude-worktrees-"
    transcripts = [
        t
        for d in sorted(Path(projects_dir).iterdir())
        if d.is_dir() and (d.name in exact or d.name.startswith(worktree_prefix))
        for t in sorted(d.glob("*.jsonl"))
    ]
    return record(engine, transcripts)
