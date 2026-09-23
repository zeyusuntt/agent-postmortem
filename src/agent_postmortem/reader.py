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

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .detectors.base import attribute_interrupt, meaningful_event_before
from .markers import (
    BASH_BACKGROUNDED,
    BASH_TIMEOUT,
    CLASSIFIER_UNAVAILABLE,
    GIT_NOT_REPO,
    KILL_SENTINEL_PREFIX,
    SANDBOX_WRITE_DENIED,
    SHELL_GUARD_BLOCK_REGEX,
    STRUCTURED_OUTPUT_MISMATCH,
    TOOL_INPUT_INVALID,
)
from .models import Attempt, Event, Interrupt, LogicalAgent, Run

_EXCERPT_LIMIT = 400
_EXIT_CODE_RE = re.compile(r"[Ee]xit code[: ]+(-?\d+)")

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
_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{16}$")
_WORKFLOW_RUN_ID_RE = re.compile(r"^wf_[0-9a-f]{8}-[0-9a-f]{3}$")


def _valid_agent_id(agent_id: str | None, diagnostics: list[str], context: str) -> str:
    """A conforming agent id (docs/DESIGN.md 5.1: 17 hex chars starting with
    'a') passes through unchanged -- it carries no user text, so it is not
    redacted anywhere else in the tool. A non-conforming one (a corrupted
    journal entry, a hand-edited or foreign transcript filename) is opaque
    for a different reason: it might be anything, including a real name, so
    it is replaced by 'agent-' + sha256(id)[:8] and never carried into a
    Run/Attempt/Finding verbatim. The diagnostic names only the fixed
    `context` and the replacement, never the original value."""
    if agent_id and _AGENT_ID_RE.match(agent_id):
        return agent_id
    replacement = "agent-" + hashlib.sha256((agent_id or "").encode("utf-8")).hexdigest()[:8]
    diagnostics.append(
        f"{context}: agent id did not match the expected format, replaced with {replacement}"
    )
    return replacement


