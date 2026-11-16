"""Thin HTTP layer over the Spend module."""

from functools import lru_cache

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, create_engine

from hundred_acre.spend import total_spend
from hundred_acre.spend.db import spend_database_url

router = APIRouter(prefix="/api/spend")


@lru_cache
def get_spend_engine() -> Engine:
    return create_engine(spend_database_url())


@router.get("/total")
def get_total_spend(engine: Engine = Depends(get_spend_engine)) -> dict[str, str | int]:
    total = total_spend(engine)
    return {
        "dollars": format(total.dollars.normalize(), "f"),  # full precision; the UI rounds to the cent
        "unpriced_messages": total.unpriced_messages,
    }
