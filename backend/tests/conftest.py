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


@pytest.fixture
def market_db_url():
    """URL of a throwaway, migrated `hundred_acre_market`-style database; dropped at teardown."""
    from hundred_acre.market.db import market_server_admin_url
    from hundred_acre.market.db import migrate as migrate_market

    admin = create_engine(market_server_admin_url(), isolation_level="AUTOCOMMIT")
    name = f"hundred_acre_market_test_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(market_server_admin_url()).set(database=name).render_as_string(hide_password=False)
    migrate_market(url)
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def market_engine(market_db_url):
    engine = create_engine(market_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def lake(market_db_url, tmp_path):
    """A DuckLake whose catalog is the throwaway database and whose data path is a temp directory."""
    from hundred_acre.market.lake import Lake

    return Lake(catalog_url=market_db_url, data_path=tmp_path / "ducklake")
