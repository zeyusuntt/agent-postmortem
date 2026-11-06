"""Background commands that stopped writing, and sessions idle on them (docs/DESIGN.md 4.14.4:
a background `codex exec` hung 23 h on stdin and nothing flagged it).

Claude Code tells a session when one of its background commands ends, and nothing when one
never ends: a `codex exec` waiting on a stdin nobody closes sat 26 hours under a worker whose
turn had ended, while its job state read `working`. What this module reads, all read-only: the
times and sizes of the files a command writes (its task output file and its own redirects,
`live.command_outputs`), at most the last kilobyte of each for codex's stdin line, and the
session files Claude Code keeps per live process (`~/.claude/sessions/<pid>.json`), whose pid
is checked with signal 0, which sends nothing.

Standard library only. The clock is passed in.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

# A command whose files have not changed for this long is silent (the
# per-call hook and status say so, never more than once per command).
SILENT_SECONDS = 1800.0
# codex exec prints this line, then reads stdin to its end; under a stdin that never closes it
# waits for good. The line alone is no hang (3 of the 4 background commands whose log still held
# it had finished): it is one when it is a file's last line and nothing changed for this long.
STDIN_LINE = "Reading additional input from stdin..."
STDIN_SECONDS = 120.0
# A session whose turn ended this long ago, with a silent command and nothing armed to wake it,
# is STALLED.
IDLE_SECONDS = 1800.0
# The same mark as a silent command's: on one machine over 14 days, of 33,453 calls in background
# sessions 26 ran past 15 minutes and 1 past 30, that one across a restart of the session. Claude
# Code moves a Bash call to the background at its timeout, so this is mostly an MCP tool's mark
TOOL_WAIT_SECONDS = IDLE_SECONDS
_TOOL_TAIL_BYTES = 1 << 18
_LONG_BY_NATURE = frozenset({"Agent", "Task", "Workflow", "AskUserQuestion", "Monitor",
                             "ExitPlanMode", "EnterPlanMode"})
# Claude Code's own names, safe to print. Any other name may be the user's, however plain it looks
_OWN_TOOLS = frozenset({"Bash", "PowerShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
                        "NotebookRead", "Glob", "Grep", "LSP", "WebFetch", "WebSearch", "Skill",
                        "ToolSearch", "TaskOutput", "TaskStop", "KillShell", "SendMessage"})
# What Claude Code's result says of an MCP call it moved to the background (2.1.319)
_MOVED_WORDS = "moved to the background"
_TAIL_BYTES = 1024
_SESSION_FILES_MAX = 200
_SESSION_FILE_BYTES = 65536


def default_sessions_root(main_path: Path | None = None) -> Path:
    """`~/.claude/sessions`, beside the projects root a session transcript sits under."""
    if main_path is not None:
        return Path(main_path).parent.parent.parent / "sessions"
    return Path.home() / ".claude" / "sessions"


_UNREADABLE = object()  # a tail that could not be read, unlike an empty one


def _last_line(path: str, size: int):
    try:
        with Path(path).open("rb") as handle:
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read(_TAIL_BYTES)
    except OSError:
        return _UNREADABLE
    lines = [line.strip() for line in tail.decode("utf-8", "replace").splitlines()]
    lines = [line for line in lines if line]
    return lines[-1] if lines else None


def _tail_lines(main: Path | str, info: os.stat_result) -> list[bytes] | None:
    """The whole lines in the end of a transcript, or None when it cannot be read. Only a regular
    file is opened: a FIFO with no writer would hold the open for ever."""
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        with Path(main).open("rb") as handle:
            handle.seek(max(0, info.st_size - _TOOL_TAIL_BYTES))
            raw = handle.read(_TOOL_TAIL_BYTES)
    except OSError:
        return None
    lines = raw.split(b"\n")
    # the first is only the end of a line
    return lines[1:] if info.st_size > _TOOL_TAIL_BYTES else lines


def _call_digest(call_id: str) -> str:
    import hashlib  # only for a session past a mark

    return hashlib.sha256(call_id.encode("utf-8", "replace")).hexdigest()[:8]


def _entry_time(entry: dict) -> float | None:
    from datetime import datetime  # only for a session past the mark

    value = entry.get("timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, OverflowError, OSError):
        return None


def _call_mark(name: str, shape: object) -> float:
    """How long a call may stay out before it reads as stuck: a Bash call has its own timeout,
    past which Claude Code moves it to the background, so a slow build is not told. Any other
    tool's input is its own and says nothing here."""
    timeout = shape.get("timeout") if name == "Bash" and isinstance(shape, dict) else None
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        return max(TOOL_WAIT_SECONDS, timeout / 1000 + 60)
    return TOOL_WAIT_SECONDS


