"""Live mode (docs/DESIGN.md 4.14): incremental transcript reads, the session follower behind
`watch --session`, and the facts `status` and the subagent statusline print.

Standard library only. Read-only over every transcript and job file; the only writes are the
live store (4.14.8) in the tool's own state directory. Everything printed is share level: an
agent is its stable alias, a session its public id, and no path, label, description, prompt or
job detail text is ever printed.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import commands, disclosure, ledger, markers, reader, severity, state, workers
from .detectors.base import PARENT_SENTINEL_WINDOW_SECONDS, PARENT_STOP_WINDOW_SECONDS
from .detectors.missing_artifact import artifact_paths, is_placeholder
from .redact import Redactor

_AGENT_FILE_RE = re.compile(r"^agent-(a[0-9a-f]{16})\.jsonl$")
_WORKFLOW_RUN_ID_RE = re.compile(r"^wf_[0-9a-f]{8}-[0-9a-f]{3}$")
_FIRST_PROMPT_LIMIT = 65536
_LABEL_WIDTH = 7
# docs/DESIGN.md 4.14.3: an ending older than this when a session is first watched is counted in
# START, not replayed.
BASELINE_AGE_SECONDS = 1800.0
# 4.14.5 (0.4): a kill marker the post-tool hook sees waits this long for a native notice of the
# same ending, which wins (the watch waits one 2 s poll)
KILL_NOTICE_GRACE_SECONDS = 2.0
# docs/DESIGN.md 4.14.3: ENDED needs the session's trailing cost-state records and this many
# polls with nothing appended.
ENDED_QUIET_POLLS = 2
# a clean process exit writes two cost-state records; one alone is not an exit (4.14.3)
ENDED_COST_STATES = 2
# a transcript that grew this long after an agent's return notice means the agent was resumed
RESUMED_AFTER_SECONDS = 2.0
# docs/DESIGN.md 4.14.4: quiet time is shown above the measured p99 of healthy child gaps.
QUIET_SHOW_SECONDS = 120.0
_JOB_RESCAN_SECONDS = 30.0
_STORE_PRUNE_SECONDS = 7 * 86400.0
_TERMINAL_JOB_STATES = frozenset({"stopped", "failed", "done"})
_SILENT_JOB_STATES = frozenset({"working"})


def _ts(value: object) -> float | None:
    parsed = reader._parse_ts(value) if isinstance(value, str) else None
    return parsed.timestamp() if parsed is not None else None


# --- incremental reading ---------------------------------------------------


class Tail:
    """Reads a JSONL file incrementally: only complete lines past the last offset are parsed; a
    file that shrank, or whose inode changed, is read again from 0 (`reset` is then True for
    that read). A line that is not a JSON object is skipped."""

    def __init__(self, path: Path, offset: int = 0, inode: int | None = None) -> None:
        self.path = Path(path)
        self.offset = offset
        self.inode = inode
        self.size = -1
        self.mtime = None
        self.reset = False
        self.grew = False
        self.failed = False  # the last read could not open or read a file that stat found
        self.lines = 0  # complete physical lines before `offset`

    def stat(self):
        try:
            return self.path.stat()
        except FileNotFoundError:
            return None  # gone: nothing to read
        except OSError:
            self.failed = True  # there, perhaps, but not readable now (4.14.5)
            return None

    def has_new_bytes(self) -> bool:
        """Whether a read now would take anything: the file grew, shrank or was replaced. Keeps
        the file's mtime, as a read does, so a transcript left unread still has its age."""
        st = self.stat()
        if st is None:
            return False
        self.mtime = st.st_mtime
        return st.st_size != self.offset or (self.inode is not None and st.st_ino != self.inode)

    def read(self, want=None, with_lines: bool = False) -> list:
        """Entries appended since the last read. `want`, when given, is a bytes test applied to
        each raw line before it is parsed (a cheap pre-filter). `with_lines` returns
        (physical line number, entry) pairs instead."""
        self.reset = False
        self.grew = False
        self.failed = False
        st = self.stat()
        if st is None:
            return []
        self.mtime = st.st_mtime
        if self.inode is not None and st.st_ino != self.inode:
            self.offset = 0
            self.lines = 0
            self.reset = True
        elif st.st_size < self.offset:
            self.offset = 0
            self.lines = 0
            self.reset = True
        self.inode = st.st_ino
        if st.st_size == self.offset and st.st_size == self.size:
            return []
        self.size = st.st_size
        if st.st_size == self.offset:
            return []
        entries: list[dict] = []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            self.failed = True
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        chunk = data[: end + 1]
        self.offset += len(chunk)
        self.grew = True
        first = self.lines
        raws = chunk.split(b"\n")[:-1]  # the chunk ends in its own newline
        self.lines += len(raws)
        for index, raw in enumerate(raws, 1):
            if not raw.strip():
                continue
            if want is not None and not want(raw):
                continue
            try:
                entry = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(entry, dict):
                entries.append((first + index, entry) if with_lines else entry)
        return entries


def tail_state(tail: Tail) -> dict:
    return {"inode": tail.inode, "offset": tail.offset, "size": tail.size, "lines": tail.lines}


def tail_from(path: Path, data: object) -> Tail:
    tail = Tail(path)
    if isinstance(data, dict):
        offset, inode, size = data.get("offset"), data.get("inode"), data.get("size")
        lines = data.get("lines")
        if isinstance(offset, int) and offset >= 0 and isinstance(lines, int) and lines >= 0:
            tail.offset = offset
            tail.lines = lines
        if isinstance(inode, int):
            tail.inode = inode
        if isinstance(size, int):
            tail.size = size
    return tail


def _content(entry: dict):
    message = entry.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _user_text(entry: dict) -> str:
    """The plain text of a user entry (string content or text blocks), never a tool_result."""
    content = _content(entry)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content
                         if isinstance(x, dict) and isinstance(x.get("text"), str))
    return ""


def _blocks(entry: dict) -> list[dict]:
    content = _content(entry)
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


# --- per-transcript facts ----------------------------------------------------


@dataclass
class SubFacts:
    """What the live reads keep about one subagent transcript (4.14.8)."""

    outputs: list[str] | None = None  # the first prompt's artifact paths; None until seen
    final_kind: str | None = None  # 'kill' | 'user' | 'assistant' | 'tool_use' | 'tool_result'
    final_ts: float | None = None
    last_ts: float | None = None
    open_tool: str | None = None
    edited: set[str] = field(default_factory=set)
    entries: int = 0
    final_error: bool = False  # the final assistant text starts with a runtime error marker
    # the latest tool_result was the person rejecting the tool call (markers.PERMISSION_DENIED_USER)
    rejected_by_user: bool = False
    cwd: str | None = None  # the first working directory its entries name (0.4)
    # docs/DESIGN.md 4.14.3 (0.4, field report 2026-10-31): the Agent/Task launches this
    # transcript made, by tool_use id, and the synchronous result of each one that returned
    # here: {"status": completed|failed|killed|stopped, "ts"}. A nested child's return.
    launches: set[str] = field(default_factory=set)
    launch_results: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"outputs": self.outputs, "final_kind": self.final_kind, "final_ts": self.final_ts,
                "cwd": self.cwd,
                "final_error": self.final_error, "rejected_by_user": self.rejected_by_user,
                "last_ts": self.last_ts, "open_tool": self.open_tool,
                "edited": sorted(self.edited), "entries": self.entries,
                "launches": sorted(self.launches), "launch_results": self.launch_results}

    @classmethod
    def from_dict(cls, data: object) -> "SubFacts":
        facts = cls()
        if not isinstance(data, dict):
            return facts
        outputs = data.get("outputs")
        if isinstance(outputs, list):
            facts.outputs = [p for p in outputs if isinstance(p, str)]
        for name in ("final_kind", "open_tool"):
            value = data.get(name)
            setattr(facts, name, value if isinstance(value, str) else None)
        for name in ("final_ts", "last_ts"):
            value = data.get(name)
            setattr(facts, name, float(value) if isinstance(value, (int, float)) else None)
        edited = data.get("edited")
        if isinstance(edited, list):
            facts.edited = {p for p in edited if isinstance(p, str)}
        entries = data.get("entries")
        facts.entries = entries if isinstance(entries, int) else 0
        facts.final_error = data.get("final_error") is True
        facts.rejected_by_user = data.get("rejected_by_user") is True
        cwd = data.get("cwd")
        facts.cwd = cwd if isinstance(cwd, str) else None
        launches = data.get("launches")
        if isinstance(launches, list):
            facts.launches = {t for t in launches if isinstance(t, str)}
        results = data.get("launch_results")
        if isinstance(results, dict):
            facts.launch_results = {
                tool_id: {"status": value["status"], "ts": value.get("ts"),
                          "agent": value.get("agent")}
                for tool_id, value in results.items()
                if isinstance(tool_id, str) and isinstance(value, dict)
                and isinstance(value.get("status"), str) and value["status"] in _LAUNCH_END_WORDS
                and (value.get("ts") is None or (isinstance(value.get("ts"), (int, float))
                                                 and not isinstance(value.get("ts"), bool)))
                and (value.get("agent") is None
                     or disclosure.is_canonical_id("agent", value.get("agent")))
            }
        return facts

    def may_end_on_a_cause(self) -> bool:
        """A cheap pre-filter for final_cause()'s full parse: only a transcript whose final entry
        is the kill marker or an assistant text starting with a runtime error marker can end on
        an attempt_failed cause."""
        return self.final_kind == "kill" or (self.final_kind == "assistant" and self.final_error)

    def update(self, entries: list[dict]) -> None:
        for entry in entries:
            kind = entry.get("type")
            if kind not in ("user", "assistant"):
                continue
            self.entries += 1
            if self.cwd is None and isinstance(entry.get("cwd"), str):
                self.cwd = entry["cwd"]
            ts = _ts(entry.get("timestamp"))
            if ts is not None:
                self.last_ts = ts
            if kind == "user":
                text = _user_text(entry)
                results = [b for b in _blocks(entry) if b.get("type") == "tool_result"]
                if self.outputs is None and text:
                    self.outputs = artifact_paths(text[:_FIRST_PROMPT_LIMIT])
                if text.rstrip(" \t\r\n") in markers.KILL_SENTINEL_VARIANTS:
                    self.final_kind = "kill"
                elif results:
                    for block in results:
                        tool_id = block.get("tool_use_id")
                        if isinstance(tool_id, str) and tool_id in self.launches \
                                and len(self.launch_results) < _LAUNCH_LIMIT:
                            ended = launch_end(block, entry.get("toolUseResult", _NO_RESULT))
                            if ended is not None:
                                self.launch_results[tool_id] = {"status": ended[0], "ts": ts,
                                                                "agent": ended[1]}
                    self.final_kind = "tool_result"
                    self.open_tool = None
                    self.rejected_by_user = any(
                        _result_text(b).lstrip().startswith(markers.PERMISSION_DENIED_USER)
                        for b in results
                    )
                else:
                    self.final_kind = "user"
            else:
                uses = [b for b in _blocks(entry) if b.get("type") == "tool_use"]
                for block in uses:
                    if block.get("name") in ("Agent", "Task") and isinstance(block.get("id"), str) \
                            and len(self.launches) < _LAUNCH_LIMIT:
                        self.launches.add(block["id"])
                    if block.get("name") in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
                        target = (block.get("input") or {}).get("file_path")
                        if isinstance(target, str) and len(self.edited) < 500:
                            self.edited.add(target)
                texts = [b.get("text", "") for b in _blocks(entry)
                         if b.get("type") == "text" and isinstance(b.get("text"), str)]
                self.final_error = any(t.lstrip().startswith(_ERROR_PREFIXES) for t in texts)
                if uses:
                    self.final_kind = "tool_use"
                    name = uses[-1].get("name")
                    self.open_tool = name if isinstance(name, str) else None
                else:
                    self.final_kind = "assistant"
            self.final_ts = ts if ts is not None else self.final_ts