def _valid_workflow_run_id(name: str, diagnostics: list[str]) -> str:
    """A conforming workflow run id (docs/DESIGN.md 5.1:
    'wf_' + 8 hex + '-' + 3 hex) passes through unchanged. A non-conforming
    run directory name is replaced by 'wf_' + sha256(name)[:8] + '-x'; the
    diagnostic never echoes the original directory name."""
    if _WORKFLOW_RUN_ID_RE.match(name or ""):
        return name
    replacement = "wf_" + hashlib.sha256((name or "").encode("utf-8")).hexdigest()[:8] + "-x"
    diagnostics.append(
        f"workflow run directory: run id did not match the expected format, "
        f"replaced with {replacement}"
    )
    return replacement

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
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


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
    if not root.exists():
        return []

    refs: list[RunRef] = []
    if "workflow" in kinds:
        for path in root.glob("*/*/subagents/workflows/wf_*"):
            if path.is_dir():
                refs.append(RunRef(kind="workflow", path=path, run_id=path.name))
    if "session" in kinds:
        session_paths = [
            path
            for path in root.glob("*/*.jsonl")
            if path.is_file() and _UUID_RE.match(path.stem) and _has_timestamped_conversation(path)
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
    if not path.is_file() or path.suffix != ".jsonl":
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
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
    except OSError:
        return False
    return False


def _is_workflow_dir(path: Path) -> bool:
    """A run is a directory with its own journal.jsonl (docs/DESIGN.md
    section 1)."""
    return path.is_dir() and (path / "journal.jsonl").exists()


def _sniff_session_file(path: Path) -> bool:
    """Whether the first *decodable* line of a .jsonl file carries sessionId
    (docs/DESIGN.md 4.2's load_run dispatch: "a .jsonl file whose first
    parsed line carries sessionId"), without parsing the rest of it. A
    malformed physical line ahead of it is skipped, not decisive: it is
    still recorded as a diagnostic once load_session actually reads the
    file, same as any other malformed line."""
    if not path.is_file() or path.suffix != ".jsonl":
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                return isinstance(entry, dict) and "sessionId" in entry
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

    journal, started_events, journal_ok = _read_journal(path, diagnostics)
    incomplete = not journal_ok

    claude_version: str | None = None
    session_id: str | None = None
    all_event_ts: list[datetime] = []
    attempts: list[Attempt] = []
    agents_by_key: dict[str, LogicalAgent] = {}
    ordinal_counts: dict[str | None, int] = {}

    for entry in started_events:
        key = entry.get("key")
        raw_agent_id = entry.get("agentId", "")
        label = entry.get("label", "")
        phase = entry.get("phase", "")

        ordinal_counts[key] = ordinal_counts.get(key, 0) + 1
        ordinal = ordinal_counts[key]

        # Filesystem lookups use the raw journal value: the transcript and
        # meta files on disk are named with it regardless of whether it
        # conforms to the documented id shape. Only the value stored on the
        # Attempt (and everything rendered from it) is validated.
        meta = _load_meta(path, raw_agent_id, diagnostics)
        agent_type = meta.get("agentType")
        agent_id = _valid_agent_id(raw_agent_id, diagnostics, "journal.jsonl")

        transcript_path = path / f"agent-{raw_agent_id}.jsonl"
        if transcript_path.exists():
            transcript_result = _read_transcript_tracked(transcript_path, diagnostics)
            events, version, transcript_session_id, unreadable, over_threshold = transcript_result
            incomplete = incomplete or unreadable or over_threshold
            if claude_version is None and version:
                claude_version = version
            if session_id is None and transcript_session_id:
                session_id = transcript_session_id
        else:
            transcript_path = None
            events = []

        event_ts = [event.ts for event in events if event.ts is not None]
        first_ts = min(event_ts) if event_ts else None
        last_ts = max(event_ts) if event_ts else None
        all_event_ts.extend(event_ts)
        incomplete = incomplete or _edge_event_has_timestamp_diagnostic(events, diagnostics)

        outcome = _compute_outcome(transcript_path, events)
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
        )
        if outcome == "killed":
            attempt.interrupt = _build_interrupt(attempt)
        attempts.append(attempt)

        if key is not None:
            agent = agents_by_key.get(key)
            if agent is None:
                agent = LogicalAgent(key=key, label=label, phase=phase, attempts=[])
                agents_by_key[key] = agent
            agent.attempts.append(attempt)

    # docs/DESIGN.md 7.4's D8: finished when the journal has a 'result' for
    # every 'started' key -- the same rule hooks_cli._workflow_finished
    # uses to decide whether to analyze a unit at all, computed here so a
    # loaded Run can answer the question for itself (scan's display, run's
    # RUNNING fast path).
    started_keys = {
        entry.get("key") for entry in started_events if isinstance(entry.get("key"), str)
    }
    result_keys = {
        entry.get("key") for entry in journal
        if entry.get("type") == "result" and isinstance(entry.get("key"), str)
    }
    finished = bool(started_keys) and started_keys <= result_keys

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
        finished=finished,
    )
    _attribute_interrupts(run)
    return run


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


def _read_journal(path: Path, diagnostics: list[str]) -> tuple[list[dict], list[dict], bool]:
    """journal, started_events, and whether the journal could be parsed
    cleanly (docs/DESIGN.md 4.6): False when the file is missing or any line
    in it failed to decode as a JSON object, in which case Run.status must
    be INCOMPLETE rather than trusting a partial read."""
    journal: list[dict] = []
    started_events: list[dict] = []
    journal_path = path / "journal.jsonl"
    if not journal_path.exists():
        diagnostics.append("journal.jsonl: missing")
        return journal, started_events, False

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
                if entry.get("type") == "started":
                    started_events.append(entry)
    except OSError as exc:
        diagnostics.append(f"journal.jsonl: unreadable: {exc}")
        return journal, started_events, False
    except UnicodeError as exc:
        diagnostics.append(f"journal.jsonl: undecodable: {exc}")
        return journal, started_events, False
    return journal, started_events, ok


def _load_meta_file(meta_path: Path, diagnostics: list[str]) -> dict:
    if not meta_path.exists():
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
    if not subagents_dir.is_dir():
        return []
    return sorted(p for p in subagents_dir.glob("agent-*.jsonl") if p.is_file())


