"""Discover Claude Code run directories/transcripts and parse them into a Run.

Tolerant by design: a malformed line becomes an entry in Run.diagnostics, not
an exception that aborts the scan. Transcripts are read line by line and
never held in memory as a whole file or a list of raw lines; only the parsed
Event fields (each text field truncated to _EXCERPT_LIMIT chars) survive. A
field's full decoded text is still scanned for exit codes and known runtime
markers before it is truncated (see _scan_markers): the excerpt bounds what
survives for display and evidence, not what a detector can see happened.

Two run kinds (docs/DESIGN.md section 1): 'workflow' (a wf_* directory under
.../subagents/workflows/) and 'session' (a main-session transcript plus its
Agent-tool subagent transcripts under .../subagents/agent-*.jsonl). Both
share the transcript-parsing helpers below (_read_transcript and friends);
load_run dispatches on layout and load_session owns the session-specific
assembly (docs/DESIGN.md section 4.2).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import commands, disclosure, ledger, markers, returns
from .claims import self_reported_problems
from .detectors.base import attribute_interrupt, meaningful_event_before
from .markers import (
    API_ERROR,
    BASH_BACKGROUNDED,
    BASH_TIMEOUT,
    CLASSIFIER_UNAVAILABLE,
    GIT_NOT_REPO,
    KILL_SENTINEL_PREFIX,
    MACHINE_SLEEP,
    SANDBOX_WRITE_DENIED,
    SHELL_GUARD_BLOCK_REGEX,
    STRUCTURED_OUTPUT_MISMATCH,
    TOOL_INPUT_INVALID,
    USAGE_LIMIT_SESSION,
    USAGE_LIMIT_WEEKLY,
)
from .models import (
    FACTS_UNKNOWN,
    LAUNCH_DIAGNOSTIC_PREFIXES,
    Attempt,
    AttemptFacts,
    Event,
    Generation,
    Interrupt,
    KeyedDiagnostic,
    Lifecycle,
    LogicalAgent,
    MarkerHit,
    QualityNote,
    Run,
    ToolCall,
    discovery_part,
)

_EXCERPT_LIMIT = 400
_EXIT_CODE_RE = re.compile(r"[Ee]xit code[: ]+(-?\d+)")

# ToolCall bounds (docs/DESIGN.md 4.1's v0.2 reader changes): a path or
# command past these lengths is not truncated (which would silently make it
# look like a different, shorter one); a path is dropped to None, a command
# is dropped to None with its sha256 kept instead.
_TOOL_CALL_PATH_LIMIT = 1024
_TOOL_CALL_COMMAND_LIMIT = 2048
# Which tool_use input field ToolCall.path is read from, per tool name
# (docs/DESIGN.md 4.1's v0.2 reader changes). Every other tool leaves path
# None.
_PATH_TOOL_INPUT_FIELD = {
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

# docs/DESIGN.md 4.1 (N4b-1): MultiEdit's `file_path` lives in its own private
# ToolCall field, never in `path`, so schema-2 paths_written, in_flight, and
# rescue (all of which read `path`) cannot change.
_MULTIEDIT_TOOL = "MultiEdit"
# Tools whose tool_use `command` is kept on a ToolCall (docs/DESIGN.md 4.1).
_COMMAND_TOOLS = ("Bash", "Monitor")

# Harness/tooling scaffolding a field's text can carry that is not something
# the agent said or did (docs/DESIGN.md 4.5): stripped before the excerpt is
# taken so it never dominates meaningful_event_before's "last real event"
# search or an evidence line, but markers.py still scans the text before
# this strip runs, so a marker fragment hidden inside one of these spans is
# never missed.
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_TOTAL_TOKENS_RE = re.compile(r"<total_tokens>.*?</total_tokens>", re.DOTALL)
_HARNESS_NOTE_RE = re.compile(r"\[harness:.*?\]", re.DOTALL)

# docs/DESIGN.md 5.3: the opaque id shapes a workflow agent id / session
# subagent id and a workflow run directory name are documented to have.
# Ingestion validates every id against these before it is carried into a
# Run, Attempt, or Finding; see _valid_agent_id and _valid_workflow_run_id.
_AGENT_ID_RE = re.compile(r"a[0-9a-f]{16}")
_WORKFLOW_RUN_ID_RE = re.compile(r"wf_[0-9a-f]{8}-[0-9a-f]{3}")


def _valid_agent_id(agent_id: str | None, diagnostics: list[str], context: str) -> str:
    """A conforming agent id (docs/DESIGN.md 5.1: 17 hex chars starting with
    'a') passes through unchanged -- it carries no user text, so it is not
    redacted anywhere else in the tool. A non-conforming one (a corrupted
    journal entry, a hand-edited or foreign transcript filename) is opaque
    for a different reason: it might be anything, including a real name, so
    it is replaced by 'agent-' + sha256(id)[:8] and never carried into a
    Run/Attempt/Finding verbatim. The diagnostic names only the fixed
    `context` and the replacement, never the original value."""
    if agent_id and _AGENT_ID_RE.fullmatch(agent_id):
        return agent_id
    replacement = "agent-" + hashlib.sha256((agent_id or "").encode("utf-8")).hexdigest()[:8]
    diagnostics.append(
        f"{context}: agent id did not match the expected format, replaced with {replacement}"
    )
    return replacement


def valid_workflow_run_id(name: str, diagnostics: list[str] | None = None) -> str:
    """A conforming workflow run id (docs/DESIGN.md 5.1:
    'wf_' + 8 hex + '-' + 3 hex) passes through unchanged. A non-conforming
    run directory name is replaced by 'wf_' + sha256(name)[:8] + '-x'; the
    diagnostic never echoes the original directory name."""
    if _WORKFLOW_RUN_ID_RE.fullmatch(name or ""):
        return name
    replacement = "wf_" + hashlib.sha256((name or "").encode("utf-8")).hexdigest()[:8] + "-x"
    if diagnostics is not None:
        diagnostics.append(
            f"workflow run directory: run id did not match the expected format, "
            f"replaced with {replacement}"
        )
    return replacement


# Kept under its old name for the in-module callers; every other module
# (watch, hooks_cli) uses the public valid_workflow_run_id (N3b C2).
_valid_workflow_run_id = valid_workflow_run_id

# Every literal marker fragment a detector matches with a plain substring
# test, in one place, so _scan_markers can check all of them against a
# field's full text in a single pass.
_MARKER_FRAGMENTS: tuple[str, ...] = (
    *BASH_BACKGROUNDED,
    *BASH_TIMEOUT,
    *TOOL_INPUT_INVALID,
    *STRUCTURED_OUTPUT_MISMATCH,
    *SANDBOX_WRITE_DENIED,
    *CLASSIFIER_UNAVAILABLE,
    *GIT_NOT_REPO,
    *USAGE_LIMIT_SESSION,
    *USAGE_LIMIT_WEEKLY,
    *MACHINE_SLEEP,
    *API_ERROR,
)
_SHELL_GUARD_RE = re.compile(SHELL_GUARD_BLOCK_REGEX)
_SHELL_GUARD_MARKER = "SHELL_GUARD_BLOCK_REGEX"

# A tool_result whose resolved tool_name is one of these wraps a child's own
# output inside the parent's transcript (docs/DESIGN.md 4.2): it is marked
# envelope=True with markers scrubbed so a child's error text quoted here is
# never double-counted.
_ENVELOPE_TOOL_NAMES = frozenset({"Agent", "Task", "Workflow", "TaskOutput"})

# docs/DESIGN.md 4.3's self-reference rule: a tool_result whose resolved
# tool_name is one of these is read_only_output=True unconditionally -- the
# tool can only ever report what is already on disk, never a runtime
# failure that happened to this agent.
_READ_ONLY_TOOL_NAMES = frozenset({"Read", "Grep", "Glob", "NotebookRead"})

# docs/DESIGN.md 4.3's self-reference rule (D-B): a Bash command counts as
# read-only when its text contains one of these read commands as a whole
# word ANYWHERE in it -- not only as the first word -- so a loop like `for f
# in ...; do echo "=== $f ==="; cat "$f"; done` is caught too, and none of
# _NOT_READ_ONLY_BASH_RE's write-indicating tokens (a real write, an
# interpreter/test runner invocation, or output redirection) appear anywhere
# in it either.
_READ_ONLY_BASH_RE = re.compile(
    r"\b(?:cat|sed|head|tail|grep|rg|nl|less|more|bat|wc|diff)\b"
    r"|\bgit\s+(?:show|diff|log|grep|blame)\b"
)
_NOT_READ_ONLY_BASH_RE = re.compile(
    r"\b(?:node|codex|python|pytest|npm|npx|curl|make|tee)\b|\buv\s+run\b"
)


def _is_read_only_bash_command(command: str | None) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if ">" in command:
        return False
    if _NOT_READ_ONLY_BASH_RE.search(command):
        return False
    return bool(_READ_ONLY_BASH_RE.search(command))


def _tool_result_read_only(
    tool_name: str | None, tool_use_id: object, tool_commands: dict | None
) -> bool:
    """docs/DESIGN.md 4.3's self-reference rule (D-A): whether a tool_result
    resolved to tool_name is read_only_output -- unconditionally for Read/
    Grep/Glob/NotebookRead, or for Bash when the paired tool_use's command
    (tracked in tool_commands by tool_use_id) is one of the read-only
    forms."""
    if tool_name in _READ_ONLY_TOOL_NAMES:
        return True
    if tool_name != "Bash" or tool_commands is None or not isinstance(tool_use_id, str):
        return False
    return _is_read_only_bash_command(tool_commands.get(tool_use_id))

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _exists(path: Path) -> bool:
    """Whether `path` may exist. Only an absent path (or a file under a non-directory) is False:
    an unreadable one (a directory without the execute bit, an overlong name) is not proof of
    absence, so the caller's own open reports it as an unreadable file, a diagnostic and never a
    crash (F fix 8, W8-5). `Path.exists` raises `PermissionError` there."""
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    except ValueError:
        return False
    return True


def _is_file(path: Path) -> bool:
    """`Path.is_file` that treats any `OSError` as "not a file" (older Pythons raise for an
    unreadable parent directory)."""
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _is_dir(path: Path) -> bool:
    """`Path.is_dir` that treats any `OSError` as "not a directory"."""
    try:
        return path.is_dir()
    except (OSError, ValueError):
        return False


class NotARun(Exception):
    """path is neither a workflow run directory (has journal.jsonl) nor a
    session transcript (a .jsonl file whose first parsed line carries
    sessionId). docs/DESIGN.md 4.2; the CLI turns this into exit 2."""


def _scan_markers(text: str | None) -> frozenset[str]:
    """Which markers.py fragments (or, for the one regex marker, its name)
    appear anywhere in text. Called on a field's full decoded text before
    _excerpt bounds it, so a marker past the excerpt limit is still seen."""
    if not text:
        return frozenset()
    hits = {fragment for fragment in _MARKER_FRAGMENTS if fragment in text}
    if _SHELL_GUARD_RE.search(text):
        hits.add(_SHELL_GUARD_MARKER)
    return frozenset(hits)


# --- run discovery and dispatch --------------------------------------------


@dataclass(frozen=True)
class RunRef:
    kind: str  # 'workflow' | 'session'
    path: Path
    run_id: str


# Process-level registry backing _session_public_id's collision detection,
# mirroring ParentIndex's process-level cache below: every (slug, uuid) pair
# seen so far, keyed by the 10-hex id its payload hashes to. A second,
# distinct pair landing on the same key is a public-id collision.
_SESSION_ID_REGISTRY: dict[str, set[tuple[str, str]]] = {}


def _session_id_digest(payload: str) -> str:
    """The full sha256 hex digest behind a session's public id. A thin,
    monkeypatchable seam: a test forces a public-id collision between two
    distinct (slug, uuid) pairs by replacing this function, since sha256
    makes a real 10-hex collision practically unreachable."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _session_public_id(slug: str, uuid: str, diagnostics: list[str] | None = None) -> str:
    """'s_' + the first 10 hex chars of sha256(f"{slug}/{uuid}") (docs/
    DESIGN.md 4.2). A second, distinct (slug, uuid) pair landing on the same
    10-hex id switches both to a 12-hex id instead and, when diagnostics is
    given, records the collision there."""
    digest = _session_id_digest(f"{slug}/{uuid}")
    short = digest[:10]
    seen = _SESSION_ID_REGISTRY.setdefault(short, set())
    seen.add((slug, uuid))
    if len(seen) > 1:
        if diagnostics is not None:
            others = sorted(f"{s}/{u}" for s, u in seen if (s, u) != (slug, uuid))
            diagnostics.append(
                f"session public id collision on s_{short}: {slug}/{uuid} collides with "
                f"{', '.join(others)}; using a 12-hex id instead"
            )
        return f"s_{digest[:12]}"
    return f"s_{short}"


def discover_runs(
    root: Path, kinds: frozenset[str] = frozenset({"workflow", "session"})
) -> list[RunRef]:
    """Find every run of the given kinds under root, sorted by path.

    Workflow refs: <root>/<project>/<session>/subagents/workflows/wf_*
    (docs/DESIGN.md section 1). Session refs: <root>/<project>/<uuid>.jsonl.
    """
    root = Path(root)
    if not _exists(root):
        return []

    refs: list[RunRef] = []
    if "workflow" in kinds:
        for path in root.glob("*/*/subagents/workflows/wf_*"):
            if _is_dir(path):
                refs.append(RunRef(kind="workflow", path=path, run_id=path.name))
    if "session" in kinds:
        session_paths = [
            path
            for path in root.glob("*/*.jsonl")
            if _is_file(path)
            and _UUID_RE.fullmatch(path.stem)
            and _has_timestamped_conversation(path)
        ]
        # Register every (slug, uuid) pair in this batch before reading any
        # of them back: a collision between two paths discovered together
        # must give both the same 12-hex id, not just whichever is asked for
        # second (docs/DESIGN.md 4.2).
        for path in session_paths:
            _session_public_id(path.parent.name, path.stem)
        for path in session_paths:
            run_id = _session_public_id(path.parent.name, path.stem)
            refs.append(RunRef(kind="session", path=path, run_id=run_id))

    refs.sort(key=lambda ref: ref.path)
    return refs


# docs/DESIGN.md 4.1 (G-C, GC-1): a final physical line with no trailing newline whose bytes
# are not valid UTF-8 is a half-written line (a live write cut inside a character); an
# undecodable byte anywhere else is the N4 C2 rule. A transcript is read strictly, the cost it
# always had; only a strict read that fails to decode, on a file whose last line is torn, goes
# on with errors="surrogateescape", where an undecodable byte shows up as one U+DC80..U+DCFF
# character in its line (GC-1b: scanning every line cost 4.3 s of CPU on a 248-run scan).
_UNDECODABLE_BYTE_RE = re.compile("[\udc80-\udcff]")
_TAIL_STEP = 65536


