"""Synthetic session builders for milestone G-A (docs/DESIGN.md 4.13.1 "Return evidence").

Everything is built in tmp_path from these helpers; every id is built in code. The main
transcript is `<root>/<slug>/<SESSION>.jsonl`, a child is
`<root>/<slug>/<SESSION>/subagents/agent-<id>.jsonl` with an optional `.meta.json`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION = "00000000-0000-4000-8000-000000000000"
SLUG = "-tmp-gproj"
BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
ABSENT = object()
NULL = object()  # a JSON null where a value is expected

X = "a" + "7" * 16
Y = "a" + "8" * 16
Z = "a" + "9" * 16


def iso(n: int) -> str:
    """The timestamp n seconds after BASE."""
    return (BASE + timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def use(tool_id, n, name="Agent", inp=None, *, ts=True):
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "id": tool_id, "name": name,
                "input": {} if inp is None else inp,
            }],
            "usage": {},
        },
    }
    if isinstance(ts, str):
        entry["timestamp"] = ts
    elif ts:
        entry["timestamp"] = iso(n)
    return entry


def result(tool_id, n, *, tur=ABSENT, agent=ABSENT, is_error=False):
    """A tool_result entry: block `agentId` is `agent`, entry `toolUseResult` is `tur`."""
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
    if agent is not ABSENT:
        block["agentId"] = agent
    if is_error:
        block["is_error"] = True
    entry = {
        "type": "user", "message": {"role": "user", "content": [block]}, "timestamp": iso(n),
    }
    if tur is not ABSENT:
        entry["toolUseResult"] = tur
    return entry


def sync_launch(tool_id, child, n, status="completed", *, ts=True):
    """An Agent launch and its synchronous result; status None means no status key, NULL a null status."""
    tur = {"agentId": child}
    if status is NULL:
        tur["status"] = None
    elif status is not None:
        tur["status"] = status
    return [use(tool_id, n, ts=ts), result(tool_id, n + 1, tur=tur)]


def async_launch(tool_id, child, n):
    tur = {"agentId": child, "status": "async_launched", "isAsync": True}
    return [use(tool_id, n), result(tool_id, n + 1, tur=tur)]


def envelope(task, tool, status):
    tool_part = "" if tool is None else f"<tool-use-id>{tool}</tool-use-id>\n"
    return (
        "<task-notification>\n"
        f"<task-id>{task}</task-id>\n{tool_part}"
        f"<output-file>/tmp/o</output-file>\n<status>{status}</status>\n"
        "</task-notification>"
    )


def note(n, task, tool, status, form="queue"):
    """An accepted notification: the queue form, the origin form, or the plain user form."""
    text = envelope(task, tool, status)
    if form == "queue":
        return {"type": "queue-operation", "operation": "enqueue", "content": text,
                "timestamp": iso(n)}
    entry = {"type": "user", "message": {"role": "user", "content": text}, "timestamp": iso(n)}
    if form == "origin":
        entry["origin"] = {"kind": "task-notification"}
    return entry


def send(tool_id, n, to, *, recipient=ABSENT, ts=True):
    inp = {} if to is ABSENT else {"to": to}
    if recipient is not ABSENT:
        inp["recipient"] = recipient
    return use(tool_id, n, "SendMessage", inp, ts=ts)


def resumed(tool_id, n, child):
    """A SendMessage result that resumed `child`."""
    return result(tool_id, n, tur={"success": True, "resumedAgentId": child})


def queued(tool_id, n):
    """A SendMessage result of a message queued into the running round."""
    return result(
        tool_id, n,
        tur={"success": True, "message": "Message queued for delivery to the running agent"},
    )


def child_entries(agent_id, start=1, end=3, last_text="Done."):
    return [
        {"type": "user", "message": {"role": "user", "content": "Do it."},
         "timestamp": iso(start), "agentId": agent_id},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [{"type": "text", "text": last_text}],
                     "usage": {}},
         "timestamp": iso(end), "agentId": agent_id},
    ]


def write_lines(path: Path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


class Session:
    """A synthetic session: main entries, children (entries, meta), then `build()`."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "root"
        self.main_entries: list = [{
            "type": "user", "message": {"role": "user", "content": "Start."},
            "timestamp": iso(0), "sessionId": SESSION,
        }]
        self.children: dict = {}

    @property
    def main_path(self) -> Path:
        return self.root / SLUG / f"{SESSION}.jsonl"

    @property
    def subagents(self) -> Path:
        return self.root / SLUG / SESSION / "subagents"

    def child_path(self, child: str) -> Path:
        return self.subagents / f"agent-{child}.jsonl"

    def main(self, *entries):
        for entry in entries:
            if isinstance(entry, list):
                self.main_entries.extend(entry)
            else:
                self.main_entries.append(entry)
        return self

    def child(self, child, *entries, meta=None, default=True):
        body = []
        for entry in entries:
            body.extend(entry if isinstance(entry, list) else [entry])
        if default and not body:
            body = child_entries(child)
        self.children[child] = (body, meta)
        return self

    def build(self) -> Path:
        write_lines(self.main_path, self.main_entries)
        for child, (body, meta) in self.children.items():
            write_lines(self.child_path(child), body)
            if meta is not None:
                self.child_path(child).with_name(f"agent-{child}.meta.json").write_text(
                    meta if isinstance(meta, str) else json.dumps(meta), encoding="utf-8"
                )
        return self.main_path


def states(main_path: Path) -> dict:
    """{agent_id: (return_state, return_reason)} of a full load."""
    from agent_postmortem import reader

    run = reader.load_session(main_path)
    return {a.agent_id: (a.return_state, a.return_reason) for a in run.attempts}


def touch_age(path: Path, seconds: float) -> None:
    """Set a file's mtime to `seconds` ago."""
    import time

    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def append_lines(path: Path, entries) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
