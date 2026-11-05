"""Background workers a session launched, and what their job files say (docs/DESIGN.md 4.14.5;
field reports 2026-11-04, Zeyu's direction: Claude Code records the state, the plugin tells you).

Claude Code keeps a background session's state in `~/.claude/jobs/<short id>/state.json` and
tells nobody: a worker sat 8.5 hours on a permission prompt while the session that launched it
heard nothing. No file links a job to the session that launched it, so the link is read from the
launch itself: a Bash call that runs `claude --bg` and whose result names the new job's short id,
and whose job was created during that call.

This module reads a job file and says what it means. Standard library only and cheap: the
per-call gate (hook_gate.py) imports it on every tool call. The shell lexer is imported only for
a command that names `claude` and `--bg`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

WORKERS_KEPT = 128
PARSES_PER_CALL = 8
_JOB_FILE_BYTES = 1 << 20
_NAME_MAX = 40
_LABEL_MAX = 40
# The call's own time window, with this much slack, must hold the job's createdAt. On the launch's
# own PostToolUse the transcript may not hold the call yet, so "the last 5 minutes" stands in.
LAUNCH_SLACK_SECONDS = 5.0
PAYLOAD_WINDOW_SECONDS = 300.0
# At the first look at a worker: trouble older than this, and a delivery older than the second,
# are recorded and not told (the first call after an upgrade must not recite last week).
FIRST_LOOK_TROUBLE_SECONDS = 86400.0
FIRST_LOOK_DELIVERED_SECONDS = 600.0

_SHORT = r"[0-9a-f]{8}"
_SHORT_RE = re.compile(_SHORT)
_BACKGROUNDED_RE = re.compile(rf"^\s*backgrounded\s+·\s+({_SHORT})\b(?:\s+·\s+(.*\S))?\s*$", re.M)
_LAUNCH_NOTE_RE = re.compile(r"\s+\(idle\b.*$")  # `name (idle — send a prompt to start)`
_HINT_RE = re.compile(rf"^\s*claude (?:attach|logs|stop) ({_SHORT})\b", re.M)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# What Claude Code itself writes into `needs` for a prompt: `approve <tool>[: description]`, with
# a tool's display or plain name (`Write`, `Entering worktree`, `mcp__x__y`, `a tool`, `server -
# tool (MCP)`), `approve plan`, `approve N new project MCP ...`, `approve message from ...`. A job
# whose `state` reads `blocked` can also hold the worker's own words from its last turn, even
# starting with "approve" (seen live: `approve Write for worker <id>: run ...`, written by a manager
# about its worker), so there only these forms count, a display name being at most two words; a
# job still `working` with `tempo` blocked is on a live prompt, where any `approve ` is Claude
# Code's. A misreading says "waiting for an answer", whose next step still helps.
_SYSTEM_APPROVAL_RE = re.compile(
    r"approve (?:plan$|\d+ new project MCP|message from |(?:a tool|mcp__[\w-]+"
    r"|[A-Z][\w-]*(?: [\w-]+)?|[^:]*\(MCP\))(?::|$))")
_MCP_FORM_RE = re.compile(r"approve \d+ new project MCP")
_MCP_LABEL_RE = re.compile(r"approve \d+ new project MCP servers?\b")
_LABEL_CHARS_RE = re.compile(r"[A-Za-z0-9 _.()\-]+")
# Words before the command word that only change when or whether it runs.
_RESERVED = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{"})
# `claude <subcommand>` never starts a background session, whatever else its line holds.
_SUBCOMMANDS = frozenset({"agents", "attach", "config", "doctor", "install", "logs", "mcp",
                          "plugin", "rm", "stop", "update"})

PERMISSION = "permission"
QUESTION = "question"
WAITING_PROMPT = "waiting-prompt"
ERROR = "error"
UNKNOWN = "unknown"
FAILED = "failed"
STALLED = "stalled"
DELIVERED_WAITING = "delivered-waiting"
DELIVERED = "delivered"
WORKING = "working"
STOPPED = "stopped"

TROUBLE = frozenset({PERMISSION, QUESTION, WAITING_PROMPT, ERROR, UNKNOWN, FAILED, STALLED})
INFORMATION = frozenset({DELIVERED_WAITING, DELIVERED})


# --- the link ----------------------------------------------------------------------------------


def command_launches(command: object) -> bool:
    return count_launches(command) > 0


def count_launches(command: object) -> int:
    """Whether a segment of this command runs `claude` with `--bg` or `--background` among its own
    options, before any `--` and not as a quoted value: a grep naming the flag, an echo, or a prompt
    mentioning it is no launch."""
    if not isinstance(command, str):
        return 0
    plain = command.replace("\\\n", "").replace('"', "").replace("'", "").replace("\\", "")
    if "claude" not in plain or not ("--bg" in plain or "--background" in plain):
        return 0
    from . import commands, pre_bash

    try:
        segments = commands.lex(pre_bash._without_substitutions(command)).segments
        return sum(1 for segment in segments if _segment_launches(segment, pre_bash))
    except Exception:  # never a reason to fail a turn: this call is simply not read as a launch
        return 0


def _segment_launches(segment, pre_bash) -> bool:
    words = [word.text for word in segment.words]
    while words and (words[0] in _RESERVED or not words[0].strip("(")):
        words = words[1:]
    if words:
        words[0] = words[0].lstrip("(")
    found = pre_bash._command_words(words)
    if not found or found[0].rsplit("/", 1)[-1] != "claude":
        return False
    options = found[1:found.index("--", 1)] if "--" in found[1:] else found[1:]
    if not options or options[0] in _SUBCOMMANDS:
        return False
    flags = {flag for flag in ("--bg", "--background") if flag in options}
    if not flags:
        return False
    as_words = [word for word in segment.words if word.text in flags]
    return not as_words or any(not word.quoted for word in as_words)


def launched_ids(text: object) -> dict[str, str | None]:
    """Each worker a launch's result names at a line start (`backgrounded · X · name`, else one
    of the hints after it, since a `| tail -1` leaves only the last: 33 of 85 real launches), with
    its name when printed."""
    if not isinstance(text, str):
        return {}
    found: dict[str, str | None] = {}
    for match in _BACKGROUNDED_RE.finditer(text):
        name = match.group(2)
        found[match.group(1)] = _LAUNCH_NOTE_RE.sub("", name) if name else name
    for match in _HINT_RE.finditer(text):
        found.setdefault(match.group(1), None)
    return found


def payload_launches(payload: dict) -> dict[str, str | None]:
    """The workers a PostToolUse payload's own Bash call launched: the one read that does not
    wait for the transcript."""
    if payload.get("tool_name") != "Bash":
        return {}
    tool_input = payload.get("tool_input")
    response = payload.get("tool_response")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    stdout = response.get("stdout") if isinstance(response, dict) else response
    if not command_launches(command):
        return {}
    return launched_ids(stdout)


def job_file(jobs_root: Path | str, short: str) -> str:
    return os.path.join(jobs_root, short, "state.json")


def job_signature(path: str) -> list[int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [info.st_mtime_ns, info.st_size]


def job_gone(path: str) -> bool:
    """Only a job file that is really not there: a stat that fails for any other reason says
    nothing about the worker."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def read_job(path: str) -> dict | None:
    """A job file as a dict, or None: missing, over 1 MB, unreadable, or not a JSON object."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(_JOB_FILE_BYTES + 1)
        if len(raw) > _JOB_FILE_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _instant(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def created_during(data: dict, start: object, end: object) -> bool:
    """Provenance: the job was created inside the launching call's own time window. A real launch
    line printing an older id links nothing; a job with no readable time links nothing."""
    created = _instant(data.get("createdAt"))
    if created is None or not isinstance(start, (int, float)) \
            or not isinstance(end, (int, float)):
        return False
    return start - LAUNCH_SLACK_SECONDS <= created <= end + LAUNCH_SLACK_SECONDS


# --- what a job file says ----------------------------------------------------------------------


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def effective_state(data: dict) -> object:
    """The job's state as it is now. `state` is set when a turn ends, so it can be stale, and
    `tempo` is live: a worker on a permission prompt reads `state: working, tempo: blocked`
    (2.1.318, seen live; the field worker read `state: blocked` from its earlier question all
    night), and one given a new prompt after delivering reads `state: done, tempo: active`. Only
    a job that ended keeps its word."""
    word = data.get("state")
    if word in ("stopped", "failed"):
        return word
    tempo = data.get("tempo")
    if tempo == "blocked":
        return "blocked"
    if tempo == "active":
        return "working"
    return word


def classify(data: dict) -> tuple[str | None, str | None]:
    """(class, label) of a job file, the worker's own words never in either. Precedence for a
    blocked job: a permission prompt, then `awaiting input`, `send a prompt to start`, an API
    error, a delivered result, the worker's free text, unknown. None when the state word is not
    one this reads."""
    word = effective_state(data)
    needs = _text(data.get("needs")).strip()
    output = data.get("output")
    result = output.get("result") if isinstance(output, dict) else None
    if word == "blocked":
        # `state` says whether `needs` can hold the worker's words from its last turn (above)
        turn_ended = data.get("state") == "blocked" or data.get("tempo") != "blocked"
        if needs.startswith("approve ") and (not turn_ended or _SYSTEM_APPROVAL_RE.match(needs)):
            return PERMISSION, _permission_label(needs)
        if needs.startswith("awaiting input"):
            return QUESTION, None
        if needs.startswith("send a prompt to start"):
            return WAITING_PROMPT, None
        if "API Error" in _text(data.get("detail")):
            return ERROR, None
        if result and turn_ended:
            return DELIVERED_WAITING, None
        return (QUESTION, None) if needs else (UNKNOWN, None)
    if word == "done":
        return DELIVERED, None
    if word == "failed":
        return FAILED, None
    if word == "working":
        return WORKING, None
    if word == "stopped":
        return STOPPED, None
    return None, None


def _permission_label(needs: str) -> str:
    """The text before the first `:`, which holds the tool; its description is withheld. Anything
    but plain words reads `approve`."""
    if needs.startswith("approve message from "):
        return "approve message from another session"  # what follows is the sender's claim
    if _MCP_FORM_RE.match(needs):
        mcp = _MCP_LABEL_RE.match(needs)  # the server names after it can be anything
        return mcp.group(0) if mcp is not None else "approve"
    label = _CONTROL_RE.sub("", needs.partition(":")[0]).strip()
    if len(label) > _LABEL_MAX or not _LABEL_CHARS_RE.fullmatch(label):
        return "approve"
    return label


def identity(data: dict, cls: str | None = None) -> str:
    """What must change for a worker to be told about again: state word, class, and the digest of
    `needs`, so a second prompt of the same tool or a new question counts and a touch of the file
    does not."""
    cls = classify(data)[0] if cls is None else cls
    word = effective_state(data)
    needs = _text(data.get("needs")) if word == "blocked" else ""  # else it is a stale leftover
    digest = hashlib.sha256(needs.encode("utf-8", "replace")).hexdigest()[:8] if needs else ""
    return f"{_text(word)}/{cls}/{digest}"


def name_of(data: dict, short: str, printed: str | None = None) -> str:
    # the launch result's own name first: the job's can be renamed later, from what it was told
    value = printed if isinstance(printed, str) and printed else data.get("name")
    if not isinstance(value, str):
        return "worker"
    clean = " ".join(_CONTROL_RE.sub(" ", _ANSI_RE.sub("", value)).split())
    if "/" in clean or "\\" in clean:
        return "worker"  # what looks like a path stays out of a hook line
    return clean[:_NAME_MAX] or "worker"



def view(short: str, path: str, data: dict, now: float, printed: str | None = None) -> dict | None:
    """One worker as the channels need it, or None when its state word is not one this reads."""
    cls, label = classify(data)
    if cls is None:
        return None
    try:
        age = max(0.0, now - os.stat(path).st_mtime)
    except OSError:
        age = 0.0
    return {"short": short, "name": name_of(data, short, printed), "class": cls, "label": label,
            "age": age, "identity": identity(data, cls)}


def is_silent_first_look(worker: dict) -> bool:
    cls = worker["class"]
    if cls in TROUBLE:
        return worker["age"] > FIRST_LOOK_TROUBLE_SECONDS
    if cls in INFORMATION:
        return worker["age"] > FIRST_LOOK_DELIVERED_SECONDS
    return True


def due(told: dict, observed: list[dict], finishes: dict | None = None
        ) -> tuple[list[dict], dict[str, str]]:
    """Which observed workers must be told (trouble first, oldest change first) and which to
    record without a line. `told` is the live store's per-worker last identity (4.14.6 rule 3):
    a line is due when the observed identity differs from it and means something. `finishes` holds
    how many times each was told delivering since its last trouble: a worker that wakes on its
    own cron finishes every hour, so its second finish says so and later ones are recorded
    (field report 2026-11-04)."""
    lines: list[dict] = []
    quiet: dict[str, str] = {}
    finishes = finishes or {}
    for worker in observed:
        short = worker["short"]
        known = told.get(short)
        if known == worker["identity"]:
            continue
        told_finishing = finishes.get(short, 0)
        if known is None and is_silent_first_look(worker):
            quiet[short] = worker["identity"]
        elif worker["class"] == DELIVERED and told_finishing >= 2:
            quiet[short] = worker["identity"]
        elif worker["class"] in TROUBLE or worker["class"] in INFORMATION:
            lines.append({**worker, "finishes": told_finishing}
                         if worker["class"] == DELIVERED else worker)
        else:
            quiet[short] = worker["identity"]
    lines.sort(key=lambda w: (w["class"] not in TROUBLE, -w["age"]))
    return lines, quiet


# --- the words ---------------------------------------------------------------------------------


def age_text(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 7200:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def reason_words(cls: str, label: str | None = None) -> str:
    """What a class means, without the worker's own words."""
    if cls == PERMISSION:
        return f"waiting for a permission approval ({label})" if label \
            else "waiting for a permission approval"
    return {
        QUESTION: "waiting for an answer",
        WAITING_PROMPT: "waits for its first prompt",
        ERROR: "stopped on an API error",
        UNKNOWN: "the reason is not recorded",
        DELIVERED_WAITING: "delivered, waiting on a decision",
    }.get(cls, cls)