def _ends_in_torn_line(path: Path) -> bool:
    """Whether the bytes after the file's last line break are not valid UTF-8: a final line
    with no newline, cut inside a character. Reads backwards from the end of the file."""
    chunks: list[bytes] = []
    try:
        with path.open("rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            while position > 0:
                step = min(_TAIL_STEP, position)
                position -= step
                handle.seek(position)
                chunk = handle.read(step)
                cut = max(chunk.rfind(b"\n"), chunk.rfind(b"\r"))
                if cut >= 0:
                    chunks.append(chunk[cut + 1 :])
                    break
                chunks.append(chunk)
    except OSError:
        return False
    tail = b"".join(reversed(chunks))
    try:
        tail.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _transcript_lines(path: Path):
    """(line_no, line, torn) for every physical line of a transcript, read strictly. When the
    strict read fails to decode and the file's last line is torn, the read goes on from the
    first line it had not given yet, with errors="surrogateescape": the torn last line comes
    back once, with an empty `line` and torn True; an undecodable byte on any other line raises
    UnicodeDecodeError, as the strict read did. OSError propagates."""
    given = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                given = line_no
                yield line_no, line, False
        return
    except UnicodeDecodeError:
        if not _ends_in_torn_line(path):
            raise
    with path.open("r", encoding="utf-8", errors="surrogateescape") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no <= given:
                continue
            if _UNDECODABLE_BYTE_RE.search(line):
                if line.endswith("\n"):
                    raise UnicodeDecodeError(
                        "utf-8", b"", 0, 1, "undecodable byte before the end of a transcript line"
                    )
                yield line_no, "", True
            else:
                yield line_no, line, False


@contextlib.contextmanager
def _transcript_reader(path: Path):
    """_transcript_lines(path), closed on exit, so a caller that stops early closes the file."""
    lines = _transcript_lines(path)
    try:
        yield lines
    finally:
        lines.close()


def _has_timestamped_conversation(path: Path) -> bool:
    """Whether path carries at least one 'user' or 'assistant' entry with a
    parseable timestamp (docs/DESIGN.md 4.2's definition of a session run).

    Real Claude Code project directories can hold one-line 'bridge-session'
    files (type 'bridge-session', no timestamp, no user/assistant entry at
    all) that are plumbing, not a run anyone had a conversation in; without
    this check they would still match the '<uuid>.jsonl' discovery glob and
    show up in scan as an empty session. Stops at the first qualifying
    entry rather than reading the whole file: a real session's first
    timestamped entry is typically near the top, and a file with none (the
    only case that reads to EOF) is small by construction."""
    if not _is_file(path) or path.suffix != ".jsonl":
        return False
    try:
        with _transcript_reader(path) as lines:
            for _line_no, line, _torn in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") not in ("user", "assistant"):
                    continue
                if _parse_ts(entry.get("timestamp")) is not None:
                    return True
    except UnicodeError:
        # bytes that are not UTF-8 before any qualifying entry: the file
        # cannot be ruled out as a session, and its own read reports it
        # unreadable (docs/DESIGN.md 4.1, N4 fix C2)
        return True
    except OSError:
        return False
    return False


def _is_workflow_dir(path: Path) -> bool:
    """A run is a directory with its own journal.jsonl (docs/DESIGN.md
    section 1)."""
    return _is_dir(path) and _exists(path / "journal.jsonl")


def _sniff_session_file(path: Path) -> bool:
    """Whether the first *decodable* line of a .jsonl file carries sessionId
    (docs/DESIGN.md 4.2's load_run dispatch: "a .jsonl file whose first
    parsed line carries sessionId"), without parsing the rest of it. A
    malformed physical line ahead of it is skipped, not decisive: it is
    still recorded as a diagnostic once load_session actually reads the
    file, same as any other malformed line."""
    if not _is_file(path) or path.suffix != ".jsonl":
        return False
    try:
        with _transcript_reader(path) as lines:
            for _line_no, line, _torn in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                return isinstance(entry, dict) and "sessionId" in entry
    except UnicodeError:
        return True
    except OSError:
        return False
    return False



def load_run(ref_or_path: "RunRef | Path | str") -> Run:
    """Parse one run (a workflow directory or a session transcript) into a
    Run. Dispatches on layout, per docs/DESIGN.md 4.2; raises NotARun for
    anything else.

    A RunRef already carries the kind discover_runs found it under, so it
    is honored directly rather than re-sniffed: discover_runs's workflow
    refs come from */*/subagents/workflows/wf_* directories regardless of
    whether journal.jsonl exists yet (docs/DESIGN.md 4.2), and _load_workflow
    already tolerates a missing journal.jsonl with a diagnostic. Re-checking
    _is_workflow_dir here would raise NotARun for exactly the directories
    discover_runs promised callers a RunRef for, aborting a fleet scan.
    """
    if isinstance(ref_or_path, RunRef):
        if ref_or_path.kind == "workflow":
            return _load_workflow(ref_or_path.path)
        if ref_or_path.kind == "session":
            return load_session(ref_or_path.path)

    path = ref_or_path.path if isinstance(ref_or_path, RunRef) else Path(ref_or_path)
    if _is_workflow_dir(path):
        return _load_workflow(path)
    if _sniff_session_file(path):
        return load_session(path)
    raise NotARun(str(path))


# --- workflow runs -----------------------------------------------------


def _load_workflow(path: Path) -> Run:
    """Parse one workflow run directory (journal.jsonl, meta files,
    transcripts) into a Run."""
    path = Path(path)
    diagnostics: list[str] = []
    run_id = _valid_workflow_run_id(path.name, diagnostics)
    project_slug = _project_slug(path)

    # docs/DESIGN.md 4.1/4.13.1 (N4b-1): Run.discovery keeps each source's
    # own diagnostics, in the order reported, beside the flat `diagnostics`
    # list (whose content and order are unchanged).
    discovery: dict[str, list[str]] = {
        "journal": [], "parent": [], "transcripts": [], "meta": [],
    }

    def note(source: str, text: str) -> None:
        diagnostics.append(text)
        if not isinstance(text, QualityNote):
            discovery[source].append(text)

    journal_start = len(diagnostics)
    journal, started_events, journal_ok, journal_line_numbers = _read_journal(path, diagnostics)
    discovery["journal"].extend(discovery_part(diagnostics[journal_start:]))
    incomplete = not journal_ok

    # docs/DESIGN.md 4.1 (unit M1a): the per-key state machine decides how
    # each 'started' occurrence closes (or stays open); _workflow_finished_
    # from_occurrences turns that into Run.finished below, and every
    # diagnostic it raises (a mismatched agentId, an orphan result/failed)
    # forces status INCOMPLETE, same as any other reader diagnostic.
    journal_attempts, journal_sm_diagnostics = _run_journal_state_machine(
        journal, journal_line_numbers
    )
    attempt_lookup = {id(occ.started_entry): occ for occ in journal_attempts}
    diagnostics.extend(journal_sm_diagnostics)
    discovery["journal"].extend(journal_sm_diagnostics)
    incomplete = incomplete or bool(journal_sm_diagnostics)
    # docs/DESIGN.md 4.6: line numbers for every journal entry, by identity,
    # so a malformed 'started' entry's diagnostic can still cite
    # 'journal.jsonl:<line>' below -- entries in `started_events` are the
    # same dict objects `_read_journal` appended from `journal`, never copies.
    entry_line_no = {id(entry): line_no for entry, line_no in zip(journal, journal_line_numbers)}

    claude_version: str | None = None
    session_id: str | None = None
    all_event_ts: list[datetime] = []
    attempts: list[Attempt] = []
    agents_by_key: dict[str, LogicalAgent] = {}
    ordinal_counts: dict[str | None, int] = {}

    for entry in started_events:
        raw_key = entry.get("key")
        raw_agent_id_field = entry.get("agentId", "")
        raw_label_field = entry.get("label", "")
        raw_phase_field = entry.get("phase", "")
        line_no = entry_line_no.get(id(entry))

        # docs/DESIGN.md 4.6: a non-string key or agentId names no attempt
        # this reader can safely build -- an int/list/bool key can never
        # index agents_by_key the way a string key does below, and a
        # non-string agentId used to crash _valid_agent_id (M1 review
        # finding 2). Skip the whole entry, with a line-specific diagnostic,
        # and force status INCOMPLETE, rather than build a malformed Attempt.
        # A missing or explicit-null key/agentId is the existing, valid
        # "unkeyed"/"blank id" shape and is untouched here; only a *present
        # but wrongly typed* value is a problem.
        if raw_key is not None and not isinstance(raw_key, str):
            note("journal", f"journal.jsonl:{line_no}: started event has a non-string key")
            incomplete = True
            continue
        if raw_agent_id_field is not None and not isinstance(raw_agent_id_field, str):
            note("journal", f"journal.jsonl:{line_no}: started event has a non-string agentId")
            incomplete = True
            continue

        key = raw_key
        raw_agent_id = raw_agent_id_field if raw_agent_id_field is not None else ""

        # docs/DESIGN.md 4.6 (N3b, C-5): a missing, null, or non-canonical
        # string agentId still builds its Attempt below, exactly as before,
        # but the journal entry is not a trustworthy identity, so the run is
        # INCOMPLETE and the diagnostic names the line. (A non-string value
        # was skipped above with its own diagnostic.)
        canonical_agent_id = disclosure.is_canonical_id("agent", raw_agent_id_field)
        if not canonical_agent_id:
            note("journal", f"journal.jsonl:{line_no}: started event has no canonical agentId")
            incomplete = True

        # A non-string label or phase is cosmetic, never identifying: the
        # entry still becomes an attempt, with the bad value replaced by ""
        # (never None -- report.py's AGENT table formats it directly with
        # f"{label:<22}", which raises on None) and a diagnostic recorded.
        if isinstance(raw_label_field, str):
            label = raw_label_field
        else:
            label = ""
            if raw_label_field is not None:
                note(
                    "journal",
                    QualityNote(f"journal.jsonl:{line_no}: started event has a non-string label"),
                )
        if isinstance(raw_phase_field, str):
            phase = raw_phase_field
        else:
            phase = ""
            if raw_phase_field is not None:
                note(
                    "journal",
                    QualityNote(f"journal.jsonl:{line_no}: started event has a non-string phase"),
                )

        # journal_attempt is None only for an unkeyed entry (key is None --
        # the state machine tracks attempts per key and has nothing to key
        # an unkeyed entry's occurrence by either); a non-string key or
        # agentId already continued past this point above. ordinal falls
        # back to the same per-key counting v0.1 always did.
        journal_attempt = attempt_lookup.get(id(entry))
        if journal_attempt is not None:
            ordinal = journal_attempt.ordinal
        else:
            ordinal_counts[key] = ordinal_counts.get(key, 0) + 1
            ordinal = ordinal_counts[key]

        # Filesystem lookups use the journal value only when it is a
        # canonical agent id (N3b S14): the id is spliced into a file name,
        # so a value such as `q/../../OUTSIDE` would name a file outside the
        # run directory. A non-canonical id has no transcript and no meta
        # file opened for it; the attempt is already INCOMPLETE (C-5 above).
        meta_start = len(diagnostics)
        meta = _load_meta(path, raw_agent_id, diagnostics) if canonical_agent_id else {}
        discovery["meta"].extend(discovery_part(diagnostics[meta_start:]))
        agent_type = meta.get("agentType")
        id_start = len(diagnostics)
        agent_id = _valid_agent_id(raw_agent_id, diagnostics, "journal.jsonl")
        discovery["journal"].extend(discovery_part(diagnostics[id_start:]))

        transcript_path = path / f"agent-{raw_agent_id}.jsonl"
        unreadable = False
        transcript_state = None
        if canonical_agent_id and _exists(transcript_path):
            transcript_start = len(diagnostics)
            transcript_state = ledger.TranscriptState()
            transcript_result = _read_transcript_tracked(
                transcript_path, diagnostics, state=transcript_state
            )
            discovery["transcripts"].extend(discovery_part(diagnostics[transcript_start:]))
            (
                events, version, transcript_session_id, unreadable, over_threshold,
                tool_calls, marker_hits,
            ) = transcript_result
            incomplete = incomplete or unreadable or over_threshold
            # docs/DESIGN.md 4.2 (F5 hardening): a duplicate tool_use_id
            # anywhere in this attempt's transcript is a reader diagnostic
            # like any other, so it forces Run.status INCOMPLETE too.
            incomplete = incomplete or any(call.ambiguous for call in tool_calls)
            if claude_version is None and version:
                claude_version = version
            if session_id is None and transcript_session_id:
                session_id = transcript_session_id
        else:
            transcript_path = None
            events = []
            tool_calls = []
            marker_hits = []

        event_ts = [event.ts for event in events if event.ts is not None]
        first_ts = min(event_ts) if event_ts else None
        last_ts = max(event_ts) if event_ts else None
        all_event_ts.extend(event_ts)
        incomplete = incomplete or _edge_event_has_timestamp_diagnostic(events, diagnostics)

        # docs/DESIGN.md 4.1: the journal state machine decides the final
        # outcome whenever it closed this attempt ('completed' / 'failed'
        # always win, even over a trailing kill sentinel; 'superseded'
        # becomes 'killed' instead when the sentinel is present -- the v0.1
        # silent retry, unchanged). An attempt the state machine never
        # closed (still open at end of journal) falls back to the
        # transcript alone, exactly as v0.1 always did for every attempt.
        base_outcome = _compute_outcome(transcript_path, events)
        closed_by = journal_attempt.closed_by if journal_attempt is not None else None
        if closed_by == "completed":
            outcome = "completed"
        elif closed_by == "failed":
            outcome = "failed"
        elif closed_by == "superseded":
            if base_outcome == "killed":
                outcome = "killed"
            else:
                outcome = "superseded"
                incomplete = True
                note(
                    "journal",
                    KeyedDiagnostic(
                        f"journal.jsonl: key@{line_no} attempt {ordinal} was replaced by a "
                        "later attempt with no recorded outcome",
                        f"journal.jsonl: key {key!r} attempt {ordinal} was replaced by a "
                        "later attempt with no recorded outcome",
                    ),
                )
        else:
            outcome = base_outcome

        # docs/DESIGN.md section 3/4.1: reported is read only for an
        # attempt the state machine actually closed with a 'result' event
        # (closed_by == 'completed') -- never for 'failed', 'superseded',
        # or an attempt still open at the end of the journal, even when
        # base_outcome alone would otherwise read 'completed' for one of
        # those (an attempt with no journal-recorded result at all has
        # nothing of its own to report).
        reported = None
        reported_line = None
        self_report = None
        if closed_by == "completed" and journal_attempt is not None:
            reported = _build_reported(journal_attempt.closing_entry)
            reported_line = journal_attempt.closing_line
            # docs/DESIGN.md 4.3 (G3): only the shared predicate's answer survives, never a key
            # name or a value
            self_report = self_reported_problems(
                journal_attempt.closing_entry.get("result")
                if isinstance(journal_attempt.closing_entry, dict) else None
            )

        attempt = Attempt(
            agent_id=agent_id,
            key=key,
            label=label,
            phase=phase,
            agent_type=agent_type,
            meta=meta,
            transcript_path=transcript_path,
            events=events,
            first_ts=first_ts,
            last_ts=last_ts,
            ordinal=ordinal,
            outcome=outcome,
            usage=_sum_usage(events),
            reported=reported,
            reported_line=reported_line,
            _self_report=self_report,
            tool_calls=tool_calls,
            marker_hits=marker_hits,
            # docs/DESIGN.md 4.1 (N4b-1): the state machine's own closure,
            # None for an unkeyed entry (it has no occurrence).
            journal_state=(
                None if journal_attempt is None else (closed_by if closed_by else "open")
            ),
            _transcript_state=transcript_state,
            entry_cwds=tuple(transcript_state.cwds) if transcript_state is not None else (),
        )
        attempt.facts = _build_attempt_facts(
            tool_calls, outcome, last_ts, transcript_path, unreadable
        )
        # Interrupt tracks the transcript's own kill sentinel, independent
        # of the final outcome above (docs/DESIGN.md 4.4): a 'failed'
        # attempt whose transcript also ends in the sentinel still keeps it
        # as evidence rather than losing it to the journal's own verdict.
        if base_outcome == "killed":
            attempt.interrupt = _build_interrupt(attempt)
        attempts.append(attempt)

        if key is not None:
            agent = agents_by_key.get(key)
            if agent is None:
                agent = LogicalAgent(key=key, label=label, phase=phase, attempts=[])
                agents_by_key[key] = agent
            agent.attempts.append(attempt)

    # docs/DESIGN.md 4.13.1 (S1, N4 fix): a transcript or meta file of the run
    # directory that no journal started event names.
    discovery["transcripts"].extend(_unnamed_file_diagnostics(path, started_events))

    run = Run(
        run_id=run_id,
        kind="workflow",
        path=path,
        project_slug=project_slug,
        session_id=session_id,
        claude_version=claude_version,
        journal=journal,
        agents=list(agents_by_key.values()),
        attempts=attempts,
        started_at=min(all_event_ts) if all_event_ts else None,
        ended_at=max(all_event_ts) if all_event_ts else None,
        diagnostics=diagnostics,
        status="INCOMPLETE" if incomplete else "COMPLETE",
        discovery=discovery,
    )
    # One parent-index snapshot serves interrupt attribution, the parent
    # discovery source, and the lifecycle (and so Generation.result_ts):
    # no second parent read (docs/DESIGN.md 4.13.1, N4b-1).
    parent_index = _parent_index_for(run)
    _attribute_interrupts(run, parent_index)
    # docs/DESIGN.md section 3 (N2 root-cause fix): _lifecycle_from_parts is
    # the one table _load_workflow and the public workflow_lifecycle (used
    # by the hook, which has no already-parsed Run to reuse) both build on --
    # hooks_cli's workflow-unit finished check (7.2) ultimately calls the
    # same table (through lifecycle_finished), so this reader and that hook
    # can never again drift into two different answers the way the N2
    # checkpoint did. Unlike workflow_lifecycle, this call passes the
    # journal resolution _load_workflow already computed above (N1N2 review
    # finding 7) instead of letting workflow_lifecycle read and
    # state-machine journal.jsonl a second time.
    journal_resolved = _workflow_finished_from_occurrences(journal_attempts)
    run.lifecycle = _lifecycle_from_parts(run.path, run_id, journal_resolved, parent_index)
    run.finished = lifecycle_finished(run.lifecycle)
    if run.lifecycle.terminal == "unknown":
        # docs/DESIGN.md 4.13.3 (v0.3 N2): a terminal the reader could not
        # pin down (a conflict, an unrecognized status, an ambiguous launch,
        # an unreadable parent, an incomplete generation) is exactly the
        # same "the reader could not fully establish what happened" shape
        # 4.6 already forces INCOMPLETE for everywhere else.
        run.status = "INCOMPLETE"
    # docs/DESIGN.md 4.13.1 (N4b-2): the ledgers and the run's own risk
    # population completeness, from the final discovery and generations.
    ledger.attach_ledgers(run)
    return run


def _unnamed_file_diagnostics(run_dir: Path, started_events: list[dict]) -> list[str]:
    """One diagnostic per `agent-<id>.jsonl` or `agent-<id>.meta.json` regular
    file of a workflow run directory whose id no journal `started` event names
    as its `agentId` string, sorted by file name (docs/DESIGN.md 4.13.1, S1)."""
    named = {
        event["agentId"] for event in started_events if isinstance(event.get("agentId"), str)
    }
    unnamed: list[str] = []
    for pattern, suffix in (("agent-*.jsonl", ".jsonl"), ("agent-*.meta.json", ".meta.json")):
        for file_path in run_dir.glob(pattern):
            agent_id = file_path.name.removeprefix("agent-").removesuffix(suffix)
            if _is_file(file_path) and agent_id not in named:
                unnamed.append(file_path.name)
    return [
        f"{name}: transcript named by no journal started event" for name in sorted(unnamed)
    ]


def _project_slug(path: Path) -> str:
    """The project directory name, two levels above 'subagents' in path.

    Falls back to the run directory's parent name when path was not reached
    through the standard <root>/<project>/<session>/subagents/workflows/wf_*
    layout (for example a fixture or a bare directory passed on the command
    line), so load_run never raises just because of where it was called from.
    """
    parts = path.parts
    if "subagents" in parts:
        idx = parts.index("subagents")
        if idx >= 2:
            return parts[idx - 2]
    return path.parent.name


def _read_journal(
    path: Path, diagnostics: list[str]
) -> tuple[list[dict], list[dict], bool, list[int]]:
    """journal, started_events, whether the journal could be parsed cleanly
    (docs/DESIGN.md 4.6: False when the file is missing or any line in it
    failed to decode as a JSON object, in which case Run.status must be
    INCOMPLETE rather than trusting a partial read), and line_numbers -- the
    1-based journal.jsonl line number of each entry in `journal`, in the same
    order, so a caller can later point evidence at the exact line a 'result'
    or 'failed' event came from (docs/DESIGN.md section 3,
    Attempt.reported_line)."""
    journal: list[dict] = []
    started_events: list[dict] = []
    line_numbers: list[int] = []
    journal_path = path / "journal.jsonl"
    if not _exists(journal_path):
        diagnostics.append("journal.jsonl: missing")
        return journal, started_events, False, line_numbers

    ok = True
    try:
        with journal_path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    diagnostics.append(f"journal.jsonl:{line_no}: {exc}")
                    ok = False
                    continue
                if not isinstance(entry, dict):
                    diagnostics.append(f"journal.jsonl:{line_no}: not a JSON object")
                    ok = False
                    continue
                journal.append(entry)
                line_numbers.append(line_no)
                if entry.get("type") == "started":
                    started_events.append(entry)
    except OSError as exc:
        diagnostics.append(f"journal.jsonl: unreadable: {exc}")
        return journal, started_events, False, line_numbers
    except UnicodeError as exc:
        diagnostics.append(f"journal.jsonl: undecodable: {exc}")
        return journal, started_events, False, line_numbers
    return journal, started_events, ok, line_numbers


# --- per-key journal state machine (docs/DESIGN.md 4.1, unit M1a) ------
#
# Real Claude Code journals carry a `failed` event ("type", "key",
# "agentId") that v0.1 never read, and a real `result` event carries
# "type", "key", "agentId", "result" -- never the "outcome" field v0.1's
# fixtures happened to include but no code ever looked at. Because `failed`
# was ignored, a run whose every key actually ended (some by result, some
# by failure) was shown as unfinished forever. `_run_journal_state_machine`
# is the one place that tracks, per key, at most one open attempt at a
# time, consuming events in file order. `workflow_lifecycle` builds on it
# (via `_workflow_finished_from_occurrences`) for the journal-resolved row
# of the run lifecycle table (docs/DESIGN.md section 3, 4.13.3) that
# `_load_workflow`'s Run.finished and hooks_cli's workflow-unit finished
# check both ultimately read off Lifecycle -- never this state machine
# directly. `workflow_journal_finished` also builds on it, but only for
# watch's own, deliberately narrower, journal-only DONE rule (4.12).


@dataclass
class _JournalAttempt:
    """One 'started' occurrence of a key and how the state machine below
    resolved it, in journal order. `closed_by` is None while the attempt
    is still open at the end of the journal. reader._load_workflow turns
    `closed_by` plus whether this attempt's own transcript ends in the kill
    sentinel into the Attempt's final `outcome`: 'completed' and 'failed'
    are final as soon as the state machine assigns them (even over a
    trailing sentinel: the sentinel still becomes Attempt.interrupt, kept
    as evidence, but the journal's own report of what happened wins); a
    'superseded' occurrence becomes 'killed' instead when its transcript
    carries the sentinel (the v0.1 silent retry, unchanged); an occurrence
    that is still open falls back to the transcript alone, exactly as
    v0.1 always did for every attempt."""

    key: str
    agent_id: str
    ordinal: int
    started_entry: dict
    closed_by: str | None = None  # None | 'completed' | 'failed' | 'superseded'
    closing_entry: dict | None = None
    # The 1-based journal.jsonl line number closing_entry came from, or None
    # when line numbers were not supplied to the state machine (callers that
    # only care about 'finished', such as workflow_journal_finished, never
    # pass them). _load_workflow uses this to set Attempt.reported_line.
    closing_line: int | None = None


def _run_journal_state_machine(
    journal: list[dict],
    line_numbers: list[int] | None = None,
) -> tuple[list[_JournalAttempt], list[str]]:
    """The per-key attempt state machine of docs/DESIGN.md 4.1: at most one
    attempt is open per key at a time, events consumed in file order.

    - 'started' with no attempt open for its key opens attempt n+1.
    - 'started' while one is open closes it 'superseded' (reader.py may
      reclassify this as 'killed' once it has the transcript; see
      _JournalAttempt above), then opens n+1.
    - 'result' / 'failed' whose agentId equals the open attempt's, or that
      carries no agentId at all, closes it 'completed' / 'failed'.
    - 'result' / 'failed' with a different agentId, or with no attempt open
      for its key, closes nothing and adds a diagnostic.

    An entry that is not a dict, or whose 'key' is not a string, is skipped
    silently: _read_journal already turned a line that could not become a
    JSON object into its own diagnostic, and a keyless entry names no
    attempt this state machine could ever resolve. Returns (occurrences,
    diagnostics); occurrences are in the same order as their 'started'
    events.

    line_numbers, when given, is parallel to `journal` (as _read_journal
    returns it) and fills in each occurrence's `closing_line`. Omitted by
    every caller that only needs 'finished' (workflow_journal_finished), so
    it defaults to None for every occurrence rather than requiring every
    caller to thread a matching list through."""
    open_by_key: dict[str, _JournalAttempt] = {}
    ordinal_counts: dict[str, int] = {}
    occurrences: list[_JournalAttempt] = []
    diagnostics: list[str] = []
    if line_numbers is None:
        line_numbers = [None] * len(journal)

    for entry, line_no in zip(journal, line_numbers):
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str):
            continue
        etype = entry.get("type")

        if etype == "started":
            raw_agent_id = entry.get("agentId")
            agent_id = raw_agent_id if isinstance(raw_agent_id, str) else ""
            current = open_by_key.get(key)
            if current is not None:
                current.closed_by = "superseded"
                current.closing_entry = entry
                current.closing_line = line_no
            ordinal_counts[key] = ordinal_counts.get(key, 0) + 1
            occurrence = _JournalAttempt(
                key=key, agent_id=agent_id, ordinal=ordinal_counts[key], started_entry=entry,
            )
            occurrences.append(occurrence)
            open_by_key[key] = occurrence
        elif etype in ("result", "failed"):
            current = open_by_key.get(key)
            event_agent_id = entry.get("agentId")
            if current is None:
                diagnostics.append(
                    KeyedDiagnostic(
                        f"journal.jsonl: {etype!r} event for key@{line_no} has no open attempt",
                        f"journal.jsonl: {etype!r} event for key {key!r} has no open attempt",
                    )
                )
                continue
            if event_agent_id is not None and event_agent_id != current.agent_id:
                diagnostics.append(
                    KeyedDiagnostic(
                        f"journal.jsonl: {etype!r} event for key@{line_no} does not match "
                        "the open attempt's agent id",
                        f"journal.jsonl: {etype!r} event for key {key!r} does not match "
                        "the open attempt's agent id",
                    )
                )
                continue
            current.closed_by = "completed" if etype == "result" else "failed"
            current.closing_entry = entry
            current.closing_line = line_no
            del open_by_key[key]
        # every other type (launched, unknown types) is ignored here, same
        # as everywhere else in the reader (docs/DESIGN.md 3).

    return occurrences, diagnostics


# --- Attempt.reported: the agent's own self-reported result (docs/ -----
# --- DESIGN.md section 3, 4.1; .reviews/proposals/2026-09-v0.2-guided- ---
# --- rescue.md section 4.3) ----------------------------------------------
#
# A real journal 'result' event's own value is a dict in most cases and a
# string in the rest (measured: 483/705 dict, 222/705 string on real data).
# A dict's values are the agent's self-reported claims about its own work
# (tests_passed, ok, report_path, ...); many of those observed keys (note,
# summary, bytes, ...) are free text or otherwise unbounded and are never
# carried past the key name itself. A string result can run to 25k chars
# and is private end to end: only its length ever leaves this function.

_REPORTED_MAX_KEYS = 40
_REPORTED_KEY_NAME_LIMIT = 64
_REPORTED_STATUS_LIMIT = 32
_REPORTED_REPORT_PATH_LIMIT = 1024
_REPORTED_MAX_COMMITS = 20
_COMMIT_HEX_RE = re.compile(r"[0-9a-f]{7,40}")


def _reported_claims(value: dict) -> dict:
    """The allowlisted subset of a dict result's own claims (docs/DESIGN.md
    section 3): only these exact keys, only when present with the one
    shape each is allowed to have; every other key or shape is dropped
    silently (it still shows up in the sibling 'keys' list, never here).
    `bool` is checked with `type(v) is int` for the integer claims (not
    `isinstance`), since Python's bool is a subclass of int and 'ok'/
    'ready' already have their own, separate bool-only claims."""
    claims: dict = {}

    for key in ("ok", "ready"):
        if key in value and isinstance(value[key], bool):
            claims[key] = value[key]

    if "status" in value:
        status = value["status"]
        if isinstance(status, str) and len(status) <= _REPORTED_STATUS_LIMIT:
            claims["status"] = status

    for key in ("tests_passed", "tests_failed", "suite_passed", "suite_failed"):
        if key in value and type(value[key]) is int:
            claims[key] = value[key]

    if "blockers" in value and isinstance(value["blockers"], list):
        claims["blockers_count"] = len(value["blockers"])

    if "commits" in value and isinstance(value["commits"], list):
        commits = value["commits"]
        claims["commits_count"] = len(commits)
        claims["commits"] = [
            item for item in commits if isinstance(item, str) and _COMMIT_HEX_RE.fullmatch(item)
        ][:_REPORTED_MAX_COMMITS]

    if "report_path" in value:
        report_path = value["report_path"]
        if isinstance(report_path, str) and len(report_path) <= _REPORTED_REPORT_PATH_LIMIT:
            claims["report_path"] = report_path

    return claims


