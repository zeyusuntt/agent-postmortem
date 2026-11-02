"""Synthetic fixture builders shared by the live-mode tests (docs/DESIGN.md 4.14).

Every root is built under tmp_path; ids are the repository's one fake session id and wf_ ids
listed in tests/synthetic-ids.txt. Nothing here copies real data.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAKE_UUID = "00000000-0000-4000-8000-000000000000"
CWD = "/home/user/project"
KILL_SENTINEL = "[Request interrupted by user]"
KILL_SENTINEL_TOOL = "[Request interrupted by user for tool use]"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def aid(n: int) -> str:
    """A conforming agent id: 'a' + 16 hex."""
    return "a" + format(n, "016x")


def user(agent_id: str | None, ts: datetime, content, **extra) -> dict:
    entry = {
        "type": "user", "message": {"role": "user", "content": content},
        "sessionId": FAKE_UUID, "version": "2.1.314", "cwd": CWD, "timestamp": iso(ts),
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
        entry["isSidechain"] = True
    entry.update(extra)
    return entry


def assistant_text(agent_id: str | None, ts: datetime, text: str) -> dict:
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        },
        "sessionId": FAKE_UUID, "version": "2.1.314", "cwd": CWD, "timestamp": iso(ts),
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


def tool_use(agent_id: str | None, ts: datetime, tool_id: str, name: str, tool_input: dict) -> dict:
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        },
        "sessionId": FAKE_UUID, "version": "2.1.314", "cwd": CWD, "timestamp": iso(ts),
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


def tool_result(agent_id: str | None, ts: datetime, tool_id: str, content: str,
                is_error: bool = False, **extra) -> dict:
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content,
                         "is_error": is_error}],
        },
        "sessionId": FAKE_UUID, "version": "2.1.314", "cwd": CWD, "timestamp": iso(ts),
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    entry.update(extra)
    return entry


def notification(ts: datetime, task_id: str, tool_use_id: str, status: str, summary: str) -> dict:
    envelope = (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n</task-notification>"
    )
    return {
        "type": "user", "message": {"role": "user", "content": envelope},
        "origin": {"kind": "task-notification"}, "sessionId": FAKE_UUID,
        "timestamp": iso(ts),
    }


def cost_state() -> dict:
    return {"type": "cost-state", "totalCostUSD": 0.01}


def write_jsonl(path: Path, entries: list[dict], *, mode: str = "w") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return path


def wf_dir(root: Path, slug: str, wf_id: str, session: str = FAKE_UUID) -> Path:
    return root / slug / session / "subagents" / "workflows" / wf_id


def write_clean_workflow(root: Path, slug: str, wf_id: str, start: datetime,
                         agent: int = 1) -> Path:
    """A finished one-agent workflow run whose journal resolves (result event)."""
    run_dir = wf_dir(root, slug, wf_id)
    agent_id = aid(agent)
    write_jsonl(run_dir / "journal.jsonl", [
        {"type": "launched"},
        {"type": "started", "key": "v2:k1", "agentId": agent_id, "label": "task", "phase": "P"},
        {"type": "result", "key": "v2:k1", "agentId": agent_id, "result": {"ok": True}},
    ])
    (run_dir / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "task"}), encoding="utf-8")
    write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        user(agent_id, start, "do the task"),
        assistant_text(agent_id, start + timedelta(seconds=30), "done"),
    ])
    return run_dir


def backdate_tree(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    targets = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    for target in targets:
        os.utime(target, (ts, ts))