_LAUNCH_LIMIT = 500
_LAUNCH_END_WORDS = frozenset({"completed", "failed", "killed", "stopped"})
_NO_RESULT = object()  # an entry with no toolUseResult key


def launch_end(block: dict, result: object) -> tuple[str, str | None] | None:
    """docs/DESIGN.md 4.14.3: how a subagent transcript's Agent/Task result ended a nested
    child, as (status, the agent id a structured result names, or None), or None when it is no
    return. Only measured synchronous shapes count, since a
    background nested launch also gets a result with no toolUseResult ("Async agent launched"):
    the `[Subagent hand-back]` text with no toolUseResult, or a toolUseResult object of the
    main transcript's T2 shape (one agent id; async_launched, or async with no status, is no
    return; a missing or `completed` status a return; `failed`, `killed`, `stopped` an end)."""
    if block.get("is_error") is True:
        return None
    if result is _NO_RESULT:
        text = _result_text(block).lstrip()
        return ("completed", None) if text.startswith(markers.SUBAGENT_HAND_BACK) else None
    if not isinstance(result, dict):
        return None
    ids = set()
    for source in (result, block):
        if "agentId" in source:
            if not disclosure.is_canonical_id("agent", source["agentId"]):
                return None  # a present agent id that is not one, null included (T0)
            ids.add(source["agentId"])
    if len(ids) != 1:
        return None
    agent = ids.pop()
    word = result.get("status")
    if "status" in result and not isinstance(word, str):
        return None  # a present status that is not a string, null included (T0)
    if word == "async_launched" or (result.get("isAsync") is True and word is None):
        return None
    if word is None:
        return "completed", agent
    return (word, agent) if word in _LAUNCH_END_WORDS else None


_ERROR_PREFIXES = tuple(
    marker for group in (markers.API_ERROR, markers.MACHINE_SLEEP, markers.USAGE_LIMIT_SESSION,
                         markers.USAGE_LIMIT_WEEKLY) for marker in group
) + ("API Error",)


def _sub_want(raw: bytes) -> bool:
    return any(mark in raw for mark in _ENTRY_TYPE_MARKS[:4])


_NOTICE_KINDS = (
    ("is waiting on its own background work", "waiting"),
    ("Agent stalled: no progress for", "stalled"),
    ("You've hit your session limit", "session_limit"),
    ("You've hit your weekly limit", "weekly_limit"),
    ("You've hit your 5-hour limit", "session_limit"),
    ("was stopped by Claude", "stopped_by_session"),
    ("was stopped by a usage limit", "session_limit"),
    ("terminated early due to an API error", "api_error"),
)
_STALL_SECONDS_RE = re.compile(r"Agent stalled: no progress for ([0-9]+)s")


def _notice_kind(summary: str) -> tuple[str, str | None]:
    for needle, kind in _NOTICE_KINDS:
        if needle in summary:
            if kind == "stalled":
                match = _STALL_SECONDS_RE.search(summary)
                return kind, match.group(1) if match else None
            return kind, None
    return "other", None


_ENVELOPE_RE = {
    "task": re.compile(r"<task-id>(.*?)</task-id>", re.DOTALL),
    "tool": re.compile(r"<tool-use-id>(.*?)</tool-use-id>", re.DOTALL),
    "status": re.compile(r"<status>(.*?)</status>", re.DOTALL),
    "summary": re.compile(r"<summary>(.*?)</summary>", re.DOTALL),
}


# docs/DESIGN.md 4.14.8: the shape of the main-transcript facts kept in the live store; a stored
# entry of another version is read again from the transcript (2: workflow task ids, 0.4)
MAIN_FACTS_VERSION = 10  # 10: when this tool last ran here; 9: the workers it launched, its
# Monitors' task ids, quiet commands; 3: a workflow's latest launch with its tool-use id and status;
# 4: the session's backgrounded shell commands and their native ends (4.14.4); 5: early ends kept
# per tool-use id and a timeout-moved command aged from its start (4 never released); 6: each
# command's output files, the session's last turn end and activity, its armed wakes; 7, 8: the same
# with crons by id and the output rules of review rounds 1 to 4 (6, 7 never released)
# the same for a subagent transcript's facts (2: its Agent/Task launches and their synchronous
# results, 0.4; 3: a present agent id that is not one is no result); a stored entry of another
# version, or none, is read again from byte 0
SUB_FACTS_VERSION = 3
# the rule a session's first-look baseline was taken under (2: nested returns count, 0.4); a
# baseline of another rule is extended once, over a complete read (4.14.3)
BASELINE_RULE = 3
# What a native task id looks like. It is printed as is in hook lines and local rows, on the ground
# that it is no path or name, so anything else is not taken for one (0.5.0 review)
TASK_ID_RE = re.compile(r"[a-z0-9]{1,32}")
_META_LIMIT = 65536
_WORKFLOW_END_WORDS = {
    "completed": "completed", "failed": "failed", "killed": "killed", "stopped": "stopped",
    "cancelled": "stopped",
}
# a background command's end words, as status counts them (4.14.4); another word reads `ended`
COMMAND_STATES = ("running", "completed", "failed", "killed", "stopped")