def open_tool_call(main: Path | str, now: float) -> dict | None:
    """The tool call a session is still waiting on, past TOOL_WAIT_SECONDS with nothing written
    since, or None (4.14.5). Read from the end of its
    transcript alone, so it costs the same on any session. `tool` says nothing private: an MCP
    server's name is the user's, and the call's input is never read."""
    try:
        info = os.stat(main)
    except OSError:
        return None
    if now - info.st_mtime < TOOL_WAIT_SECONDS:
        return None
    lines = _tail_lines(main, info)
    if lines is None:
        return None
    out: dict[str, tuple[str, float | None, float]] = {}
    last_message = None  # one message can span entries: a thinking block between two parallel calls
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        kind = entry.get("type") if isinstance(entry, dict) else None
        if kind not in ("assistant", "user"):
            continue  # a hook's or a system's line says nothing about the session moving on
        message = entry.get("message")
        if not isinstance(message, dict):
            continue  # a line it cannot read says nothing, as one that is not JSON says nothing
        content = message.get("content")
        blocks = [block for block in content if isinstance(block, dict)] \
            if isinstance(content, list) else []
        answers = [block for block in blocks if block.get("type") == "tool_result"]
        calls = [block for block in blocks if block.get("type") == "tool_use"]
        same_message = last_message is not None and message.get("id") == last_message
        if not answers and not calls and (kind == "user" or not same_message):
            out.clear()  # it wrote or was told something after them: not waiting on them
        for block in answers:
            answered = block.get("tool_use_id")
            if isinstance(answered, str):
                out.pop(answered, None)
        for block in calls:
            name, shape = block.get("name"), block.get("input")
            background = name == "Bash" and isinstance(shape, dict) \
                and shape.get("run_in_background") is True
            last_message = message.get("id")
            if isinstance(name, str) and isinstance(block.get("id"), str) \
                    and name not in _LONG_BY_NATURE and not background:
                out[block["id"]] = (name, _entry_time(entry), _call_mark(name, shape))
    for call_id, (name, started, mark) in out.items():
        seconds = now - (info.st_mtime if started is None else started)
        if seconds < mark:
            continue  # a slow build inside its own timeout: nothing is odd yet
        tool = "MCP" if name.startswith("mcp__") else name if name in _OWN_TOOLS else "tool"
        return {"tool": tool, "seconds": round(seconds), "digest": _call_digest(call_id)}
    return None


def _result_says_moved(block: dict) -> bool:
    content = block.get("content")
    parts = content if isinstance(content, list) else [content]
    for part in parts:
        text = part.get("text") if isinstance(part, dict) else part
        if isinstance(text, str) and _MOVED_WORDS in text:
            return True
    return False


def newest_mcp_call(main: Path | str) -> str | None:
    """A digest of the MCP call a worker's background task most likely is: the newest one Claude
    Code says it moved to the background, else the newest MCP call, or None. A job file holds only
    the kinds of what is out, so this is what tells one stuck task from the next, and a call that
    returned meanwhile does not read as another (4.14.5). The name and the input are not kept."""
    try:
        info = os.stat(main)
    except OSError:
        return None
    newest = moved = None
    calls: set[str] = set()
    for line in _tail_lines(main, info) or []:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind, call_id = block.get("type"), block.get("id")
            if kind == "tool_use" and isinstance(call_id, str) \
                    and isinstance(block.get("name"), str) and block["name"].startswith("mcp__"):
                newest = call_id
                calls.add(call_id)
            elif kind == "tool_result":
                answered = block.get("tool_use_id")
                if isinstance(answered, str) and answered in calls and _result_says_moved(block):
                    moved = answered
    found = moved if moved is not None else newest
    return _call_digest(found) if found is not None else None


