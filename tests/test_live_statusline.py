"""docs/DESIGN.md 4.14.7: `agent-postmortem statusline subagents`, the subagentStatusLine command.
It writes a row only where it adds a fact; every other row keeps Claude Code's own rendering."""

from __future__ import annotations

import io
import json
import os
import time
from datetime import timedelta

from live_helpers import (
    FAKE_UUID,
    KILL_SENTINEL_TOOL,
    aid,
    assistant_text,
    tool_use,
    user,
    write_jsonl,
)
from test_live_session import SLUG, T0, _sub_path, _writes

from agent_postmortem.cli import main


def _main(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    return main_path


def _task(agent_id: str, status: str, **extra) -> dict:
    task = {"id": agent_id, "type": "local_agent", "agentType": "general-purpose",
            "status": status, "description": "Review the diff", "label": "Review the diff",
            "startTime": 0, "tokenCount": 12345, "tokenSamples": [12345] * 16, "cwd": "/tmp"}
    task.update(extra)
    return task


def _run(monkeypatch, capsys, main_path, tasks, columns=100) -> dict[str, str]:
    payload = {"session_id": FAKE_UUID, "transcript_path": str(main_path), "cwd": "/tmp",
               "columns": columns, "tasks": tasks}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["statusline", "subagents"]) == 0
    rows = {}
    for line in capsys.readouterr().out.splitlines():
        obj = json.loads(line)
        rows[obj["id"]] = obj["content"]
    return rows


def test_a_quiet_running_subagent_shows_its_quiet_time(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    quiet, busy = aid(51), aid(52)
    for agent in (quiet, busy):
        write_jsonl(_sub_path(main_path, agent), [
            user(agent, T0, "do it"), tool_use(agent, T0, "toolu_q", "Bash", {"command": "make"})])
    old = time.time() - 300
    os.utime(_sub_path(main_path, quiet), (old, old))
    rows = _run(monkeypatch, capsys, main_path, [_task(quiet, "running"), _task(busy, "running")])
    assert list(rows) == [quiet]
    assert rows[quiet].startswith("Review the diff · 12.3k tokens · ")
    assert rows[quiet].endswith("quiet 5m")


def test_an_unexplained_interrupt_shows_what_it_changed(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(53)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"), *_writes(agent, T0, 2),
        user(agent, T0 + timedelta(seconds=700), [{"type": "text", "text": KILL_SENTINEL_TOOL}]),
    ])
    rows = _run(monkeypatch, capsys, main_path, [_task(agent, "killed")])
    assert "interrupted, nothing in this session stopped it" in rows[agent]
    assert "changed 2 paths" in rows[agent]


def test_a_return_on_an_error_says_so(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(54)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"),
        assistant_text(agent, T0, "API Error: Server error mid-response"),
    ])
    rows = _run(monkeypatch, capsys, main_path, [_task(agent, "completed")])
    assert rows[agent].endswith("returned, but its last entry says: a server error occurred")


def test_a_clean_row_and_an_unknown_id_keep_the_native_rendering(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(55)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "do it"),
                                             assistant_text(agent, T0, "Done.")])
    rows = _run(monkeypatch, capsys, main_path,
                [_task(agent, "completed"), _task("not-an-agent-id", "killed")])
    assert rows == {}


def test_a_row_is_cut_to_the_columns_given(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(56)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"),
        assistant_text(agent, T0, "API Error: Server error mid-response"),
    ])
    rows = _run(monkeypatch, capsys, main_path,
                [_task(agent, "completed", label="x" * 200)], columns=60)
    assert len(rows[agent]) <= 60
    assert rows[agent].endswith("a server error occurred")


def test_bad_input_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert main(["statusline", "subagents"]) == 0
    assert capsys.readouterr().out == ""


def test_opt_out_prints_nothing(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(57)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    monkeypatch.setenv("AGENT_POSTMORTEM_STATUSLINE", "0")
    assert _run(monkeypatch, capsys, main_path, [_task(agent, "completed")]) == {}