@dataclass
class MainFacts:
    """What the live reads keep about a main transcript (4.14.3, 4.14.8)."""

    launches: dict[str, dict] = field(default_factory=dict)  # tool_use id -> {type, background}
    agents: dict[str, dict] = field(default_factory=dict)  # agent id -> facts
    notices: dict[str, dict] = field(default_factory=dict)  # task id -> {status, kind, ts}
    task_stops: list[tuple[float | None, tuple[str, ...]]] = field(default_factory=list)
    kill_ts: list[float] = field(default_factory=list)
    workflows: dict[str, float | None] = field(default_factory=dict)
    # run id -> {task, tool, status} of its latest launch: the task id and tool-use id its end
    # notice must name, and a synchronous result's own status (4.14.4)
    workflow_tasks: dict[str, dict] = field(default_factory=dict)
    # background task id -> {tool, ts, end} of each shell command the session backgrounded: its
    # launch's tool-use id and time, and its native end word, None while none is known (4.14.4)
    shells: dict[str, dict] = field(default_factory=dict)
    # task id -> {ends: {tool-use id or "": status}, n: arrival} of notices no launch of this
    # session claimed yet: a command's end can be written before the tool result naming its task
    # (4.14.4); the newest few kept
    early_ends: dict[str, dict] = field(default_factory=dict)
    trailing_cost_states: int = 0
    entrypoint: str | None = None
    # field report 2026-11-03 (4.14.4): where each Bash call writes, kept from its tool_use
    # until its result names a background task ({outputs, unknown, ts} by tool-use id, the
    # newest _PENDING_BASH_KEPT); when the session's last turn ended (a system turn_duration
    # entry) and when it last did anything; until when a wake it armed can still come
    # (ScheduleWakeup, Monitor), and how many crons it created and did not delete
    pending_bash: dict[str, dict] = field(default_factory=dict)
    turn_end: float | None = None
    activity: float | None = None
    wake_until: float | None = None
    # wake calls waiting for their result ({name, ts, input} by tool-use id), and the crons a
    # successful CronCreate armed and no successful CronDelete removed ({recurring, next} by id)
    pending_wakes: dict[str, dict] = field(default_factory=dict)
    crons: dict[str, dict] = field(default_factory=dict)
    # field report 2026-11-04: the background sessions this session launched ({ts, end, name} by
    # short id: the launching call's own time window, which the job's createdAt must fall in),
    # and the task ids of its Monitors, which Claude Code can list as shell tasks
    workers: dict[str, dict] = field(default_factory=dict)
    monitors: dict[str, float] = field(default_factory=dict)
    # when this session last ran `status`, `scan`, `run` or `watch` of this tool (field report
    # 2026-11-04: the reminder at Stop)
    status_ran: float | None = None
    pending_looks: dict[str, dict] = field(default_factory=dict)  # until its result, as a rejected
    # call ran nothing (review round 2)
    # `claude --bg` calls whose result named no id: under `run_in_background` the id goes to the
    # task's output file, so that worker cannot be told from another session's
    unlinked_launches: int = 0

    def update(self, entries: list[dict]) -> None:
        for entry in entries:
            kind = entry.get("type")
            if kind == "cost-state":
                self.trailing_cost_states += 1
                continue
            if kind in ("user", "assistant", "system"):
                self.trailing_cost_states = 0
            entrypoint = entry.get("entrypoint")
            if isinstance(entrypoint, str):
                self.entrypoint = entrypoint
            ts = _ts(entry.get("timestamp"))
            if ts is not None:
                if kind == "system" and entry.get("subtype") == "turn_duration":
                    self.turn_end = ts if self.turn_end is None else max(self.turn_end, ts)
                elif kind in ("user", "assistant", "attachment", "queue-operation"):
                    self.activity = ts if self.activity is None else max(self.activity, ts)
            if kind in ("queue-operation", "user", "attachment"):
                self._notice(entry, ts)
            if kind == "assistant":
                for block in _blocks(entry):
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if name in ("Bash", "Monitor") and ts is not None \
                            and isinstance(block.get("id"), str) \
                            and isinstance(tool_input.get("command"), str) \
                            and commands.runs_this_tool(tool_input["command"]):
                        self._keep(self.pending_looks, block["id"], {"ts": ts})
                    if name in ("Agent", "Task") and isinstance(block.get("id"), str):
                        self.launches[block["id"]] = {
                            "type": tool_input.get("subagent_type"),
                            "background": bool(tool_input.get("run_in_background")),
                            "ts": ts,
                        }
                    elif name == "TaskStop":
                        ids = tuple(str(v) for v in tool_input.values() if isinstance(v, str))
                        self.task_stops.append((ts, ids))
                    elif name == "Workflow" and isinstance(block.get("id"), str):
                        self.launches.setdefault("wf:" + block["id"], {"ts": ts})
                    elif name == "Bash" and isinstance(block.get("id"), str):
                        self._bash_call(block["id"], tool_input, entry.get("cwd"), ts)
                    elif name in _WAKE_TOOLS and isinstance(block.get("id"), str):
                        self._keep(self.pending_wakes, block["id"],
                                   {"name": name, "ts": ts, "input": {
                                       k: tool_input.get(k) for k in _WAKE_INPUT_KEYS
                                       if isinstance(tool_input.get(k), (str, int, float))}})
            if kind == "user":
                text = _user_text(entry)
                if text.startswith(markers.KILL_SENTINEL_PREFIX) and ts is not None:
                    self.kill_ts.append(ts)
                    del self.kill_ts[:-50]
                result = entry.get("toolUseResult")
                for block in _blocks(entry):
                    if block.get("type") != "tool_result":
                        continue
                    tool_id = block.get("tool_use_id")
                    if not isinstance(tool_id, str):
                        continue
                    if tool_id in self.launches:
                        self._agent_result(tool_id, block, result, ts)
                    if tool_id in self.pending_wakes:
                        self._wake(self.pending_wakes.pop(tool_id), block, result, ts, tool_id)
                    self._worker_launch(tool_id, block, result, ts)
                    look = self.pending_looks.pop(tool_id, None)
                    if isinstance(look, dict) and block.get("is_error") is not True \
                            and isinstance(look.get("ts"), (int, float)):
                        self.status_ran = max(look["ts"], self.status_ran or look["ts"])
                    if not isinstance(result, dict):
                        continue
                    self._shell_result(tool_id, block, result, ts)
                    run_id = result.get("runId")
                    if ("wf:" + tool_id) in self.launches and isinstance(run_id, str) \
                            and _WORKFLOW_RUN_ID_RE.fullmatch(run_id):
                        self.workflows.setdefault(run_id, ts)
                        # the latest launch of a run decides (a resume names a new task)
                        task_id = result.get("taskId")
                        status = result.get("status")
                        self.workflow_tasks[run_id] = {
                            "task": task_id if isinstance(task_id, str) and task_id else None,
                            "tool": tool_id,
                            "status": status if isinstance(status, str) else None,
                            "ts": ts,
                        }

    def workflow_end(self, run_id: str) -> str | None:
        """How the harness said a workflow of this session ended, or None while no ending is
        known: the task notification naming both the task id and the tool-use id of the run's
        latest launch, else that launch's own synchronous status. `cancelled` reads `stopped`."""
        binding = self.workflow_tasks.get(run_id)
        if not isinstance(binding, dict):
            return None
        notice = self.notices.get(binding.get("task") or "")
        status = None
        if isinstance(notice, dict) and notice.get("tool") == binding.get("tool"):
            status = notice.get("status")
        if status is None:
            status = binding.get("status")
        status = _WORKFLOW_END_WORDS.get(status)
        return status

    def commands(self) -> tuple[dict[str, int], float | None]:
        """docs/DESIGN.md 4.14.4 (0.4): the session's background commands by native end, and the
        launch time of the oldest one with no end known (None when none is running)."""
        counts = dict.fromkeys(COMMAND_STATES, 0)
        oldest = None
        for shell in self.shells.values():
            end = shell.get("end") if isinstance(shell, dict) else None
            word = _WORKFLOW_END_WORDS.get(end, "ended") if isinstance(end, str) and end \
                else "running"
            counts[word] = counts.get(word, 0) + 1
            launched = shell.get("ts") if isinstance(shell, dict) else None
            if word == "running" and isinstance(launched, (int, float)) \
                    and not isinstance(launched, bool):
                oldest = launched if oldest is None else min(oldest, launched)
        return counts, oldest

    def _shell_result(self, tool_id: str, block: dict, result: dict, ts: float | None) -> None:
        """A tool result that backgrounds a shell command (its `backgroundTaskId`; Monitor's
        task is a `taskId`), or a TaskStop result naming one: stopped, unless its own notice
        already said how it ended (a later notice still replaces this)."""
        task_id = result.get("backgroundTaskId")
        if isinstance(task_id, str) and TASK_ID_RE.fullmatch(task_id) \
                and task_id not in self.shells:
            started = ts
            waited = result.get("timedOutAfterMs")  # moved to the background by the timeout
            if ts is not None and isinstance(waited, (int, float)) \
                    and not isinstance(waited, bool) and waited > 0:
                started = ts - waited / 1000
            self.shells[task_id] = {"tool": tool_id, "ts": started, "end": None}
            pending = self.pending_bash.pop(tool_id, None)
            outputs = list(pending.get("outputs") or []) if isinstance(pending, dict) else []
            task_output = _TASK_OUTPUT_RE.search(_block_text(block))
            if task_output:
                outputs.insert(0, task_output.group(1))
            self.shells[task_id]["outputs"] = outputs
            if isinstance(pending, dict) and pending.get("quiet"):
                self.shells[task_id]["quiet"] = True
            # what is not redirected goes to the task output file: with none named (an older
            # Claude Code), that part of the output cannot be watched (review round 1); with the
            # call itself no longer kept, its redirects are unknown (review round 3)
            if not task_output or not isinstance(pending, dict) or pending.get("unknown"):
                self.shells[task_id]["unknown_outputs"] = True
            early = self.early_ends.pop(task_id, None)
            ends = early.get("ends") if isinstance(early, dict) else None
            if isinstance(ends, dict):
                # the launch's own notice first, as for a known command; a leftover one else
                end = ends.get(tool_id) or ends.get("")
                if isinstance(end, str) and end:
                    self.shells[task_id]["end"] = end
        stopped = result.get("task_id")
        if result.get("task_type") == "local_bash" and isinstance(stopped, str) \
                and block.get("is_error") is not True:
            shell = self.shells.get(stopped)
            if isinstance(shell, dict) and not shell.get("end"):
                shell["end"] = "stopped"

    def _command_end(self, task_ids: list[str], tool: str, status: str) -> bool:
        """A notice's end for each background command it names, under the task id its launch
        named and, when the notice names a tool-use id, the launch's own. One naming none (the
        previous session's leftovers: "no completion record was found") only ends a command
        with no end yet. A task id no launch has named yet is kept for one that may follow
        (never a subagent's id). True when it named a known command: then it is no subagent's
        or workflow's notice."""
        named = False
        for task_id in task_ids:
            shell = self.shells.get(task_id)
            if not isinstance(shell, dict):
                monitor = self.crons.get(f"monitor:{task_id}")
                if status and isinstance(monitor, dict) and (
                        not tool or monitor.get("tool") in (None, "", tool)):
                    # a persistent Monitor's own end, by its launch's own tool-use id when the
                    # notice names one: it arms nothing more (review rounds 2 and 3)
                    del self.crons[f"monitor:{task_id}"]
                    continue
                if not disclosure.is_canonical_id("agent", task_id):
                    self._early_end(task_id, tool, status)
                continue
            named = True
            if status and (tool == shell.get("tool") or (not tool and not shell.get("end"))):
                shell["end"] = status
        return named

    def _early_end(self, task_id: str, tool: str, status: str) -> None:
        """Keep the newest `_EARLY_ENDS_KEPT` by an arrival number stored with each: the live
        store is written with sorted keys, so dict order does not survive a restore."""
        if not task_id or not status:
            return
        def arrival(key: str) -> int:
            entry = self.early_ends[key]
            number = entry.get("n") if isinstance(entry, dict) else None
            return number if isinstance(number, int) and not isinstance(number, bool) else 0

        newest = max((arrival(key) for key in self.early_ends), default=0)
        entry = self.early_ends.get(task_id)
        ends = entry.get("ends") if isinstance(entry, dict) else None
        ends = dict(ends) if isinstance(ends, dict) else {}
        ends[tool] = status  # per tool-use id: a later notice of the same one replaces it
        self.early_ends[task_id] = {"ends": ends, "n": newest + 1}
        while len(self.early_ends) > _EARLY_ENDS_KEPT:
            del self.early_ends[min(self.early_ends, key=arrival)]

    def to_dict(self) -> dict:
        return {"launches": self.launches, "agents": self.agents, "notices": self.notices,
                "task_stops": [[ts, list(ids)] for ts, ids in self.task_stops[-200:]],
                "kill_ts": self.kill_ts, "workflows": self.workflows,
                "workflow_tasks": self.workflow_tasks, "shells": self.shells,
                "early_ends": self.early_ends,
                "trailing_cost_states": self.trailing_cost_states, "entrypoint": self.entrypoint,
                "pending_bash": self.pending_bash, "turn_end": self.turn_end,
                "activity": self.activity, "wake_until": self.wake_until, "crons": self.crons,
                "pending_wakes": self.pending_wakes, "workers": self.workers,
                "monitors": self.monitors, "status_ran": self.status_ran,
                "pending_looks": self.pending_looks,
                "unlinked_launches": self.unlinked_launches}

    @classmethod
    def from_dict(cls, data: object) -> "MainFacts":
        facts = cls()
        if not isinstance(data, dict):
            return facts
        for name in ("launches", "agents", "notices", "workflows", "workflow_tasks", "shells",
                     "early_ends", "pending_bash", "pending_wakes", "crons", "workers",
                     "monitors", "pending_looks"):
            value = data.get(name)
            if isinstance(value, dict):
                setattr(facts, name, {k: v for k, v in value.items() if isinstance(k, str)})
        facts.shells = {k: v for k, v in facts.shells.items() if TASK_ID_RE.fullmatch(k)}
        stops = data.get("task_stops")
        if isinstance(stops, list):
            facts.task_stops = [
                (item[0] if isinstance(item[0], (int, float)) else None,
                 tuple(v for v in item[1] if isinstance(v, str)))
                for item in stops
                if isinstance(item, list) and len(item) == 2 and isinstance(item[1], list)
            ]
        kills = data.get("kill_ts")
        if isinstance(kills, list):
            facts.kill_ts = [t for t in kills if isinstance(t, (int, float))]
        unlinked = data.get("unlinked_launches")
        facts.unlinked_launches = unlinked if isinstance(unlinked, int) else 0
        trailing = data.get("trailing_cost_states")
        facts.trailing_cost_states = trailing if isinstance(trailing, int) else 0
        entrypoint = data.get("entrypoint")
        facts.entrypoint = entrypoint if isinstance(entrypoint, str) else None
        for name in ("turn_end", "activity", "wake_until", "status_ran"):
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(facts, name, float(value))
        return facts

    def _bash_call(self, tool_id: str, tool_input: dict, cwd: object, ts: float | None) -> None:
        """Where a Bash call writes (field report 2026-11-03), kept until its result: a
        background task's result takes them, every other call's entry ages out."""
        command = tool_input.get("command")
        if not isinstance(command, str):
            return
        cwd = cwd if isinstance(cwd, str) else None
        outputs, unknown = command_outputs(command, cwd)
        call = {"outputs": outputs, "unknown": unknown, "ts": ts}
        if commands.quiet_by_design(command, cwd):
            call["quiet"] = True  # field report 2026-11-04: silence is its normal state
        launches = workers.count_launches(command)
        if launches:
            call["launch"] = launches
        out_before = sum(1 for held in self.pending_bash.values()
                         if isinstance(held, dict) and held.get("launch"))
        self._keep(self.pending_bash, tool_id, call)
        out_after = sum(1 for held in self.pending_bash.values()
                        if isinstance(held, dict) and held.get("launch"))
        if out_after < out_before + (1 if launches else 0):
            self.unlinked_launches += 1  # its result can no longer be tied to it (review round 2)

    def _worker_launch(self, tool_id: str, block: dict, result: object, ts: float | None) -> None:
        """docs/DESIGN.md 4.14.5: a `claude --bg` call's result names the worker it started. Its
        job's createdAt is checked against this window when the job is read, so a printed old
        id links nothing."""
        call = self.pending_bash.get(tool_id)
        if not isinstance(call, dict) or not call.get("launch"):
            return
        text = _block_text(block)  # the call stays: a background launch's redirects are read next
        if isinstance(result, dict) and isinstance(result.get("stdout"), str):
            text = f"{result['stdout']}\n{text}"
        launched = workers.launched_ids(text)
        if len(launched) < call.get("launch"):  # a redirect or a `tail` can hide one of several
            self.unlinked_launches += 1
        call.pop("launch", None)  # answered: evicting it later is no unlinked launch (0.5.0 review)
        for short, name in launched.items():
            self.workers[short] = {"ts": call.get("ts"), "end": ts, "name": name}
        while len(self.workers) > workers.WORKERS_KEPT:
            def age(short: str) -> float:
                when = self.workers[short].get("ts") if isinstance(self.workers[short], dict) \
                    else None
                return when if isinstance(when, (int, float)) else float("-inf")
            del self.workers[min(self.workers, key=age)]

    @staticmethod
    def _keep(table: dict, key: str, value: dict) -> None:
        """Keep a call until its result, the newest _PENDING_KEPT by their own time (the live
        store is written with sorted keys, so dict order does not survive a restore)."""
        table[key] = value
        if len(table) > _PENDING_KEPT:
            def age(name: str) -> float:
                item = table[name].get("ts") if isinstance(table[name], dict) else None
                return item if isinstance(item, (int, float)) else float("-inf")
            for name in sorted(table, key=age)[:-_PENDING_KEPT]:
                del table[name]

    def _wake(self, call: dict, block: dict, result: object, ts: float | None,
              tool_id: str = "") -> None:
        """A wake the session armed for itself (field report 2026-11-03), taken from the call's
        own successful result only (review round 1: a failed CronCreate armed nothing, a failed
        CronDelete removed nothing): a ScheduleWakeup fires at most an hour out, a Monitor tells
        its end or its expiry, a cron fires until a CronDelete names it (a one-shot one until
        its `nextFireAt`)."""
        if block.get("is_error") is True or not isinstance(call, dict):
            return
        result = result if isinstance(result, dict) else {}
        name = call.get("name")
        tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
        started = call.get("ts") if isinstance(call.get("ts"), (int, float)) else ts
        if name == "CronCreate":
            cron_id = result.get("id")
            if isinstance(cron_id, str) and cron_id:
                fire = result.get("nextFireAt")
                fire = float(fire) / 1000 if isinstance(fire, (int, float)) \
                    and not isinstance(fire, bool) else None
                recurring = result.get("recurring", tool_input.get("recurring", True))
                self.crons[cron_id] = {"recurring": recurring is not False, "next": fire}
                while len(self.crons) > _PENDING_KEPT:
                    del self.crons[min(self.crons)]
            return
        if name == "CronDelete":
            cron_id = result.get("id") or tool_input.get("id")
            if isinstance(cron_id, str):
                self.crons.pop(cron_id, None)
            return
        if not isinstance(started, (int, float)):
            return
        if name == "ScheduleWakeup":
            delay = tool_input.get("delaySeconds")
            seconds = min(max(float(delay), 60.0), 3600.0) if isinstance(delay, (int, float)) \
                and not isinstance(delay, bool) else 3600.0
        else:  # Monitor
            monitor_task = result.get("taskId")
            if isinstance(monitor_task, str) and monitor_task:
                self.monitors[monitor_task] = started
                while len(self.monitors) > _PENDING_KEPT:
                    del self.monitors[min(self.monitors, key=lambda t: self.monitors[t])]
            if result.get("persistent") is True:
                task = result.get("taskId")
                # its own end, written before this result, retires it at once (review round 3)
                early = self.early_ends.get(task) if isinstance(task, str) else None
                ends = early.get("ends") if isinstance(early, dict) else None
                if isinstance(ends, dict) and (ends.get(tool_id) or ends.get("")):
                    self.early_ends.pop(task, None)
                    return
                self.crons[f"monitor:{task}"] = {"recurring": True, "next": None, "tool": tool_id}
                return
            limit = result.get("timeoutMs", tool_input.get("timeout_ms"))
            seconds = min(float(limit), 3_600_000.0) / 1000 if isinstance(limit, (int, float)) \
                and not isinstance(limit, bool) and limit > 0 else 300.0
        until = started + seconds
        self.wake_until = until if self.wake_until is None else max(self.wake_until, until)

    def last_ending(self) -> float | None:
        """When the last subagent or workflow of this session was last known to end."""
        times = [notice.get("ts") for notice in self.notices.values() if isinstance(notice, dict)]
        times += [facts.get("foreground_done") for facts in self.agents.values()
                  if isinstance(facts, dict)]
        for run_id in self.workflows:
            if self.workflow_end(run_id) is not None:
                binding = self.workflow_tasks.get(run_id)  # the latest launch: a resume is new
                times.append(binding.get("ts") if isinstance(binding, dict) else None)
        times = [when for when in times if isinstance(when, (int, float))]
        return max(times) if times else None

    def wake_pending(self, now: float) -> bool:
        """Whether something the session armed can still wake it."""
        for cron in self.crons.values():
            if not isinstance(cron, dict) or cron.get("recurring") is not False:
                return True
            fire = cron.get("next")
            if not isinstance(fire, (int, float)) or fire > now:
                return True
        return self.wake_until is not None and self.wake_until > now

    def running_shells(self) -> dict[str, dict]:
        """The background commands with no end known, by task id."""
        return {task: shell for task, shell in self.shells.items()
                if isinstance(shell, dict) and not shell.get("end")}

    def _agent_result(self, tool_id: str, block: dict, result: object, ts: float | None) -> None:
        """An Agent/Task launch's result, by returns.py's T2 rule: the agent id from the entry's
        toolUseResult or the result block itself; synchronous unless its status is
        async_launched or it is async with no status; then a missing or `completed` status is a
        return, `failed`, `killed` or `stopped` a native ending."""
        result = result if isinstance(result, dict) else {}
        ids = {value for value in (result.get("agentId"), block.get("agentId"))
               if disclosure.is_canonical_id("agent", value)}
        if len(ids) != 1:
            return
        agent_id = ids.pop()
        launch = self.launches[tool_id]
        facts = self.agents.setdefault(agent_id, {})
        facts.update({"type": launch.get("type"), "launched": launch.get("ts"),
                      "tool_use_id": tool_id})
        status_word = result.get("status")
        asynchronous = status_word == "async_launched" or (
            result.get("isAsync") is True and status_word is None)
        if asynchronous or block.get("is_error") is True:
            return
        if status_word in (None, "completed"):
            facts["foreground_done"] = ts
        elif status_word in ("failed", "killed", "stopped"):
            self.notices[agent_id] = {"status": status_word, "kind": "other", "detail": None,
                                      "ts": ts}

    def _notice(self, entry: dict, ts: float | None) -> None:
        """A task notification, accepted only in the shapes the reader accepts (4.13.3): the
        whole text is one envelope, carried by a `user` entry whose origin is a task
        notification or by a queue `enqueue`. A copy quoted inside prose, or a user entry with
        no such origin, changes nothing. A `queued_command` attachment in task-notification
        mode, which sometimes carries the only copy of a notice, is read for a background
        command's end only (4.14.4): for a subagent or workflow it would change which endings
        the per-call checks tell, which the baseline rule (4.14.3) does not cover."""
        commands_only = False
        if entry.get("type") == "queue-operation":
            if entry.get("operation") != "enqueue":
                return
            text = entry.get("content")
        elif entry.get("type") == "attachment":
            attachment = entry.get("attachment")
            if not (isinstance(attachment, dict) and attachment.get("type") == "queued_command"
                    and attachment.get("commandMode") == "task-notification"):
                return
            text = attachment.get("prompt")
            commands_only = True
        else:
            origin = entry.get("origin")
            if not (isinstance(origin, dict) and origin.get("kind") == "task-notification"):
                return
            text = _user_text(entry)
        if not isinstance(text, str):
            return
        command = _command_envelope(text)
        if command is not None and self._command_end(*command):
            return  # a background command's end, in any of its envelope shapes
        if reader._parse_envelope(text) is None and not (
                command is not None and not command[1] and len(command[0]) == 1
                and command[0][0] in self.agents):
            # field report 2026-11-04: a subagent resumed by a message ends with a notice naming
            # no tool-use id; only for one this session launched, since nothing else ties it here
            return
        values = {}
        for name, pattern in _ENVELOPE_RE.items():
            match = pattern.search(text)
            values[name] = match.group(1).strip() if match else ""
        task_id = values["task"]
        if commands_only:
            return
        if not disclosure.is_canonical_id("agent", task_id) and not any(
            isinstance(binding, dict) and binding.get("task") == task_id
            and binding.get("tool") == (values["tool"] or None)
            for binding in self.workflow_tasks.values()
        ):
            # a subagent's id, or the task id and tool-use id of a workflow launch of this
            # session: a notice naming another tool-use id is dropped before it can replace one
            return
        kind, detail = _notice_kind(values["summary"])
        previous = self.notices.get(task_id)
        # the user entry is the queue entry's copy, delivered when the session got to it: the end
        # is when it was queued, or a resumed subagent would read as ended (review round 3)
        queued = entry.get("type") == "queue-operation"
        if not queued and isinstance(previous, dict) and previous.get("queued") == text:
            previous.pop("queued")
            return
        if previous is not None and previous.get("status") == values["status"] \
                and previous.get("kind") == kind and (ts is None or previous.get("ts") == ts) \
                and previous.get("tool") == (values["tool"] or None):
            return  # the queue-operation and the user entry of one notice
        self.notices[task_id] = {"status": values["status"], "kind": kind, "detail": detail,
                                 "ts": ts, "tool": values["tool"] or None,
                                 **({"queued": text} if queued else {})}


