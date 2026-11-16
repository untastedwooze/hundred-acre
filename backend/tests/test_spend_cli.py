import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text


def _spend_executable() -> str:
    found = shutil.which("spend", path=str(Path(sys.executable).parent))
    assert found, "the `spend` console script is not installed"
    return found


def test_spend_cli_responds_to_help():
    result = subprocess.run([_spend_executable(), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_spend_migrate_brings_database_to_head(spend_engine_factory):
    # Create a database via the factory (already migrated), then re-run via CLI: idempotent.
    engine = spend_engine_factory()
    url = engine.url.render_as_string(hide_password=False)
    env = {"SPEND_DATABASE_URL": url, "PATH": str(Path(sys.executable).parent)}
    result = subprocess.run([_spend_executable(), "migrate"], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from alembic_version")).scalar() == 1
