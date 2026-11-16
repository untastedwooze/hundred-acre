"""`spend` command line interface."""

import argparse

from hundred_acre.spend.db import migrate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spend", description="hundred-acre Spend tracking")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("migrate", help="migrate the hundred_acre_spend database to the latest revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate()
        return 0
    parser.print_help()
    return 0