_EARLY_ENDS_KEPT = 100
_PENDING_KEPT = 64
_WAKE_TOOLS = frozenset({"ScheduleWakeup", "Monitor", "CronCreate", "CronDelete"})
_WAKE_INPUT_KEYS = ("delaySeconds", "timeout_ms", "recurring", "id")
# the launch result's own sentence for a background command: "Output is being written to: <path>"
_TASK_OUTPUT_RE = re.compile(r"Output is being written to: (/\S+?)(?:\.(?=\s|$)|(?=\s|$))")
_OUTPUT_OPS = frozenset({">", ">>", ">|", "&>", "&>>"})
_DIRECTORY_CHANGES = frozenset({"cd", "pushd", "popd"})
_DESCRIPTOR_RE = re.compile(r"[0-9]*-?")


def _opens_group(raw: str) -> bool:
    """Whether a word as written holds an unquoted `(` that opens a subshell or group, not a
    substitution (`$(`, `<(`, `>(`): `(cd x && ...)` and an unspaced `time(cd x && ...)` both do
    (review rounds 3 and 4)."""
    single = double = False
    previous = ""
    i = 0
    while i < len(raw):
        ch = raw[i]
        if single:
            single = ch != "'"
        elif ch == "\\":
            i += 2
            previous = ""
            continue
        elif ch == "'" and not double:
            single = True
        elif ch == '"':
            double = not double
        elif ch == "(" and not double and previous not in ("$", "<", ">"):
            return True
        previous = ch
        i += 1
    return False
_SHELL_FLOW = frozenset({"{", "}", "if", "then", "elif", "else", "fi", "for", "while", "until",
                         "do", "done", "case", "esac", "select", "function"})
_UNPLACEABLE_RE = re.compile(r"[$`*?\[]")


def _block_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content
                         if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def _resolve_path(target: str, base: str | None) -> str | None:
    if target.startswith("~"):
        expanded = os.path.expanduser(target)
        return os.path.normpath(expanded) if expanded.startswith("/") else None
    if target.startswith("/"):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(base, target)) if base else None


