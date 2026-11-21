"""Opt-in smoke test: build and start the real Compose stack under a throwaway project, port and data dir."""
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

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

    def container_state(self, service: str) -> dict:
        out = self.compose("ps", "-a", "--format", "json", service).stdout
        return json.loads(out.splitlines()[0])


@pytest.fixture(scope="module")
def stack():
    web_port = _free_port()
    env = {"POSTGRES_PASSWORD": "smoke_test", "POSTGRES_BIND": "127.0.0.1", "POSTGRES_PORT": str(_free_port()),
           "WEB_BIND": "127.0.0.1", "WEB_PORT": str(web_port), "GH_TOKEN_READ": ""}
    s = Stack(f"hundred-acre-smoke-{uuid.uuid4().hex[:8]}", env, web_port)
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
