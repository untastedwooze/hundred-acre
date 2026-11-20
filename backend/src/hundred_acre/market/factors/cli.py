"""`market factors ...` commands (#43). Needs no Massive access: they never touch the Request Engine."""

import argparse
import sys
from datetime import date

from sqlalchemy import Engine

from hundred_acre.market.factors import (
    FACTOR_LABELS, FRENCH_ATTRIBUTION, MODELS, FrenchDataLibrarySource, ReferenceFactorError, ReferenceFactorSource,
    import_reference_factor_returns, latest_vintage, lookup_factor_exposures, refresh_factor_exposures,
)
from hundred_acre.market.lake import Lake


def add_parser(sub) -> None:
    """Register `factors` on the `market` command's subparsers."""
    factors = sub.add_parser("factors", help="Reference Factor Returns and Factor Exposures")
    fsub = factors.add_subparsers(dest="factors_command", required=True)
    fsub.add_parser("import", help="import French's Reference Factor Returns as a new vintage if they changed")
    refresh = fsub.add_parser("refresh", help="recompute Factor Exposures for Securities whose source data or vintage changed")
    refresh.add_argument("--full", action="store_true", help="recompute every Security's exposures, stale or not")
    exp = fsub.add_parser("exposures", help="print a Security's Factor Exposures")
    exp.add_argument("ticker")
    exp.add_argument("--date", type=date.fromisoformat, default=None, help="Ticker date and window end limit (default: today)")
    exp.add_argument("--model", choices=sorted(MODELS), default="ff6")


def run(args: argparse.Namespace, engine: Engine, lake: Lake, source: ReferenceFactorSource | None = None) -> int:
    if args.factors_command == "import":
        return _import(lake, source)
    if args.factors_command == "refresh":
        return _refresh(args, engine, lake)
    return _exposures(args, engine, lake)


def _import(lake: Lake, source: ReferenceFactorSource | None) -> int:
    if source is None:
        source = FrenchDataLibrarySource()
    try:
        result = import_reference_factor_returns(lake, source)
    except ReferenceFactorError as exc:
        print(f"market: factor import failed, previous vintage kept: {exc}", file=sys.stderr)
        return 1
    state = "created vintage" if result.created else "no change, latest vintage is"
    print(f"{state} {result.vintage}: {result.rows} daily rows, {result.first_date} to {result.last_date}")
    print(FRENCH_ATTRIBUTION)
    return 0


def _refresh(args: argparse.Namespace, engine: Engine, lake: Lake) -> int:
    if latest_vintage(lake) is None:
        print("market: No Reference Factor Returns yet: run `market factors import` first.", file=sys.stderr)
        return 1
    result = refresh_factor_exposures(engine, lake, full=args.full)
    print(f"refreshed Factor Exposures, Securities rebuilt: {len(result.rebuilt)}{' (full rebuild)' if args.full else ''}")
    return 0


def _exposures(args: argparse.Namespace, engine: Engine, lake: Lake) -> int:
    on = args.date or date.today()
    found = lookup_factor_exposures(engine, lake, args.ticker, args.model, on)
    if found.exposures is None:
        print(f"market: {found.message}", file=sys.stderr)
        return 1
    row = found.exposures.sort("window_end").row(-1, named=True)
    print(f"{args.ticker} {args.model.upper()} exposure, window {row['window_start']} to {row['window_end']} ({row['n_obs']} observations)")
    print(f"Factor Return vintage {row['factor_vintage']}")
    print(f"alpha (daily)\t{row['alpha']:+.6f}\tt={row['t_alpha']:.2f}")
    for f in MODELS[args.model]:
        print(f"{FACTOR_LABELS[f]}\t{row[f'beta_{f}']:+.4f}\tt={row[f't_{f}']:.2f}")
    print(f"R-squared\t{row['r_squared']:.4f}")
    print(FRENCH_ATTRIBUTION)
    return 0