def line(worker: dict) -> str:
    """docs/DESIGN.md 4.14.5: one worker's change, with the action that helps. Fixed words, the
    short id and name its own launch result printed, and a permission's tool."""
    cls = worker["class"]
    who = f"WORKER {worker['name']} ({worker['short']})"
    short = worker["short"]
    age = age_text(worker["age"])
    if cls == PERMISSION:
        return (f"{who} blocked {age}: {reason_words(cls, worker.get('label'))}. A person must "
                f"approve it: claude attach {short}")
    if cls == QUESTION:
        return f"{who} blocked {age}: {reason_words(cls)}. Reply with SendMessage, or: " \
               f"claude attach {short}"
    if cls == WAITING_PROMPT:
        return f"{who} {reason_words(cls)}: claude attach {short}"
    if cls == ERROR:
        return f"{who} blocked {age}: {reason_words(cls)}: claude logs {short}"
    if cls == UNKNOWN:
        return f"{who} blocked {age}; {reason_words(cls)}: claude attach {short}"
    if cls == FAILED:
        return f"{who} failed {age} ago: claude logs {short}"
    if cls == STALLED:
        if worker.get("stdin"):
            return (f"{who} stalled: idle {age} on a background command that waits on stdin, and "
                    f"nothing will wake it: claude attach {short}")
        # field report 2026-11-04: only the command's own files are seen, and it may end yet
        return (f"{who} idle {age}: its background command's output has not changed. It may be "
                f"hung or writing elsewhere, and only its end would wake the worker. Check before "
                f"stopping it: claude attach {short}")
    if cls == DELIVERED_WAITING:
        return f"{who} delivered {age} ago and waits for your reply: claude logs {short}"
    if worker.get("finishes"):
        return (f"{who} finished again {age} ago. It may wake on its own schedule, so its later "
                f"finishes are not told: claude logs {short}")
    return f"{who} finished {age} ago: claude logs {short}"


def save_told(record: dict, known, quiet: dict[str, str], shown: list[dict]) -> None:
    """Into the session's record in the live store, inside the claim's lock: the identities just
    recorded or told, and how many times each worker was told delivering since its last trouble.
    Only the workers still `known` stay."""
    table = record.get("workers") if isinstance(record.get("workers"), dict) else {}
    counts = record.get("worker_finishes") if isinstance(record.get("worker_finishes"), dict) \
        else {}
    table = {**table, **quiet, **{w["short"]: w["identity"] for w in shown}}
    for worker in shown:
        if worker["class"] == DELIVERED:
            count = counts.get(worker["short"])
            counts[worker["short"]] = (count if isinstance(count, int) else 0) + 1
        elif worker["class"] in TROUBLE:
            counts.pop(worker["short"], None)
    record["workers"] = {short: value for short, value in table.items() if short in known}
    record["worker_finishes"] = {short: value for short, value in counts.items()
                                 if short in known}
