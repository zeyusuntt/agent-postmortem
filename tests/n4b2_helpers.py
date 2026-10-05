"""Shared synthetic builders for the N4b-2 tests (ledger rows, reasons, risk
population, repository grouping). Fixtures are synthetic: the paths are under
/tmp, the ids are the registered synthetic ones, nothing is a real transcript.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem import reader
from tests.test_field_v03 import _aid, _wf_dir


class _Lazy:
    """The ledger module, looked up on first use (RED: it does not exist yet)."""

    def __getattr__(self, name):
        return getattr(importlib.import_module("agent_postmortem.ledger"), name)


ledger = _Lazy()
CWD = "/tmp/ws"
WF = "wf_00000000-d01"
T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
ANCHORED = "Command did not complete within its 120s timeout and was moved to the background"
NAME = "t.jsonl"


def iso(seconds: int = 0) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def use(tool_id, name, input_obj, cwd=CWD, seconds=None):
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}],
            "usage": {},
        },
    }
    if cwd is not None:
        entry["cwd"] = cwd
    if seconds is not None:
        entry["timestamp"] = iso(seconds)
    return entry


def result(tool_id, text="ok", is_error=False, seconds=None):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    if is_error:
        block["is_error"] = True
    entry = {"type": "user", "message": {"role": "user", "content": [block]}}
    if seconds is not None:
        entry["timestamp"] = iso(seconds)
    return entry


def text(seconds=None, who="assistant", body="done"):
    content = body if who == "user" else [{"type": "text", "text": body}]
    entry = {"type": who, "message": {"role": who, "content": content}}
    if seconds is not None:
        entry["timestamp"] = iso(seconds)
    return entry


def write_lines(path: Path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in entries),
        encoding="utf-8",
    )


def read_calls(tmp_path, entries):
    path = tmp_path / NAME
    write_lines(path, entries)
    return reader._read_transcript(path, [])[5]


def one_call(tmp_path, name, input_obj, result_text=None, *, cwd=CWD, is_error=False):
    entries = [use("t1", name, input_obj, cwd)]
    if result_text is not None or is_error:
        entries.append(result("t1", result_text or "", is_error=is_error))
    (call,) = read_calls(tmp_path, entries)
    return call


def stamp(entries, start=10):
    """Give every dict entry without a timestamp one (T0 + start + position), so
    the attempt has a first_ts that falls after the generation's result."""
    out = []
    for position, entry in enumerate(entries):
        if isinstance(entry, dict) and "timestamp" not in entry:
            entry = {**entry, "timestamp": iso(start + position)}
        out.append(entry)
    return out


def auto_parent(wf_id=WF):
    """A parent transcript with one Workflow launch (line 1) and its result
    (line 2, T0 + 1): result_ts is T0 + 1, so an attempt starting later is
    assigned to the one generation."""
    from tests.test_field_v03 import _workflow_launch, _workflow_launch_result
    return [_workflow_launch("toolu_A", T0), _workflow_launch_result("toolu_A", T0 + timedelta(seconds=1), wf_id, "tA")]


def workflow_run(tmp_path, entries, *, journal="completed", agent_n=1, wf_id=WF, parent="auto",
                 stamped=True):
    """A workflow run with one attempt whose transcript is `entries`.
    journal: 'completed', 'failed', 'open' (started only), or None (no key).
    parent: 'auto' (one generation), None (no parent transcript), or entries."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(agent_n)
    started = {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl",
               "phase": "Implement"}
    if journal is None:
        started = {"type": "started", "agentId": agent_id, "label": "impl", "phase": "Implement"}
    lines = [started]
    if journal == "completed":
        lines.append({"type": "result", "key": "v2:x", "agentId": agent_id, "result": {"ok": True}})
    elif journal == "failed":
        lines.append({"type": "failed", "key": "v2:x", "agentId": agent_id, "error": "x"})
    write_lines(run_dir / "journal.jsonl", lines)
    if entries is not None:
        write_lines(run_dir / f"agent-{agent_id}.jsonl", stamp(entries) if stamped else entries)
    if parent == "auto":
        parent = auto_parent(wf_id)
    if parent is not None:
        write_lines(root / "-home-user-project" / "00000000-0000-4000-8000-000000000000.jsonl",
                    parent)
    return reader.load_run(run_dir)


def ledger_of(tmp_path, entries, **kwargs):
    return workflow_run(tmp_path, entries, **kwargs).attempts[0].ledger


SESSION_UUID = "00000000-0000-4000-8000-000000000000"


def session_run(tmp_path, main_entries, children=None):
    """A session run: main transcript plus subagent transcripts {agent_id: entries}."""
    root = tmp_path / "root"
    main = root / "-tmp-proj" / f"{SESSION_UUID}.jsonl"
    stamped = stamp(main_entries)
    stamped[0] = {**stamped[0], "sessionId": SESSION_UUID}
    write_lines(main, stamped)
    for agent_id, entries in (children or {}).items():
        write_lines(
            root / "-tmp-proj" / SESSION_UUID / "subagents" / f"agent-{agent_id}.jsonl",
            stamp(entries),
        )
    return reader.load_run(main)