def _build_reported(closing_entry: dict) -> dict | None:
    """Attempt.reported for an attempt the journal state machine closed with
    a 'result' event (docs/DESIGN.md section 3): None for a 'failed' event,
    for one that never closed, or for a malformed/absent `result` value.
    Never reads anything the journal did not itself carry on this one
    entry."""
    value = closing_entry.get("result") if isinstance(closing_entry, dict) else None
    if isinstance(value, str):
        return {"type": "text", "text_length": len(value)}
    if isinstance(value, dict):
        keys = [key[:_REPORTED_KEY_NAME_LIMIT] for key in sorted(value.keys())]
        return {
            "type": "dict",
            "keys": keys[:_REPORTED_MAX_KEYS],
            "claims": _reported_claims(value),
        }
    return None


def _workflow_finished_from_occurrences(occurrences: list[_JournalAttempt]) -> bool:
    """docs/DESIGN.md 4.1: a key is resolved once it has no open attempt.
    True when there is at least one (started) key and every key is
    resolved -- the last occurrence recorded for each key is the one that
    matters, since an earlier occurrence of the same key is always closed
    (superseded) the moment a later one starts."""
    last_by_key: dict[str, _JournalAttempt] = {}
    for occurrence in occurrences:
        last_by_key[occurrence.key] = occurrence
    if not last_by_key:
        return False
    return all(occurrence.closed_by is not None for occurrence in last_by_key.values())


def workflow_journal_finished(journal: list[dict]) -> bool:
    """docs/DESIGN.md 4.1 and 4.12: whether every key the journal started has
    been resolved by the per-key state machine above -- the journal-only
    reading of 'finished', never the full definition of Run.finished (that
    is workflow_lifecycle/lifecycle_finished, docs/DESIGN.md section 3). This
    narrower rule is kept, and called out by name, for exactly one caller:
    watch's DONE event (4.12), which deliberately tracks the journal alone as
    a live progress signal while it is still growing, not the parent's own
    (slower-arriving) terminal evidence. Not used by _load_workflow or by
    hooks_cli._workflow_finished since the N2 root-cause fix -- both of
    those now go through workflow_lifecycle so they can never again drift
    into two different answers for the same run (the N2 checkpoint had
    hooks_cli still calling this function directly, which is exactly how
    that drift happened)."""
    occurrences, _ = _run_journal_state_machine(journal)
    return _workflow_finished_from_occurrences(occurrences)


def _load_meta_file(meta_path: Path, diagnostics: list[str]) -> dict:
    if not _exists(meta_path):
        return {}
    try:
        with meta_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        diagnostics.append(f"{meta_path.name}: {exc}")
        return {}
    if not isinstance(data, dict):
        diagnostics.append(f"{meta_path.name}: not a JSON object")
        return {}
    return data


def _load_meta(run_path: Path, agent_id: str, diagnostics: list[str]) -> dict:
    return _load_meta_file(run_path / f"agent-{agent_id}.meta.json", diagnostics)


# --- session runs -----------------------------------------------------


def _session_dir(path: Path) -> Path:
    """The <root>/<slug>/<session>/ directory sitting next to a session's
    own <session>.jsonl transcript, holding its subagents/ subdirectory."""
    return path.parent / path.stem


def _discover_session_agent_paths(path: Path) -> list[Path]:
    subagents_dir = _session_dir(path) / "subagents"
    if not _is_dir(subagents_dir):
        return []
    return sorted(p for p in subagents_dir.glob("agent-*.jsonl") if _is_file(p))


def _orphan_meta_diagnostics(main_path: Path) -> list[str]:
    """One diagnostic per canonical `subagents/agent-<id>.meta.json` of the
    session directory whose `agent-<id>.jsonl` is not a file, in sorted id
    order (docs/DESIGN.md 4.13.1, C1). The meta file's content is not read."""
    subagents_dir = _session_dir(main_path) / "subagents"
    if not _is_dir(subagents_dir):
        return []
    orphans: list[str] = []
    for meta_path in subagents_dir.glob("agent-*.meta.json"):
        agent_id = meta_path.name.removeprefix("agent-").removesuffix(".meta.json")
        if (
            _is_file(meta_path)
            and disclosure.is_canonical_id("agent", agent_id)
            and not _is_file(subagents_dir / f"agent-{agent_id}.jsonl")
        ):
            orphans.append(agent_id)
    return [
        f"agent-{agent_id}.meta.json: child named by a meta file has no transcript"
        for agent_id in sorted(orphans)
    ]


def _parse_main_attempt(
    main_path: Path,
) -> tuple[Attempt, str | None, str | None, list[str], bool, list[datetime]]:
    """(main_attempt, claude_version, session_id, diagnostics, incomplete,
    event_timestamps) for main_path alone, with no subagent involved. The
    building block both load_session (one fresh parse) and
    load_session_view (docs/DESIGN.md 7.2, D6: optionally cached across
    several calls within one hook invocation) share."""
    diagnostics: list[str] = []
    launch_log = _LaunchLog()
    main_state = ledger.TranscriptState()
    # docs/DESIGN.md 4.2 (FA4): a fork's inherited prefix is the parent's, not this
    # session's; the same raw boundary ParentIndex uses.
    inherited = _fork_boundary(main_path)
    (
        main_events, claude_version, session_id, unreadable, over_threshold, main_tool_calls,
        main_marker_hits,
    ) = _read_transcript_tracked(
        main_path, diagnostics, launch_log=launch_log, state=main_state,
        skip_through_line=inherited,
    )
    if inherited and not unreadable:
        diagnostics.append(QualityNote(
            f"inherited {inherited} leading lines from another session (a fork); "
            "they are that session's"
        ))
    incomplete = unreadable or over_threshold
    incomplete = incomplete or any(call.ambiguous for call in main_tool_calls)
    main_ts = [event.ts for event in main_events if event.ts is not None]
    incomplete = incomplete or _edge_event_has_timestamp_diagnostic(main_events, diagnostics)
    main_outcome = _compute_outcome(main_path, main_events)
    main_last_ts = max(main_ts) if main_ts else None
    main_attempt = Attempt(
        agent_id=main_path.stem,
        key=None,
        label="main",
        phase="",
        agent_type=None,
        meta={},
        transcript_path=main_path,
        events=main_events,
        first_ts=min(main_ts) if main_ts else None,
        last_ts=main_last_ts,
        ordinal=1,
        outcome=main_outcome,
        usage=_sum_usage(main_events),
        tool_calls=main_tool_calls,
        marker_hits=main_marker_hits,
        _launch_log=launch_log,
        _transcript_state=main_state,
        entry_cwds=tuple(main_state.cwds),
    )
    main_attempt.facts = _build_attempt_facts(
        main_tool_calls, main_outcome, main_last_ts, main_path, unreadable
    )
    if main_outcome == "killed":
        main_attempt.interrupt = _build_interrupt(main_attempt)
    return main_attempt, claude_version, session_id, diagnostics, incomplete, main_ts


def _load_session_from_paths(
    main_path: Path, agent_paths: list[Path], *, main_cache: dict | None = None,
    reconcile: bool = False,
) -> Run:
    """Parse a session run from an explicit main transcript plus an
    explicit list of subagent transcript paths (docs/DESIGN.md 4.2, 7.2's
    D6): the shared body of load_session (every subagent discovered under
    the session directory) and load_session_view (only the named ones, for
    an isolated single-subagent analysis).

    `main_cache`, when given, is a plain dict the caller owns and reuses
    across several calls: the main transcript is then parsed at most once
    per dict, keyed by path, rather than once per call (docs/DESIGN.md
    7.2's "the main transcript is parsed once per invocation"). load_session
    never passes one, so its own behavior (and cost) is unchanged.

    Raises NotARun (docs/DESIGN.md 4.2) for a main_path with no timestamped
    user/assistant entry -- a bridge-session marker, for example -- the same
    rule discover_runs applies, so a direct call here is never fooled by one
    that slipped past discovery some other way."""
    main_path = Path(main_path)
    if not _has_timestamped_conversation(main_path):
        raise NotARun(str(main_path))
    diagnostics: list[str] = []
    slug = main_path.parent.name
    uuid = main_path.stem
    run_id = _session_public_id(slug, uuid, diagnostics)

    cached = main_cache.get(main_path) if main_cache is not None else None
    if cached is None:
        cached = _parse_main_attempt(main_path)
        if main_cache is not None:
            main_cache[main_path] = cached
    main_attempt, claude_version, session_id, main_diagnostics, incomplete, main_ts = cached
    if main_cache is not None:
        # The cached parse is shared by every view of this invocation, but a
        # ledger depends on the run it belongs to (its discovery), so each run
        # gets its own copy of the main attempt.
        main_attempt = copy.copy(main_attempt)
    diagnostics.extend(main_diagnostics)
    # docs/DESIGN.md 4.1/4.13.1 (N4b-1): per-source diagnostics beside the
    # flat list. A session has no journal.
    discovery: dict[str, list[str]] = {
        "journal": [], "parent": [], "transcripts": discovery_part(main_diagnostics),
        "meta": [],
    }

    all_ts = list(main_ts)
    agents: list[LogicalAgent] = []
    attempts: list[Attempt] = []

    # docs/DESIGN.md 4.13.1 (G-A): the facts every analyzed child's transcript recorded, for the
    # return decision (resume requests may come from any transcript of the session).
    child_facts: dict[Path, returns.ReturnFacts] = {}
    child_meta: list[tuple[Attempt, str, dict, bool]] = []

    for agent_path in agent_paths:
        agent_path = Path(agent_path)
        id_start = len(diagnostics)
        agent_id = _valid_agent_id(
            agent_path.stem.removeprefix("agent-"), diagnostics, "session subagent"
        )
        discovery["transcripts"].extend(discovery_part(diagnostics[id_start:]))
        meta_path = agent_path.with_name(f"{agent_path.stem}.meta.json")
        meta_start = len(diagnostics)
        meta = _load_meta_file(meta_path, diagnostics)
        meta_bad = len(diagnostics) > meta_start
        discovery["meta"].extend(discovery_part(diagnostics[meta_start:]))
        label = meta.get("description", "")
        if not isinstance(label, str):
            label = ""
        agent_type = meta.get("agentType")
        phase = meta.get("workflowPhase", "")
        if not isinstance(phase, str):
            phase = ""

        transcript_start = len(diagnostics)
        sub_state = ledger.TranscriptState()
        sub_log = _LaunchLog()
        (
            events, sub_version, _, sub_unreadable, sub_over_threshold, sub_tool_calls,
            sub_marker_hits,
        ) = _read_transcript_tracked(agent_path, diagnostics, state=sub_state, launch_log=sub_log)
        sub_log.facts.path = agent_path
        sub_log.facts.unreadable = sub_unreadable
        child_facts[agent_path] = sub_log.facts
        discovery["transcripts"].extend(discovery_part(diagnostics[transcript_start:]))
        incomplete = incomplete or sub_unreadable or sub_over_threshold
        incomplete = incomplete or any(call.ambiguous for call in sub_tool_calls)
        if claude_version is None and sub_version:
            claude_version = sub_version
        event_ts = [event.ts for event in events if event.ts is not None]
        all_ts.extend(event_ts)
        incomplete = incomplete or _edge_event_has_timestamp_diagnostic(events, diagnostics)
        outcome = _compute_outcome(agent_path, events)
        sub_last_ts = max(event_ts) if event_ts else None

        attempt = Attempt(
            agent_id=agent_id,
            key=agent_id,
            label=label,
            phase=phase,
            agent_type=agent_type,
            meta=meta,
            transcript_path=agent_path,
            events=events,
            first_ts=min(event_ts) if event_ts else None,
            last_ts=sub_last_ts,
            ordinal=1,
            outcome=outcome,
            usage=_sum_usage(events),
            tool_calls=sub_tool_calls,
            marker_hits=sub_marker_hits,
            _transcript_state=sub_state,
            entry_cwds=tuple(sub_state.cwds),
        )
        attempt.facts = _build_attempt_facts(
            sub_tool_calls, outcome, sub_last_ts, agent_path, sub_unreadable
        )
        child_meta.append((attempt, agent_path.stem.removeprefix("agent-"), meta, meta_bad))
        if outcome == "killed":
            attempt.interrupt = _build_interrupt(attempt)
        attempts.append(attempt)
        agents.append(LogicalAgent(key=agent_id, label=label, phase=phase, attempts=[attempt]))

    run = Run(
        run_id=run_id,
        kind="session",
        path=main_path,
        project_slug=slug,
        session_id=session_id or uuid,
        claude_version=claude_version,
        journal=[],
        main=main_attempt,
        agents=agents,
        attempts=attempts,
        started_at=min(all_ts) if all_ts else None,
        ended_at=max(all_ts) if all_ts else None,
        diagnostics=diagnostics,
        status="INCOMPLETE" if incomplete else "COMPLETE",
        # docs/DESIGN.md 4.13.3 (v0.3 N2, D11): a session carries no Workflow
        # launch and no generation to pair, so it has no lifecycle model to
        # report at all -- finished is null, not true, distinct from the
        # dataclass's own "never set" default of True.
        finished=None,
        discovery=discovery,
    )
    if child_meta:
        _assign_return_states(main_path, child_facts, child_meta)
    parent_index = _parent_index_for(run)
    _attribute_interrupts(run, parent_index)
    if reconcile:
        # docs/DESIGN.md 4.13.1 (C1, N4 fix): a canonical meta file whose
        # child has no transcript, in sorted id order, before the launches.
        discovery["transcripts"].extend(_orphan_meta_diagnostics(main_path))
    if reconcile and main_attempt._launch_log is not None:
        # docs/DESIGN.md 4.13.1 (C-14, N4b-1): every Agent, Task, and
        # Workflow launch of the main transcript reconciles with a discovered
        # child, or adds one `transcripts` diagnostic. Workflow launches use
        # the generations of the same parent-index snapshot.
        discovery["transcripts"].extend(
            _reconcile_session_launches(
                main_path, main_attempt._launch_log, agent_paths, list(parent_index.generations)
            )
        )
    ledger.attach_ledgers(run)
    return run


def _read_evidence_facts(path: Path) -> "returns.ReturnFacts":
    """The return facts of a transcript read as evidence only (never an analyzed attempt): a
    nested child's launcher, or any other session transcript that may hold a resume request. A
    transcript that is not a file, or cannot be read, is `unreadable`."""
    log = _LaunchLog()
    if not _is_file(path):
        facts = returns.ReturnFacts(path, unreadable=True)
        return facts
    result = _read_transcript_tracked(path, [], launch_log=log)
    facts = log.facts
    facts.path = path
    facts.unreadable = result[3]
    return facts


class _SessionReturns:
    """The return decision of the session subagents of one main transcript (docs/DESIGN.md
    4.13.1, T0 to T5): the main transcript's facts and accepted notifications come from its
    ParentIndex (incremental, shared with the hooks); a nested child's launcher and every other
    transcript read for a resume request are evidence only."""

    def __init__(self, main_path: Path, loaded: "dict[Path, returns.ReturnFacts]"):
        self.main_path = Path(main_path)
        self.index = ParentIndex.for_path(self.main_path)
        self.main = self.index.facts
        self.main_unreadable = self.index._unreadable_diagnostic is not None
        self.loaded = loaded
        self._evidence: dict[Path, returns.ReturnFacts] = {}
        self.subagents = _session_dir(self.main_path) / "subagents"
        ids: set[str] = set()
        if _is_dir(self.subagents):
            patterns = (("agent-*.jsonl", ".jsonl"), ("agent-*.meta.json", ".meta.json"))
            for pattern, suffix in patterns:
                for found in self.subagents.glob(pattern):
                    ids.add(found.name.removeprefix("agent-").removesuffix(suffix))
        self.siblings = frozenset(ids)
        self._notes = [
            returns.Note(n.line_no, n.task_id, n.tool_use_id, n.status, n.ts)
            for n in self.index._accepted_notifications()
        ]

    def path_of(self, agent_id: str) -> Path:
        return self.subagents / f"agent-{agent_id}.jsonl"

    def facts_of(self, agent_id: str) -> "returns.ReturnFacts":
        path = self.path_of(agent_id)
        if path in self.loaded:
            return self.loaded[path]
        if path not in self._evidence:
            self._evidence[path] = _read_evidence_facts(path)
        return self._evidence[path]

    def notes_of(self, agent_id: str) -> "list[returns.Note]":
        return [n for n in self._notes if n.task_id == agent_id]

    def launcher_facts(self, key: str) -> "returns.ReturnFacts":
        return self.main if key == "main" else self.facts_of(key)

    def digest_for(self, agent_id: str) -> str:
        return returns.main_side_digest(agent_id, self.main, self.notes_of(agent_id))

    def decide(self, agent_id: str, meta: dict, meta_bad: bool):
        """(Decision, the launcher's facts or None)."""
        key, reason = returns.launcher_of(meta, self.siblings, agent_id)
        issuers = [self.main, *self.loaded.values()]
        launcher = None
        if key is not None and not meta_bad:
            launcher = self.launcher_facts(key)
            if not any(launcher is f for f in issuers):
                issuers.append(launcher)

        def early(reason_word: str):
            return returns.Decision(
                returns.UNKNOWN, reason_word,
                returns.resume_count(agent_id, issuers, self.siblings),
            ), launcher

        if meta_bad:
            return early("malformed")
        if key is None:
            return early(reason)
        # GA-1: a transcript that may hold a resume request and cannot be read at all hides
        # which child it resumed, so every child of the session is unknown (a malformed or
        # half-written line inside a readable transcript is only a recorded limit, 4.13.1)
        if self.main_unreadable or any(f.unreadable for f in issuers):
            return early("parent_unreadable")
        return returns.decide(
            agent_id, launcher, self.main, self.notes_of(agent_id), issuers, self.siblings
        ), launcher


def _assign_return_states(
    main_path: Path, child_facts: "dict[Path, returns.ReturnFacts]", children: list,
) -> None:
    """Set return_state, return_reason, resumes, returned_to_parent and return_evidence on
    every session subagent attempt (docs/DESIGN.md 4.1, 4.13.1)."""
    context = _SessionReturns(main_path, child_facts)
    for attempt, agent_id, meta, meta_bad in children:
        decision, launcher = context.decide(agent_id, meta, meta_bad)
        attempt.return_state = decision.state
        attempt.return_reason = decision.reason
        attempt.resumes = decision.resumes
        attempt.returned_to_parent = decision.state == returns.RETURNED
        if decision.evidence is not None:
            source, line, in_launcher = decision.evidence
            where = launcher.path if in_launcher and launcher is not None else Path(main_path)
            attempt.return_evidence = (source, where, line)


def session_child_return(main_path: Path, agent_path: Path, cache: dict | None = None):
    """(Decision, main-side digest, launcher transcript path or None, unreadable tokens) of one
    session subagent, from the main transcript and, when it is nested, its launcher read as
    evidence only (docs/DESIGN.md 7.2's finished rule and fingerprint). The child's own
    transcript is not read, only its meta file. `cache`, a dict the caller owns for one hook
    invocation, keeps the main transcript's context so its notifications are listed once."""
    agent_path = Path(agent_path)
    agent_id = agent_path.stem.removeprefix("agent-")
    meta_path = agent_path.with_name(f"{agent_path.stem}.meta.json")
    diagnostics: list[str] = []
    meta = _load_meta_file(meta_path, diagnostics)
    key = ("returns", Path(main_path))
    context = cache.get(key) if cache is not None else None
    if context is None:
        context = _SessionReturns(Path(main_path), {})
        if cache is not None:
            cache[key] = context
    decision, launcher = context.decide(agent_id, meta, bool(diagnostics))
    path = launcher.path if launcher is not None and launcher is not context.main else None
    # GA-3: which inputs could not be read, as stable tokens for the hook's fingerprint
    unreadable = tuple(
        token for token, bad in (
            ("main", context.main_unreadable),
            ("meta", bool(diagnostics)),
            ("launcher", path is not None and launcher.unreadable),
        ) if bad
    )
    return decision, context.digest_for(agent_id), path, unreadable


def load_session(path: Path) -> Run:
    """Parse a session run: the main transcript plus its Agent-tool
    subagent transcripts (docs/DESIGN.md 4.2). Run.main is the main
    transcript as an Attempt (label='main', key=None, ordinal=1); each
    subagents/agent-*.jsonl becomes one Attempt and one LogicalAgent.
    Retries of an Agent-tool subagent are not observable in v0.1: each
    transcript file is exactly one attempt, ordinal 1."""
    path = Path(path)
    return _load_session_from_paths(path, _discover_session_agent_paths(path), reconcile=True)


def load_session_view(
    main_path: Path, subagent_paths: list[Path], *, main_cache: dict | None = None
) -> Run:
    """Parse main_path plus only the named subagent_paths -- never the rest
    of the session's subagents/ directory (docs/DESIGN.md 7.2's D6):
    analyzing one finished subagent unit in isolation must not be
    contaminated by an unrelated sibling's parse failure, and must not cost
    O(every subagent of the session) to do it. An empty subagent_paths list
    is a main-thread-only view (docs/DESIGN.md 7.2's D5: session-end).

    Attempts are matched back to a unit by `attempt.transcript_path`, never
    by re-deriving an agent id from the filename: _valid_agent_id may
    replace a non-conforming id, so a caller must never assume the id it
    passed in survives unchanged onto the Attempt it gets back."""
    return _load_session_from_paths(
        Path(main_path), [Path(p) for p in subagent_paths], main_cache=main_cache
    )


# --- outcome and interrupt attribution --------------------------------


def _last_user_event_index(events: list[Event]) -> int | None:
    last = None
    for index, event in enumerate(events):
        if event.kind == "user":
            last = index
    return last


def _compute_outcome(transcript_path: Path | None, events: list[Event]) -> str:
    if transcript_path is None:
        return "unknown"
    index = _last_user_event_index(events)
    if index is not None and events[index].text.startswith(KILL_SENTINEL_PREFIX):
        return "killed"
    return "completed"


