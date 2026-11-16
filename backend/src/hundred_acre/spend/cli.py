"""`spend` command line interface."""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine

from hundred_acre.spend.db import migrate, spend_database_url
from hundred_acre.spend.autorecord import record_from_hook, sweep
from hundred_acre.spend.ledger import add_price, record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spend", description="hundred-acre Spend tracking")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("migrate", help="migrate the hundred_acre_spend database to the latest revision")
    rec = sub.add_parser("record", help="record Claude Code transcripts (and their subagent transcripts)")
    rec.add_argument("transcripts", nargs="+", type=Path)
    price = sub.add_parser("price", help="add a Price Schedule entry (unpriced usage becomes priced)")
    price.add_argument("model")
    price.add_argument("dimension", choices=["input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "web_search"])
    price.add_argument("rate_per_million", type=Decimal, help="dollars per million tokens (or requests)")
    price.add_argument("--from", dest="effective_from", type=_utc_date, required=True, help="effective-from date, YYYY-MM-DD (UTC)")
    price.add_argument("--modifier", choices=["standard", "fast", "long_context"], default="standard")
    price.add_argument("--above-prompt-tokens", type=int, help="long_context only: prompt-length threshold")
    sub.add_parser("hook", help="Claude Code hook: read the hook payload on stdin and record; always exits 0")
    sw = sub.add_parser("sweep", help="record every transcript for this repo and its git worktrees")
    sw.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude" / "projects")
    sw.add_argument("--repo-root", help="defaults to the main checkout of the current git repo")
    return parser


def _utc_date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)



def _git_worktrees() -> tuple[str | None, list[str]]:
    try:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, []
    paths = [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]
    return (paths[0], paths[1:]) if paths else (None, [])


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate()
        return 0
    if args.command == "record":
        engine = create_engine(spend_database_url())
        result = record(engine, args.transcripts)
        print(f"recorded {result.messages_added} new of {result.messages_seen} messages")
        return 0
    if args.command == "price":
        add_price(
            create_engine(spend_database_url()),
            model=args.model,
            dimension=args.dimension,
            rate_per_million=args.rate_per_million,
            effective_from=args.effective_from,
            modifier=args.modifier,
            above_prompt_tokens=args.above_prompt_tokens,
        )
        return 0
    if args.command == "hook":
        try:
            engine = create_engine(spend_database_url(), connect_args={"connect_timeout": 2})
            record_from_hook(sys.stdin.read(), engine)
        except BaseException:
            pass
        return 0
    if args.command == "sweep":
        root, worktrees = _git_worktrees()
        root = args.repo_root or root
        if not root:
            parser.error("not in a git repo; pass --repo-root")
        engine = create_engine(spend_database_url())
        result = sweep(engine, args.projects_dir, root, worktrees)
        print(f"swept: recorded {result.messages_added} new of {result.messages_seen} messages")
        return 0
    parser.print_help()
    return 0