def _parse_main_attempt(
    main_path: Path,
) -> tuple[Attempt, str | None, str | None, list[str], bool, list[datetime]]:
    """(main_attempt, claude_version, session_id, diagnostics, incomplete,
    event_timestamps) for main_path alone, with no subagent involved. The
    building block both load_session (one fresh parse) and
    load_session_view (docs/DESIGN.md 7.2, D6: optionally cached across
    several calls within one hook invocation) share."""
    diagnostics: list[str] = []
    main_events, claude_version, session_id, unreadable, over_threshold = _read_transcript_tracked(
        main_path, diagnostics
    )
    incomplete = unreadable or over_threshold
    main_ts = [event.ts for event in main_events if event.ts is not None]
    incomplete = incomplete or _edge_event_has_timestamp_diagnostic(main_events, diagnostics)
    main_outcome = _compute_outcome(main_path, main_events)
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
        last_ts=max(main_ts) if main_ts else None,
        ordinal=1,
        outcome=main_outcome,
        usage=_sum_usage(main_events),
    )
    if main_outcome == "killed":
        main_attempt.interrupt = _build_interrupt(main_attempt)
    return main_attempt, claude_version, session_id, diagnostics, incomplete, main_ts


def _load_session_from_paths(
    main_path: Path, agent_paths: list[Path], *, main_cache: dict | None = None
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
    diagnostics.extend(main_diagnostics)

    all_ts = list(main_ts)
    agents: list[LogicalAgent] = []
    attempts: list[Attempt] = []

    for agent_path in agent_paths:
        agent_path = Path(agent_path)
        agent_id = _valid_agent_id(
            agent_path.stem.removeprefix("agent-"), diagnostics, "session subagent"
        )
        meta_path = agent_path.with_name(f"{agent_path.stem}.meta.json")
        meta = _load_meta_file(meta_path, diagnostics)
        label = meta.get("description", "")
        if not isinstance(label, str):
            label = ""
        agent_type = meta.get("agentType")
        phase = meta.get("workflowPhase", "")
        if not isinstance(phase, str):
            phase = ""

        events, sub_version, _, sub_unreadable, sub_over_threshold = _read_transcript_tracked(
            agent_path, diagnostics
        )
        incomplete = incomplete or sub_unreadable or sub_over_threshold
        if claude_version is None and sub_version:
            claude_version = sub_version
        event_ts = [event.ts for event in events if event.ts is not None]
        all_ts.extend(event_ts)
        incomplete = incomplete or _edge_event_has_timestamp_diagnostic(events, diagnostics)
        outcome = _compute_outcome(agent_path, events)

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
            last_ts=max(event_ts) if event_ts else None,
            ordinal=1,
            outcome=outcome,
            usage=_sum_usage(events),
        )
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
    )
    _attribute_interrupts(run)
    return run


def load_session(path: Path) -> Run:
    """Parse a session run: the main transcript plus its Agent-tool
    subagent transcripts (docs/DESIGN.md 4.2). Run.main is the main
    transcript as an Attempt (label='main', key=None, ordinal=1); each
    subagents/agent-*.jsonl becomes one Attempt and one LogicalAgent.
    Retries of an Agent-tool subagent are not observable in v0.1: each
    transcript file is exactly one attempt, ordinal 1."""
    path = Path(path)
    return _load_session_from_paths(path, _discover_session_agent_paths(path))


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


def _sum_usage(events: list[Event]) -> dict:
    totals: dict[str, int | float] = {}
    for event in events:
        for key, value in event.usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals


