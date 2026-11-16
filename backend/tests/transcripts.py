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
