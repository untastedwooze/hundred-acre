import json
from decimal import Decimal

from sqlalchemy import create_engine

from hundred_acre.spend import record_from_hook, sweep, total_spend

from transcripts import assistant_line, write_transcript

REPO = "/home/u/proj"


def _dollars_line(msg_id, session="sess-1", **kw):
    return assistant_line(msg_id, session=session, output_tokens=1_000_000, **kw)  # $10 each


def _hook_payload(transcript):
    return json.dumps({"hook_event_name": "Stop", "session_id": "x", "transcript_path": str(transcript)})


def test_hook_records_current_transcript_and_its_subagents(spend_engine, tmp_path):
    t = write_transcript(tmp_path / "sess-1.jsonl", [_dollars_line("m1")])
    write_transcript(tmp_path / "sess-1" / "subagents" / "agent-a.jsonl", [_dollars_line("m2", subagent=True)])
    record_from_hook(_hook_payload(t), spend_engine, queue_file=tmp_path / "queue")
    assert total_spend(spend_engine).dollars == Decimal("20")


def test_hook_never_raises_on_garbage_input(spend_engine, tmp_path):
    for payload in ["", "not json", "{}", json.dumps({"transcript_path": str(tmp_path / "missing.jsonl")})]:
        record_from_hook(payload, spend_engine, queue_file=tmp_path / "queue")
    assert total_spend(spend_engine).dollars == Decimal("0")


def test_failed_recording_is_queued_and_retried_on_next_run(spend_engine, tmp_path):
    queue = tmp_path / "queue"
    t1 = write_transcript(tmp_path / "sess-1.jsonl", [_dollars_line("m1")])
    t2 = write_transcript(tmp_path / "sess-2.jsonl", [_dollars_line("m2", session="sess-2")])
    unreachable = create_engine(
        "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )

    record_from_hook(_hook_payload(t1), unreachable, queue_file=queue)  # must not raise
    assert total_spend(spend_engine).dollars == Decimal("0")

    record_from_hook(_hook_payload(t2), spend_engine, queue_file=queue)  # retries t1 too
    assert total_spend(spend_engine).dollars == Decimal("20")
    # Queue drained: a later run does not need the queued file again.
    t1.unlink()
    record_from_hook(_hook_payload(t2), spend_engine, queue_file=queue)
    assert total_spend(spend_engine).dollars == Decimal("20")


def test_sweep_records_repo_and_worktree_transcripts_but_not_other_projects(spend_engine, tmp_path):
    projects = tmp_path / "projects"
    write_transcript(projects / "-home-u-proj" / "s-main.jsonl", [_dollars_line("m1", session="s-main")])
    write_transcript(
        projects / "-home-u-proj--claude-worktrees-agent-1" / "s-wt.jsonl", [_dollars_line("m2", session="s-wt")]
    )
    write_transcript(projects / "-home-u-elsewhere-wt" / "s-wt2.jsonl", [_dollars_line("m3", session="s-wt2")])
    write_transcript(projects / "-home-u-proj-other" / "s-other.jsonl", [_dollars_line("m4", session="s-other")])
    write_transcript(projects / "-home-u-other" / "s-x.jsonl", [_dollars_line("m5", session="s-x")])

    result = sweep(spend_engine, projects, REPO, worktrees=["/home/u/elsewhere/wt"])
    assert result.messages_seen == 3
    assert total_spend(spend_engine).dollars == Decimal("30")
    assert sweep(spend_engine, projects, REPO, worktrees=["/home/u/elsewhere/wt"]).messages_added == 0
