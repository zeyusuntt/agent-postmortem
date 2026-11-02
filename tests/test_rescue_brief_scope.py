"""The relaunch brief of a session names only what the subagents being rescued wrote
(docs/DESIGN.md 4.11, 0.4).

Field report 2026-09-30, "rescue plan names the right unit but attributes other agents'
files to it": in a session with 11 subagents, one was killed; the brief said "already
exists: 2 paths ... start from what exists; do not redo it", and both paths were written by
OTHER subagents of the session. A relaunched agent would have built on unrelated work. The
subagents of a session are separate tasks, so a session's brief takes the rescue units' own
attempts; a workflow relaunch re-runs the whole workflow, so its brief keeps every attempt.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from agent_postmortem import cli
from tests.test_field_v03 import (
    FAKE_SLUG,
    FAKE_UUID,
    _aid,
    _assistant_text,
    _assistant_tool,
    _kill,
    _no_config,
    _tool_result,
    _user,
    _write_jsonl,
)

KILLED = _aid(0xB51)
OTHER = _aid(0xB52)
KILLED_PATH = "/tmp/agent-postmortem-fixture/brief-own.py"
OTHER_PATH = "/tmp/agent-postmortem-fixture/brief-other.md"


def _session(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    main = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_jsonl(main, [
        {"type": "user", "message": {"role": "user", "content": "start the work"},
         "sessionId": FAKE_UUID, "timestamp": base.strftime("%Y-%m-%dT%H:%M:%SZ")},
    ])
    subagents = root / FAKE_SLUG / FAKE_UUID / "subagents"
    _write_jsonl(subagents / f"agent-{OTHER}.jsonl", [
        _user("Write the notes.", base, OTHER),
        _assistant_tool("to", "Write", {"file_path": OTHER_PATH, "content": "n"},
                        base + timedelta(seconds=1), OTHER),
        _tool_result("to", "File created successfully", base + timedelta(seconds=2), OTHER),
        _assistant_text("Done.", base + timedelta(seconds=3), OTHER),
    ])
    _write_jsonl(subagents / f"agent-{KILLED}.jsonl", [
        _user("Fix the solver.", base + timedelta(seconds=10), KILLED),
        _assistant_tool("tk", "Write", {"file_path": KILLED_PATH, "content": "k"},
                        base + timedelta(seconds=11), KILLED),
        _tool_result("tk", "File created successfully", base + timedelta(seconds=12), KILLED),
        _kill(base + timedelta(seconds=900), KILLED),
    ])
    old = (base + timedelta(seconds=901)).timestamp()
    for path in [main, *subagents.iterdir()]:
        os.utime(path, (old, old))
    return main


def _plan(capsys, main, *flags) -> dict:
    cli.main(["rescue", "plan", str(main), "--json", "--raw", *flags])
    return json.loads(capsys.readouterr().out)


def test_a_session_brief_names_only_what_the_rescued_subagent_wrote(tmp_path, monkeypatch, capsys):
    main = _session(tmp_path, monkeypatch)
    payload = _plan(capsys, main)
    assert [len(unit["paths"]) for unit in payload["units"]] == [1]
    assert payload["brief"]["already_exists"]["paths"] == [KILLED_PATH]


def test_the_session_brief_text_counts_the_same_paths(tmp_path, monkeypatch, capsys):
    main = _session(tmp_path, monkeypatch)
    cli.main(["rescue", "plan", str(main)])
    out = capsys.readouterr().out
    assert "already exists: 1 path" in out, out
