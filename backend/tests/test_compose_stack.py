"""Opt-in smoke test: build and start the real Compose stack under a throwaway project, port and data dir."""
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.compose

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    def __init__(self, project: str, env: dict[str, str], web_port: int):
        self.project, self.env, self.web_port = project, env, web_port

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", str(REPO_ROOT / "compose.yaml"), *args],
            cwd=REPO_ROOT, env={**os.environ, **self.env}, capture_output=True, text=True, check=check,
        )

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.web_port}{path}", timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def run_in_api(self, *cmd: str) -> subprocess.CompletedProcess:
        return self.compose("run", "--rm", "-T", "api", *cmd, check=False)

    def container_state(self, service: str) -> dict:
        out = self.compose("ps", "-a", "--format", "json", service).stdout
        return json.loads(out.splitlines()[0])


@pytest.fixture(scope="module")
def stack():
    web_port = _free_port()
    # under the repo, not /tmp: sandboxed Docker installs (snap) cannot bind-mount host /tmp or hidden directories
    data_dir = REPO_ROOT / f"smoke-data-{uuid.uuid4().hex[:8]}"
    data_dir.mkdir()
    data_dir.chmod(0o777)
    env = {"HUNDRED_ACRE_DATA_DIR": str(data_dir), "MASSIVE_API_KEY": "smoke-test-key","POSTGRES_PASSWORD": "smoke_test", "POSTGRES_BIND": "127.0.0.1", "POSTGRES_PORT": str(_free_port()),
           "WEB_BIND": "127.0.0.1", "WEB_PORT": str(web_port), "GH_TOKEN_READ": ""}
    s = Stack(f"hundred-acre-smoke-{uuid.uuid4().hex[:8]}", env, web_port)
    s.data_dir = data_dir
    try:
        up = s.compose("up", "-d", "--build", check=False)
        assert up.returncode == 0, up.stdout + up.stderr
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                if s.get("/api/health")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            pytest.fail("web did not become healthy:\n" + s.compose("logs", check=False).stdout)
        yield s
    finally:
        s.compose("down", "-v", "--rmi", "local", "--remove-orphans", check=False)
        # containers write as root, so clear the data dir from inside one before pytest removes it
        subprocess.run(["docker", "run", "--rm", "-v", f"{data_dir}:/d", "busybox", "sh", "-c", "rm -rf /d/* /d/.[!.]*"],
                       check=False, capture_output=True)
        shutil.rmtree(data_dir, ignore_errors=True)


def test_health_is_proxied_to_the_api_through_web(stack):
    status, body = stack.get("/api/health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_web_serves_the_front_end_with_spa_fallback(stack):
    status, body = stack.get("/")
    assert status == 200 and '<div id="root">' in body
    status, deep = stack.get("/some/deep/link")
    assert status == 200 and deep == body


def test_migrate_exited_zero(stack):
    state = stack.container_state("migrate")
    assert state["State"] == "exited" and state["ExitCode"] == 0


def test_spend_views_report_github_unavailable_without_a_token(stack):
    status, body = stack.get("/api/spend/features/49")
    assert status == 200
    assert json.loads(body)["github_available"] is False


def test_request_engine_is_running_after_migrate(stack):
    deadline = time.time() + 60
    while time.time() < deadline and stack.container_state("request-engine")["State"] != "running":
        time.sleep(1)
    state = stack.container_state("request-engine")
    assert state["State"] == "running", stack.compose("logs", "request-engine", check=False).stdout
    time.sleep(5)  # it stays running rather than crash-looping
    assert stack.container_state("request-engine")["State"] == "running"


def test_scheduler_is_running_after_migrate(stack):
    deadline = time.time() + 60
    while time.time() < deadline and stack.container_state("scheduler")["State"] != "running":
        time.sleep(1)
    state = stack.container_state("scheduler")
    assert state["State"] == "running", stack.compose("logs", "scheduler", check=False).stdout
    time.sleep(5)  # it stays running rather than crash-looping
    assert stack.container_state("scheduler")["State"] == "running"


def test_status_command_reports_the_running_engine(stack):
    deadline = time.time() + 60
    while True:
        out = stack.run_in_api("market", "request-engine", "status")
        if "engine: running" in out.stdout or time.time() > deadline:
            break
        time.sleep(2)
    assert "engine: running" in out.stdout, out.stdout + out.stderr


def test_host_and_container_read_the_same_bars_from_the_lake(stack):
    from hundred_acre.market.lake import Lake, BAR_SCHEMA, bar_row, read_bars, write_bars
    import polars as pl

    data_dir = stack.data_dir
    pg_port = stack.env["POSTGRES_PORT"]
    host_lake = Lake(f"postgresql+psycopg://hundred_acre:smoke_test@127.0.0.1:{pg_port}/hundred_acre_market", data_dir / "lake")
    bar = SimpleNamespace(open=1.0, high=2.0, low=0.5, close=1.5, volume=100.0, vwap=1.4, trade_count=7)
    day = date(2024, 1, 2)
    row = bar_row(1, day, bar, source="smoke", fetched_at=datetime(2024, 1, 3, tzinfo=timezone.utc))
    write_bars(host_lake, pl.DataFrame([row], schema=BAR_SCHEMA), day)

    out = stack.run_in_api(
        "python", "-c",
        "from datetime import date; from hundred_acre.market.lake import Lake, read_bars; "
        "print(read_bars(Lake.from_env(), [1], date(2024,1,2), date(2024,1,2), ['close']).to_dicts())",
    )
    assert "'close': 1.5" in out.stdout, out.stdout + out.stderr
    assert read_bars(host_lake, [1], day, day, ["close"]).to_dicts() == [{"close": 1.5}]
    assert list((data_dir / "lake").rglob("*.parquet"))


def test_duckdb_extensions_load_with_no_network(stack):
    image = stack.compose("images", "--format", "json", "api").stdout
    image_id = json.loads(image)[0]["ID"] if image.lstrip().startswith("[") else json.loads(image.splitlines()[0])["ID"]
    out = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", image_id, "python", "-c",
         "import duckdb; c = duckdb.connect(); "
         "c.execute('INSTALL ducklake; INSTALL postgres; LOAD ducklake; LOAD postgres'); print('loaded')"],
        capture_output=True, text=True,
    )
    assert "loaded" in out.stdout, out.stdout + out.stderr