def command_outputs(command: str, cwd: str | None) -> tuple[list[str], bool]:
    """docs/DESIGN.md 4.14.4 (field report 2026-11-03): the files a shell command's own
    redirects write (`>`, `>>`, `>|`, `&>`, `&>>` of stdout or stderr), each resolved against
    the call's cwd as moved by a `cd DIR` segment before it, and whether some output cannot be
    watched: thrown away (`/dev/null`), or named by an expansion, a glob or a directory change
    that cannot be followed. Read for their sizes and times only, never their contents beyond
    a last line (stalls.py)."""
    base = cwd if isinstance(cwd, str) and cwd.startswith("/") else None
    outputs: list[str] = []
    unknown = False
    lexed = commands.lex(command)
    if any(seg.words and (seg.words[0].text in _SHELL_FLOW
                          or any(_opens_group(word.raw) for word in seg.words))
           for seg in lexed.segments):
        # a subshell, a group or control flow (`if ...; then cd x; fi`, a loop) moves
        # directories or runs segments this reading cannot follow (review rounds 1 and 2)
        return [], True
    for index, seg in enumerate(lexed.segments):
        words = [word.text for word in seg.words]
        if words[:1] == ["cd"] and index == 0 and seg.sep_after in ("&&", ";", "\n"):
            # only a leading `cd DIR &&` or `cd DIR;` is followed (review round 1)
            if len(words) == 1:
                base = os.path.expanduser("~")
            elif len(words) == 2 and words[1] != "-" and not _UNPLACEABLE_RE.search(words[1]):
                base = _resolve_path(words[1], base)
            else:
                base = None
        elif any(word in _DIRECTORY_CHANGES for word in words):
            # any other directory change, also wrapped (`builtin cd x`, `command cd x`), after
            # another segment, or in a pipe: relative outputs from here on cannot be placed
            base = None
        for redirect in seg.redirects:
            target = redirect.target
            # `>& file` is bash's other spelling of `&> file`; `2>&1` and `>&-` only duplicate
            # or close a descriptor (review round 3)
            to_file = redirect.op == ">&" and redirect.fd is None and isinstance(target, str) \
                and not _DESCRIPTOR_RE.fullmatch(target)  # `2>&1`, `>&-`, `>&1-` (round 4)
            if not to_file and (redirect.op not in _OUTPUT_OPS
                                or redirect.fd not in (None, "1", "2")):
                continue
            if target in ("/dev/stdout", "/dev/stderr"):
                continue  # into the task's own output file
            if not target or _UNPLACEABLE_RE.search(target) or target.startswith("/dev/"):
                unknown = True
                continue
            path = _resolve_path(target, base)
            if path is None:
                unknown = True
            elif path not in outputs:
                outputs.append(path)
    return outputs, unknown


def _command_envelope(text: str) -> tuple[list[str], str, str] | None:
    """A notice in the shapes only a background command's end takes besides the reader's
    (4.14.4): the whole text one envelope with exactly one status and at most one tool-use id,
    naming one or more task ids. A notice for commands left over from the previous session
    names no tool-use id and lists every such task, then a summary id; only known commands of
    this session are ended by it. Returns (task ids, tool-use id or "", status)."""
    stripped = text.strip()
    if not (stripped.startswith("<task-notification>")
            and stripped.endswith("</task-notification>")):
        return None
    statuses = _ENVELOPE_RE["status"].findall(stripped)
    tools = _ENVELOPE_RE["tool"].findall(stripped)
    tasks = [task.strip() for task in _ENVELOPE_RE["task"].findall(stripped)]
    if len(statuses) != 1 or len(tools) > 1 or not tasks:
        return None
    return tasks, (tools[0].strip() if tools else ""), statuses[0].strip()


_ENTRY_TYPE_MARKS = tuple(
    f'"type"{sep}"{kind}"'.encode()
    for kind in ("user", "assistant", "system", "attachment", "queue-operation")
    for sep in (":", ": ")
)  # attachments and queue entries mark a session's activity (review round 1)


def _main_want(raw: bytes) -> bool:
    """A cheap byte test before a main-transcript line is parsed: the kinds MainFacts reads, and
    every user, assistant or system entry, compact or spaced, since each one clears a trailing
    cost-state run (4.14.3)."""
    return (
        b"task-notification" in raw or b'"Agent"' in raw or b'"Task"' in raw
        or b'"TaskStop"' in raw or b'"Workflow"' in raw or b"toolUseResult" in raw
        or b"cost-state" in raw or b"[Request interrupted by user" in raw
        or any(mark in raw for mark in _ENTRY_TYPE_MARKS)
    )


# --- full parses at a transition ---------------------------------------------


def changes(path: Path) -> tuple[int, int, bool]:
    """(paths, commits, partial) a subagent's own transcript shows it changed, from one full
    parse with the same helpers `watch` uses for a KILL line (4.12): the attempt's
    AttemptFacts and ledger completeness. `commits` is -1 when a commit command ran but
    reported no id (the count is unknown, 4.7's G4b); an unreadable transcript is (0, 0, True),
    a lower bound, never an established zero."""
    diagnostics: list[str] = []
    transcript_state = ledger.TranscriptState()
    try:
        events, _v, _s, _t, _b, tool_calls, _m = reader._read_transcript(
            path, diagnostics, state=transcript_state,
        )
    except OSError:
        return 0, 0, True
    last_ts = events[-1].ts if events else None
    facts = reader._build_attempt_facts(tool_calls, "killed", last_ts, path, False)
    written = facts.paths_written if facts is not None else None
    paths = len(written) if isinstance(written, list) else 0
    commits = len(facts.commits) if facts is not None and isinstance(facts.commits, list) else 0
    without_id = facts.commit_commands_without_id if facts is not None else 0
    if isinstance(without_id, int) and without_id > 0 or without_id == reader.FACTS_UNKNOWN:
        commits = -1
    partial = ledger.build_ledger(tool_calls, path.name, transcript_state).completeness == "partial"
    return paths, commits, partial


def final_cause(path: Path) -> str | None:
    """The attempt_failed phrase for a transcript's final entry, None when it carries none."""
    from . import watch  # local: watch imports this module's siblings, not this module

    phrase, _reset = watch._cause_phrase_for_failed(path)
    return None if phrase == "cause not recorded" else phrase


def missing_outputs(outputs: list[str] | None, cwd: str | None = None) -> list[str]:
    """R4: the first prompt's artifact paths that are absent right now (4.14.2), leaving out one
    inside the subagent's own working directory when that directory is gone too (0.4: a worktree
    removed after the run; nothing there can be checked). A placeholder an older build kept names
    no one file (a store of the same facts version is read as it is)."""
    return [
        p for p in outputs or []
        if not is_placeholder(p) and severity.absence(p) == "absent_now"
        and not (cwd is not None and severity.working_dir_gone(p, (cwd,)))
    ]


def result_clauses(closing_entry: object) -> list[tuple[str, str]]:
    """R1 and R2 (4.14.2): (clause id, phrase) for a journal `result` entry whose own claims
    contradict it, read the moment the result is: `claim_contradiction`'s clauses over the same
    allowlisted claims, with an absolute report_path stat'ed now, at return, so an absent one is
    a fact about the return ("does not exist"), not the post-hoc "absent now". A path whose stat
    failed is no evidence either way and makes no clause."""
    from . import claims as _claims
    from .detectors import claim_contradiction

    if not isinstance(closing_entry, dict):
        return []
    reported = reader._build_reported(closing_entry)
    if not isinstance(reported, dict) or reported.get("type") != "dict":
        return []
    self_report = _claims.self_reported_problems(closing_entry.get("result"))
    out: list[tuple[str, str]] = []
    for clause_id, phrase, absence in claim_contradiction._clauses(
        reported.get("claims", {}), True, self_report,
    ):
        if clause_id == "report_path_missing":
            if absence != "absent_now":
                continue
            phrase = "its report_path does not exist"
        out.append((clause_id, phrase))
    return out


# --- names ---------------------------------------------------------------------


class _Names:
    """An agent is its stable alias (4.13.5); positional `agent-N` when the book is unavailable."""

    def __init__(self) -> None:
        self._book = None
        self.unavailable = False

    def __call__(self, agent_id: str) -> str:
        if self._book is None:
            self._book = disclosure.lazy_book()
        alias = self._book.alias("agent", agent_id)
        if not self._book.available:
            self.unavailable = True
        return disclosure.rendered_form("agent", alias)


def session_public_id(main_path: Path) -> str:
    return reader._session_public_id(main_path.parent.name, main_path.stem)


def session_redactor(main_path: Path, agent_ids=(), base: Redactor | None = None) -> Redactor:
    """`base` at share with the session's own identities collected (P9 to P11, 4.14.3): a cause
    phrase or claim detail that quotes the project slug, the session id or an agent id shows its
    alias, as a report's free text does; the structural backstop alone keeps an agent id raw.
    The agents are `agent_ids` plus every transcript under the session's subagents directory.
    Any other level returns `base` unchanged."""
    base = Redactor() if base is None else base
    if base.level != "share":
        return base
    found = list(agent_ids)
    subagents = main_path.parent / main_path.stem / "subagents"
    try:
        for pattern in ("agent-*.jsonl", "workflows/*/agent-*.jsonl"):
            found += [path.stem[len("agent-"):] for path in subagents.glob(pattern)]
    except OSError:
        pass
    identities: list[tuple[str, str]] = []
    for kind, value in (("project", main_path.parent.name), ("session", main_path.stem),
                        *(("agent", agent_id) for agent_id in found)):
        if disclosure.is_canonical_id(kind, value) and (kind, value) not in identities:
            identities.append((kind, value))
    book = base.context.book if base.context.book is not None else disclosure.lazy_book()
    return Redactor(context=replace(base.context, book=book, identities=tuple(identities)))


def session_key(main_path: Path) -> str:
    from . import hooks_cli  # local: hooks_cli is heavy and imports reader

    return hooks_cli._session_state_key(main_path.parent.parent, main_path.stem)


# --- the store -------------------------------------------------------------------


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _store_session(data: dict, key: str) -> dict:
    sessions = data.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = data["sessions"] = {}
    record = sessions.get(key)
    if not isinstance(record, dict):
        record = sessions[key] = {}
    return record


def _prune(data: dict, now: float) -> None:
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return
    for key in list(sessions):
        record = sessions[key]
        seen = record.get("seen") if isinstance(record, dict) else None
        if not isinstance(seen, (int, float)) or now - seen > _STORE_PRUNE_SECONDS:
            del sessions[key]


# --- job state (background sessions) ------------------------------------------


def default_jobs_root(main_path: Path | None = None) -> Path:
    """`~/.claude/jobs`, found beside the projects root a session transcript sits under."""
    if main_path is not None:
        return Path(main_path).parent.parent.parent / "jobs"
    return Path.home() / ".claude" / "jobs"


def _read_job_state(job_dir: Path) -> tuple[str | None, str | None]:
    try:
        data = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    state_word = workers.effective_state(data)  # field report 2026-11-04: `tempo` is the live one
    session_id = data.get("sessionId")
    return (state_word if isinstance(state_word, str) and state_word.isascii()
            and state_word.replace("_", "").isalpha() else None,
            session_id if isinstance(session_id, str) else None)


def find_job(jobs_root: Path, session_id: str) -> Path | None:
    try:
        candidates = [p for p in jobs_root.iterdir() if p.is_dir()]
    except OSError:
        return None
    for job_dir in candidates:
        _state, sid = _read_job_state(job_dir)
        if sid == session_id:
            return job_dir
    return None


