"""Connection and migration helpers for the isolated hundred_acre_spend database (ADR-0001)."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from hundred_acre.pgdb import create_database_if_missing

DEFAULT_SERVER_URL = "postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/postgres"
DEFAULT_SPEND_URL = "postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/hundred_acre_spend"

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def spend_database_url() -> str:
    return os.environ.get("SPEND_DATABASE_URL", DEFAULT_SPEND_URL)


def server_admin_url() -> str:
    """URL of the Postgres server (any database) used to create/drop test databases."""
    return os.environ.get("SPEND_TEST_SERVER_URL", DEFAULT_SERVER_URL)


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def migrate(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url or spend_database_url()), revision)


def create_and_migrate(url: str | None = None) -> None:
    """Create the target database if it is missing, then upgrade it to head."""
    url = url or spend_database_url()
    create_database_if_missing(url)
    migrate(url)