def _build_interrupt(attempt: Attempt) -> Interrupt | None:
    """Interrupt for an attempt whose last user event is the kill sentinel
    (docs/DESIGN.md 4.4). preceding comes from
    detectors.base.meaningful_event_before (4.5); silence_seconds is None
    when a timestamp is missing, the delta is negative, or preceding is None
    (including because a compaction event stopped the walk -- a context
    reset makes "silence" meaningless). cause stays 'unattributed' and
    cause_evidence None here; _attribute_interrupts fills them in once every
    attempt of the run has been loaded (docs/DESIGN.md 4.4 needs to know
    whether a key has a later attempt)."""
    events = attempt.events
    kill_index = _last_user_event_index(events)
    if kill_index is None:
        return None
    kill_event = events[kill_index]
    if not kill_event.text.startswith(KILL_SENTINEL_PREFIX):
        return None

    preceding = meaningful_event_before(attempt, kill_index)

    silence_seconds: float | None = None
    if preceding is not None and preceding.ts is not None and kill_event.ts is not None:
        delta = (kill_event.ts - preceding.ts).total_seconds()
        if delta >= 0:
            silence_seconds = delta

    return Interrupt(
        kill_ts=kill_event.ts,
        preceding=preceding,
        silence_seconds=silence_seconds,
        cause="unattributed",
        cause_evidence=None,
    )


# docs/DESIGN.md 4.1's v0.2 reader changes: the tool names ToolCall.path is
# ever set for; used by _build_attempt_facts as a defensive filter (path is
# already None for every other tool -- see _make_tool_call -- so this only
# guards against a future tool being added to _PATH_TOOL_INPUT_FIELD without
# also being a "wrote a file" tool).
_PATH_WRITING_TOOL_NAMES = frozenset(_PATH_TOOL_INPUT_FIELD)

# outcomes for which a not-yet-resolved tool call is genuinely "in flight"
# (docs/DESIGN.md 4.1's v0.2 reader changes): a 'completed' attempt reports
# [] unconditionally, since the journal's own verdict wins and a stray
# no_result call in its transcript proves nothing was actually abandoned.
_IN_FLIGHT_OUTCOMES = frozenset({"killed", "failed", "superseded"})


def _build_attempt_facts(
    tool_calls: list[ToolCall],
    outcome: str,
    last_ts: datetime | None,
    transcript_path: Path | None,
    unreadable: bool,
) -> AttemptFacts | None:
    """AttemptFacts derived from one attempt's tool_calls, computed once the
    transcript is fully read (docs/DESIGN.md 4.1/4.2's v0.2 reader changes).

    None only when transcript_path is None -- no transcript exists at all.
    A transcript that exists but could not even be opened (`unreadable`,
    docs/DESIGN.md 4.6) still produces an AttemptFacts, with every field set
    to FACTS_UNKNOWN rather than a misleadingly empty list or zero count."""
    if transcript_path is None:
        return None
    if unreadable:
        return AttemptFacts(
            paths_written=FACTS_UNKNOWN,
            in_flight=FACTS_UNKNOWN,
            commits=FACTS_UNKNOWN,
            cwds=FACTS_UNKNOWN,
            background_unfinished=FACTS_UNKNOWN,
            last_tool=FACTS_UNKNOWN,
            seconds_since_last_tool_at_end=FACTS_UNKNOWN,
            commit_commands_without_id=FACTS_UNKNOWN,
        )

    paths_written: list[str] = []
    seen_paths: set[str] = set()
    in_flight: list[dict] = []
    commits: list[str] = []
    seen_commits: set[str] = set()
    cwds: list[str] = []
    seen_cwds: set[str] = set()
    background_unfinished = 0
    commit_commands_without_id = 0

    for call in tool_calls:
        completion = call.completion

        if (
            call.tool_name in _PATH_WRITING_TOOL_NAMES
            and call.path is not None
            and completion == "completed_ok"
            and call.path not in seen_paths
        ):
            seen_paths.add(call.path)
            paths_written.append(call.path)

        if completion == "no_result" and outcome in _IN_FLIGHT_OUTCOMES:
            in_flight.append(
                {"tool": call.tool_name, "path": call.path, "line_no": call.line_no}
            )

        for commit_id in call.commit_ids:
            if commit_id not in seen_commits:
                seen_commits.add(commit_id)
                commits.append(commit_id)

        # FA6 (C1): a CALL (never a segment) that completed_ok, contains a commit command,
        # and yielded no id (a quiet commit prints no summary line). Whether the shell
        # reached the command is not provable from one call's exit status.
        if (
            call.tool_name == "Bash"
            and completion == "completed_ok"
            and call.classification is not None
            and call.classification.commit_command_present
            and not call.commit_ids
        ):
            commit_commands_without_id += 1

        if call.cwd is not None and call.cwd not in seen_cwds:
            seen_cwds.add(call.cwd)
            cwds.append(call.cwd)

        if completion == "backgrounded":
            background_unfinished += 1

    last_tool = tool_calls[-1].tool_name if tool_calls else None

    seconds_since_last_tool_at_end: float | None = None
    if tool_calls and last_ts is not None:
        last_call_ts = tool_calls[-1].ts
        if last_call_ts is not None:
            delta = (last_ts - last_call_ts).total_seconds()
            if delta >= 0:
                seconds_since_last_tool_at_end = delta

    return AttemptFacts(
        paths_written=paths_written,
        in_flight=in_flight,
        commits=commits,
        cwds=cwds,
        background_unfinished=background_unfinished,
        last_tool=last_tool,
        seconds_since_last_tool_at_end=seconds_since_last_tool_at_end,
        commit_commands_without_id=commit_commands_without_id,
    )


def _sum_usage(events: list[Event]) -> dict:
    totals: dict[str, int | float] = {}
    for event in events:
        for key, value in event.usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals


def _note(text: str, quality: bool) -> str:
    """text tagged as a data-quality note (docs/DESIGN.md 4.13.1, A2) when
    `quality`, else unchanged."""
    return QualityNote(text) if quality else text


def _parse_ts(
    value: object,
    path: Path | None = None,
    line_no: int | None = None,
    diagnostics: list[str] | None = None,
    *,
    quality: bool = False,
) -> datetime | None:
    """Parse an ISO 8601 timestamp, or None for an absent one.

    A present but unparseable value is a malformed timestamp, not a merely
    absent one: it is recorded as a diagnostic (path:line) so a reader never
    silently loses temporal evidence without a trace, even though the caller
    still gets None back and must treat duration as unknown.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        if diagnostics is not None and path is not None and line_no is not None:
            diagnostics.append(
                _note(f"{path.name}:{line_no}: timestamp is not a string", quality)
            )
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        if diagnostics is not None and path is not None and line_no is not None:
            diagnostics.append(
                _note(f"{path.name}:{line_no}: timestamp could not be parsed", quality)
            )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _strip_harness_noise(text: str) -> str:
    """Remove <system-reminder>, <total_tokens>, and [harness: ...] spans
    (docs/DESIGN.md 4.5) from a field's full text before it becomes an
    Event's excerpt."""
    text = _SYSTEM_REMINDER_RE.sub("", text)
    text = _TOTAL_TOKENS_RE.sub("", text)
    text = _HARNESS_NOTE_RE.sub("", text)
    return text


def _excerpt(text: str | None) -> str:
    if not text:
        return ""
    return _strip_harness_noise(text)[:_EXCERPT_LIMIT]


def _parse_exit_code(text: str) -> int | None:
    match = _EXIT_CODE_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _flatten_block_text(content: object) -> str:
    """Flatten a tool_result 'content' field (string or list of blocks) to
    text. docs/DESIGN.md section 3: a content list item can itself be a
    plain string, not only a {"text": ...} dict block, so both forms are
    collected here."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


# --- marker provenance (docs/DESIGN.md 4.13.2, v0.3 N1) --------------------


def _flatten_for_markers(content: object) -> str:
    """docs/DESIGN.md 4.13.2's text flattening, used only for MarkerHit
    evaluation -- never Event.text, which keeps using _flatten_block_text's
    older "join every text/string element" rule so existing display/export
    behavior is untouched. A string content as-is; a list's first element
    whose type is 'text' (a bare string list item does not count here,
    unlike _flatten_block_text); anything else, ''. Leading whitespace
    (spaces, tabs, carriage returns, newlines) is then stripped; no other
    normalization."""
    if isinstance(content, str):
        flat = content
    elif isinstance(content, list):
        flat = ""
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                flat = block["text"]
                break
    else:
        flat = ""
    return flat.lstrip(" \t\r\n")


def _tool_result_shapes(
    tool_name: str | None, is_codex_bash: bool, is_error: bool = False,
) -> set[str]:
    """Which marker-registry shape tags a tool_result block paired to
    tool_name (and, for a Bash call, whether its own command runs codex as a
    program; whether the block has is_error true) is a candidate block for
    (docs/DESIGN.md 4.13.2)."""
    shapes: set[str] = set()
    if is_error:
        shapes.add(markers.SHAPE_TOOL_RESULT_ERROR)
    if tool_name == "Bash":
        shapes.add(markers.SHAPE_TOOL_RESULT_BASH)
        if is_codex_bash:
            shapes.add(markers.SHAPE_TOOL_RESULT_BASH_CODEX)
    if tool_name == "StructuredOutput":
        shapes.add(markers.SHAPE_TOOL_RESULT_STRUCTURED_OUTPUT)
    else:
        shapes.add(markers.SHAPE_TOOL_RESULT_ANY_EXCEPT_STRUCTURED_OUTPUT)
    return shapes


def _marker_hits_for_text(
    text: str,
    applicable_shapes: set[str],
    *,
    event_line: int,
    tool_use_id: str | None,
    result_line: int | None,
) -> list[MarkerHit]:
    """Every MarkerHit the registry yields for one flattened block of text
    (docs/DESIGN.md 4.13.2's candidate rule): for each canonical id whose
    permitted shape intersects applicable_shapes, a candidate (one of its
    fragment strings occurs anywhere in text) yields exactly one MarkerHit,
    anchored when its predicate holds. An id whose shape never matches this
    block yields nothing at all, not even a negative hit."""
    hits: list[MarkerHit] = []
    for spec in markers.REGISTRY.values():
        matched_shape = next((shape for shape in spec.shapes if shape in applicable_shapes), None)
        if matched_shape is None:
            continue
        if not spec.candidate(text):
            continue
        anchored = spec.predicate(text, matched_shape)
        hits.append(
            MarkerHit(
                marker=spec.id,
                producer=spec.producer,
                event_line=event_line,
                tool_use_id=tool_use_id,
                result_line=result_line,
                anchored=anchored,
                in_payload=not anchored,
            )
        )
    return hits


def _user_entry_marker_hits(
    entry: dict, line_no: int, tool_names: dict, tool_commands: dict,
) -> list[MarkerHit]:
    """Every MarkerHit from one user entry (docs/DESIGN.md 4.13.2): the
    whole entry's own flattened text (kill_sentinel's permitted shape --
    "a user entry, not a tool_result block"), plus each tool_result block it
    carries, evaluated separately by its own tool_use_id."""
    hits: list[MarkerHit] = []
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")

    whole_text = _flatten_for_markers(content)
    hits.extend(
        _marker_hits_for_text(
            whole_text, {markers.SHAPE_USER_ENTRY},
            event_line=line_no, tool_use_id=None, result_line=None,
        )
    )

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw_tool_use_id = block.get("tool_use_id")
            tool_use_id = raw_tool_use_id if isinstance(raw_tool_use_id, str) else None
            tool_name = tool_names.get(tool_use_id) if tool_use_id is not None else None
            command = tool_commands.get(tool_use_id) if tool_use_id is not None else None
            is_codex_bash = tool_name == "Bash" and markers.runs_codex_program(command)
            block_text = _flatten_for_markers(block.get("content"))
            shapes = _tool_result_shapes(tool_name, is_codex_bash, block.get("is_error") is True)
            hits.extend(
                _marker_hits_for_text(
                    block_text, shapes,
                    event_line=line_no, tool_use_id=tool_use_id, result_line=line_no,
                )
            )
    return hits


def _assistant_entry_flatten(entry: dict) -> str:
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    return _flatten_for_markers(message.get("content"))


def _finalize_assistant_marker_hits(
    assistant_entries: list[tuple[int, str]],
    final_entry_type: str | None,
    final_entry_line_no: int | None,
    attempt_has_codex_bash: bool,
) -> list[MarkerHit]:
    """The two whole-assistant-entry marker shapes that need attempt-wide
    context the streaming pass cannot have yet (docs/DESIGN.md 4.13.2):
    sandbox_write_denied's "assistant text block of an attempt that has
    such a call" shape (every assistant entry, once attempt_has_codex_bash
    is known), and usage_limit_session/weekly, machine_sleep, api_error's
    "the attempt's own final transcript entry, an assistant entry" shape
    (only the one entry that really is last, once the whole transcript has
    been read)."""
    hits: list[MarkerHit] = []
    for line_no, text in assistant_entries:
        shapes: set[str] = set()
        if attempt_has_codex_bash:
            shapes.add(markers.SHAPE_ASSISTANT_CODEX_ATTEMPT)
        if final_entry_type == "assistant" and line_no == final_entry_line_no:
            shapes.add(markers.SHAPE_FINAL_ASSISTANT_ENTRY)
        if not shapes:
            continue
        hits.extend(
            _marker_hits_for_text(
                text, shapes, event_line=line_no, tool_use_id=None, result_line=None,
            )
        )
    return hits


def _make_tool_call(
    tool_id: str,
    tool_name: str | None,
    input_obj: dict,
    line_no: int,
    ts: datetime | None,
    cwd_raw: object,
) -> ToolCall:
    """One ToolCall, filled from the tool_use block's own FULL parsed input
    (never Event's 400-char excerpt) -- docs/DESIGN.md 4.1/4.2's v0.2 reader
    changes, plus the N4b-1 private fields (run_in_background, a Monitor
    command, MultiEdit's path, and the 4.13.1 classification computed from
    the FULL command)."""
    path = None
    path_state = None  # why a file tool's target is None (docs/DESIGN.md 4.13.1)
    field_name = _PATH_TOOL_INPUT_FIELD.get(tool_name or "")
    if field_name is not None:
        raw_path = input_obj.get(field_name)
        if isinstance(raw_path, str) and len(raw_path) <= _TOOL_CALL_PATH_LIMIT:
            path = raw_path
        else:
            path_state = "truncated" if isinstance(raw_path, str) else "missing"

    multiedit_path = None
    if tool_name == _MULTIEDIT_TOOL:
        raw_path = input_obj.get("file_path")
        if isinstance(raw_path, str) and len(raw_path) <= _TOOL_CALL_PATH_LIMIT:
            multiedit_path = raw_path
        else:
            path_state = "truncated" if isinstance(raw_path, str) else "missing"

    command = None
    command_sha256 = None
    run_in_background = None
    raw_command = None
    if tool_name in _COMMAND_TOOLS:
        raw_command = input_obj.get("command")
        run_in_background = input_obj.get("run_in_background")
        if isinstance(raw_command, str):
            if len(raw_command) <= _TOOL_CALL_COMMAND_LIMIT:
                command = raw_command
            else:
                command_sha256 = hashlib.sha256(raw_command.encode("utf-8")).hexdigest()

    cwd = os.path.normpath(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else None

    return ToolCall(
        tool_use_id=tool_id,
        tool_name=tool_name,
        line_no=line_no,
        ts=ts,
        path=path,
        command=command,
        command_sha256=command_sha256,
        cwd=cwd,
        run_in_background=run_in_background,
        multiedit_path=multiedit_path,
        classification=commands.classify_call(
            tool_name, raw_command, run_in_background=run_in_background, cwd=cwd
        ),
        _path_state=path_state,
    )


def _apply_background_hits(
    hits: list[MarkerHit], tool_call_index: dict[str, ToolCall], tool_commands: dict
) -> None:
    """An anchored bash_backgrounded hit on a Bash result makes that call
    row 2 of the truth table (docs/DESIGN.md 4.13.1): the classification is
    recomputed from the FULL command with the hit. A payload-only hit never
    does."""
    for hit in hits:
        if hit.marker != "bash_backgrounded" or not hit.anchored or hit.tool_use_id is None:
            continue
        call = tool_call_index.get(hit.tool_use_id)
        if call is None or call.tool_name != "Bash":
            continue
        call.classification = commands.classify_call(
            "Bash", tool_commands.get(hit.tool_use_id),
            run_in_background=call.run_in_background, backgrounded_hit=True, cwd=call.cwd,
        )


# --- the session launch log (docs/DESIGN.md 4.13.1, C-14, N4b-1) ----------------

# The child-launching session tools; SendMessage continues an existing child
# and launches none.
_LAUNCH_TOOLS = ("Agent", "Task", "Workflow")
_ABSENT = object()


def _result_digest(block: dict, tool_use_result: object) -> str | None:
    """docs/DESIGN.md 4.13.1 and 4.13.3 (N4 fix, A4): identifies one launch
    result for the duplicate rule. Two results with the same digest (the
    `tool_result` block and the entry-level `toolUseResult` identical as
    parsed JSON) are one result. None when the pair cannot be serialized, so
    such a result never equals another."""
    try:
        text = json.dumps([block, tool_use_result], sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _is_rejected_launch(block: dict, tool_use_result: object) -> bool:
    """docs/DESIGN.md 4.13.1 (N4 fix, A4 and A5): a launch result whose
    `tool_result` block has `is_error` true and whose entry-level
    `toolUseResult` is a string. This is the rejected shape only; it is not
    proof that no run was created (Codex round 2, R2-3): a rejected resume
    launch stays involved with the run it names, and an initial rejected
    launch stays `undiscovered` in the session launch table."""
    return block.get("is_error") is True and isinstance(tool_use_result, str)


def _distinct_results(results: list, digest_of) -> list:
    """The results of one launch with byte-identical repeats (same digest, a
    digest of None never repeats) collapsed into the first of each."""
    seen: set[str] = set()
    out = []
    for result in results:
        digest = digest_of(result)
        if digest is not None:
            if digest in seen:
                continue
            seen.add(digest)
        out.append(result)
    return out


def _launch_status_kind(tool_use_result: object) -> str:
    """The status class of an Agent launch result (docs/DESIGN.md 4.1, 4.13.1; N5 fix 1, S2):
    'async' for `status` `async_launched` or `isAsync` true, 'plain' for a result with no
    `status` key, 'completed' for the string `completed`, else 'other' (any other string, or a
    value that is not a string)."""
    if not isinstance(tool_use_result, dict):
        return "plain"
    status = tool_use_result.get("status", _ABSENT)
    if tool_use_result.get("isAsync") is True or status == "async_launched":
        return "async"
    if status is _ABSENT:
        return "plain"
    return "completed" if status == "completed" else "other"


def _carried_ids(result: tuple) -> list[str]:
    """The string ids one recorded launch result carries, in either location."""
    return [value for value in (result[0], result[1]) if isinstance(value, str)]


class _LaunchLog:
    """What a session main transcript recorded while it streamed, for the
    launch reconciliation below: every launch (an assistant `tool_use` block
    named Agent, Task, or Workflow, with its raw id and physical line), how
    many assistant `tool_use` blocks carry each string id (4.2's duplicate
    rule), and, per tool_use_id, each `tool_result` block's own `agentId` and
    its entry's `toolUseResult.agentId` (_ABSENT when the key is missing)."""

    def __init__(self) -> None:
        # (tool, raw id, physical line, the string resumeFromRunId of a Workflow's input)
        self.launches: list[tuple[str, object, int, str | None]] = []
        self.use_counts: dict[str, int] = {}
        # per tool_use_id: (block agentId, entry agentId, rejected, digest, is_error, kind)
        # where kind is the launch result's status class: 'plain' (no status),
        # 'completed', 'async' (async_launched or isAsync true), or 'other'
        self.results: dict[str, list[tuple[object, object, bool, str | None, bool, str]]] = {}
        # docs/DESIGN.md 4.13.1 (G-A): the facts the return decision reads (returns.ReturnFacts)
        self.facts = returns.ReturnFacts()

    def clear(self) -> None:
        self.launches.clear()
        self.use_counts.clear()
        self.results.clear()
        self.facts = returns.ReturnFacts()

    def record_use(
        self, tool_name: str | None, tool_id: object, line_no: int,
        resume_from: str | None = None, ts: datetime | None = None, tool_input: object = None,
    ) -> None:
        self.facts.note_use(tool_name, tool_id, line_no, ts, tool_input)
        if isinstance(tool_id, str):
            self.use_counts[tool_id] = self.use_counts.get(tool_id, 0) + 1
        if tool_name in _LAUNCH_TOOLS:
            self.launches.append((tool_name, tool_id, line_no, resume_from))

    def record_result(
        self, tool_use_id: str, block: dict, tool_use_result: object,
        tur_present: bool = True, line_no: int = 0,
    ) -> None:
        self.facts.note_result(tool_use_id, block, tool_use_result, tur_present, line_no)
        block_agent = block["agentId"] if "agentId" in block else _ABSENT
        entry_agent = (
            tool_use_result["agentId"]
            if isinstance(tool_use_result, dict) and "agentId" in tool_use_result
            else _ABSENT
        )
        # docs/DESIGN.md 4.13.3 (A5): matching is by id only, so the digest of
        # every result is computed, whether or not its launch came first.
        digest = _result_digest(block, tool_use_result)
        self.results.setdefault(tool_use_id, []).append((
            block_agent, entry_agent, _is_rejected_launch(block, tool_use_result), digest,
            block.get("is_error") is True, _launch_status_kind(tool_use_result),
        ))

    def child_returned(self, child_id: str, notifications=None) -> bool:
        """docs/DESIGN.md 4.1, 4.13.1 (S11, N5c-1): whether the child with this
        id returned a result to its parent. True only when exactly one Agent or
        Task launch block names the child through a result, no other `tool_use`
        block shares that launch's id, the launch has exactly one result (results
        identical as parsed JSON count once), and that result is not an error,
        not the rejected shape, carries only string ids, and its ids equal the
        child when both locations are present. Anything else is False: a missing,
        error, rejected, malformed, duplicate, or conflicting result, a
        duplicated launch id, and a child with no matching launch. A result whose
        status is `async_launched` (or whose `isAsync` is true) is not a return by
        itself (N5 fix 1, S2): `notifications`, a callable giving the transcript's
        accepted task notifications (4.13.3), must hold one for this child and this
        launch after the launch's own `tool_use` line (FA3: not after its result line,
        which the lifecycle uses), and every one of that pair must say `completed`. Any other status
        string is not a return."""
        naming = []
        launch_lines: dict[str, int] = {}
        for tool, raw_id, line, _resume in self.launches:
            if tool not in ("Agent", "Task") or not isinstance(raw_id, str):
                continue
            if any(child_id in _carried_ids(result) for result in self.results.get(raw_id, [])):
                naming.append(raw_id)
                launch_lines[raw_id] = line
        if len(naming) != 1:
            return False
        launch_id = naming[0]
        if self.use_counts.get(launch_id, 0) != 1:
            return False
        results = _distinct_results(self.results[launch_id], lambda result: result[3])
        if len(results) != 1:
            return False
        block_agent, entry_agent, rejected, _digest, is_error, kind = results[0]
        if rejected or is_error:
            return False
        present = [value for value in (block_agent, entry_agent) if value is not _ABSENT]
        if not (present and all(value == child_id for value in present)):
            return False
        if kind in ("plain", "completed"):
            return True
        if kind != "async" or notifications is None:
            return False
        # FA3: the return-specific boundary is the launch's own tool_use line (a
        # fast child can notify between the call and its result line); a
        # notification before it cannot be about this launch.
        launch_line = launch_lines[launch_id]
        pair = [
            notification for notification in notifications()
            if notification.task_id == child_id and notification.tool_use_id == launch_id
            and notification.line_no > launch_line
        ]
        return bool(pair) and all(
            _TERMINAL_STATUS_VOCAB.get(notification.status) == "completed"
            for notification in pair
        )


def _discovered_run_dir(workflows_dir: Path, run_id: str) -> bool:
    """A canonical workflow run id that names a directory of the session's
    `workflows` directory."""
    return bool(_WORKFLOW_RUN_ID_RE.fullmatch(run_id)) and _is_dir(workflows_dir / run_id)


def _launch_code(
    tool: str,
    raw_id: object,
    line_no: int,
    resume_from: str | None,
    log: _LaunchLog,
    child_ids: frozenset[str],
    generations: list[Generation],
    workflows_dir: Path,
) -> str | None:
    """The first applicable code of the decision table for one launch (4.13.1),
    or None when it reconciles."""
    if not isinstance(raw_id, str):
        return "malformed_launch"
    if log.use_counts.get(raw_id, 0) > 1:
        return "ambiguous_tool_id"
    results = log.results.get(raw_id, [])
    if not results:
        return "no_result"
    results = _distinct_results(results, lambda result: result[3])
    if len(results) > 1:
        return "ambiguous_result"
    if tool == "Workflow":
        if results[0][2]:
            # A5: an error result is not proof that no run exists.
            if resume_from is not None:
                return None if _discovered_run_dir(workflows_dir, resume_from) else "undiscovered"
            return "undiscovered"
        for generation in generations:
            if (
                generation.tool_use_id == raw_id
                and generation.launch_line == line_no
                and _WORKFLOW_RUN_ID_RE.fullmatch(generation.run_id or "")
                and _is_dir(workflows_dir / generation.run_id)
            ):
                return None
        return "undiscovered"
    block_agent, entry_agent, _rejected, _digest, _is_error, _kind = results[0]
    present = [value for value in (block_agent, entry_agent) if value is not _ABSENT]
    if any(not isinstance(value, str) for value in present):
        return "non_string_id"
    if len(present) == 2 and present[0] != present[1]:
        return "conflicting_ids"
    if not present:
        return "no_id"
    return None if present[0] in child_ids else "undiscovered"


def _reconcile_session_launches(
    main_path: Path,
    log: _LaunchLog,
    agent_paths: list[Path],
    generations: list[Generation],
) -> list[str]:
    """One diagnostic per Agent, Task, or Workflow launch of the main
    transcript that does not reconcile with a discovered child, in tool-use
    order (docs/DESIGN.md 4.13.1, C-14)."""
    child_ids = frozenset(Path(p).stem.removeprefix("agent-") for p in agent_paths)
    workflows_dir = _session_dir(main_path) / "subagents" / "workflows"
    out: list[str] = []
    for tool, raw_id, line_no, resume_from in log.launches:
        code = _launch_code(
            tool, raw_id, line_no, resume_from, log, child_ids, generations, workflows_dir
        )
        if code is not None:
            out.append(
                f"{main_path.name}:{line_no}: {tool} launch has no discovered child ({code})"
            )
    return out


def _note_entry_shape(entry: dict, raw_type: object, state: "ledger.TranscriptState") -> None:
    """The entry-level shape rules of docs/DESIGN.md 4.13.1 (unexpected shape and
    unknown entry type) for the ledger's reasons."""
    if not isinstance(raw_type, str):
        state.unexpected_shape = True
        return
    if raw_type == "user" or raw_type == "assistant":
        message = entry.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), (str, list)):
            state.unexpected_shape = True
    elif not ledger.entry_type_known(raw_type):
        state.unknown_type = True