# --- the follower ------------------------------------------------------------------


def ending_key(agent_id: str, facts: "SubFacts | None") -> str:
    """One ending of one agent: its id and its transcript's final entry time. A resumed agent
    writes newer entries, so its next ending has a new key (4.14.6 rule 3)."""
    final = facts.final_ts if facts is not None else None
    return f"{agent_id}@{int(final * 1000) if isinstance(final, (int, float)) else 'none'}"


def returned_after(event_ts: object, facts: "SubFacts | None") -> bool:
    """A return notice (or foreground result) still describes the agent: its transcript has not
    grown more than RESUMED_AFTER_SECONDS past it, which would mean it was resumed."""
    if facts is None or not isinstance(facts.last_ts, (int, float)):
        return True
    if not isinstance(event_ts, (int, float)):
        return True
    return facts.last_ts <= event_ts + RESUMED_AFTER_SECONDS


@dataclass
class _Sub:
    agent_id: str
    tail: Tail
    facts: SubFacts = field(default_factory=SubFacts)
    pending_kill: bool = False
    deferred: float | None = None  # when a bounded read first left this transcript unread


class SessionFollower:
    """docs/DESIGN.md 4.14.3: one poll at a time over one session. `poll()` returns event dicts;
    `render_text` and `render_json` print them."""

    def __init__(self, main_path: Path, *, jobs_root: Path | None = None, now=time.time,
                 caller_session: str | None | object = ..., store_path: Path | None = None) -> None:
        self.main_path = Path(main_path)
        self.session_id = self.main_path.stem
        self.session_dir = self.main_path.parent / self.main_path.stem
        self.public_id = session_public_id(self.main_path)
        self.key = session_key(self.main_path)
        self.jobs_root = default_jobs_root(self.main_path) if jobs_root is None else Path(jobs_root)
        self.now = now
        if caller_session is ...:
            caller_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        self.own_session = isinstance(caller_session, str) and \
            caller_session.lower() == self.session_id.lower()
        self.store_path = state.live_store_path() if store_path is None else Path(store_path)
        self.main_tail = Tail(self.main_path)
        self.main = MainFacts()
        self.fork_line = 0  # FA4: inherited prefix lines, 0 when none
        self._fork_settled = False
        self._fork_scanned_size: int | None = None
        self._facts_fork_line = 0  # the boundary the current main facts were built with
        self.subs: dict[str, _Sub] = {}
        self.reported: set[str] = set()
        self.baseline: set[str] = set()
        self.names = _Names()
        self.token = secrets.token_hex(8)
        self.duplicate = False
        self.watched_by: int | None = None
        self.started = False
        self.ended = False
        self.quiet_polls = 0
        self.job_dir: Path | None = None
        self.job_state: str | None = None
        self.job_seen = False
        self._saved_job_state: str | None = None
        self._job_scan_at = float("-inf")
        self._links: dict[str, tuple[str, str] | None] = {}  # nested child -> (launcher, tool)
        self.read_failed = False  # the last _read could not list, stat or open something
        self.rule_pending = False  # the baseline is not yet extended over a complete read
        # nested candidates whose meta file is there but could not be read or parsed yet: the
        # hook checks again at its next call and the baseline rule is not recorded (4.14.5)
        self.unsettled_meta: set[str] = set()

    # -- reading
    def _fork_boundary(self) -> int:
        """docs/DESIGN.md 4.2 (FA4): the last physical line a fork inherited from another
        session, from the reader's own rule; rescanned only while unsettled and growing."""
        if not self._fork_settled:
            try:
                size = self.main_path.stat().st_size
            except OSError:
                size = None
            if size != self._fork_scanned_size:
                self._fork_scanned_size = size
                boundary, settled = reader._fork_scan(self.main_path)
                self._fork_settled = settled
                if settled:
                    self.fork_line = boundary
        return self.fork_line

    def read_main(self) -> None:
        """Main-transcript lines appended since the last read, past a fork's inherited prefix."""
        boundary = self._fork_boundary()
        if boundary != self._facts_fork_line:
            self.main_tail = Tail(self.main_path)  # the boundary moved: rebuild from line 1
            self.main = MainFacts()
            self._facts_fork_line = boundary
        entries = self.main_tail.read(_main_want, with_lines=True)
        if self.main_tail.reset:
            self.main = MainFacts()  # the replacement says nothing the old content said
        self.main.update([entry for line, entry in entries if line > boundary])

    def _read(self, deadline: float | None = None) -> bool:
        """Read what every transcript of the session appended. With a `deadline` (a
        time.monotonic() value, 4.14.5) the subagent reads stop once it has passed and at least
        one transcript gave new lines, so every call makes progress; returns False when some
        transcript was left for the next read."""
        self.read_main()
        failed = self.main_tail.failed
        self.unsettled_meta.clear()  # each look below consults again the meta files it needs
        sub_dir = self.session_dir / "subagents"
        try:
            names = [p.name for p in sub_dir.iterdir()]
        except FileNotFoundError:
            names = []  # no subagents yet
        except OSError:
            names = []
            failed = True  # it may hold transcripts this read could not list
        for name in names:
            match = _AGENT_FILE_RE.match(name)
            if match and match.group(1) not in self.subs:
                self.subs[match.group(1)] = _Sub(match.group(1), Tail(sub_dir / name))
        complete = True
        progressed = False
        subs = list(self.subs.values())
        if deadline is not None:
            # the ones an earlier bounded read left unread first, oldest first, then the ones
            # never read: a transcript that keeps growing can never hold another back
            subs.sort(key=lambda sub: (sub.deferred is None, sub.deferred or 0.0,
                                       sub.tail.offset > 0))
        for sub in subs:
            if deadline is not None and progressed and time.monotonic() >= deadline \
                    and sub.tail.has_new_bytes():
                complete = False  # left for the next read; one with nothing new is still read
                if sub.deferred is None:
                    sub.deferred = time.time()
                continue
            sub.deferred = None
            entries = sub.tail.read(_sub_want)
            failed = failed or sub.tail.failed
            progressed = progressed or sub.tail.grew or sub.tail.reset
            if sub.tail.reset:
                sub.facts = SubFacts()
            sub.facts.update(entries)
        # a transcript that could not be listed, stat'ed or opened was not read: not complete,
        # and no baseline rule is recorded over it (it could miss an ending, 4.14.5)
        self.read_failed = failed
        return complete and not failed

    def read_once_each(self) -> bool:
        """Whether every subagent transcript was read at least once, in this call or an earlier
        one (4.14.5): enough for a baseline, since one still growing is recent and so never in
        it, while one never read has no ending to judge."""
        return all(sub.tail.offset > 0 or not sub.tail.has_new_bytes()
                   for sub in self.subs.values())

    def _agent_ids(self) -> list[str]:
        ids = set(self.subs) | set(self.main.agents)
        return sorted(ids)

    def _nested_link(self, agent_id: str) -> tuple[str, str] | None:
        """(launcher subagent id, launch tool_use id) from a nested child's meta file (DESIGN
        T1: a string `parentAgentId` naming another subagent of this session and a string
        `toolUseId`), else None. Read once per child; a meta file not there yet is read again."""
        if agent_id in self._links:
            return self._links[agent_id]
        path = self.session_dir / "subagents" / f"agent-{agent_id}.meta.json"
        try:
            with path.open("rb") as handle:
                raw = handle.read(_META_LIMIT + 1)
        except FileNotFoundError:
            return None  # no meta file: no link, looked for again next time
        except OSError:
            self.unsettled_meta.add(agent_id)
            return None
        link = None
        try:
            meta = json.loads(raw) if len(raw) <= _META_LIMIT else {}
        except (ValueError, RecursionError):
            self.unsettled_meta.add(agent_id)  # still being written, perhaps: read again
            return None
        self.unsettled_meta.discard(agent_id)
        if isinstance(meta, dict):
            parent, tool_id = meta.get("parentAgentId"), meta.get("toolUseId")
            if isinstance(parent, str) and parent != agent_id \
                    and disclosure.is_canonical_id("agent", parent) \
                    and isinstance(tool_id, str) and tool_id:
                link = (parent, tool_id)
        self._links[agent_id] = link
        return link

    def _main_current(self, agent_id: str) -> bool:
        """Whether the main transcript holds return evidence that still describes the agent
        (a notice or foreground result its transcript has not grown past, returned_after)."""
        facts = self._facts(agent_id)
        notice = self.main.notices.get(agent_id)
        done_ts = self.main.agents.get(agent_id, {}).get("foreground_done")
        return (notice is not None and returned_after(notice.get("ts"), facts)) or \
            (done_ts is not None and returned_after(done_ts, facts))

    def _launcher_result(self, agent_id: str) -> dict | None:
        """A nested child's synchronous result in its launcher's transcript (4.14.3), read only
        when the main transcript has no current return evidence for it."""
        if self._main_current(agent_id):
            return None
        link = self._nested_link(agent_id)
        launcher = self.subs.get(link[0]) if link is not None else None
        if launcher is None:
            return None
        result = launcher.facts.launch_results.get(link[1])
        if result is None or result.get("agent") not in (None, agent_id):
            return None  # a structured result naming another agent is no return of this one
        return result

    def notice(self, agent_id: str) -> dict | None:
        """The task notification of an agent, or for a nested child that a synchronous
        `failed`, `killed` or `stopped` result ended in its launcher, that ending."""
        notice = self.main.notices.get(agent_id)
        if notice is not None and self._main_current(agent_id):
            return notice
        result = self._launcher_result(agent_id)
        if result is not None and result.get("status") != "completed":
            return {"status": result["status"], "kind": "other", "detail": None,
                    "ts": result.get("ts"), "tool": None}
        if result is not None:
            return None  # the launcher's later return stands over an earlier notice
        return notice  # an earlier round's notice, if any: callers read it as such

    def foreground_done(self, agent_id: str) -> float | None:
        """When a synchronous result returned the agent: its main-transcript Agent result, or
        for a nested child its launcher's (the `[Subagent hand-back]`, 4.14.3)."""
        done_ts = self.main.agents.get(agent_id, {}).get("foreground_done")
        if done_ts is not None and self._main_current(agent_id):
            return done_ts
        result = self._launcher_result(agent_id)
        if result is not None and result.get("status") == "completed":
            ts = result.get("ts")
            return ts if isinstance(ts, (int, float)) else None  # as the main rule: no time,
        if result is not None:
            return None  # no return; a launcher ending stands over an earlier result
        return done_ts  # an earlier round's result, if any: callers read it as such

    def _terminal(self, agent_id: str) -> bool:
        sub = self.subs.get(agent_id)
        facts = sub.facts if sub is not None else None
        notice = self.notice(agent_id)
        if notice is not None and notice.get("kind") != "waiting" \
                and returned_after(notice.get("ts"), facts):
            return True
        done_ts = self.foreground_done(agent_id)
        if done_ts is not None and returned_after(done_ts, facts):
            return True
        return facts is not None and facts.final_kind == "kill"

    # -- the store
    def _load_endings(self, record: dict, now: float) -> None:
        """The delivered keys and the first-arm baseline of this session's record (4.14.3),
        recording the baseline the first time any channel looks."""
        self.reported = key_set(record.get("reported"))
        baseline = record.get("baseline")
        if isinstance(baseline, list):
            self.baseline = key_set(baseline)
            if record.get("baseline_rule") != BASELINE_RULE:
                # taken under an older rule, which could not see some endings (a nested
                # child's return, 0.4), or over a read that missed a transcript: the ones old
                # enough now join it, so none is told as new
                self._extend(record, now)
        else:
            self.baseline = set()
            self._extend(record, now)

    def _first_look(self, now: float) -> set[str]:
        return {
            ending_key(agent_id, self._facts(agent_id))
            for agent_id in self._agent_ids()
            if self._terminal(agent_id) and self._older_than_baseline(agent_id, now)
        }

    def _extend(self, record: dict, now: float) -> None:
        """Add the endings terminal now and older than the baseline age (a union, so safe to
        repeat); the rule counts as current only once it was done with every transcript read
        at least once, no read failing and every needed meta file read (4.14.5)."""
        self.baseline |= self._first_look(now)
        record["baseline"] = sorted(self.baseline)
        if not self.read_failed and not self.unsettled_meta and self.read_once_each():
            record["baseline_rule"] = BASELINE_RULE
            self.rule_pending = False
        else:
            self.rule_pending = True

    def _extend_pending(self) -> None:
        """A watch whose baseline was taken or extended over an incomplete read extends it
        again at each poll until a complete read settles it."""
        try:
            with state.cache_transaction(self.store_path) as data:
                record = _store_session(data, self.key)
                stored = record.get("baseline")
                if isinstance(stored, list):
                    self.baseline |= key_set(stored)
                if record.get("baseline_rule") == BASELINE_RULE:
                    self.rule_pending = False  # another channel settled it
                else:
                    self._extend(record, self.now())
        except state.StateError:
            pass

    def load_endings(self, *, new_baseline: bool = True) -> bool:
        """_load_endings for a one-shot check (the post-tool hook, 4.14.5): no watch is
        registered and no job state is read. With new_baseline False (a read that did not reach
        every transcript) a session with no baseline yet loads nothing and returns False, so a
        baseline is only ever taken over a complete read."""
        try:
            with state.cache_transaction(self.store_path) as data:
                record = _store_session(data, self.key)
                if not new_baseline and not isinstance(record.get("baseline"), list):
                    return False
                self._load_endings(record, self.now())
                record["seen"] = self.now()
        except state.StateError:
            return new_baseline
        # a baseline still to be extended over a complete read: no ending is judged yet, so
        # none it would absorb is told first (4.14.5)
        return not self.rule_pending

    def _load_store(self) -> None:
        now = self.now()
        try:
            with state.cache_transaction(self.store_path) as data:
                record = _store_session(data, self.key)
                self._load_endings(record, now)
                job = record.get("job_state")
                if isinstance(job, str):
                    self.job_state = job
                    self.job_seen = True
                watch = record.get("watch")
                if isinstance(watch, dict) and watch.get("token") != self.token \
                        and _pid_alive(watch.get("pid")):
                    self.duplicate = True
                    self.watched_by = watch.get("pid")
                else:
                    record["watch"] = {"pid": os.getpid(), "token": self.token}
                record["seen"] = now
                _prune(data, now)
        except state.StateError:
            pass

    def _facts(self, agent_id: str) -> "SubFacts | None":
        sub = self.subs.get(agent_id)
        return sub.facts if sub is not None else None

    def _refresh_reported(self) -> None:
        """Pick up keys another channel delivered since (4.14.6 rule 3): the agent-launched hook
        writes them to the same session record."""
        self.reported |= _record_keys(state.read_cache(self.store_path), self.key)

    def _older_than_baseline(self, agent_id: str, now: float) -> bool:
        sub = self.subs.get(agent_id)
        mtime = sub.tail.mtime if sub is not None else None
        return mtime is not None and now - mtime > BASELINE_AGE_SECONDS

    def _save_reported(self, new_keys: list[str]) -> None:
        if not new_keys and self.job_state == self._saved_job_state:
            return
        try:
            with state.cache_transaction(self.store_path) as data:
                record = _store_session(data, self.key)
                merged = key_set(record.get("reported")) | self.reported
                record["reported"] = sorted(merged)
                if self.job_state is not None:
                    record["job_state"] = self.job_state
                record["seen"] = self.now()
            self._saved_job_state = self.job_state
        except state.StateError:
            pass

    def close(self) -> None:
        """Release this watch's claim on the session (4.14.3)."""
        try:
            with state.cache_transaction(self.store_path) as data:
                record = _store_session(data, self.key)
                watch = record.get("watch")
                if isinstance(watch, dict) and watch.get("token") == self.token:
                    del record["watch"]
        except state.StateError:
            pass

    # -- one poll
    def poll(self) -> list[dict]:
        if self.duplicate or self.ended:
            return []
        self._read()
        events: list[dict] = []
        if not self.started:
            self.started = True
            self._load_store()
            events.append(self._start_event())
            if self.duplicate:
                return events
        else:
            self._refresh_reported()
            if self.rule_pending:
                self._extend_pending()
        new_keys: list[str] = []
        # while the baseline waits for a complete read, no ending is judged (4.14.5)
        for agent_id in ([] if self.rule_pending else self._agent_ids()):
            for event, key in self._agent_events(agent_id):
                self.reported.add(key)
                new_keys.append(key)
                # the shared locked gate (4.14.6 rule 3): an ending the hook claimed while this
                # poll was deciding is not delivered twice
                if event is not None and claim_delivery(self.main_path, key, self.store_path):
                    events.append(event)
        if not self.own_session:
            for run_id in sorted(self.main.workflows):
                key = f"wf:{run_id}"
                if key in self.reported:
                    continue
                self.reported.add(key)
                new_keys.append(key)
                events.append({"event": "WORKFLOW", "run_id": run_id,
                               "next_command": f"agent-postmortem watch {run_id}"})
        events.extend(self._job_events())
        self._save_reported(new_keys)
        if not self.ended and self.main.trailing_cost_states >= ENDED_COST_STATES:
            if self.main_tail.grew:
                self.quiet_polls = 0
            else:
                self.quiet_polls += 1
                if self.quiet_polls >= ENDED_QUIET_POLLS:
                    self.ended = True
        else:
            self.quiet_polls = 0
        if self.ended:
            events.append({"event": "ENDED", "session": self.public_id})
        return events

    def _start_event(self) -> dict:
        ended_before = sum(1 for a in self.baseline if a not in self.reported)
        workflows = set(self.main.workflows)
        try:
            workflows |= {p.name for p in (self.session_dir / "subagents" / "workflows").iterdir()
                          if _WORKFLOW_RUN_ID_RE.fullmatch(p.name)}
        except OSError:
            pass
        return {
            "event": "START", "session": self.public_id, "subagents": len(self._agent_ids()),
            "workflows": len(workflows), "ended_before": ended_before,
            "watched_by": self.watched_by,
        }

    def _agent_events(self, agent_id: str) -> list[tuple[dict | None, str]]:
        """(event or None, reported key) pairs for one agent this poll. A None event still
        marks the key handled: the transition was seen and needs no line (4.14.6 rule 1). Keys
        name one ending (ending_key), so a resumed agent's next ending is judged again."""
        out: list[tuple[dict | None, str]] = []
        sub = self.subs.get(agent_id)
        facts = sub.facts if sub is not None else None
        ending = ending_key(agent_id, facts)
        if ending in self.baseline:
            return out
        end_key, claim_key = f"{ending}:end", f"{ending}:claim"
        path = sub.tail.path if sub is not None else None
        notice = self.notice(agent_id)
        current = notice is not None and returned_after(notice.get("ts"), facts)
        done_ts = self.foreground_done(agent_id)
        returned = (current and notice.get("status") == "completed"
                    and notice.get("kind") != "waiting") or \
            (done_ts is not None and returned_after(done_ts, facts))

        if current and notice.get("status") in ("failed", "killed", "stopped") \
                and end_key not in self.reported:
            out.append((self._failed_event(agent_id, notice, path), end_key))
            return out
        if returned and claim_key not in self.reported and path is not None:
            out.append((self._claim_event(agent_id, sub), claim_key))
            return out
        if facts is not None and facts.final_kind == "kill" and end_key not in self.reported \
                and not current:
            if not sub.pending_kill:
                sub.pending_kill = True  # one poll for the native notice to land first
                return out
            out.append((self._kill_event(agent_id, sub), end_key))
        return out

    def _counts(self, path: Path | None) -> tuple[int, int, bool]:
        if path is None:
            return 0, 0, True
        return changes(path)

    def _failed_event(self, agent_id: str, notice: dict, path: Path | None) -> dict | None:
        kind = notice.get("kind")
        if kind == "stalled":
            seconds = notice.get("detail")
            cause = f"stalled, no progress for {seconds}s" if seconds else "stalled"
        elif kind in ("session_limit", "weekly_limit"):
            cause = "stopped by a usage limit"
        elif kind == "api_error":
            cause = "ended on an API error"
        elif kind == "stopped_by_session":
            cause = "stopped by this session"
        else:
            cause = "failed (see its task notification)"
        paths, commits, partial = self._counts(path)
        if paths == 0 and commits == 0:
            return None  # no known change (commits is -1 when a commit ran with no id)
        return self._with_counts({"event": "FAILED", "agent": self.names(agent_id),
                                  "cause": cause}, paths, commits, partial)

    def _kill_event(self, agent_id: str, sub: _Sub) -> dict | None:
        kill_ts = sub.facts.final_ts
        for stop_ts, ids in self.main.task_stops:
            if any(agent_id in value for value in ids):
                return self._stopped_event(agent_id, sub)
            if kill_ts is not None and stop_ts is not None and \
                    0 <= kill_ts - stop_ts <= PARENT_STOP_WINDOW_SECONDS:
                return self._stopped_event(agent_id, sub)
        if kill_ts is not None and any(
            abs(kill_ts - ts) <= PARENT_SENTINEL_WINDOW_SECONDS for ts in self.main.kill_ts
        ):
            return None  # interrupted together with the session: the person did it
        if sub.facts.rejected_by_user:
            return None  # the person rejected its tool call, and the marker followed
        paths, commits, partial = self._counts(sub.tail.path)
        return self._with_counts({"event": "KILL", "agent": self.names(agent_id)},
                                 paths, commits, partial)

    def _stopped_event(self, agent_id: str, sub: _Sub) -> dict | None:
        paths, commits, partial = self._counts(sub.tail.path)
        if paths == 0 and commits == 0:
            return None
        return self._with_counts({"event": "FAILED", "agent": self.names(agent_id),
                                  "cause": "stopped by this session"}, paths, commits, partial)

    def _claim_event(self, agent_id: str, sub: _Sub | None) -> dict | None:
        if sub is None:
            return None
        cause = final_cause(sub.tail.path) if sub.facts.may_end_on_a_cause() else None
        if cause is not None:
            return {"event": "CLAIM", "agent": self.names(agent_id), "check": "returned_on_error",
                    "detail": cause, "run_id": self.public_id,
                    "next_command": f"agent-postmortem run {self.public_id}"}
        missing = missing_outputs(sub.facts.outputs, sub.facts.cwd)
        if missing:
            return {"event": "CLAIM", "agent": self.names(agent_id),
                    "check": "output_missing_at_return", "missing": len(missing),
                    "run_id": self.public_id,
                    "next_command": f"agent-postmortem status {self.public_id} --local"}
        return None

    def _with_counts(self, event: dict, paths: int, commits: int, partial: bool) -> dict:
        event.update({"paths": paths, "paths_partial": partial, "commits": max(commits, 0),
                      "commits_unknown": commits < 0,
                      "run_id": self.public_id,
                      "next_command": f"agent-postmortem run {self.public_id}"})
        return event

    def _job_events(self) -> list[dict]:
        now = time.monotonic()
        if self.job_dir is None and now - self._job_scan_at >= _JOB_RESCAN_SECONDS:
            self._job_scan_at = now
            self.job_dir = find_job(self.jobs_root, self.session_id)
        if self.job_dir is None:
            return []
        word, _sid = _read_job_state(self.job_dir)
        if word is None or word == self.job_state:
            if word is not None and word in _TERMINAL_JOB_STATES and self.job_seen:
                self.ended = True
            return []
        first = not self.job_seen
        self.job_seen = True
        self.job_state = word
        events: list[dict] = []
        if not (first and word in _SILENT_JOB_STATES) and word != "working":
            events.append({"event": "STATE", "session": self.public_id, "state": word})
        if word in _TERMINAL_JOB_STATES:
            self.ended = True
        return events


