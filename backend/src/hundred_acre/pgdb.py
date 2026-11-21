"""Create a Postgres database on its server if it is missing."""

from collections.abc import Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

MAINTENANCE_DATABASE = "postgres"


def create_database_if_missing(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database=MAINTENANCE_DATABASE), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("select 1 from pg_database where datname = :n"), {"n": target.database}).scalar()
            if not exists:
                quoted = target.database.replace('"', '""')
                conn.execute(text(f'CREATE DATABASE "{quoted}"'))
    finally:
        admin.dispose()


def create_and_migrate(url: str, migrate: Callable[[str], None]) -> None:
    """Create the target database if it is missing, then run `migrate(url)` (an upgrade to head)."""
    create_database_if_missing(url)
    migrate(url)
