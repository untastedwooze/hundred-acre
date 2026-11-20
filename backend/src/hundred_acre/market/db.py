"""Connection and migration helpers for the isolated hundred_acre_market database (ADR-0002).

The database holds relational market data (Securities, Tickers) and the DuckLake catalog for Bars.
"""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

DEFAULT_SERVER_URL = "postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/postgres"
DEFAULT_MARKET_URL = "postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/hundred_acre_market"

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def market_database_url() -> str:
    return os.environ.get("MARKET_DATABASE_URL", DEFAULT_MARKET_URL)


def market_server_admin_url() -> str:
    """URL of the Postgres server (any database) used to create/drop test databases."""
    return os.environ.get("MARKET_TEST_SERVER_URL", DEFAULT_SERVER_URL)


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def migrate(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url or market_database_url()), revision)
