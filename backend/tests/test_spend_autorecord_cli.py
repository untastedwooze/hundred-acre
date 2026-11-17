import shutil
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from hundred_acre.spend import total_spend

from transcripts import assistant_line, write_transcript


def _spend() -> str:
    found = shutil.which("spend", path=str(Path(sys.executable).parent))
    assert found
    return found


def test_spend_hook_exits_zero_quickly_even_when_database_is_unreachable(tmp_path):
    transcript = write_transcript(tmp_path / "s.jsonl", [assistant_line("m1", output_tokens=5)])
    env = {
        "SPEND_DATABASE_URL": "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none",
        "SPEND_QUEUE_FILE": str(tmp_path / "queue"),
        "PATH": str(Path(sys.executable).parent),
    }
    started = time.monotonic()
    result = subprocess.run(
        [_spend(), "hook"],
        input=f'{{"transcript_path": "{transcript}"}}',
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 5
    assert str(transcript) in (tmp_path / "queue").read_text()


def test_spend_sweep_records_project_and_worktree_transcripts(spend_engine, tmp_path):
    line = assistant_line("m1", output_tokens=1_000_000)
    write_transcript(tmp_path / "projects" / "-r-proj--claude-worktrees-w1" / "sess-1.jsonl", [line])
    env = {
        "SPEND_DATABASE_URL": spend_engine.url.render_as_string(hide_password=False),
        "PATH": str(Path(sys.executable).parent),
    }
    result = subprocess.run(
        [_spend(), "sweep", "--projects-dir", str(tmp_path / "projects"), "--repo-root", "/r/proj"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert total_spend(spend_engine).dollars == Decimal("10")