# --- what the hooks share with the watch (4.14.5) -----------------------------------


def claim_for(path: Path) -> dict | None:
    """R3 then R4 for one returned subagent's transcript: {check, detail|missing} or None."""
    facts = SubFacts()
    facts.update(Tail(path).read(_sub_want))
    if facts.may_end_on_a_cause():
        cause = final_cause(path)
        if cause is not None:
            return {"check": "returned_on_error", "detail": cause}
    missing = missing_outputs(facts.outputs, facts.cwd)
    if missing:
        return {"check": "output_missing_at_return", "missing": missing}
    return None


def mark_reported(main_path: Path, keys: list[str], *, hinted: bool | None = None) -> dict:
    """Add reported event keys (and the hint flag) to a session's live-store record, returning
    the record as it was before (4.14.6 rule 3)."""
    before: dict = {}
    with state.cache_transaction(state.live_store_path()) as data:
        record = _store_session(data, session_key(main_path))
        before = json.loads(json.dumps(record))
        reported = key_set(record.get("reported")) | set(keys)
        record["reported"] = sorted(reported)
        if hinted is not None:
            record["hinted"] = hinted
        record["seen"] = time.time()
    return before


def key_set(value) -> set[str]:
    """A stored key list as a set of its string items: anything else in it, or a value that is
    not a list, is dropped, so a store of the wrong shape never stops a check (4.14.5)."""
    return {key for key in value if isinstance(key, str)} if isinstance(value, list) else set()


