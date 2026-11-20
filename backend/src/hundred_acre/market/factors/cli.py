"""`market factors ...` commands (#43). Needs no Massive access: they never touch the Request Engine."""

import argparse
import sys
from datetime import date

from sqlalchemy import Engine

from hundred_acre.market.factors.exposures import MODELS, read_factor_exposures
from hundred_acre.market.factors.reference_returns import import_reference_factor_returns, latest_vintage
from hundred_acre.market.factors.source import FACTOR_COLUMNS, FRENCH_ATTRIBUTION, ReferenceFactorError, ReferenceFactorSource
from hundred_acre.market.lake import Lake
from hundred_acre.market.reference import security_on

LABELS = dict(zip(FACTOR_COLUMNS, ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD")))


def add_parser(sub) -> None:
    """Register `factors` on the `market` command's subparsers."""
    factors = sub.add_parser("factors", help="Reference Factor Returns and Factor Exposures")
    fsub = factors.add_subparsers(dest="factors_command", required=True)
    fsub.add_parser("import", help="import French's Reference Factor Returns as a new vintage if they changed")
    exp = fsub.add_parser("exposures", help="print a Security's Factor Exposures")
    exp.add_argument("ticker")
    exp.add_argument("--date", type=date.fromisoformat, default=None, help="Ticker date and window end limit (default: today)")
    exp.add_argument("--model", choices=sorted(MODELS), default="ff6")


def run(args: argparse.Namespace, engine: Engine, lake: Lake, source: ReferenceFactorSource | None = None) -> int:
    if args.factors_command == "import":
        return _import(lake, source)
    return _exposures(args, engine, lake)


def _import(lake: Lake, source: ReferenceFactorSource | None) -> int:
    if source is None:
        from hundred_acre.market.factors.source import FrenchDataLibrarySource

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


def _exposures(args: argparse.Namespace, engine: Engine, lake: Lake) -> int:
    on = args.date or date.today()
    security_id = security_on(engine, args.ticker, on)
    if security_id is None:
        print(f"market: unknown Ticker {args.ticker!r} on {on}", file=sys.stderr)
        return 1
    if latest_vintage(lake) is None:
        print("market: no Reference Factor Returns yet; run `market factors import` first", file=sys.stderr)
        return 1
    df = read_factor_exposures(lake, [security_id], date.min, on, model=args.model)
    if df.height == 0:
        print(
            f"market: no {args.model.upper()} exposures for {args.ticker} on or before {on}: too little history "
            "(a window needs enough days with both a return and a Factor Return) or Factor Returns do not reach a month-end yet",
            file=sys.stderr,
        )
        return 1
    row = df.sort("window_end").row(-1, named=True)
    print(f"{args.ticker} {args.model.upper()} exposure, window {row['window_start']} to {row['window_end']} ({row['n_obs']} observations)")
    print(f"Factor Return vintage {row['factor_vintage']}")
    print(f"alpha (daily)\t{row['alpha']:+.6f}\tt={row['t_alpha']:.2f}")
    for f in MODELS[args.model]:
        print(f"{LABELS[f]}\t{row[f'beta_{f}']:+.4f}\tt={row[f't_{f}']:.2f}")
    print(f"R-squared\t{row['r_squared']:.4f}")
    print(FRENCH_ATTRIBUTION)
    return 0