def _fork_scan(path: Path) -> tuple[int, bool]:
    """(boundary, settled) for a session main transcript (docs/DESIGN.md 4.2, FA4).

    boundary is the physical line number of the last line of the inherited prefix: the
    last entry whose `sessionId` is a string differing from the file's own stem, before
    the first entry carrying the stem itself (0 when there is no such entry, or no own
    entry at all). settled is True once an own entry was seen, so the boundary can never
    change as the file grows; False means the file has no own entry yet (nothing is
    inherited for now). An unreadable file inherits nothing. Undecodable, blank, and
    sessionId-less lines never decide the boundary themselves."""
    own = path.stem
    last_foreign = 0
    try:
        with _transcript_reader(path) as lines:
            for line_no, line, _torn in lines:
                # cheap prefilter: an entry with a top-level sessionId spells the key
                # literally, so a line without it is never decoded here
                if '"sessionId"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                session = entry.get("sessionId")
                if not isinstance(session, str):
                    continue
                if session == own:
                    return last_foreign, True
                last_foreign = line_no
    except (OSError, UnicodeError):
        return 0, False
    return 0, False


def _fork_boundary(path: Path) -> int:
    """The number of the last inherited physical line of a session main transcript, 0
    when nothing is inherited (see _fork_scan)."""
    return _fork_scan(path)[0]


def _read_transcript(
    transcript_path: Path, diagnostics: list[str], *, launch_log: "_LaunchLog | None" = None,
    state: "ledger.TranscriptState | None" = None, skip_through_line: int = 0,
) -> tuple[list[Event], str | None, str | None, int, int, list[ToolCall], list[MarkerHit]]:
    """events, version, session_id, (total_lines, bad_lines), tool_calls, and
    marker_hits: total_lines/bad_lines are the non-blank physical line count
    and how many of them failed to decode as JSON at all (docs/DESIGN.md
    4.6's "5 percent of a transcript's lines were undecodable"; a line that
    decodes but is not a JSON object is a different, already-diagnosed
    problem, not an undecodable one). tool_calls is every ToolCall in
    transcript order (docs/DESIGN.md 4.1/4.2's v0.2 reader changes), filled
    from each tool_use block's own full parsed input and updated in place
    when its matching tool_result is seen. marker_hits is every MarkerHit
    built from the marker provenance registry (docs/DESIGN.md 4.13.2, v0.3
    N1), in the order its block was encountered, except the two whole-
    assistant-entry shapes (sandbox_write_denied's attempt-has-a-codex-call
    shape, and the four final-transcript-entry markers), which are resolved
    once the whole transcript -- and therefore tool_calls and the true last
    entry -- are known, and so are appended last.

    skip_through_line (FA4): the physical lines 1..skip_through_line are a fork's
    inherited prefix (a session main transcript only, _fork_boundary) and are skipped
    entirely, before any accumulator, count, or diagnostic is touched; line numbers
    keep counting physically."""
    events: list[Event] = []
    version: str | None = None
    session_id: str | None = None
    tool_names: dict[str, str | None] = {}
    # docs/DESIGN.md 4.3 (D-A): the Bash tool_use command text paired to
    # each tool_use_id, so a later tool_result can be judged read-only
    # without re-reading the tool_use event.
    tool_commands: dict[str, str | None] = {}
    # docs/DESIGN.md 4.1/4.2's v0.2 reader changes: every ToolCall built so
    # far, in transcript order, plus an index by tool_use_id so the matching
    # tool_result (which can arrive in any order relative to a sibling call
    # started in the same assistant message) updates the right one.
    tool_calls: list[ToolCall] = []
    tool_call_index: dict[str, ToolCall] = {}
    # docs/DESIGN.md 4.2's v0.2 reader changes (F5 hardening): every
    # ToolCall built so far for a given tool_use_id, in the order seen --
    # unlike tool_call_index (which only ever points at the latest one),
    # this is how a second tool_use block with the same id is recognized as
    # a duplicate at all.
    tool_calls_by_id: dict[str, list[ToolCall]] = {}
    total_lines = 0
    bad_lines = 0
    # Usage dedup is scoped to one transcript (docs/DESIGN.md 4.2): the
    # reader keeps a set of (message.id, requestId) pairs seen while reading
    # this file only, resetting for the next attempt's transcript.
    dedup: dict[tuple[str, str], dict] = {}
    # docs/DESIGN.md 4.13.2 (v0.3 N1): marker hits decided immediately
    # (every tool_result block, plus kill_sentinel on each user entry's own
    # text), and the raw material for the two whole-assistant-entry shapes
    # that need attempt-wide context, resolved after the loop.
    marker_hits: list[MarkerHit] = []
    assistant_marker_candidates: list[tuple[int, str]] = []
    final_entry_type: str | None = None
    final_entry_line_no: int | None = None

    with _transcript_reader(transcript_path) as lines:
        for line_no, line, torn in lines:
            if line_no <= skip_through_line:
                continue
            if torn:
                # docs/DESIGN.md 4.1 (G-C, GC-1): a final line cut inside a character is a
                # half-written line, like an ASCII one; the diagnostic quotes none of it
                total_lines += 1
                bad_lines += 1
                diagnostics.append(
                    f"{transcript_path.name}:{line_no}: half-written last line (cut inside a "
                    "character)"
                )
                if state is not None:
                    state.undecodable = True
                continue
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                diagnostics.append(f"{transcript_path.name}:{line_no}: {exc}")
                bad_lines += 1
                if state is not None:
                    state.undecodable = True
                continue
            if not isinstance(entry, dict):
                diagnostics.append(f"{transcript_path.name}:{line_no}: not a JSON object")
                if state is not None:
                    state.unexpected_shape = True
                continue

            if state is not None:
                # docs/DESIGN.md 4.13.6 (N6): the run cwd set, from the one read this
                # transcript already gets; a top-level non-empty string only.
                cwd_value = entry.get("cwd")
                if isinstance(cwd_value, str) and cwd_value:
                    state.cwds.setdefault(cwd_value, None)

            if version is None and isinstance(entry.get("version"), str):
                version = entry["version"]
            if session_id is None and isinstance(entry.get("sessionId"), str):
                session_id = entry["sessionId"]

            raw_type = entry.get("type", "")
            ts = _parse_ts(
                entry.get("timestamp"), transcript_path, line_no, diagnostics, quality=True
            )
            final_entry_type = raw_type
            final_entry_line_no = line_no
            if state is not None:
                _note_entry_shape(entry, entry.get("type"), state)

            if raw_type == "user":
                events.extend(
                    _user_events(
                        entry, transcript_path, line_no, ts, raw_type,
                        tool_names, tool_commands, diagnostics,
                        tool_call_index=tool_call_index, launch_log=launch_log, state=state,
                    )
                )
                entry_hits = _user_entry_marker_hits(entry, line_no, tool_names, tool_commands)
                marker_hits.extend(entry_hits)
                _apply_background_hits(entry_hits, tool_call_index, tool_commands)
            elif raw_type == "assistant":
                events.extend(
                    _assistant_events(
                        entry, transcript_path, line_no, ts, raw_type,
                        tool_names, tool_commands, dedup, diagnostics,
                        tool_calls=tool_calls, tool_call_index=tool_call_index,
                        tool_calls_by_id=tool_calls_by_id, launch_log=launch_log, state=state,
                    )
                )
                assistant_marker_candidates.append((line_no, _assistant_entry_flatten(entry)))
            elif raw_type == "attachment":
                events.append(
                    _attachment_event(entry, transcript_path, line_no, ts, raw_type, diagnostics)
                )
            elif raw_type == "system" and entry.get("subtype") == "compact_boundary":
                events.append(_compaction_event(transcript_path, line_no, ts, raw_type))
            else:
                events.append(_unknown_event(entry, transcript_path, line_no, ts, raw_type))

    attempt_has_codex_bash = any(
        call.tool_name == "Bash" and markers.runs_codex_program(call.command)
        for call in tool_calls
    )
    marker_hits.extend(
        _finalize_assistant_marker_hits(
            assistant_marker_candidates, final_entry_type, final_entry_line_no,
            attempt_has_codex_bash,
        )
    )

    return events, version, session_id, total_lines, bad_lines, tool_calls, marker_hits


# docs/DESIGN.md 4.6: a transcript whose undecodable-line rate exceeds this
# is not merely lossy in a few spots, it is unreliable enough that the run's
# analysis status must say so rather than silently reporting COMPLETE over
# whatever survived.
_UNDECODABLE_LINE_THRESHOLD = 0.05


def _read_transcript_tracked(
    transcript_path: Path, diagnostics: list[str], *, launch_log: "_LaunchLog | None" = None,
    state: "ledger.TranscriptState | None" = None, skip_through_line: int = 0,
) -> tuple[list[Event], str | None, str | None, bool, bool, list[ToolCall], list[MarkerHit]]:
    """_read_transcript, plus the two docs/DESIGN.md 4.6 conditions it alone
    can observe: `unreadable` when the file could not even be opened (a
    permission or IO error, never mere absence -- a caller only reaches this
    for a transcript path it already confirmed exists), and
    `over_threshold` when more than _UNDECODABLE_LINE_THRESHOLD of its lines
    failed to decode. tool_calls and marker_hits are [] when unreadable
    (docs/DESIGN.md 4.1's v0.2 reader changes: a caller that gets
    unreadable=True builds an AttemptFacts with every field set to
    FACTS_UNKNOWN instead of trusting an empty tool_calls list; marker_hits
    has no such sibling and is simply [])."""
    try:
        handle = transcript_path.open("r", encoding="utf-8")
        handle.close()
    except OSError as exc:
        diagnostics.append(f"{transcript_path.name}: unreadable: {exc}")
        if state is not None:
            state.unreadable = True
        return [], None, None, True, False, [], []

    diagnostics_before = len(diagnostics)
    try:
        events, version, session_id, total_lines, bad_lines, tool_calls, marker_hits = (
            _read_transcript(
                transcript_path, diagnostics, launch_log=launch_log, state=state,
                skip_through_line=skip_through_line,
            )
        )
    except (OSError, UnicodeError) as exc:
        # docs/DESIGN.md 4.1 (N4 fix, C2): a failure anywhere in the read (bytes
        # that are not UTF-8, or an I/O error, after any number of good lines)
        # is the same unreadable shape as an open failure: whatever the read
        # accumulated before it failed is discarded, so the ledger is exactly
        # Ledger([], 'partial', ['transcript_unreadable', ...]).
        del diagnostics[diagnostics_before:]
        diagnostics.append(f"{transcript_path.name}: unreadable: {exc}")
        if state is not None:
            state.undecodable = False
            state.unexpected_shape = False
            state.unknown_type = False
            state.unreadable = True
            state.cwds.clear()
        if launch_log is not None:
            launch_log.clear()
        return [], None, None, True, False, [], []
    over_threshold = total_lines > 0 and (bad_lines / total_lines) > _UNDECODABLE_LINE_THRESHOLD
    return events, version, session_id, False, over_threshold, tool_calls, marker_hits


def _edge_event_has_timestamp_diagnostic(events: list[Event], diagnostics: list[str]) -> bool:
    """Whether the first or last event in events came from a line _parse_ts
    already flagged with a timestamp diagnostic (docs/DESIGN.md 4.6): such
    an event's ts is None not because the field was absent (harmless) but
    because it was present and malformed, which means first_ts/last_ts for
    this attempt may be silently wrong, not just imprecise."""
    if not events:
        return False
    for event in (events[0], events[-1]):
        prefix = f"{event.path.name}:{event.line_no}: timestamp"
        if any(diag.startswith(prefix) for diag in diagnostics):
            return True
    return False


def _as_object(
    value: object, path: Path, line_no: int, diagnostics: list[str] | None, field: str,
    *, quality: bool = False,
) -> dict:
    """value if it is already a dict, else {} with a path:line diagnostic
    (docs/DESIGN.md section 3: the reader tolerates unknown shapes). A
    missing field (None) is the common, harmless case and never diagnosed;
    only a present-but-wrong-shaped field is."""
    if isinstance(value, dict):
        return value
    if value is not None and diagnostics is not None:
        diagnostics.append(_note(
            f"{path.name}:{line_no}: {field} is not an object (got {type(value).__name__})",
            quality,
        ))
    return {}


def _as_text(
    value: object, path: Path, line_no: int, diagnostics: list[str] | None, field: str
) -> str:
    """value if it is already a string, else '' with a path:line
    diagnostic. A missing field ('' from .get(field, "")) is never
    diagnosed; only a present-but-wrong-shaped field is."""
    if isinstance(value, str):
        return value
    if value and diagnostics is not None:
        diagnostics.append(_note(
            f"{path.name}:{line_no}: {field} is not a string (got {type(value).__name__})",
            True,
        ))
    return ""


def _user_events(
    entry, path, line_no, ts, raw_type, tool_names, tool_commands=None, diagnostics=None,
    tool_call_index=None, launch_log=None, state=None,
) -> list[Event]:
    message = _as_object(entry.get("message"), path, line_no, diagnostics, "message")
    content = message.get("content")
    events: list[Event] = []

    if isinstance(content, str):
        events.append(Event(
            path=path, line_no=line_no, ts=ts, kind="user", raw_type=raw_type,
            tool_name=None, text=_excerpt(content), exit_code=None,
            duration_ms=None, is_error=False, usage={}, markers=_scan_markers(content),
        ))
        return events

    if not isinstance(content, list):
        return events

    for block in content:
        if isinstance(block, str):
            # docs/DESIGN.md section 3: "User blocks: tool_result (...) or a
            # plain string." A bare string list item is user text, the same
            # as a {"type":"text","text":...} block, and it is where the
            # kill sentinel itself sometimes arrives (docs/DESIGN.md 4.4).
            events.append(Event(
                path=path, line_no=line_no, ts=ts, kind="user", raw_type=raw_type,
                tool_name=None, text=_excerpt(block), exit_code=None,
                duration_ms=None, is_error=False, usage={}, markers=_scan_markers(block),
            ))
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if state is not None and not isinstance(tool_use_id, str):
                state.unexpected_shape = True
            tool_name = tool_names.get(tool_use_id) if isinstance(tool_use_id, str) else None
            full_text = _flatten_block_text(block.get("content"))
            is_envelope = tool_name in _ENVELOPE_TOOL_NAMES
            is_error = bool(block.get("is_error", False))
            events.append(Event(
                path=path, line_no=line_no, ts=ts, kind="tool_result", raw_type=raw_type,
                tool_name=tool_name, text=_excerpt(full_text),
                exit_code=_parse_exit_code(full_text),
                duration_ms=None, is_error=is_error,
                usage={},
                markers=frozenset() if is_envelope else _scan_markers(full_text),
                envelope=is_envelope,
                read_only_output=_tool_result_read_only(tool_name, tool_use_id, tool_commands),
                tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
            ))
            # docs/DESIGN.md 4.1/4.2's v0.2 reader changes: fill in the
            # matching ToolCall (by tool_use_id, never by position -- two
            # tool_use blocks in one assistant message can have their
            # results arrive in either order) from this tool_result's own
            # FULL text, before _excerpt bounds it.
            if launch_log is not None and isinstance(tool_use_id, str):
                launch_log.record_result(
                    tool_use_id, block, entry.get("toolUseResult"),
                    "toolUseResult" in entry, line_no,
                )
            elif launch_log is not None:
                launch_log.facts.note_bad_result(tool_use_id, block, entry.get("toolUseResult"))
            if tool_call_index is not None and isinstance(tool_use_id, str):
                call = tool_call_index.get(tool_use_id)
                if call is not None:
                    call.result_line = line_no
                    call.is_error = is_error
                    call.backgrounded = all(
                        fragment in full_text for fragment in BASH_BACKGROUNDED
                    )
                    call.exit_code = _parse_exit_code(full_text)
                    # docs/DESIGN.md 4.13.1's commit grammar (C-9), from the
                    # commit segments of the call's classification (computed
                    # from the FULL command text, C-11), for a completed_ok or
                    # completed_error call only (A1).
                    if (
                        call.tool_name == "Bash"
                        and call.classification is not None
                        and call.completion in commands.COMMIT_COMPLETIONS
                    ):
                        call.commit_ids = commands.commit_ids(
                            call.classification.commit, full_text
                        )
        elif block_type == "text":
            block_text = _as_text(block.get("text", ""), path, line_no, diagnostics, "block text")
            events.append(Event(
                path=path, line_no=line_no, ts=ts, kind="user", raw_type=raw_type,
                tool_name=None, text=_excerpt(block_text), exit_code=None,
                duration_ms=None, is_error=False, usage={}, markers=_scan_markers(block_text),
            ))
        else:
            events.append(_unknown_event(block, path, line_no, ts, raw_type))
    return events