def command_state(task_id: str, shell: dict, now: float) -> dict:
    """One running background command: how long since any of its files changed (None when an
    output cannot be watched, `live.command_outputs`), whether that makes it silent, and whether
    it waits on stdin: an output's last line is codex's stdin line and nothing changed for
    STDIN_SECONDS."""
    newest = None
    stdin_line = False
    uninspected = False  # an output that cannot be seen is no evidence of silence
    outputs = shell.get("outputs") if isinstance(shell.get("outputs"), list) else []
    for path in outputs:
        if not isinstance(path, str):
            uninspected = True
            continue
        try:
            info = os.stat(path)
        except OSError:  # gone, or behind a directory it may not search
            uninspected = True
            continue
        if not stat.S_ISREG(info.st_mode):
            uninspected = True
            continue
        newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
        last = _last_line(path, info.st_size) if info.st_size > 0 else None
        if last is _UNREADABLE:
            uninspected = True  # it may hold the stdin line: tried again in 60 s
        elif last == STDIN_LINE:
            stdin_line = True
    launched = shell.get("ts")
    launched = launched if isinstance(launched, (int, float)) and not isinstance(launched, bool) \
        else None
    since = newest if newest is not None else launched
    quiet = max(0.0, now - since) if since is not None else None
    waiting = stdin_line and quiet is not None and quiet >= STDIN_SECONDS
    # a command that starts with `sleep N` says it will be silent that long: the mark runs from the
    # end of the sleep. The stdin rule above reads its files, which do not sleep
    silence = quiet
    sleep_first = shell.get("sleep_first")
    if silence is not None and launched is not None and isinstance(sleep_first, (int, float)) \
            and not isinstance(sleep_first, bool):
        silence = max(0.0, now - max(since, launched + sleep_first))
    # nothing to watch (no file named, an older Claude Code), an output that cannot be watched,
    # or one that could not be inspected now
    silent_seconds = None if shell.get("unknown_outputs") or not outputs or uninspected \
        else silence
    # under a real timeout, or marked: silence is its normal state, and
    # only the stdin rule still speaks
    by_design = shell.get("quiet") is True
    silent = waiting or (not by_design and silent_seconds is not None
                         and silent_seconds >= SILENT_SECONDS)
    # when this command can first turn silent or a wait on stdin, for the per-call gate: from
    # its files' own clock, and in its first 10 minutes also at the 2-minute mark, since codex
    # writes its stdin line within seconds of starting
    options = []
    if not silent and quiet is not None:
        if stdin_line:
            options.append(STDIN_SECONDS - quiet)
        if silent_seconds is not None and not by_design:
            options.append(SILENT_SECONDS - silent_seconds)
        if launched is not None and now - launched < 600 and not stdin_line:
            options.append(STDIN_SECONDS - quiet)
    if not silent and uninspected:
        options.append(60.0)  # a file that could not be inspected now is tried again
    return {
        "task_id": task_id,
        "launched_seconds": round(now - launched) if launched is not None else None,
        "silent_seconds": round(silent_seconds) if silent_seconds is not None else None,
        "silent": silent,
        "waiting_on_stdin": waiting,
        "end_only": shell.get("end_only") if isinstance(shell.get("end_only"), str) else None,
        "recheck_seconds": max(5.0, min(options)) if options else None,
    }


def running_commands(main, now: float) -> list[dict]:
    """`command_state` of every background command of the session with no end known, oldest
    launch first."""
    states = [command_state(task, shell, now) for task, shell in main.running_shells().items()]
    return sorted(states, key=lambda c: -(c["launched_seconds"] or 0))


def live_session_kind(session_id: str, sessions_root: Path) -> str | None:
    """The `kind` of the live Claude Code process that runs this session (`bg`, `interactive`),
    or None when no session file names it or its pid is gone."""
    return live_sessions(sessions_root).get(session_id)


def live_sessions(sessions_root: Path) -> dict[str, str]:
    """Every session a live Claude Code process runs, by session id, with its `kind`. Signal 0
    shows only that a pid exists, not whose it is: a session file left by a crash whose pid was
    reused would read as live, so a STALLED reading also needs the job's own state to say
    `working` (session_stall)."""
    found: dict[str, str] = {}
    try:
        files = sorted(sessions_root.glob("*.json"))[:_SESSION_FILES_MAX]
    except OSError:
        return found
    for path in files:
        try:
            if path.stat().st_size > _SESSION_FILE_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        session_id = data.get("sessionId") if isinstance(data, dict) else None
        if not isinstance(session_id, str) or session_id in found:
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass  # it exists, owned by someone else
        except OSError:
            continue
        kind = data.get("kind")
        found[session_id] = kind if isinstance(kind, str) else "unknown"
    return found


def session_stall(main, *, session_id: str, sessions_root: Path, now: float,
                  waiting_on_agents: bool, job_state: str | None,
                  live: dict[str, str] | None = None) -> dict | None:
    """docs/DESIGN.md 4.14.4: the session is STALLED when it is a live background session whose
    turn ended at least IDLE_SECONDS ago with nothing since, no subagent or workflow of its own
    still out, nothing it armed (a wakeup, a Monitor, a cron) still able to wake it, and at
    least one background command silent or waiting on stdin. An interactive session's idle turn
    is the person's, and a server it runs is silent by design, so it is never STALLED here. The
job's own state must still read `working`."""
    turn_end = main.turn_end
    if turn_end is None or (main.activity is not None and main.activity > turn_end):
        return None
    idle = now - turn_end
    if idle < IDLE_SECONDS or waiting_on_agents or main.wake_pending(now):
        return None
    silent = [c for c in running_commands(main, now) if c["silent"]]
    if not silent:
        return None
    if job_state != "working":
        return None  # its job settled (or it has none): a pid read as live may be reused
    kinds = live_sessions(sessions_root) if live is None else live
    if kinds.get(session_id) != "bg":
        return None
    quiet = [c["silent_seconds"] for c in silent if c["silent_seconds"] is not None]
    return {
        "idle_seconds": round(idle),
        "commands": len(silent),
        "waiting_on_stdin": sum(1 for c in silent if c["waiting_on_stdin"]),
        "silent_seconds": max(quiet) if quiet else None,
        "task_ids": [c["task_id"] for c in silent],
    }