def _parse_ts(
    value: object,
    path: Path | None = None,
    line_no: int | None = None,
    diagnostics: list[str] | None = None,
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
            diagnostics.append(f"{path.name}:{line_no}: timestamp is not a string")
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        if diagnostics is not None and path is not None and line_no is not None:
            diagnostics.append(f"{path.name}:{line_no}: timestamp could not be parsed")
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


def _read_transcript(
    transcript_path: Path, diagnostics: list[str]
) -> tuple[list[Event], str | None, str | None, int, int]:
    """events, version, session_id, and (total_lines, bad_lines): the
    non-blank physical line count and how many of them failed to decode as
    JSON at all (docs/DESIGN.md 4.6's "5 percent of a transcript's lines
    were undecodable"; a line that decodes but is not a JSON object is a
    different, already-diagnosed problem, not an undecodable one)."""
    events: list[Event] = []
    version: str | None = None
    session_id: str | None = None
    tool_names: dict[str, str | None] = {}
    # docs/DESIGN.md 4.3 (D-A): the Bash tool_use command text paired to
    # each tool_use_id, so a later tool_result can be judged read-only
    # without re-reading the tool_use event.
    tool_commands: dict[str, str | None] = {}
    total_lines = 0
    bad_lines = 0
    # Usage dedup is scoped to one transcript (docs/DESIGN.md 4.2): the
    # reader keeps a set of (message.id, requestId) pairs seen while reading
    # this file only, resetting for the next attempt's transcript.
    dedup: dict[tuple[str, str], dict] = {}

    with transcript_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                diagnostics.append(f"{transcript_path.name}:{line_no}: {exc}")
                bad_lines += 1
                continue
            if not isinstance(entry, dict):
                diagnostics.append(f"{transcript_path.name}:{line_no}: not a JSON object")
                continue

            if version is None and isinstance(entry.get("version"), str):
                version = entry["version"]
            if session_id is None and isinstance(entry.get("sessionId"), str):
                session_id = entry["sessionId"]

            raw_type = entry.get("type", "")
            ts = _parse_ts(entry.get("timestamp"), transcript_path, line_no, diagnostics)

            if raw_type == "user":
                events.extend(
                    _user_events(
                        entry, transcript_path, line_no, ts, raw_type,
                        tool_names, tool_commands, diagnostics
                    )
                )
            elif raw_type == "assistant":
                events.extend(
                    _assistant_events(
                        entry, transcript_path, line_no, ts, raw_type,
                        tool_names, tool_commands, dedup, diagnostics
                    )
                )
            elif raw_type == "attachment":
                events.append(
                    _attachment_event(entry, transcript_path, line_no, ts, raw_type, diagnostics)
                )
            elif raw_type == "system" and entry.get("subtype") == "compact_boundary":
                events.append(_compaction_event(transcript_path, line_no, ts, raw_type))
            else:
                events.append(_unknown_event(entry, transcript_path, line_no, ts, raw_type))

    return events, version, session_id, total_lines, bad_lines


# docs/DESIGN.md 4.6: a transcript whose undecodable-line rate exceeds this
# is not merely lossy in a few spots, it is unreliable enough that the run's
# analysis status must say so rather than silently reporting COMPLETE over
# whatever survived.
_UNDECODABLE_LINE_THRESHOLD = 0.05


def _read_transcript_tracked(
    transcript_path: Path, diagnostics: list[str]
) -> tuple[list[Event], str | None, str | None, bool, bool]:
    """_read_transcript, plus the two docs/DESIGN.md 4.6 conditions it alone
    can observe: `unreadable` when the file could not even be opened (a
    permission or IO error, never mere absence -- a caller only reaches this
    for a transcript path it already confirmed exists), and
    `over_threshold` when more than _UNDECODABLE_LINE_THRESHOLD of its lines
    failed to decode."""
    try:
        handle = transcript_path.open("r", encoding="utf-8")
        handle.close()
    except OSError as exc:
        diagnostics.append(f"{transcript_path.name}: unreadable: {exc}")
        return [], None, None, True, False

    events, version, session_id, total_lines, bad_lines = _read_transcript(
        transcript_path, diagnostics
    )
    over_threshold = total_lines > 0 and (bad_lines / total_lines) > _UNDECODABLE_LINE_THRESHOLD
    return events, version, session_id, False, over_threshold


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
    value: object, path: Path, line_no: int, diagnostics: list[str] | None, field: str
) -> dict:
    """value if it is already a dict, else {} with a path:line diagnostic
    (docs/DESIGN.md section 3: the reader tolerates unknown shapes). A
    missing field (None) is the common, harmless case and never diagnosed;
    only a present-but-wrong-shaped field is."""
    if isinstance(value, dict):
        return value
    if value is not None and diagnostics is not None:
        diagnostics.append(
            f"{path.name}:{line_no}: {field} is not an object (got {type(value).__name__})"
        )
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
        diagnostics.append(
            f"{path.name}:{line_no}: {field} is not a string (got {type(value).__name__})"
        )
    return ""


