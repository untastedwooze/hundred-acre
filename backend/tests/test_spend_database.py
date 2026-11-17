from alembic.script import ScriptDirectory
from sqlalchemy import text

from hundred_acre.spend.db import alembic_config


def test_fixture_provides_migrated_throwaway_database(spend_engine):
    head = ScriptDirectory.from_config(alembic_config("unused")).get_current_head()
    with spend_engine.connect() as conn:
        version = conn.execute(text("select version_num from alembic_version")).scalar()
    assert version == head


def test_fixtures_are_isolated_databases(spend_engine, spend_engine_factory):
    other = spend_engine_factory()
    assert other.url.database != spend_engine.url.database
