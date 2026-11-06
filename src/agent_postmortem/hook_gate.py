"""The fast gate of `hook post-tool` and `hook prompt-submit` (docs/DESIGN.md 4.14.5, 0.4).

These hooks fire on every tool call (PostToolUse) and every prompt (UserPromptSubmit) of a
session, so the return-time checks reach the agent without a Monitor. Most calls change nothing
the checks read, and this module answers those from a few stats, imports nothing but the
standard library and prints nothing. It opens the gate to the full check (hooks_cli.
post_tool_check) only when:

- no gate state exists for the session yet;
- a subagent transcript under `<session>/subagents/` was added or changed size or mtime: an
  ending (a kill marker, a final entry, an error) is always written there;
- the bytes appended to the main transcript since the last call name a task notification, an
  agent id (a launch or a return), an interrupt or a TaskStop: the facts that decide or explain
  an ending;
- an earlier check left a kill waiting for its native notice (`pending`);
- a background command was launched (`"backgroundTaskId"` in the
  appended bytes), or the time the last check stored, when a running command can first have
  stopped writing (`recheck_at`), has come. A session with no subagent keeps no gate state
  until its transcript's tail names a background command or a worker;
- this call launched a worker (`claude --bg`, read from the payload,
  since the transcript can lag), the appended bytes name one (`--bg`, `--background`, `claude
  attach `), or a worker's job file says something new: its file is parsed only when its size or
  mtime moved, at most 8 per call, and the gate opens only if its state, class or `needs` did.

Gate state is one small private file per session in `$STATE_DIR/hook-gate/`, read and written
only through state.py's verified private-file calls (4.14.9 item 4), and pruned after 7 days
when a new session's file is written. Any error is silent: the hook never fails a turn.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import state, workers  # standard library only; every write goes through state

try:  # the same sha256 as hashlib's, without loading OpenSSL on every tool call (3.12 and later)
    from _sha2 import sha256
except ImportError:
    try:
        from _sha256 import sha256  # 3.11
    except ImportError:
        from hashlib import sha256  # a build with neither

_EVENTS = {"post-tool": "PostToolUse", "prompt-submit": "UserPromptSubmit"}
_LAUNCH_MARKS = (b'"backgroundTaskId"', b"--bg", b"--background", b"claude attach ")
_MAIN_MARKS = (b"task-notification", b'"agentId"', b"[Request interrupted", b"TaskStop",
               *_LAUNCH_MARKS)
_GATE_VERSION = 2  # 2: `workers`; a file of another version opens the gate once
_TAIL_KEEP = 64  # bytes kept before the offset, so a mark split across two reads is still seen
_LINE_SEARCH = 1 << 16  # how far back a first call looks for the last complete line
_GATE_PRUNE_SECONDS = 7 * 86400  # the live store's own pruning age (4.14.8)


def _gate_path(main_path: str) -> Path:
    digest = sha256(os.path.realpath(main_path).encode("utf-8", "surrogateescape"))
    return state.state_dir() / "hook-gate" / f"{digest.hexdigest()[:32]}.json"


def _signature(subagents: str) -> str:
    """A digest of every `agent-*.jsonl` directly under `subagents` with its size and mtime."""
    rows = []
    try:
        with os.scandir(subagents) as entries:
            for entry in entries:
                name = entry.name
                if not (name.startswith("agent-") and name.endswith(".jsonl")):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rows.append(f"{name}:{info.st_size}:{info.st_mtime_ns}")
    except OSError:
        return ""
    rows.sort()
    return sha256("\n".join(rows).encode()).hexdigest()


def _read_gate(path: Path) -> dict | None:
    raw = state.read_private_bytes(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write_gate(path: Path, data: dict, *, new: bool = False) -> None:
    try:
        state.write_private_bytes(path, json.dumps(data).encode("utf-8"))
    except state.StateError:
        return
    if new:  # a session's first gate file: drop the ones no session touched for 7 days
        state.prune_private_files(path.parent, time.time() - _GATE_PRUNE_SECONDS)


def _read_range(main_path: str, start: int, end: int) -> bytes | None:
    try:
        with Path(main_path).open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start)
    except OSError:
        return None


def _main_delta(main_path: str, gate: dict | None) -> tuple[bool, dict]:
    """(whether the bytes appended since the gate's offset carry a mark, the new position). The
    position ends at the last complete line, so a line still being written is scanned again,
    whole, once its newline lands."""
    try:
        info = os.stat(main_path)
    except OSError:
        return False, {}
    previous = gate.get("main") if gate else None
    if not (isinstance(previous, dict) and previous.get("inode") == info.st_ino
            and isinstance(previous.get("offset"), int) and previous["offset"] <= info.st_size):
        # no gate yet, or the transcript was replaced or shrank: the full check reads it anyway,
        # so only the last 64 KB are read here, to find where its last complete line ends (a
        # 59 MB transcript took 0.4 s to read whole)
        base = max(0, info.st_size - _LINE_SEARCH)
        tail = _read_range(main_path, base, info.st_size)
        newline = tail.rfind(b"\n") if tail is not None else -1
        offset = base + newline + 1 if newline >= 0 else base
        return True, _position(info, offset)
    # a line that was still being written at the last call: its marks may lie before this scan,
    # so the gate opens until it is complete
    unsettled = previous.get("partial") is True
    start = previous["offset"]
    if start == info.st_size:
        return unsettled, _position(info, start)
    read_from = max(0, start - _TAIL_KEEP)
    appended = _read_range(main_path, read_from, info.st_size)
    if appended is None:
        return True, _position(info, start)
    newline = appended.rfind(b"\n")
    offset = max(start, read_from + newline + 1) if newline >= 0 else start
    marked = unsettled or any(mark in appended for mark in _MAIN_MARKS)
    return marked, _position(info, offset)


def _tail_names_a_launch(main_path: str) -> bool:
    """Whether the last 64 KB of the main transcript name a background command: the one read a
    session with no subagent and no gate state pays at a call."""
    try:
        size = os.stat(main_path).st_size
    except OSError:
        return False
    tail = _read_range(main_path, max(0, size - _LINE_SEARCH), size)
    return tail is not None and any(mark in tail for mark in _LAUNCH_MARKS)


def _worker_changes(main_path: str, gate: dict | None) -> tuple[bool, dict | None]:
    """(whether a worker's job file says something new, the gate's table with moved signatures
    brought up to date). Only a file whose signature moved is parsed; one past the limit opens
    the gate, whose full check has the same limit, so a later call gets to it."""
    table = gate.get("workers") if gate else None
    if not isinstance(table, dict) or not table:
        return False, None
    jobs_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(main_path))), "jobs")
    changed = False
    parsed = 0
    fresh: dict = {}
    rows = [(short, known) for short, known in table.items()
            if isinstance(known, list) and len(known) == 3]
    # the file read longest ago first, so busy workers cannot hide another behind the limit
    for short, known in sorted(rows, key=lambda row: row[1][0]):
        if known[2] == "-":
            fresh[short] = known
            continue
        path = workers.job_file(jobs_root, short)
        signature = workers.job_signature(path)
        if signature is None or signature == known[:2]:
            fresh[short] = known  # a stat that failed once ends nothing; a removed job says nothing
            continue
        if parsed >= workers.PARSES_PER_CALL:
            changed = True
            fresh[short] = known
            continue
        parsed += 1
        data = workers.read_job(path)
        if data is None:
            # tried at this signature: read again when the file changes, its place given up, so
            # a job that cannot be read cannot take the limit from the others
            fresh[short] = [*signature, known[2]]
        elif workers.identity(data) == known[2]:
            fresh[short] = [*signature, known[2]]
        else:
            changed = True
            fresh[short] = known
    return changed, fresh


def _position(info: os.stat_result, offset: int) -> dict:
    """The gate's main position: the end of the last complete line, and whether a line after it
    was still being written."""
    return {"inode": info.st_ino, "offset": offset, "partial": offset < info.st_size}


def run(command: str) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "null")
        if not isinstance(payload, dict) or "agent_id" in payload:
            return 0  # a subagent's own call (DESIGN 7.2, D9)
        if payload.get("hook_event_name") != _EVENTS.get(command):
            return 0
        main_path = payload.get("transcript_path")
        if not isinstance(main_path, str) or not main_path.endswith(".jsonl"):
            return 0
        subagents = os.path.join(main_path[: -len(".jsonl")], "subagents")
        gate_path = _gate_path(main_path)
        gate = _read_gate(gate_path)
        launched = bool(workers.payload_launches(payload))
        if not os.path.isdir(subagents) and gate is None and not launched \
                and not _tail_names_a_launch(main_path):
            return 0  # no subagent, background command or worker: nothing to check or write
        signature = _signature(subagents) if os.path.isdir(subagents) else ""
        marked, position = _main_delta(main_path, gate)
        recheck_at = gate.get("recheck_at") if gate else None
        due = (isinstance(recheck_at, (int, float)) and time.time() >= recheck_at) \
            or bool(gate and gate.get("alert"))
        worker_changed, table = _worker_changes(main_path, gate)
        if gate is not None and gate.get("v") == _GATE_VERSION and not marked and not due \
                and not launched and not worker_changed and not gate.get("pending") \
                and gate.get("sig") == signature:
            moved = {**({"main": position} if position and position != gate.get("main") else {}),
                     **({"workers": table} if table is not None and table != gate.get("workers")
                        else {})}
            if moved:
                _write_gate(gate_path, {**gate, **moved})
            return 0
        if gate is not None and table is not None and table != gate.get("workers"):
            # what this call parsed, so the full check spends its own limit on the others
            _write_gate(gate_path, {**gate, "workers": table})
        from . import hooks_cli  # the full check; heavy, so only past the gate

        deliver, pending = hooks_cli.post_tool_check(main_path, payload)
        written = {"v": _GATE_VERSION, "sig": signature, "main": position,
                   "pending": bool(pending)}
        recheck_at = getattr(deliver, "recheck_at", None)
        if isinstance(recheck_at, (int, float)):
            written["recheck_at"] = recheck_at
        checked = getattr(deliver, "workers", None)
        if isinstance(checked, dict) and checked:
            written["workers"] = checked
        turn = getattr(deliver, "stall_turn", None)
        if isinstance(turn, int) and turn:
            written["stall_turn"] = turn
        # `doctor --test-alert` (4.14.12): it stays until a check that began after it finds the flag
        # gone, so one armed while this call ran, which read everything before it, is not lost
        current = _read_gate(gate_path)
        alert = current.get("alert") if current else None
        if alert is not None and (alert != (gate or {}).get("alert")
                                  or getattr(deliver, "test_alert", False)):
            written["alert"] = alert
        _write_gate(gate_path, written, new=gate is None)
        message = deliver()  # the claims, last: a call killed before here has claimed nothing
        if message:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": payload["hook_event_name"], "additionalContext": message,
            }}))
        return 0
    except Exception:
        if os.environ.get("AGENT_POSTMORTEM_DEBUG"):
            import traceback

            from .redact import Redactor  # share level, as every hook's traceback (7.2, D10)

            print(Redactor().scrub_leftover_paths(traceback.format_exc()), file=sys.stderr)
        return 0