def _record_keys(data, key: str) -> set[str]:
    sessions = data.get("sessions") if isinstance(data, dict) else None
    record = sessions.get(key) if isinstance(sessions, dict) else None
    return key_set(record.get("reported")) if isinstance(record, dict) else set()


def delivered_keys(main_path: Path, store_path: Path | None = None) -> set[str]:
    """The delivery keys the live channels recorded for this session (4.14.6 rule 3); none when
    the store is missing or any level of it has the wrong shape, which never stops a check."""
    return _record_keys(state.read_cache(store_path or state.live_store_path()),
                        session_key(main_path))


def claim_deliveries(main_path: Path, keys: list[str], store_path: Path | None = None, *,
                     then=None, unclaim=None, strict: bool = False, before=None,
                     after=None) -> tuple[list[str], object]:
    """The delivery gate the live channels share (4.14.6 rule 3): (the keys, in order, this
    caller is the first to claim for this session, `then(those keys)` or None). Every claim is
    recorded in one write under the store lock, and `then` runs inside it before that write, so
    a caller killed or failing anywhere before the write has claimed none of them. `unclaim()`,
    read after `then` inside the same lock, names won keys the caller did not show after all
    (review round 2 of item 2): they are not recorded. A store that cannot be used lets the
    caller deliver every key (a repeat beats a lost line), except under `strict`, for a wake
    (4.14.5), where a line that cannot be recorded could repeat for good: no claim, no `then`.
    `before(record)` and `after(record)` run inside the lock, around `then`, for what a caller
    keeps in the session's record itself; with no usable store they see an empty record."""
    try:
        with state.cache_transaction(store_path or state.live_store_path()) as data:
            record = _store_session(data, session_key(main_path))
            if before is not None:
                before(record)
            reported = key_set(record.get("reported"))
            claimed = []
            for key in keys:
                if key not in reported:
                    reported.add(key)
                    claimed.append(key)
            result = then(claimed) if then is not None else None
            back = [key for key in (unclaim() if unclaim is not None else ()) if key in claimed]
            if back:
                reported.difference_update(back)
                claimed = [key for key in claimed if key not in back]
            if claimed:
                record["reported"] = sorted(reported)
                record["seen"] = time.time()
            if after is not None:
                after(record)
            return claimed, result
    except state.StateError:
        if strict:
            return [], None
        scratch: dict = {}
        if before is not None:
            before(scratch)
        result = then(list(keys)) if then is not None else None
        if after is not None:
            after(scratch)
        return list(keys), result


def claim_delivery(main_path: Path, key: str, store_path: Path | None = None) -> bool:
    """claim_deliveries for one key."""
    return bool(claim_deliveries(main_path, [key], store_path)[0])


def watch_active(record: dict) -> bool:
    watch = record.get("watch") if isinstance(record, dict) else None
    return isinstance(watch, dict) and _pid_alive(watch.get("pid"))


# --- the watch loop (4.14.3) ------------------------------------------------------

DEFAULT_SESSION_INTERVAL = 2.0
_UUID_RE = reader._UUID_RE
_PUBLIC_ID_RE = re.compile(r"^s_[0-9a-f]{10,12}$")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def resolve_session(value: str | None, root: Path) -> Path | None:
    """A session's main transcript from a path, a session UUID, or a public id (`s_...`), or
    from the caller's own session when `value` is empty. None when nothing matches."""
    value = (value or "").strip() or os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.suffix == ".jsonl" and candidate.is_file():
        return candidate
    if _UUID_RE.fullmatch(value):
        matches = sorted(root.glob(f"*/{value.lower()}.jsonl"))
        matches = matches or sorted(root.glob(f"*/{value}.jsonl"))
        return matches[0] if matches else None
    if _PUBLIC_ID_RE.fullmatch(value):
        for path in sorted(root.glob("*/*.jsonl")):
            if _UUID_RE.fullmatch(path.stem) and session_public_id(path) == value:
                return path
    return None


def watch_session(main_path: Path, *, interval: float = DEFAULT_SESSION_INTERVAL,
                  max_hours: float = 6.0, json_mode: bool = False, out=None, err=None,
                  follower: SessionFollower | None = None) -> int:
    """The poll loop: 0 on ENDED or when another watch already follows the session, 4 on
    LIMIT (4.14.3)."""
    import sys

    from .watch import _format_hours

    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    follower = SessionFollower(main_path) if follower is None else follower
    deadline = _clock() + max_hours * 3600.0
    try:
        while True:
            events = follower.poll()
            redact = session_redactor(main_path, follower._agent_ids()) if events else None
            for event in events:
                if json_mode:
                    print(render_json(event, redact), file=out, flush=True)
                else:
                    for line in render_text(event, redact):
                        print(line, file=out, flush=True)
            if follower.duplicate or follower.ended:
                return 0
            remaining = deadline - _clock()
            if remaining <= 0:
                event = {"event": "LIMIT", "hours": _format_hours(max_hours)}
                print(render_json(event) if json_mode else render_text(event)[0], file=out,
                      flush=True)
                return 4
            _sleep(min(interval, remaining))
    finally:
        follower.close()
        if follower.names.unavailable:
            print(f"agent-postmortem watch: {disclosure.UNAVAILABLE_DIAGNOSTIC}", file=err)


# --- rendering ------------------------------------------------------------------


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _line(word: str, rest: str) -> str:
    return f"{word:<{_LABEL_WIDTH}}{rest}"


def counts_text(paths: int, partial: bool, commits: int, commits_unknown: bool = False) -> str:
    """`<p> paths, <c> commits`, with `<p>+` for a partial ledger (zero included: a lower bound,
    not an established zero) and `commit count unknown` when a commit ran with no id."""
    paths_text = f"{paths}+ paths" if partial else _plural(paths, "path")
    if commits_unknown or commits < 0:
        commit_text = "commit count unknown"
    else:
        commit_text = _plural(commits, "commit")
    return f"{paths_text}, {commit_text}"


def _counts_line(event: dict) -> str:
    text = counts_text(event.get("paths", 0), bool(event.get("paths_partial")),
                       event.get("commits", 0), bool(event.get("commits_unknown")))
    return _line("", f"it had changed {text}.")


def render_text(event: dict, redact: Redactor | None = None) -> list[str]:
    """The text lines of one event (4.14.3), at most 78 columns, share level (the watch passes
    its session_redactor)."""
    redact = Redactor() if redact is None else redact
    kind = event["event"]
    if kind == "START":
        if event.get("watched_by"):
            return [_line("START", f"session {event['session']} is already watched by pid "
                                   f"{event['watched_by']}; this watch exits.")]
        lines = [_line("START", f"session {event['session']}: "
                                f"{_plural(event['subagents'], 'subagent')} so far, "
                                f"{_plural(event['workflows'], 'workflow')}.")]
        if event.get("ended_before"):
            lines.append(_line("", f"{event['ended_before']} ended before this watch.  "
                                   "-> agent-postmortem status"))
        return [redact(line) for line in lines]
    if kind == "KILL":
        return [redact(line) for line in (
            _line("KILL", f"{event['agent']} was interrupted; nothing in this session stopped it."),
            _counts_line(event),
            _line("", f"-> {event['next_command']}"),
        )]
    if kind == "FAILED":
        return [redact(line) for line in (
            _line("FAILED", f"{event['agent']}: {event['cause']}."),
            _counts_line(event),
            _line("", f"-> {event['next_command']}"),
        )]
    if kind == "CLAIM":
        if event.get("check") == "returned_on_error":
            lines = (
                _line("CLAIM", f"{event['agent']} returned, but its own last entry says:"),
                _line("", f"{event['detail']}."),
                _line("", f"-> {event['next_command']}"),
            )
        else:
            lines = (
                _line("CLAIM", f"{event['agent']} returned, but a file its prompt asked for "
                               "is missing."),
                _line("", f"-> {event['next_command']} names it"),
            )
        return [redact(line) for line in lines]
    if kind == "STATE":
        return [redact(_line("STATE", f"background session {event['session']} is now "
                                      f"{event['state']}."))]
    if kind == "WORKFLOW":
        return [f"WORKFLOW {event['run_id']} launched.  -> {event['next_command']}"]
    if kind == "ENDED":
        return [_line("ENDED", f"session {event['session']} ended.")]
    if kind == "LIMIT":
        return [_line("LIMIT", f"{event['hours']}h reached.")]
    return []


def render_json(event: dict, redact: Redactor | None = None) -> str:
    """One event as JSON, share level: every string value goes through the same Redactor the
    text lines use (a cause phrase can carry transcript text, 4.14.3)."""
    redact = Redactor() if redact is None else redact
    shown = {key: redact(value) if isinstance(value, str) else value
             for key, value in event.items()}
    return json.dumps(shown, sort_keys=True)


__all__ = [
    "SessionFollower", "SubFacts", "MainFacts", "Tail", "changes", "final_cause",
    "missing_outputs", "render_json", "render_text",
]