def _assistant_events(
    entry, path, line_no, ts, raw_type, tool_names, tool_commands=None, dedup=None,
    diagnostics=None, tool_calls=None, tool_call_index=None, tool_calls_by_id=None,
    launch_log=None, state=None,
) -> list[Event]:
    """Every block in one assistant transcript entry, with usage attached to
    exactly one of them (the first), regardless of how many blocks the
    message carries or whether any of them is a text block. message.usage
    describes the whole entry, not any single block, so summing per-Event
    usage across an attempt must not double it (two text blocks) or lose it
    (a tool-use-only message).

    dedup and diagnostics are optional so existing direct callers (tests
    exercising a single entry in isolation) keep working unchanged; a real
    _read_transcript call always supplies both. When given, a (message.id,
    requestId) pair already in dedup zeroes this entry's usage (docs/
    DESIGN.md 4.2's dedup rule); an entry missing either field is always
    counted (the ccusage convention).

    Usage never silently disappears: it is attached to the first event this
    entry emits, whatever kind that event is (tool_use, text, thinking, or
    even an 'unknown' block this milestone's markers.py does not recognize
    yet). An entry that emits no event at all (empty content, or content
    made only of unrecognized blocks) still gets a synthetic 'unknown' event
    so its usage is not lost."""
    message = _as_object(entry.get("message"), path, line_no, diagnostics, "message")
    content = message.get("content")
    remaining_usage = _as_object(
        message.get("usage"), path, line_no, diagnostics, "usage", quality=True
    )

    message_id = message.get("id")
    request_id = entry.get("requestId")
    pair = (message_id, request_id) if message_id and request_id else None
    if pair is not None and dedup is not None:
        if pair in dedup:
            if diagnostics is not None and dedup[pair] != remaining_usage:
                diagnostics.append(QualityNote(
                    f"{path.name}:{line_no}: duplicate usage for a (message id, requestId) pair "
                    "differs from its first occurrence"
                ))
            remaining_usage = {}
        else:
            dedup[pair] = remaining_usage

    events: list[Event] = []

    def _take_usage() -> dict:
        nonlocal remaining_usage
        usage, remaining_usage = remaining_usage, {}
        return usage

    def _finish() -> list[Event]:
        # message.usage describes the whole entry; if nothing above claimed
        # it (empty content, or content made only of blocks no branch below
        # recognizes), it must not simply vanish (docs/DESIGN.md 4.2).
        if not events and remaining_usage:
            unknown_event = _unknown_event(entry, path, line_no, ts, raw_type)
            unknown_event.usage = _take_usage()
            events.append(unknown_event)
        return events

    if isinstance(content, str):
        events.append(Event(
            path=path, line_no=line_no, ts=ts, kind="assistant", raw_type=raw_type,
            tool_name=None, text=_excerpt(content), exit_code=None,
            duration_ms=None, is_error=False, usage=_take_usage(), markers=_scan_markers(content),
        ))
        return _finish()

    if not isinstance(content, list):
        return _finish()

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_use":
            tool_name = block.get("name")
            if not isinstance(tool_name, str):
                tool_name = None
            tool_id = block.get("id")
            if isinstance(tool_id, str):
                tool_names[tool_id] = tool_name
            if state is not None and (tool_name is None or not isinstance(tool_id, str)):
                state.unexpected_shape = True
            if launch_log is not None:
                launch_input = block.get("input")
                resume = (
                    launch_input.get("resumeFromRunId") if isinstance(launch_input, dict) else None
                )
                launch_log.record_use(
                    tool_name, tool_id, line_no, resume if isinstance(resume, str) else None,
                    ts=ts, tool_input=launch_input,
                )
            raw_input = block.get("input", {})
            input_obj = _as_object(raw_input, path, line_no, diagnostics, "tool_use input")
            input_text = json.dumps(input_obj, ensure_ascii=False)
            if isinstance(tool_id, str) and tool_commands is not None and tool_name == "Bash":
                command = input_obj.get("command")
                tool_commands[tool_id] = command if isinstance(command, str) else None
            if isinstance(tool_id, str) and tool_calls is not None and tool_call_index is not None:
                call = _make_tool_call(tool_id, tool_name, input_obj, line_no, ts, entry.get("cwd"))
                tool_calls.append(call)
                tool_call_index[tool_id] = call
                # docs/DESIGN.md 4.2's v0.2 reader changes (F5 hardening): a
                # tool_use_id seen before in this same transcript means the
                # pairing below can no longer tell which tool_result belongs
                # to which call. Diagnose it (so Run.status becomes
                # INCOMPLETE) and mark EVERY ToolCall sharing this id --
                # including ones already built -- ambiguous, never only the
                # newest one.
                if tool_calls_by_id is not None:
                    siblings = tool_calls_by_id.setdefault(tool_id, [])
                    if siblings:
                        first_line = siblings[0].line_no
                        if diagnostics is not None:
                            diagnostics.append(
                                f"{path.name}:{line_no}: duplicate tool_use_id {tool_id} "
                                f"at lines {first_line} and {line_no}"
                            )
                        for sibling in siblings:
                            sibling.ambiguous = True
                            sibling.commit_ids = ()
                        call.ambiguous = True
                    siblings.append(call)
            events.append(Event(
                path=path, line_no=line_no, ts=ts, kind="tool_use", raw_type=raw_type,
                tool_name=tool_name, text=_excerpt(input_text),
                exit_code=None, duration_ms=None, is_error=False, usage=_take_usage(),
                markers=_scan_markers(input_text),
            ))
        elif block_type == "text":
            block_text = _as_text(block.get("text", ""), path, line_no, diagnostics, "block text")
            events.append(Event(
                path=path, line_no=line_no, ts=ts, kind="assistant", raw_type=raw_type,
                tool_name=None, text=_excerpt(block_text), exit_code=None,
                duration_ms=None, is_error=False, usage=_take_usage(),
                markers=_scan_markers(block_text),
            ))
        elif block_type == "thinking":
            events.append(Event(
                path=path, line_no=line_no, ts=ts, kind="thinking", raw_type=raw_type,
                tool_name=None, text="", exit_code=None,
                duration_ms=None, is_error=False, usage=_take_usage(), markers=frozenset(),
            ))
        else:
            unknown_event = _unknown_event(block, path, line_no, ts, raw_type)
            unknown_event.usage = _take_usage()
            events.append(unknown_event)
    return _finish()


def _attachment_event(entry, path, line_no, ts, raw_type, diagnostics=None) -> Event:
    attachment = _as_object(
        entry.get("attachment"), path, line_no, diagnostics, "attachment", quality=True
    )
    text = attachment.get("text")
    if not isinstance(text, str):
        pieces = [str(attachment[key]) for key in ("stdout", "stderr") if attachment.get(key)]
        if pieces:
            text = "\n".join(pieces)
        else:
            content = attachment.get("content")
            text = (
                content if isinstance(content, str) else json.dumps(attachment, ensure_ascii=False)
            )

    exit_code = attachment.get("exitCode")
    if not isinstance(exit_code, int):
        exit_code = None
    duration_ms = attachment.get("durationMs")
    if not isinstance(duration_ms, int):
        duration_ms = None

    return Event(
        path=path, line_no=line_no, ts=ts, kind="attachment", raw_type=raw_type,
        tool_name=None, text=_excerpt(text), exit_code=exit_code,
        duration_ms=duration_ms, is_error=False, usage={}, markers=_scan_markers(text),
    )


def _compaction_event(path, line_no, ts, raw_type) -> Event:
    """A system entry with subtype == 'compact_boundary' (docs/DESIGN.md
    section 3): a context compaction, carrying no excerpt text or markers of
    its own."""
    return Event(
        path=path, line_no=line_no, ts=ts, kind="compaction", raw_type=raw_type,
        tool_name=None, text="", exit_code=None, duration_ms=None, is_error=False,
        usage={}, markers=frozenset(),
    )


def _unknown_event(entry, path, line_no, ts, raw_type) -> Event:
    entry_text = json.dumps(entry, ensure_ascii=False)
    return Event(
        path=path, line_no=line_no, ts=ts, kind="unknown", raw_type=raw_type,
        tool_name=None, text=_excerpt(entry_text),
        exit_code=None, duration_ms=None, is_error=False, usage={},
        markers=_scan_markers(entry_text),
    )


# --- parent transcript index --------------------------------------------


@dataclass(frozen=True)
class _TaskStop:
    ts: datetime | None
    ids: frozenset[str]  # task_id, agent_id, or any other string value in input
    line_no: int
    text: str  # excerpt of the tool_use input, for Interrupt.cause_evidence
    # docs/DESIGN.md 4.13.3 (v0.3 N1): the input's own 'task_id'/'shell_id'
    # fields specifically, None when absent or not a string -- used to match
    # a generation's own task_id exactly, rather than the looser `ids`
    # membership test 4.4's attribute_interrupt still uses.
    task_id: str | None = None
    shell_id: str | None = None


@dataclass(frozen=True)
class _KillSentinel:
    ts: datetime
    line_no: int
    text: str


def _string_values(data: object) -> frozenset[str]:
    if not isinstance(data, dict):
        return frozenset()
    return frozenset(value for value in data.values() if isinstance(value, str))


@dataclass(frozen=True)
class _Launch:
    """docs/DESIGN.md 4.13.3 (v0.3 N1): one Workflow tool_use block."""

    line_no: int
    tool_use_id: str
    resume_from: str | None
    # FA2: the parsed timestamp of the assistant entry holding this tool_use
    # block (None when absent or unparseable): the generation's launch_ts.
    ts: datetime | None = None


# docs/DESIGN.md 4.13.3's status vocabulary: for notifications and
# synchronous launch results alike, 'completed' -> 'completed', 'failed' ->
# 'failed', 'killed'/'stopped'/'cancelled' -> 'stopped'. Any string not in
# this table is unrecognized.
_TERMINAL_STATUS_VOCAB = {
    "completed": "completed",
    "failed": "failed",
    "killed": "stopped",
    "stopped": "stopped",
    "cancelled": "stopped",
}

_ENVELOPE_TAG_RE = {
    "task-id": re.compile(r"<task-id>(.*?)</task-id>", re.DOTALL),
    "tool-use-id": re.compile(r"<tool-use-id>(.*?)</tool-use-id>", re.DOTALL),
    "status": re.compile(r"<status>(.*?)</status>", re.DOTALL),
}


