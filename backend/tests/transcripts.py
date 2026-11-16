"""Builders for small, hand-written fixture transcripts (no real conversation text)."""

import json
from pathlib import Path


def assistant_line(
    msg_id: str,
    *,
    session: str = "sess-1",
    model: str = "claude-sonnet-5-5",
    ts: str = "2026-11-16T03:00:00.000Z",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_5m: int = 0,
    cache_1h: int = 0,
    web_search: int = 0,
    subagent: bool = False,
    request_id: str | None = None,
    block: str = "text",
    speed: str | None = None,
) -> dict:
    line = {
        "type": "assistant",
        "sessionId": session,
        "isSidechain": subagent,
        "requestId": request_id or f"req_{msg_id}",
        "timestamp": ts,
        "cwd": "/work/project",
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": [{"type": block, "text": "REDACTED"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_5m + cache_1h,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cache_5m,
                    "ephemeral_1h_input_tokens": cache_1h,
                },
                "server_tool_use": {"web_search_requests": web_search, "web_fetch_requests": 0},
            },
        },
    }
    if speed:
        line["message"]["usage"]["speed"] = speed
    return line


def write_transcript(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    noise = [{"type": "user", "sessionId": lines[0]["sessionId"], "message": {"content": "REDACTED"}}]
    path.write_text("\n".join(json.dumps(x) for x in noise + lines) + "\n")
    return path


def bash_call(command: str, *, tool_id: str, session: str = "sess-1", branch: str | None = None) -> dict:
    """An assistant line carrying a Bash tool call (no usage; attribution reads only tool calls)."""
    line = {
        "type": "assistant",
        "sessionId": session,
        "timestamp": "2026-11-16T03:00:00.000Z",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}]},
    }
    if branch:
        line["gitBranch"] = branch
    return line


def tool_output(output: str, *, tool_id: str, session: str = "sess-1") -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}]},
    }


def text_line(text: str, *, session: str = "sess-1", role: str = "user", branch: str | None = None) -> dict:
    line = {"type": role, "sessionId": session, "message": {"role": role, "content": text}}
    if branch:
        line["gitBranch"] = branch
    return line


def creates_issue(number: int, *, tool_id: str = "t1", session: str = "sess-1") -> list[dict]:
    """The two transcript lines of a `gh issue create` call and its output."""
    return [
        bash_call("gh issue create --title X --body Y", tool_id=tool_id, session=session),
        tool_output(f"https://github.com/o/r/issues/{number}\n", tool_id=tool_id, session=session),
    ]