def _user_events(
    entry, path, line_no, ts, raw_type, tool_names, tool_commands=None, diagnostics=None
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
            tool_name = tool_names.get(tool_use_id) if isinstance(tool_use_id, str) else None
            full_text = _flatten_block_text(block.get("content"))
            is_envelope = tool_name in _ENVELOPE_TOOL_NAMES
            events.append(Event(
                path=path, line_no=line_no, ts=ts, kind="tool_result", raw_type=raw_type,
                tool_name=tool_name, text=_excerpt(full_text),
                exit_code=_parse_exit_code(full_text),
                duration_ms=None, is_error=bool(block.get("is_error", False)),
                usage={},
                markers=frozenset() if is_envelope else _scan_markers(full_text),
                envelope=is_envelope,
                read_only_output=_tool_result_read_only(tool_name, tool_use_id, tool_commands),
            ))
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
    entry, path, line_no, ts, raw_type, tool_names, tool_commands=None, dedup=None, diagnostics=None
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
    remaining_usage = _as_object(message.get("usage"), path, line_no, diagnostics, "usage")

    message_id = message.get("id")
    request_id = entry.get("requestId")
    pair = (message_id, request_id) if message_id and request_id else None
    if pair is not None and dedup is not None:
        if pair in dedup:
            if diagnostics is not None and dedup[pair] != remaining_usage:
                diagnostics.append(
                    f"{path.name}:{line_no}: duplicate usage for a (message id, requestId) pair "
                    "differs from its first occurrence"
                )
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
            raw_input = block.get("input", {})
            input_obj = _as_object(raw_input, path, line_no, diagnostics, "tool_use input")
            input_text = json.dumps(input_obj, ensure_ascii=False)
            if isinstance(tool_id, str) and tool_commands is not None and tool_name == "Bash":
                command = input_obj.get("command")
                tool_commands[tool_id] = command if isinstance(command, str) else None
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
    attachment = _as_object(entry.get("attachment"), path, line_no, diagnostics, "attachment")
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


@dataclass(frozen=True)
class _KillSentinel:
    ts: datetime
    line_no: int
    text: str


def _string_values(data: object) -> frozenset[str]:
    if not isinstance(data, dict):
        return frozenset()
    return frozenset(value for value in data.values() if isinstance(value, str))


class ParentIndex:
    """Process-level cache of a main session transcript's TaskStop calls,
    kill sentinels, and compaction timestamps only (docs/DESIGN.md 4.2): the
    minimum a killed attempt's interrupt cause needs from its parent
    transcript, without holding the parent's full content in memory.
    Consulted by detectors.base.attribute_interrupt (4.4) via
    reader._attribute_interrupts, once every attempt of a run is loaded.
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
        # already returned control to its parent.
        self.tool_result_agent_ids = tool_result_agent_ids
        # path:line diagnostics from a malformed parent-transcript entry, or
        # a single "unreadable"/"undecodable" entry when the file itself
        # could not be read (docs/DESIGN.md section 2: tolerant parsing, a
        # bad line becomes a diagnostic, never an exception). Callers fold
        # these into the consuming Run's own diagnostics.
        self.diagnostics: list[str] = diagnostics if diagnostics is not None else []

    @classmethod
    def for_path(cls, path: Path) -> "ParentIndex":
        path = Path(path)
        cached = cls._cache.get(path)
        if cached is not None:
            return cached
        index = cls._scan(path)
        cls._cache[path] = index
        return index

    @classmethod
    def _scan(cls, path: Path) -> "ParentIndex":
        task_stops: list[_TaskStop] = []
        kill_sentinels: list[_KillSentinel] = []
        compaction_timestamps: list[datetime] = []
        diagnostics: list[str] = []
        tool_result_agent_ids: set[str] = set()

        if not path.is_file():
            return cls(path, task_stops, kill_sentinels, compaction_timestamps, diagnostics)

        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue

                    raw_type = entry.get("type")
                    ts = _parse_ts(entry.get("timestamp"))

                    if raw_type == "system" and entry.get("subtype") == "compact_boundary":
                        if ts is not None:
                            compaction_timestamps.append(ts)
                        continue

                    # Tolerant shape validation (docs/DESIGN.md section 2): a
                    # truthy but non-object "message" (for example a list)
                    # must become a diagnostic, never an AttributeError that
                    # aborts the whole scan.
                    message = _as_object(
                        entry.get("message"), path, line_no, diagnostics, "message"
                    )
                    content = message.get("content")

                    if raw_type == "assistant" and isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use" and block.get("name") == "TaskStop":
                                input_obj = block.get("input")
                                input_text = json.dumps(
                                    input_obj if isinstance(input_obj, dict) else {},
                                    ensure_ascii=False,
                                )
                                task_stops.append(_TaskStop(
                                    ts=ts, ids=_string_values(input_obj), line_no=line_no,
                                    text=_excerpt(input_text),
                                ))
                    elif raw_type == "user":
                        text = _flatten_block_text(content)
                        if text.startswith(KILL_SENTINEL_PREFIX) and ts is not None:
                            kill_sentinels.append(
                                _KillSentinel(ts=ts, line_no=line_no, text=_excerpt(text))
                            )
                        # docs/DESIGN.md 7.2 (D2): a tool_result block
                        # carrying the agentId of the child that produced
                        # it -- the parent's own record that a subagent
                        # already returned control to it. Collected in the
                        # same pass as the kill sentinels above, so the
                        # hook's "light index of the main transcript" is
                        # this one scan, not two.
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    not isinstance(block, dict)
                                    or block.get("type") != "tool_result"
                                ):
                                    continue
                                block_agent_id = block.get("agentId")
                                if isinstance(block_agent_id, str) and block_agent_id:
                                    tool_result_agent_ids.add(block_agent_id)
                        # docs/DESIGN.md 7.2 (D-A): in a real main transcript
                        # the Agent tool's result carries agentId at the
                        # ENTRY level (entry["toolUseResult"]), not inside
                        # the tool_result block -- the block itself only
                        # ever has content/tool_use_id/type. The block check
                        # above stays as a fallback for a differently-shaped
                        # transcript; without this, no subagent ever gets
                        # authoritative finished evidence from here at all.
                        tool_use_result = entry.get("toolUseResult")
                        if isinstance(tool_use_result, dict):
                            entry_agent_id = tool_use_result.get("agentId")
                            if isinstance(entry_agent_id, str) and entry_agent_id:
                                tool_result_agent_ids.add(entry_agent_id)
        except OSError as exc:
            diagnostics.append(f"{path.name}: unreadable: {exc}")
        except UnicodeError as exc:
            diagnostics.append(f"{path.name}: undecodable: {exc}")

        return cls(
            path, task_stops, kill_sentinels, compaction_timestamps, diagnostics,
            tool_result_agent_ids=frozenset(tool_result_agent_ids),
        )


def _parent_transcript_path(run: Run) -> Path | None:
    """The main session transcript a killed attempt's cause is attributed
    against (docs/DESIGN.md 4.2, 4.4): run.path itself for a session (its
    subagents' parent is the session's own main transcript), or the
    <root>/<slug>/<session>.jsonl that sits next to the <session>/subagents/
    directory a workflow run was launched from. None when run.path was not
    reached through that standard layout (a flat fixture, or a bare
    directory passed directly on the command line) -- there is simply no
    parent to attribute against, the same as one that does not exist yet."""
    if run.kind == "session":
        return run.path
    parts = run.path.parts
    if "subagents" not in parts:
        return None
    idx = parts.index("subagents")
    if idx < 1:
        return None
    session_dir = Path(*parts[:idx])
    return session_dir.parent / f"{session_dir.name}.jsonl"


def _attribute_interrupts(run: Run) -> None:
    """Fill in cause and cause_evidence on every attempt.interrupt of run
    (docs/DESIGN.md 4.4), via detectors.base.attribute_interrupt. Called
    once a Run's attempts are fully assembled: the runtime cause needs to
    know whether a key has a later attempt, and the parent transcript (if
    any) is scanned once and shared across every attempt here. run.main is
    never touched: whether a main-session interrupt was human is not
    knowable from the transcript (docs/DESIGN.md 4.3's documented limit)."""
    parent_path = _parent_transcript_path(run)
    parent_index = (
        ParentIndex.for_path(parent_path)
        if parent_path is not None
        else ParentIndex(None, [], [], [])
    )
    # A malformed or unreadable parent transcript never aborts attribution
    # (it already falls back to 'unattributed' below); its diagnostics still
    # surface on the Run (docs/DESIGN.md section 2's tolerant-parsing rule).
    # This alone does not flip Run.status to INCOMPLETE: docs/DESIGN.md 4.6
    # scopes that to the journal and the transcripts the journal itself
    # names, and the parent transcript consulted here is neither -- it is
    # supplementary evidence for a cause that safely defaults to
    # 'unattributed' when it cannot be read.
    run.diagnostics.extend(parent_index.diagnostics)
    for attempt in run.attempts:
        if attempt.interrupt is None:
            continue
        cause, evidence = attribute_interrupt(run, attempt, parent_index)
        attempt.interrupt.cause = cause
        attempt.interrupt.cause_evidence = evidence
