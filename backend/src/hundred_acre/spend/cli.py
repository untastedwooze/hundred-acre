"""`spend` command line interface."""

import argparse
from pathlib import Path

from sqlalchemy import create_engine

from hundred_acre.spend.db import migrate, spend_database_url
from hundred_acre.spend.ledger import record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spend", description="hundred-acre Spend tracking")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("migrate", help="migrate the hundred_acre_spend database to the latest revision")
    rec = sub.add_parser("record", help="record Claude Code transcripts (and their subagent transcripts)")
    rec.add_argument("transcripts", nargs="+", type=Path)
    return parser


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
    parser.print_help()
    return 0
