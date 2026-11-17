import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from hundred_acre.spend.db import migrate, server_admin_url


@pytest.fixture
def spend_engine_factory():
    """Create throwaway, migrated Postgres databases; all dropped at teardown."""
    admin = create_engine(server_admin_url(), isolation_level="AUTOCOMMIT")
    created: list[tuple[str, object]] = []

    def make():
        name = f"hundred_acre_spend_test_{uuid.uuid4().hex[:12]}"
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        url = make_url(server_admin_url()).set(database=name)
        migrate(url.render_as_string(hide_password=False))
        engine = create_engine(url)
        created.append((name, engine))
        return engine

    yield make

    with admin.connect() as conn:
        for name, engine in created:
            engine.dispose()
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def spend_engine(spend_engine_factory):
    return spend_engine_factory()
