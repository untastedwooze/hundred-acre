"""Delistings and spinoffs (#24).

A Delisting is a Security's terminal outcome: cash per share, conversion into another Security at a ratio, or
worthless. `sync_delistings` gives every Security whose last Ticker period has ended an assumed cash outcome at its
last Daily Bar's close; `set_delisting_outcome` overrides it by hand (no longer assumed, and never overwritten by a
re-sync). A spinoff links a child Security to its parent by shares per parent share; the parent keeps trading.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, text

from hundred_acre.market.lake import Lake, read_bars


@dataclass(frozen=True)
class Delisting:
    security_id: int
    delisted_on: date  # last trading day
    outcome: str  # "cash" | "conversion" | "worthless"
    cash_per_share: float | None
    target_security_id: int | None
    ratio: float | None  # target shares per share held
    assumed: bool


@dataclass(frozen=True)
class Spinoff:
    parent_security_id: int
    child_security_id: int
    ratio: float  # child shares per parent share held
    spin_date: date


_COLUMNS = "security_id, delisted_on, outcome, cash_per_share, target_security_id, ratio, assumed"


def sync_delistings(engine: Engine, lake: Lake) -> list[int]:
    """Record an assumed cash Delisting (at the last close) for each newly delisted Security; returns their ids.

    A Security is delisted when every one of its Ticker periods has ended. Securities with no Bars are skipped.
    """
    with engine.connect() as conn:
        candidates = conn.execute(
            text(
                "SELECT p.security_id, max(p.valid_to) FROM ticker_periods p "
                "WHERE NOT EXISTS (SELECT 1 FROM delistings d WHERE d.security_id = p.security_id) "
                "GROUP BY p.security_id HAVING bool_and(p.valid_to IS NOT NULL) ORDER BY p.security_id"
            )
        ).all()
    created = []
    for security_id, last_day in candidates:
        bars = read_bars(lake, [security_id], date.min, last_day, columns=["trading_date", "close"])
        if bars.is_empty():
            continue
        last = bars.sort("trading_date").row(-1, named=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO delistings (security_id, delisted_on, outcome, cash_per_share, assumed) "
                    "VALUES (:s, :d, 'cash', :c, true) ON CONFLICT DO NOTHING"
                ),
                {"s": security_id, "d": last["trading_date"], "c": last["close"]},
            )
        created.append(security_id)
    return created


def set_delisting_outcome(
    engine: Engine, security_id: int, *, cash_per_share: float | None = None,
    convert_to: int | None = None, ratio: float | None = None, worthless: bool = False,
) -> None:
    """Replace a Security's Delisting outcome by hand with exactly one of cash, conversion (`convert_to` + `ratio`) or worthless."""
    chosen = [cash_per_share is not None, convert_to is not None, worthless]
    if sum(chosen) != 1:
        raise ValueError("give exactly one of cash_per_share, convert_to or worthless")
    if convert_to is not None and (ratio is None or ratio <= 0):
        raise ValueError("a conversion needs a positive ratio")
    if ratio is not None and convert_to is None:
        raise ValueError("ratio only applies to a conversion")
    outcome = "cash" if cash_per_share is not None else "conversion" if convert_to is not None else "worthless"
    with engine.begin() as conn:
        updated = conn.execute(
            text(
                "UPDATE delistings SET outcome = :o, cash_per_share = :c, target_security_id = :t, ratio = :r, "
                "assumed = false, updated_at = now() WHERE security_id = :s"
            ),
            {"o": outcome, "c": cash_per_share, "t": convert_to, "r": ratio, "s": security_id},
        ).rowcount
    if not updated:
        raise ValueError(f"Security {security_id} has no Delisting")


def delisting(engine: Engine, security_id: int) -> Delisting | None:
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT {_COLUMNS} FROM delistings WHERE security_id = :s"), {"s": security_id}).first()
    return Delisting(*row) if row else None


def assumed_delistings(engine: Engine) -> list[Delisting]:
    """Delistings whose outcome has not been confirmed by hand, oldest delisting first."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_COLUMNS} FROM delistings WHERE assumed ORDER BY delisted_on, security_id")
        ).all()
    return [Delisting(*r) for r in rows]


def record_spinoff(engine: Engine, *, parent_id: int, child_id: int, ratio: float, spin_date: date) -> None:
    """Link `child_id` to `parent_id`: `ratio` child shares per parent share held on `spin_date`. Replaces an earlier link."""
    if ratio <= 0 or parent_id == child_id:
        raise ValueError("a spinoff needs a positive ratio and a child distinct from its parent")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO spinoffs (child_security_id, parent_security_id, ratio, spin_date) VALUES (:c, :p, :r, :d) "
                "ON CONFLICT (child_security_id) DO UPDATE SET parent_security_id = :p, ratio = :r, spin_date = :d"
            ),
            {"c": child_id, "p": parent_id, "r": ratio, "d": spin_date},
        )


def spinoffs_of(engine: Engine, parent_id: int) -> list[Spinoff]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT parent_security_id, child_security_id, ratio, spin_date FROM spinoffs "
                "WHERE parent_security_id = :p ORDER BY spin_date, child_security_id"
            ),
            {"p": parent_id},
        ).all()
    return [Spinoff(*r) for r in rows]