def _parse_envelope(text: object) -> tuple[str, str, str] | None:
    """docs/DESIGN.md 4.13.3's envelope: after stripping surrounding
    whitespace, the content starts with <task-notification>, ends with
    </task-notification>, and contains exactly one each of <task-id>,
    <tool-use-id>, and <status>. Returns (task_id, tool_use_id, status), or
    None when the shape does not hold -- a quoted copy inside prose or a
    compaction summary never has the WHOLE text be just the envelope, so it
    is never mistaken for a real one."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not (
        stripped.startswith("<task-notification>") and stripped.endswith("</task-notification>")
    ):
        return None
    values: dict[str, str] = {}
    for tag, pattern in _ENVELOPE_TAG_RE.items():
        matches = pattern.findall(stripped)
        if len(matches) != 1:
            return None
        values[tag] = matches[0].strip()
    return values["task-id"], values["tool-use-id"], values["status"]


@dataclass(frozen=True)
class _NotificationCandidate:
    line_no: int
    form: str  # 'queue' | 'user_with_origin' | 'user_no_origin'
    content: object  # the raw (unparsed) envelope candidate text
    ts: datetime | None = field(default=None, compare=False)


@dataclass(frozen=True)
class _Notification:
    line_no: int
    task_id: str
    tool_use_id: str
    status: str
    ts: datetime | None = field(default=None, compare=False)


@dataclass(frozen=True)
class _AmbiguousLaunch:
    """docs/DESIGN.md 4.13.3's association rule 1 (v0.3 N2): one group of
    duplicate tool_use/tool_result blocks sharing a Workflow tool_use id.
    `candidate_run_ids` is every run id this ambiguous launch could be
    evidence for -- every launch's own `resumeFromRunId` plus every
    well-formed result's own `runId` -- so a caller can tell whether it
    "involves" a particular inspected run (rule 1: unknown/ambiguous_launch
    for exactly those runs, ignored for every other one)."""

    tool_use_id: str
    diagnostic: str  # the exact "ambiguous launch '<id>': lines ..." string
    candidate_run_ids: frozenset[str]


_HASH_CHUNK_SIZE = 65536

# docs/DESIGN.md 4.13.3's run lifecycle activity rule: 10 minutes, the same
# threshold hooks_cli._UNKNOWN_QUIET_SECONDS already uses for the "possibly
# live" hook heuristic (7.2/12). Restated here (not imported) since
# hooks_cli.py already imports reader.py -- reader.py importing hooks_cli
# back would be a cycle.
ACTIVITY_RECENT_SECONDS = 600.0


def activity_word(newest_mtime: float | None, now: float | None = None) -> str:
    """docs/DESIGN.md 4.13.3: 'recent' when the newest mtime among a run's
    own files is under 10 minutes old, 'stale' when older, 'unknown' when
    every file's stat failed (newest_mtime is None). `now` defaults to the
    real wall clock -- this is a file-freshness check, never part of
    report._now()'s own injectable-clock convention, which only ever governs
    human-facing relative ages computed from parsed transcript timestamps."""
    if newest_mtime is None:
        return "unknown"
    if now is None:
        now = time.time()
    return "recent" if (now - newest_mtime) < ACTIVITY_RECENT_SECONDS else "stale"


def _workflow_run_files(path: Path) -> list[Path]:
    """Every file directly inside a workflow run's own wf_* directory (docs/
    DESIGN.md 4.13.3's activity rule population; the same population
    cli._run_files_for_liveness uses for kind == 'workflow', duplicated here
    since reader.py cannot import cli.py)."""
    try:
        return [p for p in path.iterdir() if _is_file(p)]
    except OSError:
        return []


def _workflow_activity(path: Path) -> str:
    newest: float | None = None
    for file_path in _workflow_run_files(path):
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return activity_word(newest)


# --- parent inspection completeness (docs/DESIGN.md 4.13.3, v0.3 N2) -------


_BENIGN_ENQUEUE_KEYS = frozenset({"type", "operation", "sessionId", "timestamp"})


def _entry_shape_ok(entry: dict) -> bool:
    """docs/DESIGN.md 4.13.3's Run lifecycle paragraph: the shape every
    'user'/'assistant'/'queue-operation' entry, and every tool_use/
    tool_result block inside one, must have for parent inspection to stay
    complete. Any entry type not named here (system, thinking, ...) is
    unconstrained."""
    etype = entry.get("type")
    if etype == "user":
        message = entry.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if not isinstance(content, (str, list)):
            return False
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if not isinstance(block.get("tool_use_id"), str):
                        return False
        return True
    if etype == "assistant":
        message = entry.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if not isinstance(content, list):
            return False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                    return False
        return True
    if etype == "queue-operation":
        operation = entry.get("operation")
        if not isinstance(operation, str):
            return False
        if operation == "enqueue" and not isinstance(entry.get("content"), str):
            # docs/DESIGN.md 4.13.3 (N4 fix, M-N4-1 b): a bare enqueue marker
            # whose key set is exactly these four carries nothing at all
            return set(entry) == _BENIGN_ENQUEUE_KEYS
        return True
    return True


class ParentIndex:
    """Incremental, fingerprinted cache of a main session transcript (docs/
    DESIGN.md 4.2, 4.13.3): TaskStop calls, kill sentinels, compaction
    timestamps, and -- v0.3 N1 -- Workflow launch/result pairs turned into
    Generation records. Consulted by detectors.base.attribute_interrupt
    (4.4) via reader._attribute_interrupts, and by ParentIndex.generations
    for a run's own lifecycle work (N2).

    `for_path` re-reads only the bytes appended since the last access,
    unless the file's inode changed, shrank, or its already-parsed prefix no
    longer hashes the same (a same-size rewrite) -- any of which rebuilds
    the whole index from zero. `lines_parsed` and `rebuilds` are exposed for
    tests; both start at 0 and the initial build is never counted as a
    rebuild.
    """

    _cache: dict[Path, "ParentIndex"] = {}

    def __init__(
        self,
        path: Path | None,
        task_stops: list[_TaskStop],
        kill_sentinels: list[_KillSentinel],
        compaction_timestamps: list[datetime],
        diagnostics: list[str] | None = None,
        tool_result_agent_ids: frozenset[str] = frozenset(),
        generations: list[Generation] | None = None,
    ):
        self.path = path
        self.task_stops = task_stops
        self.kill_sentinels = kill_sentinels
        # Backward-compatible view: every earlier caller of this index only
        # ever wanted the timestamps.
        self.kill_timestamps = [sentinel.ts for sentinel in kill_sentinels]
        self.compaction_timestamps = compaction_timestamps
        # docs/DESIGN.md 7.2 (D2): agentId values seen on a tool_result
        # block anywhere in this transcript -- a subagent named here has
        # already returned control to its parent. A plain, growing set (not
        # frozen): == frozenset() still holds for an empty one, and nothing
        # else needs it immutable.
        self.tool_result_agent_ids: set[str] = set(tool_result_agent_ids)
        # docs/DESIGN.md 4.13.1 (G-A): launches, resume requests and their results, for the
        # return decision of a session subagent; incremental like everything else here
        self.facts = returns.ReturnFacts(path)
        # (docs/DESIGN.md 4.13.3, v0.3 N1) every Generation this index could
        # build from the transcript seen so far, across every run any launch
        # here is associated with (association rules 2-5 collapse into a
        # single run_id per launch -- the result's own runId when well
        # formed, otherwise the launch's own resumeFromRunId -- so no
        # external "inspected run X" is needed to compute the full set; a
        # caller that cares about one run filters by Generation.run_id).
        # Ordered by launch_line.
        self.generations: list[Generation] = list(generations) if generations is not None else []
        # (docs/DESIGN.md 4.13.3's association rule 1, v0.3 N2) every
        # ambiguous launch group seen so far, for Lifecycle's own
        # unknown/ambiguous_launch row -- an ambiguous launch produces no
        # Generation (N1), so this is the only place that information
        # survives past _rebuild_generations.
        self.ambiguous_launches: list[_AmbiguousLaunch] = []
        # path:line diagnostics from a malformed parent-transcript entry, a
        # single "unreadable"/"undecodable" entry when the file itself could
        # not be read, or a generation-building diagnostic (an ambiguous
        # launch, a result-less or malformed launch with no resume, an
        # unrecognized launch status) -- docs/DESIGN.md section 2's tolerant
        # parsing, a bad line becomes a diagnostic, never an exception.
        # Callers fold these into the consuming Run's own diagnostics.
        self.diagnostics: list[str] = diagnostics if diagnostics is not None else []

        # --- incremental cache bookkeeping (docs/DESIGN.md 4.13.3, v0.3 N1)
        # irrelevant to an index built directly with pre-collected lists
        # (as tests/test_attribution.py does): populated only by _refresh.
        self._parsed_offset = 0
        self._next_line_no = 1
        self._prefix_digest: str | None = None
        self._inode: int | None = None
        self._seen_size = 0
        self.lines_parsed = 0
        self.rebuilds = 0
        # docs/DESIGN.md 4.13.3's Run lifecycle paragraph (N1N2 review
        # findings 1/7): true when the parent transcript (none at all counts
        # as complete, vacuously) is readable, has no undecodable line, and
        # every entry seen so far passes _entry_shape_ok -- computed inside
        # this same incremental pass, never by a second, independent full
        # re-read of the file. Reset to True by _reset_state before a
        # rebuild-from-zero reparses everything; otherwise only ever flips
        # False and stays there for the life of this cached index, the same
        # append-only accumulation lines_parsed/diagnostics already use --
        # except that a transient I/O failure (below) can force one more
        # full reinspection, which may then legitimately set it True again.
        self.inspection_complete = True
        # (N1N2 review round 2, new finding 1) True exactly when the most
        # recent _refresh() ended in an OSError from stat/rehash/reparse:
        # content validity (inspection_complete, above, and every per-line
        # diagnostic) is persistent -- it only ever reflects bytes this index
        # has actually read and checked -- while this flag is the current
        # *I/O availability* of the refresh itself, which is transient. A
        # true value forces the next _refresh() to do a full reinspection
        # from byte 0 regardless of what the ordinary inode/size/prefix-hash
        # comparison would otherwise conclude (that comparison can see no
        # change at all when the file itself never changed, which is exactly
        # the case a one-time, non-recurring I/O hiccup leaves behind), so
        # that every retained line is checked again before
        # inspection_complete is allowed to become True once more. Cleared
        # the moment a reinspection's own read succeeds, whether or not its
        # own needs_reset was already true for an ordinary reason.
        self._needs_full_recheck = False
        self._raw_diagnostics: list[str] = list(self.diagnostics)
        # (N1N2 review round 2, original finding 1) gen_diagnostics as last
        # computed by _rebuild_generations, kept separately from
        # _raw_diagnostics so self.diagnostics can be recomputed by
        # _recompute_diagnostics from both of these plus
        # _unreadable_diagnostic below, on demand and without a second,
        # independent full read -- including from inside _refresh's own
        # failure branches, which never reach _rebuild_generations at all.
        self._gen_diagnostics: list[str] = []
        # The same generation-building diagnostics, each with the run ids it
        # involves (N4 fix, A4), for discovery_diagnostics_for.
        self._gen_records: list[tuple[str, frozenset[str]]] = []
        # A single, stable, file-level diagnostic ("<name>: parent
        # transcript unreadable") while the most recent _refresh() ended in
        # an OSError from stat/rehash/reparse; None once a read succeeds
        # again. Never a growing list -- the same read failure recurring
        # across repeated refreshes is one fact, not one diagnostic per
        # refresh.
        self._unreadable_diagnostic: str | None = None
        self._launches_by_id: dict[str, list[_Launch]] = {}
        # (result line, toolUseResult, the line's parsed timestamp or None)
        # (result line, toolUseResult, the line's parsed timestamp or None,
        # a rejected launch, the result digest or None)
        self._results_by_id: dict[
            str, list[tuple[int, object, datetime | None, bool, str | None]]
        ] = {}
        self._notification_candidates: list[_NotificationCandidate] = []
        self._first_origin_line: int | None = None
        # docs/DESIGN.md 4.2 (FA4): the physical lines 1.._skip_through are a fork's
        # inherited prefix and are never ingested; _boundary_settled is True once the
        # transcript's own first entry fixed that boundary for good.
        self._skip_through = 0
        self._boundary_settled = False
        # F fix 1 (S-m5): the fork scan's answer for one file fingerprint (inode, size,
        # mtime), so a poll of an unchanged file, and the two steps of one refresh, share a
        # single scan. Dropped on any read failure and bypassed while a recheck is pending.
        self._fork_fingerprint: tuple | None = None
        self._fork_result: tuple[int, bool] = (0, False)

    @property
    def pending_full_recheck(self) -> bool:
        """True while the last refresh ended in an I/O failure, so the next access reinspects
        from byte 0 whatever the file's stat says (docs/DESIGN.md 4.13.3). A caller that caches
        the file's stat signature must not cache it while this is true (watch, 4.12)."""
        return self._needs_full_recheck

    @classmethod
    def for_path(cls, path: Path) -> "ParentIndex":
        path = Path(path)
        cached = cls._cache.get(path)
        if cached is None:
            cached = cls(path, [], [], [])
            cls._cache[path] = cached
        cached._refresh()
        return cached

    @classmethod
    def _scan(cls, path: Path) -> "ParentIndex":
        """A fresh, uncached, one-shot full scan of path -- used directly by
        tests that want a single ParentIndex snapshot without touching the
        process-level `for_path` cache."""
        index = cls(path, [], [], [])
        index._refresh()
        return index

    def generations_for(self, run_id: str) -> list[Generation]:
        """Generations of run_id specifically (docs/DESIGN.md 4.13.3),
        ordered by launch_line, a convenience filter over the flat
        `generations` attribute for a caller (N2's lifecycle computation)
        that only ever cares about one run at a time."""
        return [generation for generation in self.generations if generation.run_id == run_id]

    def ambiguous_diagnostics_for(self, run_id: str) -> list[str]:
        """docs/DESIGN.md 4.13.3's association rule 1 (v0.3 N2): the
        diagnostic(s) of every ambiguous launch that involves run_id -- one
        of its duplicate results names run_id as its own runId, or one of
        its duplicate launches resumes from run_id. [] when none do (the
        launch is simply ignored for this run, per rule 1)."""
        return [
            amb.diagnostic for amb in self.ambiguous_launches
            if run_id in amb.candidate_run_ids
        ]

    def discovery_diagnostics_for(self, run_id: str | None) -> list[str]:
        """The parent diagnostics that reach run_id's `Run.discovery['parent']`
        (docs/DESIGN.md 4.13.1, N4 fix A4): every file-level diagnostic (an
        undecodable line, a malformed entry, an unreadable parent), then each
        generation-building diagnostic that involves run_id. run_id None (a
        session) gets the file-level ones only."""
        diagnostics = list(self._raw_diagnostics)
        diagnostics.extend(
            text for text, involved in self._gen_records
            if run_id is not None and run_id in involved
        )
        if self._unreadable_diagnostic is not None:
            diagnostics.append(self._unreadable_diagnostic)
        return diagnostics

    # --- incremental scan ------------------------------------------------

    def _reset_state(self) -> None:
        """Discard every piece of state derived from the file's own bytes
        (docs/DESIGN.md 4.13.3's rebuild-from-zero), keeping path and the
        two counters (lines_parsed, rebuilds), which are cumulative for the
        life of this object."""
        self.task_stops = []
        self.kill_sentinels = []
        self.kill_timestamps = []
        self.compaction_timestamps = []
        self.tool_result_agent_ids = set()
        self.facts = returns.ReturnFacts(self.path)
        self.generations = []
        self.ambiguous_launches = []
        self._raw_diagnostics = []
        self._launches_by_id = {}
        self._results_by_id = {}
        self._notification_candidates = []
        self._first_origin_line = None
        self._skip_through = 0
        self._boundary_settled = False
        self._parsed_offset = 0
        self._next_line_no = 1
        self._prefix_digest = None
        # A rebuild-from-zero reparses the whole file from byte 0, so
        # completeness is recomputed from scratch too (N1N2 review finding
        # 1/7): this reset, like the others above, is not itself one of the
        # two counters _refresh's own docstring calls "cumulative for the
        # life of this object".
        self.inspection_complete = True

    def _hash_prefix(self, offset: int) -> str:
        hasher = hashlib.sha256()
        if offset <= 0 or self.path is None:
            return hasher.hexdigest()
        with self.path.open("rb") as handle:
            remaining = offset
            while remaining > 0:
                chunk = handle.read(min(_HASH_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                hasher.update(chunk)
                remaining -= len(chunk)
        return hasher.hexdigest()

    def _state_snapshot(self) -> dict:
        """A shallow copy of every attribute a rebuild-from-zero touches
        (everything _reset_state() wipes, plus lines_parsed, which
        _parse_increment can advance before it raises) -- taken right before
        _reset_state() runs, restored verbatim by _restore_state if the
        rebuild's own read then fails (N1N2 fix review finding 1). Keeps a
        failed rebuild from committing _reset_state()'s wipe."""
        return {
            "task_stops": list(self.task_stops),
            "kill_sentinels": list(self.kill_sentinels),
            "kill_timestamps": list(self.kill_timestamps),
            "compaction_timestamps": list(self.compaction_timestamps),
            "tool_result_agent_ids": set(self.tool_result_agent_ids),
            "facts": self.facts,
            "generations": list(self.generations),
            "ambiguous_launches": list(self.ambiguous_launches),
            "_raw_diagnostics": list(self._raw_diagnostics),
            "_launches_by_id": dict(self._launches_by_id),
            "_results_by_id": dict(self._results_by_id),
            "_notification_candidates": list(self._notification_candidates),
            "_first_origin_line": self._first_origin_line,
            "_skip_through": self._skip_through,
            "_boundary_settled": self._boundary_settled,
            "_parsed_offset": self._parsed_offset,
            "_next_line_no": self._next_line_no,
            "_prefix_digest": self._prefix_digest,
            "lines_parsed": self.lines_parsed,
        }

    def _restore_state(self, snapshot: dict) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)

    def _recompute_diagnostics(self) -> None:
        """The public, combined view (docs/DESIGN.md section 2: "a bad line
        becomes a diagnostic string on the Run, never an exception"):
        per-line decode/shape diagnostics, generation-building diagnostics,
        and -- last -- the single current file-level diagnostic, if any.
        Called from _rebuild_generations after a successful pass, and
        directly from _mark_unreadable so a failure branch that never
        reaches _rebuild_generations at all still leaves self.diagnostics
        current before _refresh returns (N1N2 review round 2, original
        finding 1)."""
        diagnostics = list(self._raw_diagnostics) + list(self._gen_diagnostics)
        if self._unreadable_diagnostic is not None:
            diagnostics.append(self._unreadable_diagnostic)
        self.diagnostics = diagnostics

    def _fork_for(self, stat) -> tuple[int, bool]:
        """`_fork_scan(self.path)` once per file fingerprint (S-m5)."""
        fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if fingerprint != self._fork_fingerprint:
            self._fork_result = _fork_scan(self.path)
            self._fork_fingerprint = fingerprint
        return self._fork_result

    def _mark_unreadable(self) -> None:
        """Record a transient I/O failure from anywhere in _refresh's own
        stat/rehash/reparse steps (N1N2 review round 2, new finding 1):
        inspection is no longer complete, and the *next* access must do a
        full reinspection from byte 0 regardless of what the ordinary
        inode/size/prefix-hash comparison would otherwise conclude -- that
        comparison sees no change at all when the file itself never changed,
        which is exactly what a one-time, non-recurring I/O hiccup leaves
        behind, so nothing else would ever re-trigger the recheck this needs.
        Also records the one stable, file-level diagnostic a read failure
        gets (original finding 1, PARTLY): never a growing list, since the
        same ongoing failure recurring across refreshes is one fact, and
        visible on self.diagnostics immediately -- this failure path never
        reaches _rebuild_generations, which is the only other place that
        would otherwise bring self.diagnostics up to date."""
        self.inspection_complete = False
        self._needs_full_recheck = True
        self._unreadable_diagnostic = f"{self.path.name}: parent transcript unreadable"
        self._recompute_diagnostics()

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # Simply not there yet (docs/DESIGN.md section 2/4.6's
            # "unreadable, not merely absent" convention): tolerated,
            # complete vacuously, current state kept as-is.
            return
        except OSError:
            # Exists but could not even be stat'd -- unreadable, not merely
            # absent (N1N2 review finding 1): current state is kept as-is,
            # the same tolerant "do not raise" rule as every other branch of
            # this method, but inspection is no longer complete.
            self._mark_unreadable()
            return
        if not _is_file(self.path):
            return

        inode = stat.st_ino
        size = stat.st_size
        # A prior transient failure always forces a full reinspection here,
        # skipping the ordinary comparison below entirely: that comparison
        # can only ever detect a *structural* change (a different inode, a
        # shrink, a rewritten prefix), so a one-time I/O hiccup against a
        # file that never actually changed would otherwise never be noticed
        # again, and inspection_complete would stay False forever.
        needs_reset = self._needs_full_recheck
        if needs_reset:
            self._fork_fingerprint = None  # a pending recheck rescans (S-m5)
        if not needs_reset:
            try:
                if self._inode is not None and (inode != self._inode or size < self._seen_size):
                    needs_reset = True
                elif self._parsed_offset > 0:
                    digest = self._hash_prefix(self._parsed_offset)
                    needs_reset = digest != self._prefix_digest
                if needs_reset:
                    # R2-3: a structural change (a new inode, a shrink, a rewritten prefix)
                    # can leave the (inode, size, mtime) fingerprint equal, so the cached
                    # fork boundary is stale too: the settle scan below must be fresh.
                    self._fork_fingerprint = None
            except OSError:
                # A read failure while re-hashing the already-parsed prefix,
                # with nothing reset yet: current state is kept as-is,
                # inspection is no longer complete.
                self._mark_unreadable()
                return

        # N1N2 fix review finding 1: _reset_state() wipes generations/
        # task_stops/kill_sentinels/etc. before the rebuild's own read has
        # even been attempted. Snapshot first, so a read failure right after
        # the reset (a permission flap, a transient I/O error, a concurrent
        # edit mid-read) can be undone rather than committed -- the whole
        # reset-and-reparse only ever takes effect together.
        # FA4: a fork whose first own entry was not written yet inherited nothing; once it
        # appears the boundary moves and what was ingested so far must be rebuilt.
        if (
            not needs_reset
            and not self._boundary_settled
            and self._parsed_offset > 0
            and self._fork_for(stat)[0] != self._skip_through
        ):
            needs_reset = True
        snapshot = self._state_snapshot() if needs_reset else None
        if needs_reset:
            self._reset_state()
        if not self._boundary_settled:
            self._skip_through, self._boundary_settled = self._fork_for(stat)
        try:
            self._parse_increment(size)
        except OSError:
            # A read failure partway through reading the new tail (N1N2
            # review finding 1: the concrete scenario is Path.open raising
            # for an existing file whose stat() still succeeded) never
            # raises past this point; the index simply stays incomplete and
            # keeps whatever state it already had -- restoring the
            # pre-reset snapshot first when this call had just reset it, so
            # "whatever state it already had" means what it had before this
            # _refresh() call, not the freshly wiped, still-empty state.
            if snapshot is not None:
                self._restore_state(snapshot)
            self._mark_unreadable()
            return
        # The read just succeeded, whether or not needs_reset was already
        # true for an ordinary reason: I/O availability is restored, and
        # inspection_complete now reflects this pass's own, freshly-checked
        # content-validity findings rather than a stale failure. The
        # file-level diagnostic clears with it -- _rebuild_generations below
        # recomputes self.diagnostics from the now-current raw/gen pieces.
        self._needs_full_recheck = False
        self._unreadable_diagnostic = None
        if needs_reset:
            self.rebuilds += 1

        self._inode = inode
        self._seen_size = size
        self._rebuild_generations()

    def _parse_increment(self, size: int) -> None:
        """Parse only the complete lines in [parsed_offset, last newline
        before size) (docs/DESIGN.md 4.13.3): a partial trailing line is
        left for the next access. Updates parsed_offset, the prefix digest,
        and lines_parsed; every other raw accumulator is appended to, never
        replaced, so a later _rebuild_generations sees the full history.
        Physical line numbers continue from `_next_line_no` (reset to 1 by
        _reset_state) rather than being recounted from byte 0 each time, so
        parsing cost -- including line numbering -- stays proportional to
        new lines, never to parsed_offset itself."""
        if size <= self._parsed_offset:
            return
        with self.path.open("rb") as handle:
            handle.seek(self._parsed_offset)
            chunk = handle.read(size - self._parsed_offset)
        last_newline = chunk.rfind(b"\n")
        if last_newline == -1:
            return
        complete = chunk[: last_newline + 1]
        start_line_no = self._next_line_no
        lines = complete.split(b"\n")[:-1]
        for offset_in_chunk, raw_line in enumerate(lines):
            line_no = start_line_no + offset_in_chunk
            # docs/DESIGN.md 4.13.3: "lines_parsed adds one per complete line
            # parsed" -- every complete physical line counts, blank ones
            # included (N1N2 review finding 8), so the increment happens
            # before the blank-line skip below, not after it.
            self.lines_parsed += 1
            if line_no <= self._skip_through:
                continue
            text = raw_line.strip()
            if not text:
                continue
            try:
                entry = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                # docs/DESIGN.md 4.13.3's Run lifecycle paragraph: parent
                # inspection is complete only when the file "has no
                # undecodable line" (N1N2 review finding 1/7 -- computed
                # here, in the same incremental pass, rather than by a
                # second full re-read). docs/DESIGN.md section 2: a bad line
                # becomes a diagnostic too, never only a dropped flag (N1N2
                # review round 2, original finding 1) -- a fixed reason and
                # the line number only, never the undecodable text itself.
                self.inspection_complete = False
                self._raw_diagnostics.append(
                    f"{self.path.name}:{line_no}: undecodable parent transcript line"
                )
                continue
            if not isinstance(entry, dict):
                continue
            if not _entry_shape_ok(entry):
                # Same rule, the shape-violation half (N1N2 review round 2,
                # original finding 1): a fixed reason and the line number
                # only, regardless of which of 4.13.3's sub-rules failed.
                self.inspection_complete = False
                self._raw_diagnostics.append(
                    f"{self.path.name}:{line_no}: malformed parent transcript entry"
                )
            self._ingest_entry(entry, line_no)
        self._next_line_no = start_line_no + len(lines)
        self._parsed_offset += len(complete)
        self._prefix_digest = self._hash_prefix(self._parsed_offset)

    def _ingest_entry(self, entry: dict, line_no: int) -> None:
        raw_type = entry.get("type")
        ts = _parse_ts(entry.get("timestamp"))

        if raw_type == "system" and entry.get("subtype") == "compact_boundary":
            if ts is not None:
                self.compaction_timestamps.append(ts)
            return

        if raw_type == "queue-operation":
            # docs/DESIGN.md 4.13.3: a queue-operation 'enqueue' entry whose
            # content is the envelope, one of the two notification delivery
            # forms -- never subject to the origin-line ordering rule.
            if entry.get("operation") == "enqueue" and isinstance(entry.get("content"), str):
                self._notification_candidates.append(
                    _NotificationCandidate(line_no, "queue", entry["content"], ts)
                )
            return

        message = _as_object(
            entry.get("message"), self.path, line_no, self._raw_diagnostics, "message"
        )
        content = message.get("content")

        if raw_type == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                self.facts.note_use(name, block.get("id"), line_no, ts, block.get("input"))
                if name == "TaskStop":
                    input_obj = block.get("input")
                    input_dict = input_obj if isinstance(input_obj, dict) else {}
                    input_text = json.dumps(input_dict, ensure_ascii=False)
                    raw_task_id = input_dict.get("task_id")
                    raw_shell_id = input_dict.get("shell_id")
                    self.task_stops.append(_TaskStop(
                        ts=ts, ids=_string_values(input_obj), line_no=line_no,
                        text=_excerpt(input_text),
                        task_id=raw_task_id if isinstance(raw_task_id, str) else None,
                        shell_id=raw_shell_id if isinstance(raw_shell_id, str) else None,
                    ))
                elif name == "Workflow":
                    tool_id = block.get("id")
                    if not isinstance(tool_id, str):
                        continue
                    input_obj = block.get("input")
                    input_dict = input_obj if isinstance(input_obj, dict) else {}
                    raw_resume = input_dict.get("resumeFromRunId")
                    launch = _Launch(
                        line_no=line_no, tool_use_id=tool_id,
                        resume_from=raw_resume if isinstance(raw_resume, str) else None,
                        ts=ts,
                    )
                    self._launches_by_id.setdefault(tool_id, []).append(launch)
            return

        if raw_type != "user":
            return

        text = _flatten_block_text(content)
        if text.startswith(KILL_SENTINEL_PREFIX) and ts is not None:
            self.kill_sentinels.append(_KillSentinel(ts=ts, line_no=line_no, text=_excerpt(text)))
            self.kill_timestamps.append(ts)

        origin = entry.get("origin")
        has_origin = isinstance(origin, dict)
        if has_origin and self._first_origin_line is None:
            self._first_origin_line = line_no
        if has_origin and origin.get("kind") == "task-notification":
            self._notification_candidates.append(
                _NotificationCandidate(line_no, "user_with_origin", message.get("content"), ts)
            )
        elif not has_origin:
            # docs/DESIGN.md 4.13.3: counted only once the first line in the
            # transcript carrying 'origin' is known, at generation-build
            # time (_accepted_notifications); recorded provisionally here.
            self._notification_candidates.append(
                _NotificationCandidate(line_no, "user_no_origin", message.get("content"), ts)
            )

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                # docs/DESIGN.md 7.2 (D2): a tool_result block carrying the
                # agentId of the child that produced it -- the parent's own
                # record that a subagent already returned control to it.
                block_agent_id = block.get("agentId")
                if isinstance(block_agent_id, str) and block_agent_id:
                    self.tool_result_agent_ids.add(block_agent_id)
                # docs/DESIGN.md 4.13.3 (v0.3 N1): a Workflow launch's own
                # result is the tool_result block, in a user entry, whose
                # tool_use_id equals the launch's id; the handshake payload
                # itself sits at the ENTRY level (entry["toolUseResult"]),
                # the same place an Agent-tool result does (docs/DESIGN.md
                # 7.2's D-A).
                raw_tool_use_id = block.get("tool_use_id")
                if not isinstance(raw_tool_use_id, str):
                    self.facts.note_bad_result(raw_tool_use_id, block, entry.get("toolUseResult"))
                if isinstance(raw_tool_use_id, str):
                    self.facts.note_result(
                        raw_tool_use_id, block, entry.get("toolUseResult"),
                        "toolUseResult" in entry, line_no,
                    )
                    raw_result = entry.get("toolUseResult")
                    self._results_by_id.setdefault(raw_tool_use_id, []).append((
                        line_no, raw_result, ts, _is_rejected_launch(block, raw_result),
                        _result_digest(block, raw_result),
                    ))
        # docs/DESIGN.md 7.2 (D-A): the entry-level toolUseResult also
        # carries agentId directly for an Agent-tool result.
        tool_use_result = entry.get("toolUseResult")
        if isinstance(tool_use_result, dict):
            entry_agent_id = tool_use_result.get("agentId")
            if isinstance(entry_agent_id, str) and entry_agent_id:
                self.tool_result_agent_ids.add(entry_agent_id)

    # --- generation building (docs/DESIGN.md 4.13.3, v0.3 N1) ------------

    def _accepted_notifications(self) -> list[_Notification]:
        """Every valid envelope from an accepted notification record (docs/
        DESIGN.md 4.13.3), the two delivery forms of one notification (same
        task id, tool-use id, status) collapsed into the earliest line."""
        parsed: list[_Notification] = []
        for candidate in self._notification_candidates:
            if candidate.form == "user_no_origin":
                if (
                    self._first_origin_line is not None
                    and candidate.line_no >= self._first_origin_line
                ):
                    continue
            envelope = _parse_envelope(candidate.content)
            if envelope is None:
                continue
            task_id, tool_use_id, status = envelope
            parsed.append(
                _Notification(candidate.line_no, task_id, tool_use_id, status, candidate.ts)
            )

        best: dict[tuple[str, str, str], _Notification] = {}
        for notification in parsed:
            key = (notification.task_id, notification.tool_use_id, notification.status)
            current = best.get(key)
            if current is None or notification.line_no < current.line_no:
                best[key] = notification
        return list(best.values())

    @staticmethod
    def _combine_terminal_evidence(
        records: list[tuple[str, str, int]],
    ) -> tuple[str | None, str | None, int | None, list[str]]:
        """docs/DESIGN.md 4.13.3's "Terminal evidence per generation" rules,
        given records of (value, source, line) already restricted to this
        generation and sorted by nothing in particular (sorted here)."""
        if not records:
            return None, None, None, []
        ordered = sorted(records, key=lambda record: record[2])
        values = {record[0] for record in ordered}
        if len(values) == 1:
            value, source, line = ordered[0]
            return value, source, line, []
        if (
            len(ordered) == 2
            and ordered[0][1] == "task_stop"
            and ordered[1][1] == "notification"
            and ordered[1][0] in ("completed", "failed")
        ):
            value, source, line = ordered[1]
            return value, source, line, []
        baseline_value = ordered[0][0]
        conflict_line = next(
            record[2] for record in ordered[1:] if record[0] != baseline_value
        )
        return (
            "unknown", "conflict", conflict_line,
            [f"conflicting terminal evidence at line {conflict_line}"],
        )

    def _generation_terminal(
        self,
        *,
        task_id: str,
        tool_use_id: str,
        result_line: int,
        launch_status: str,
        accepted_notifications: list[_Notification],
    ) -> tuple[str | None, str | None, int | None, list[str]]:
        if launch_status not in _TERMINAL_STATUS_VOCAB and launch_status != "async_launched":
            # docs/DESIGN.md 4.13.3's Launch status paragraph gives this
            # exact literal string, with no line number (unlike the "...
            # at line N" diagnostics the same section gives for a
            # malformed or missing result); the line is still available on
            # terminal_line, so it is not lost by omitting it here.
            return (
                "unknown", "unrecognized_status", result_line,
                ["unrecognized launch status"],
            )

        records: list[tuple[str, str, int]] = []
        for notification in accepted_notifications:
            if (
                notification.task_id != task_id
                or notification.tool_use_id != tool_use_id
                or notification.line_no <= result_line
            ):
                continue
            value = _TERMINAL_STATUS_VOCAB.get(notification.status, "unknown")
            records.append((value, "notification", notification.line_no))
        for task_stop in self.task_stops:
            if task_stop.line_no <= result_line:
                continue
            if task_stop.task_id == task_id or task_stop.shell_id == task_id:
                records.append(("stopped", "task_stop", task_stop.line_no))
        if launch_status != "async_launched":
            records.append((_TERMINAL_STATUS_VOCAB[launch_status], "launch_result", result_line))

        return self._combine_terminal_evidence(records)

    def _incomplete_generation(
        self, launch: _Launch, result_line: int | None, diagnostic: str,
        result_ts: datetime | None = None,
    ) -> Generation:
        """docs/DESIGN.md 4.13.3: an incomplete generation (association
        rules 4 and 5), only ever built when the launch's own
        resumeFromRunId names a run -- globally, that run is the only one
        this launch could ever be incomplete evidence for."""
        return Generation(
            launch_line=launch.line_no, tool_use_id=launch.tool_use_id,
            resume_from=launch.resume_from, result_line=result_line,
            run_id=launch.resume_from, task_id=None, launch_status=None,
            terminal="unknown", terminal_source="incomplete_generation",
            terminal_line=None, diagnostics=[diagnostic], result_ts=result_ts,
            launch_ts=launch.ts,
        )

    @staticmethod
    def _involved(launch: "_Launch", results: list = ()) -> frozenset[str]:
        """The runs a result-less, malformed, or rejected launch involves: the run
        its resumeFromRunId names, plus every string `toolUseResult.runId` on any
        of its results (A4, A5); none for an initial launch whose results name no
        run."""
        involved = set() if launch.resume_from is None else {launch.resume_from}
        for _line, raw_result, _ts, _rejected, _digest in results:
            if isinstance(raw_result, dict) and isinstance(raw_result.get("runId"), str):
                involved.add(raw_result["runId"])
        return frozenset(involved)

    def _rebuild_generations(self) -> None:
        """Recomputed fresh from the raw accumulators on every access (docs/
        DESIGN.md 4.13.3): cheap, bounded by the number of launches/
        notifications seen so far, never by file size -- only the line
        parsing above is incremental."""
        generations: list[Generation] = []
        gen_diagnostics: list[str] = []
        gen_records: list[tuple[str, frozenset[str]]] = []
        ambiguous_launches: list[_AmbiguousLaunch] = []
        accepted_notifications = self._accepted_notifications()

        for tool_use_id, launches in self._launches_by_id.items():
            all_results = self._results_by_id.get(tool_use_id, [])
            # N4 fix, A4: byte-identical repeats of one result are one result.
            results = _distinct_results(all_results, lambda result: result[4])
            if len(launches) > 1 or len(results) > 1:
                lines = sorted(
                    {launch.line_no for launch in launches} | {r[0] for r in all_results}
                )
                diagnostic = (
                    f"{LAUNCH_DIAGNOSTIC_PREFIXES[0]} {tool_use_id!r}: lines "
                    + ", ".join(str(line) for line in lines)
                )
                # docs/DESIGN.md 4.13.3's association rule 1 (v0.3 N2): every
                # run id this ambiguous launch is evidence for -- each
                # launch's own resumeFromRunId, plus each well-formed
                # result's own runId -- so Lifecycle can tell whether it
                # "involves" a particular inspected run.
                candidate_run_ids: set[str] = set()
                for dup_launch in launches:
                    if dup_launch.resume_from is not None:
                        candidate_run_ids.add(dup_launch.resume_from)
                for _line, raw_result, _result_ts, _rejected, _digest in all_results:
                    if isinstance(raw_result, dict) and isinstance(raw_result.get("runId"), str):
                        candidate_run_ids.add(raw_result["runId"])
                gen_diagnostics.append(diagnostic)
                gen_records.append((diagnostic, frozenset(candidate_run_ids)))
                ambiguous_launches.append(_AmbiguousLaunch(
                    tool_use_id=tool_use_id, diagnostic=diagnostic,
                    candidate_run_ids=frozenset(candidate_run_ids),
                ))
                continue

            launch = launches[0]
            if not results:
                diagnostic = f"{LAUNCH_DIAGNOSTIC_PREFIXES[1]} at line {launch.line_no}"
                gen_diagnostics.append(diagnostic)
                # N4 fix, A4: it involves a run only through its resume.
                gen_records.append((diagnostic, self._involved(launch)))
                if launch.resume_from is not None:
                    generations.append(self._incomplete_generation(launch, None, diagnostic))
                continue

            result_line, raw_result, result_ts, rejected, _digest = results[0]
            if rejected:
                # N4 fix, A4 and A5: a rejected launch builds no generation. An
                # error result is not proof that no run exists, so one with a
                # resume stays involved with the run it names.
                if launch.resume_from is not None:
                    diagnostic = f"{LAUNCH_DIAGNOSTIC_PREFIXES[2]} at line {result_line}"
                    gen_diagnostics.append(diagnostic)
                    gen_records.append((diagnostic, self._involved(launch)))
                continue
            well_formed = (
                isinstance(raw_result, dict)
                and isinstance(raw_result.get("runId"), str)
                and isinstance(raw_result.get("taskId"), str)
                and isinstance(raw_result.get("status"), str)
            )
            if not well_formed:
                diagnostic = f"{LAUNCH_DIAGNOSTIC_PREFIXES[3]} at line {result_line}"
                gen_diagnostics.append(diagnostic)
                gen_records.append((diagnostic, self._involved(launch, all_results)))
                if launch.resume_from is not None:
                    generations.append(
                        self._incomplete_generation(launch, result_line, diagnostic, result_ts)
                    )
                continue

            run_id = raw_result["runId"]
            task_id = raw_result["taskId"]
            launch_status = raw_result["status"]
            terminal, source, line, extra_diag = self._generation_terminal(
                task_id=task_id, tool_use_id=tool_use_id, result_line=result_line,
                launch_status=launch_status, accepted_notifications=accepted_notifications,
            )
            generations.append(Generation(
                launch_line=launch.line_no, tool_use_id=tool_use_id,
                resume_from=launch.resume_from, result_line=result_line,
                run_id=run_id, task_id=task_id, launch_status=launch_status,
                terminal=terminal, terminal_source=source, terminal_line=line,
                diagnostics=extra_diag, result_ts=result_ts, launch_ts=launch.ts,
            ))

        generations.sort(key=lambda generation: generation.launch_line)
        self.generations = generations
        self.ambiguous_launches = ambiguous_launches
        self._gen_diagnostics = gen_diagnostics
        self._gen_records = gen_records
        self._recompute_diagnostics()


def _parent_transcript_path_for_workflow_dir(path: Path) -> Path | None:
    """The <root>/<slug>/<session>.jsonl that sits next to the
    <session>/subagents/ directory a workflow run directory `path` was
    launched from (docs/DESIGN.md 4.2, 4.4, 4.13.3). None when `path` was
    not reached through that standard layout (a flat fixture, or a bare
    directory passed directly on the command line) -- there is simply no
    parent to attribute against, the same as one that does not exist yet.
    Takes a bare path (never a Run) so it works equally from an already-
    loaded Run's own run.path (_parent_transcript_path, below) and from
    workflow_lifecycle's computation straight off the run directory, with
    no Run object required."""
    parts = path.parts
    if "subagents" not in parts:
        return None
    idx = parts.index("subagents")
    if idx < 1:
        return None
    session_dir = Path(*parts[:idx])
    return session_dir.parent / f"{session_dir.name}.jsonl"


def _parent_transcript_path(run: Run) -> Path | None:
    """The main session transcript a killed attempt's cause is attributed
    against (docs/DESIGN.md 4.2, 4.4): run.path itself for a session (its
    subagents' parent is the session's own main transcript), or
    _parent_transcript_path_for_workflow_dir(run.path) for a workflow."""
    if run.kind == "session":
        return run.path
    return _parent_transcript_path_for_workflow_dir(run.path)


def parent_transcript_path(run: Run) -> Path | None:
    """Public wrapper around _parent_transcript_path, for a detector
    (docs/DESIGN.md 4.13.3's workflow_failed, N2) that needs the parent
    transcript's own path to build a 'path:line' evidence string without
    re-deriving the standard-layout rule itself."""
    return _parent_transcript_path(run)


def lifecycle_terminal_line(run: Run) -> int | None:
    """The physical parent-transcript line that decided run.lifecycle's own
    terminal (docs/DESIGN.md 4.13.3), read straight off the private
    `_terminal_line` already captured in the exact snapshot that produced
    `run.lifecycle` (N1N2 review finding 2) -- never re-derived by a fresh
    ParentIndex query, which could by now reflect a newer generation
    appended to the parent transcript after load_run() computed
    `run.lifecycle`. Lifecycle's own public shape still has exactly the five
    fields docs/DESIGN.md 4.1/5.3b document; this reads the one internal
    field alongside them. None when run.lifecycle is None (a session) or
    when no generation ever decided the stored terminal."""
    if run.lifecycle is None:
        return None
    return run.lifecycle._terminal_line


def _parent_index_for(run: Run) -> "ParentIndex":
    """The one parent-index snapshot a run load uses: the run's parent
    transcript's index, or an empty path-less one when there is none."""
    parent_path = _parent_transcript_path(run)
    return (
        ParentIndex.for_path(parent_path)
        if parent_path is not None
        else ParentIndex(None, [], [], [])
    )


def _attribute_interrupts(run: Run, parent_index: "ParentIndex | None" = None) -> None:
    """Fill in cause and cause_evidence on every attempt.interrupt of run
    (docs/DESIGN.md 4.4), via detectors.base.attribute_interrupt. Called
    once a Run's attempts are fully assembled: the runtime cause needs to
    know whether a key has a later attempt, and the parent transcript (if
    any) is scanned once and shared across every attempt here. run.main is
    never touched: whether a main-session interrupt was human is not
    knowable from the transcript (docs/DESIGN.md 4.3's documented limit)."""
    if parent_index is None:
        parent_index = _parent_index_for(run)
    # A malformed or unreadable parent transcript never aborts attribution
    # (it already falls back to 'unattributed' below); its diagnostics still
    # surface on the Run (docs/DESIGN.md section 2's tolerant-parsing rule).
    # This alone does not flip Run.status to INCOMPLETE: docs/DESIGN.md 4.6
    # scopes that to the journal and the transcripts the journal itself
    # names, and the parent transcript consulted here is neither -- it is
    # supplementary evidence for a cause that safely defaults to
    # 'unattributed' when it cannot be read.
    run.diagnostics.extend(parent_index.diagnostics)
    # docs/DESIGN.md 4.13.1 (N4 fix, A4): the discovery list holds only the
    # parent diagnostics that involve this run (a session involves none of the
    # launch ones: its own launch table reports them).
    run.discovery["parent"] = parent_index.discovery_diagnostics_for(
        run.run_id if run.kind == "workflow" else None
    )
    for attempt in run.attempts:
        if attempt.interrupt is None:
            continue
        cause, evidence = attribute_interrupt(run, attempt, parent_index)
        attempt.interrupt.cause = cause
        attempt.interrupt.cause_evidence = evidence


# --- run lifecycle (docs/DESIGN.md 4.13.3, v0.3 N2) -------------------------

# docs/DESIGN.md 4.13.3: Run.finished is true for exactly these lifecycle
# terminals; false for 'unknown'/'none'; None (handled separately by every
# caller) for a session, which never has a Lifecycle at all.
_LIFECYCLE_FINISHED_TERMINALS = frozenset({"completed", "failed", "stopped", "journal_resolved"})


def _lifecycle_from_parts(
    path: Path, run_id: str, journal_resolved: bool,
    parent_index: "ParentIndex | None" = None, after_launch_line: int = 0,
) -> Lifecycle:
    """docs/DESIGN.md 4.13.3's run lifecycle table, evaluated from the newest
    generation (by launch_line) associated with run_id. Needs only the run
    directory, its run id, and whether the journal is resolved -- never an
    attempt's own transcript -- which is what lets workflow_lifecycle (below)
    compute a full Lifecycle straight from a run directory, and lets
    _load_workflow reuse the exact same table logic for an already-loaded
    Run without a second, independently-written copy of it.

    `after_launch_line` (docs/DESIGN.md 4.12, N8b K5.4, watch's journal epochs) leaves out every
    generation whose launch line is not greater than it: the generations of an ended journal
    epoch. 0, the default, leaves out none."""
    if parent_index is None:
        parent_path = _parent_transcript_path_for_workflow_dir(path)
        parent_index = (
            ParentIndex.for_path(parent_path)
            if parent_path is not None
            else ParentIndex(None, [], [], [])
        )
    generations = [
        generation for generation in parent_index.generations_for(run_id)
        if generation.launch_line > after_launch_line
    ]
    activity = _workflow_activity(path)

    # docs/DESIGN.md 4.13.3's Run lifecycle paragraph (N1N2 review findings
    # 1/7): completeness is the cached snapshot's own flag, computed inside
    # ParentIndex's incremental pass as it parsed the file -- never a second,
    # independent full re-read here. A missing parent_path builds a bare,
    # path-less ParentIndex above, whose inspection_complete defaults to
    # True (vacuous, 4.6's "unreadable, not merely absent" convention).
    if not parent_index.inspection_complete:
        return Lifecycle(
            terminal="unknown", source="parent_unavailable", activity=activity,
            generations=len(generations), diagnostics=[],
            _generations=list(generations),
        )

    ambiguous_diagnostics = parent_index.ambiguous_diagnostics_for(run_id)
    if ambiguous_diagnostics:
        return Lifecycle(
            terminal="unknown", source="ambiguous_launch", activity=activity,
            generations=len(generations), diagnostics=list(ambiguous_diagnostics),
            _generations=list(generations),
        )

    newest = max(generations, key=lambda generation: generation.launch_line, default=None)

    if newest is not None and newest.terminal is not None:
        return Lifecycle(
            terminal=newest.terminal, source=newest.terminal_source, activity=activity,
            generations=len(generations), diagnostics=list(newest.diagnostics),
            # N1N2 review finding 2: captured in this same snapshot so
            # workflow_failed's evidence never drifts to a newer generation
            # a later ParentIndex refresh might see.
            _terminal_line=newest.terminal_line,
            _generations=list(generations),
        )

    if journal_resolved:
        return Lifecycle(
            terminal="journal_resolved", source="journal", activity=activity,
            generations=len(generations), diagnostics=[],
            _generations=list(generations),
        )

    return Lifecycle(
        terminal="none", source=None, activity=activity,
        generations=len(generations), diagnostics=[],
        _generations=list(generations),
    )


def workflow_lifecycle(path: Path) -> Lifecycle:
    """docs/DESIGN.md section 3 and 4.13.3 (N2 root-cause fix): the one
    public function that computes a workflow run directory's Lifecycle from
    the run directory alone -- journal.jsonl, the parent index, and the run
    directory's own file mtimes -- parsing no agent transcript. _load_workflow
    calls this for Run.lifecycle, and hooks_cli._workflow_finished (7.2)
    calls this too (through lifecycle_finished), so a hook's notion of
    'finished' is never a second, independently maintained rule -- the same
    "one function is the only definition of this rule" docs/DESIGN.md
    section 3 states for Run.finished itself.

    `path` need not exist yet, and its journal.jsonl need not either (a
    workflow directory can exist before its journal does): both are read the
    same tolerant way _load_workflow's own journal parsing is."""
    path = Path(path)
    run_id = _valid_workflow_run_id(path.name, [])
    journal, _started_events, _journal_ok, journal_line_numbers = _read_journal(path, [])
    journal_attempts, _diagnostics = _run_journal_state_machine(journal, journal_line_numbers)
    journal_resolved = _workflow_finished_from_occurrences(journal_attempts)
    return _lifecycle_from_parts(path, run_id, journal_resolved)


def lifecycle_from_journal_state(
    path: Path, run_id: str, journal_resolved: bool, parent_index: "ParentIndex",
    *, after_launch_line: int = 0,
) -> Lifecycle:
    """docs/DESIGN.md 4.12, 4.13.3 (N8b K5.4): the run lifecycle table evaluated from a fact the
    caller already has. `watch` follows a growing journal incrementally, so it hands in
    `journal_resolved` computed from its own in-memory occurrences and the parent index it
    refreshes itself, instead of paying for `workflow_lifecycle`'s full journal read and
    state-machine run on every poll. `after_launch_line` leaves out the generations of an ended
    journal epoch (see `_lifecycle_from_parts`)."""
    return _lifecycle_from_parts(
        Path(path), run_id, journal_resolved, parent_index, after_launch_line,
    )


def journal_resolved_from_occurrences(occurrences: list) -> bool:
    """The journal-only `finished` fact (docs/DESIGN.md 4.1, 4.12) for occurrences a caller
    already holds from `_run_journal_state_machine`: true when every key the journal started has
    no open attempt."""
    return _workflow_finished_from_occurrences(occurrences)


def lifecycle_finished(lifecycle: Lifecycle) -> bool:
    """docs/DESIGN.md section 3 (N2 root-cause fix): the one rule for whether
    a workflow run's own Lifecycle counts as finished -- true for
    'completed', 'failed', 'stopped', and 'journal_resolved'; false for
    'unknown' and 'none'. Lives next to _LIFECYCLE_FINISHED_TERMINALS so
    _load_workflow's Run.finished and hooks_cli's workflow-unit finished
    check both call this one function instead of each re-deriving the
    terminal set on its own."""
    return lifecycle.terminal in _LIFECYCLE_FINISHED_TERMINALS


def attempt_unresolved_in_journal(journal: list[dict], key: str | None, ordinal: int) -> bool:
    """Public counterpart of the per-key journal state machine's own
    closed_by (docs/DESIGN.md 4.13.3): true when the ordinal-th occurrence of
    key was never closed by a 'result' or 'failed' event -- still open at the
    end of the journal the reader actually read. report.py's AGENT table END
    column (4.7) uses this to show 'no result' instead of the 'completed'
    _compute_outcome falls back to for an open attempt whose own transcript
    simply has not (yet) ended in the kill sentinel."""
    if key is None:
        return False
    occurrences, _diagnostics = _run_journal_state_machine(journal)
    for occurrence in occurrences:
        if occurrence.key == key and occurrence.ordinal == ordinal:
            return occurrence.closed_by is None
    return False


# --- identity: the run cwd set and agent ids (docs/DESIGN.md 4.13.6, N6) -----------


def run_cwd_set(run: Run) -> frozenset[str]:
    """Every top-level non-empty string `cwd` the run's own transcripts carried: a
    session's main transcript and each child attempt's, or a workflow run's attempt
    transcripts. Never the journal, a meta file, or a workflow's parent session
    transcript (the reader collects `Attempt.entry_cwds` only from an attempt's own
    transcript)."""
    cwds: set[str] = set()
    if run.main is not None:
        cwds.update(run.main.entry_cwds)
    for attempt in run.attempts:
        cwds.update(attempt.entry_cwds)
    return frozenset(cwds)


def run_matches_cwd(run: Run, caller: str) -> bool:
    """`--here` containment (docs/DESIGN.md 4.13.11, N6): both the caller's directory and
    each candidate cwd are made absolute and normalized (no `~` expansion, no symlink
    resolution); the run matches when the caller equals or lies below some candidate.
    A candidate that cannot be compared (ValueError, OSError) is no match for that
    candidate, and a run with an empty cwd set never matches."""
    candidates = run_cwd_set(run)
    if not candidates:
        return False
    try:
        caller_path = os.path.normpath(os.path.abspath(caller))
    except (ValueError, OSError):
        return False
    for candidate in candidates:
        try:
            candidate_path = os.path.normpath(os.path.abspath(candidate))
            if os.path.commonpath([candidate_path, caller_path]) == candidate_path:
                return True
        except (ValueError, OSError):
            continue
    return False


def caller_workflow_paths(
    root: Path, session_id: str | None, caller: str | None,
) -> frozenset[Path]:
    """The workflow run directories `--here` adds for the caller's own session (docs/DESIGN.md
    4.13.6, G5): the `wf_*` directories under `<project>/<session>/subagents/workflows` of the one
    session run under `root` whose transcript stem equals `session_id` (a canonical UUID,
    lowercase), when that session run itself passes `run_matches_cwd` for `caller`. Empty for no
    id, no caller directory, no or several such session runs, a session that does not work here,
    or a session that cannot be loaded."""
    if session_id is None or caller is None or not _UUID_RE.fullmatch(session_id):
        return frozenset()
    owners = [
        ref for ref in discover_runs(Path(root), kinds=frozenset({"session"}))
        if ref.path.stem.lower() == session_id.lower()
    ]
    if len(owners) != 1:
        return frozenset()
    owner = owners[0]
    try:
        run = load_run(owner)
    except NotARun:
        return frozenset()
    if not run_matches_cwd(run, caller):
        return frozenset()
    workflows = owner.path.parent / owner.path.stem / "subagents" / "workflows"
    try:
        return frozenset(path for path in workflows.glob("wf_*") if _is_dir(path))
    except OSError:
        return frozenset()


def agent_id_candidates(ref: RunRef) -> frozenset[str]:
    """A superset, found without parsing any transcript, of every agent id the loader
    can turn into an attempt of this run (docs/DESIGN.md 4.8, N6). A session: the stem id
    of every `subagents/agent-*.jsonl` the session loader would discover. A workflow:
    the `agentId` string of every journal `started` event `_read_journal` returns, plus
    the stem id of every `agent-*.jsonl` in the run directory. An id that is not
    canonical is dropped; a directory that cannot be listed contributes nothing."""
    found: set[str] = set()
    if ref.kind == "session":
        try:
            paths = _discover_session_agent_paths(ref.path)
        except OSError:
            paths = []
        found.update(path.stem.removeprefix("agent-") for path in paths)
    else:
        _journal, started_events, _ok, _lines = _read_journal(ref.path, [])
        for event in started_events:
            agent_id = event.get("agentId")
            if isinstance(agent_id, str):
                found.add(agent_id)
        try:
            for path in ref.path.glob("agent-*.jsonl"):
                if _is_file(path):
                    found.add(path.stem.removeprefix("agent-"))
        except OSError:
            pass
    return frozenset(agent_id for agent_id in found if _AGENT_ID_RE.fullmatch(agent_id))


def run_holds_agent(ref: RunRef, agent_ids) -> bool:
    """The confirmation step of agent resolution: the run, loaded, holds an attempt
    whose agent id is one of `agent_ids`. A ref that is not a run is no match."""
    wanted = set(agent_ids)
    try:
        run = load_run(ref)
    except NotARun:
        return False
    return any(attempt.agent_id in wanted for attempt in run.attempts)


def runs_holding_agent(refs, agent_id: str) -> list[RunRef]:
    """The refs, in the order given, whose loaded Run holds an attempt with this
    canonical agent id: a cheap candidate check, then the exact confirmation."""
    if not _AGENT_ID_RE.fullmatch(agent_id):
        return []
    return [
        ref for ref in refs
        if agent_id in agent_id_candidates(ref) and run_holds_agent(ref, (agent_id,))
    ]
