"""Claude Code plugin hook entry points (docs/DESIGN.md 7.2).

`agent-postmortem hook <check|session-start|session-end|workflow-launched|
capabilities>` is what plugin/hooks/run.sh invokes for each Claude Code hook
event. Every subcommand except `capabilities` reads one JSON hook payload
from stdin and exits 0 no matter what happens: a hook must never fail a
turn. `dispatch()` is the single try/except boundary -- nothing below it
ever raises out to the caller, and nothing is printed until every state
write for this invocation has already succeeded, so a mid-flight exception
never leaves a half-written notification on stdout.

State (docs/DESIGN.md 7.5) is read and written entirely through
`state.transaction()`/`state.update()`; `StateLocked` (another
agent-postmortem process holds the lock) is just another exception
`dispatch()` swallows into a silent exit 0.

Units and their fingerprints, the four unit states (running, finished,
unknown, analyzed), the finished rules for a workflow unit and a subagent
unit, the 3-stable-check unknown rule, and the notification policy are all
implemented here as docs/DESIGN.md 7.2 describes them. A few elaborations
the prose leaves implicit are pinned down concretely here:

- State is namespaced by root identity plus session id, not session id
  alone: `_session_state_key(root, session_id)` is
  `sha256(str(root.resolve()))[:8] + ":" + session_id`. Two different roots
  (two machines' state.json merged, two checkouts) that happen to reuse the
  same session id can never collide or share a unit's analyzed state.
- A `digest.pending` entry (docs/DESIGN.md 7.5) is a session state key
  qualified with its unit id as `"<session-key>::<unit-id>"` -- a bare unit
  id is only unique within one session, and the whole point of the digest
  is to look across every session under one root.
- Once a unit is analyzed, its state entry additionally carries `verdict`,
  `wasted_seconds`, and `run_id` (beyond the {fingerprint, state,
  stable_checks, reported} docs/DESIGN.md 7.2 names) so `session-start`'s
  digest can summarize it without re-parsing the run from disk. A workflow
  unit registered by `workflow-launched` before its directory exists may
  also carry `transcript_dir`, the locator it was given.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path

from . import config, detectors, disclosure, reader, report, state
from .markers import KILL_SENTINEL_PREFIX
from .redact import Redactor

# docs/DESIGN.md 7.2: AGENT_POSTMORTEM_NOTIFY in {critical, major, minor,
# info}, default major.
_SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "critical": 3}
_DEFAULT_NOTIFY = "major"

# docs/DESIGN.md 7.2's workflow-launched fallback: a "Run ID: wf_..." match
# inside tool_response text when it carries no structured runId.
_RUN_ID_RE = re.compile(r"Run ID:\s*(wf_[A-Za-z0-9-]+)")

# docs/DESIGN.md 4.6: a unit whose fingerprint has not moved for this many
# consecutive checks, with no terminal evidence yet, becomes 'unknown'.
_UNKNOWN_AFTER_STABLE_CHECKS = 3

# docs/DESIGN.md 12's D-A: the stable-check rule alone is not enough --
# real data showed the plain 3-stable-check rule finalizing a workflow that
# was still actively running, because three Stop checks happened to land
# within the same quiet minute. A unit may only cross into 'unknown' once
# it has ALSO gone quiet for a while: the newest mtime among its own files
# at least this many seconds old. 600 = 10 minutes.
_UNKNOWN_QUIET_SECONDS = 600

# session-start analyzes at most this many newly finished units across the
# whole root in one digest (docs/DESIGN.md 7.2).
_DIGEST_ANALYSIS_CAP = 20

# Separator between a session's state key and a unit id inside a
# digest.pending entry.
_DIGEST_SEPARATOR = "::"
# Separator between a root's hash and a session id inside a session state
# key (docs/DESIGN.md 7.5).
_ROOT_KEY_SEPARATOR = ":"

# docs/DESIGN.md 7.2's background_tasks status field: values that mean a
# named task has actually finished, not merely that it was mentioned.
_FINISHED_TASK_STATUSES = frozenset({"completed", "finished", "done"})


def _notify_threshold() -> str:
    value = os.environ.get("AGENT_POSTMORTEM_NOTIFY", "").strip().lower()
    return value if value in _SEVERITY_RANK else _DEFAULT_NOTIFY


# --- stdin / payload -------------------------------------------------------


def _read_payload() -> dict | None:
    """The hook JSON on stdin, or None for empty or malformed stdin (docs/
    DESIGN.md 7.2: exit 0, no output, in either case)."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_subagent_context(payload: dict) -> bool:
    """docs/DESIGN.md 7.2's D9: presence alone, regardless of value or
    type. An empty string, null, or a non-string agent_id all still mean
    this payload names a subagent -- the only payloads with no agent_id key
    at all are the ones a top-level session's own hooks ever receive."""
    return "agent_id" in payload


def _resolve_session_id(payload: dict) -> str | None:
    """The session id always comes from transcript_path's own filename stem
    (docs/DESIGN.md 7.5's D3); a payload session_id that disagrees is
    ignored; there is no fallback."""
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        return Path(transcript).stem
    return None


def _session_dir_from_payload(payload: dict) -> Path | None:
    """docs/DESIGN.md 7.2: parent of transcript_path is the slug directory,
    its stem is the session id, units live under <slug>/<session>/subagents/.
    None (exit 0 silently) when transcript_path is missing/not a string, or
    that subagents/ directory does not exist yet. Used only by `check`
    (docs/DESIGN.md 7.2's D4): session-end, session-start, and
    workflow-launched use `_session_dir_plain` instead, since each of them
    must still act on a session that has never spawned a subagent."""
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    transcript_path = Path(transcript)
    session_dir = transcript_path.parent / transcript_path.stem
    if not (session_dir / "subagents").is_dir():
        return None
    return session_dir


def _session_dir_plain(payload: dict) -> Path | None:
    """docs/DESIGN.md 7.2's D4: session-end, session-start, and
    workflow-launched need only transcript_path naming an existing file --
    plain Path arithmetic, no subagents/ directory check. Unlike `check`
    (`_session_dir_from_payload`), these three must still act on a session
    that has never spawned a subagent (the common case): session-end must
    still analyze the main thread, session-start must still be able to
    compute `root` for its cross-session sweep, and workflow-launched must
    still be able to register a unit before its own subagents/ directory
    even exists (docs/DESIGN.md 7.2's D8)."""
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    transcript_path = Path(transcript)
    if not transcript_path.is_file():
        return None
    return transcript_path.parent / transcript_path.stem


def _root_key(root: Path) -> str:
    """A short, stable fingerprint of a root directory's identity (docs/
    DESIGN.md 7.5's D3), so state.json can hold units from more than one
    root without a session id collision between them ever being possible."""
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:8]


def _session_state_key(root: Path, session_id: str) -> str:
    return f"{_root_key(root)}{_ROOT_KEY_SEPARATOR}{session_id}"


# --- units: discovery, fingerprint, finished-content rules ------------------


def _unit_files(kind: str, path: Path) -> list[Path]:
    """The files whose (name, size, mtime_ns) make up a unit's fingerprint
    (docs/DESIGN.md 7.2): every file directly inside a workflow directory,
    or a subagent transcript plus its sibling .meta.json when one exists."""
    if kind == "workflow":
        try:
            return sorted((p for p in path.iterdir() if p.is_file()), key=lambda p: p.name)
        except OSError:
            return []
    if kind == "subagent":
        files = [path]
        meta = path.with_name(f"{path.stem}.meta.json")
        if meta.exists():
            files.append(meta)
        return files
    return [path]  # a lone transcript (session-end's main-session unit)


def _fingerprint(files: list[Path]) -> list[list]:
    entries: list[list] = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        entries.append([f.name, st.st_size, st.st_mtime_ns])
    entries.sort(key=lambda entry: entry[0])
    return entries


def _quiet_long_enough(files: list[Path]) -> bool:
    """docs/DESIGN.md 12's D-A: whether every one of a unit's own files
    was last written at least _UNKNOWN_QUIET_SECONDS ago. A unit with no
    statable files at all (already gone by the time this runs) has nothing
    left to wait on, so it counts as quiet."""
    newest: float | None = None
    for f in files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return True
    return (time.time() - newest) >= _UNKNOWN_QUIET_SECONDS


def _kind_from_unit_id(unit_id: str) -> str:
    return "workflow" if unit_id.startswith("subagents/workflows/") else "subagent"


def _discover_units(session_dir: Path) -> list[tuple[str, str, Path]]:
    """(unit_id, kind, path) for every subagents/workflows/wf_* directory and
    every subagents/agent-*.jsonl file, unit_id relative to session_dir."""
    units: list[tuple[str, str, Path]] = []
    workflows_dir = session_dir / "subagents" / "workflows"
    if workflows_dir.is_dir():
        try:
            wf_dirs = sorted(
                p for p in workflows_dir.iterdir() if p.is_dir() and p.name.startswith("wf_")
            )
        except OSError:
            wf_dirs = []
        for wf_dir in wf_dirs:
            units.append((f"subagents/workflows/{wf_dir.name}", "workflow", wf_dir))
    subagents_dir = session_dir / "subagents"
    if subagents_dir.is_dir():
        try:
            agent_files = sorted(subagents_dir.glob("agent-*.jsonl"))
        except OSError:
            agent_files = []
        for agent_file in agent_files:
            if agent_file.is_file():
                units.append((f"subagents/{agent_file.name}", "subagent", agent_file))
    return units


def _iter_json_lines(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(entry, dict):
                yield entry


def _workflow_finished(journal_path: Path) -> bool:
    """docs/DESIGN.md 7.2 and section 3 (N2 root-cause fix): a workflow unit
    is finished iff Run.finished would be true for it -- reader.
    workflow_lifecycle computes the exact same Lifecycle _load_workflow
    derives Run.lifecycle from, from the run directory alone (journal,
    parent index, directory mtimes), parsing no agent transcript; reader.
    lifecycle_finished then applies the one 'finished' rule to it. This
    hook and reader.py's own Run.finished can therefore never again
    disagree about what 'finished' means the way they used to: the parent
    can report a terminal (failed/stopped) before the journal's own open
    key ever closes, or the journal can be fully resolved while an
    ambiguous launch leaves the lifecycle unknown, and a journal-only rule
    (v0.1's own, and the N2 checkpoint's unintentional revival of it here)
    gets either case backwards. `journal_path` is still a *journal* path
    (every call site and the existing unit tests pass one) -- its parent
    directory is the workflow run directory workflow_lifecycle needs, and
    is derived here rather than changing every caller's own signature.
    workflow_lifecycle already tolerates a run directory whose journal.jsonl
    is missing or empty (a workflow directory can exist before its journal
    does) the same tolerant way _load_workflow's own journal parsing does,
    so this never raises for that case either."""
    return reader.lifecycle_finished(reader.workflow_lifecycle(journal_path.parent))


def _entry_user_text(entry: dict) -> str:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
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
    return ""


def _subagent_ends_with_kill_sentinel(path: Path) -> bool:
    """docs/DESIGN.md 7.2's D2: whether the transcript's own last physical
    line is a kill sentinel. A trailing line that fails to parse as JSON at
    all (a partial write still in flight) means not finished -- it is never
    skipped in favor of an earlier, valid line."""
    last_line: str | None = None
    try:
        with reader._transcript_reader(path) as lines:
            for _line_no, raw_line, torn in lines:
                if torn:
                    # docs/DESIGN.md 4.1 (G-C, GC-1): a last line cut inside a character is a
                    # partial write still in flight: not finished
                    return False
                stripped = raw_line.strip()
                if stripped:
                    last_line = stripped
    except (OSError, UnicodeError):
        return False
    if last_line is None:
        return False
    try:
        last = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(last, dict) or last.get("type") != "user":
        return False
    return _entry_user_text(last).startswith(KILL_SENTINEL_PREFIX)


def _background_task_status(tasks: object, agent_id: str) -> str | None:
    """docs/DESIGN.md 7.2's D2: 'running' when a background_tasks entry
    names agent_id without indicating it is done (the existing, purely
    negative signal: presence alone means still active), 'finished' when an
    entry names it and carries a status/finished flag saying so, or None
    when the payload says nothing about this agent id at all."""
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if isinstance(task, str):
            if task == agent_id:
                return "running"
            continue
        if not isinstance(task, dict):
            continue
        names_agent = any(value == agent_id for value in task.values() if isinstance(value, str))
        if not names_agent:
            continue
        status = task.get("status")
        if isinstance(status, str) and status.strip().lower() in _FINISHED_TASK_STATUSES:
            return "finished"
        if task.get("finished") is True:
            return "finished"
        return "running"
    return None


def _unit_fingerprint(
    kind: str, path: Path, main_transcript: Path, main_cache: dict,
) -> list[list]:
    """The unit's fingerprint (docs/DESIGN.md 7.2): its files' (name, size, mtime_ns) and, for a
    subagent (H1, v0.3.1), a digest of the main-side evidence for the child (its launch and
    resume ids, lines, timestamps and result classes, and its accepted notifications) and,
    when the child is nested, the launcher transcript's size, mtime_ns and inode. GA-3: also the
    child's current (return_state, return_reason) and a stable token for each of the main
    transcript, the meta file and a nested launcher that cannot be read (and for a decision that
    raised), so a read failure that leaves every file's size and mtime alone still changes it."""
    entries = _fingerprint(_unit_files(kind, path))
    if kind != "subagent":
        return entries
    try:
        decision, digest, launcher, unreadable = reader.session_child_return(
            main_transcript, path, main_cache
        )
    except Exception:
        entries.append(["#unreadable:decision", 1, 0])
        entries.sort(key=lambda entry: entry[0])
        return entries
    entries.append(["#main", len(digest), int(digest, 16)])
    word = f"{decision.state}/{decision.reason}".encode("ascii", "replace")
    entries.append(["#return", len(word), int(hashlib.sha256(word).hexdigest()[:12], 16)])
    for token in unreadable:
        entries.append([f"#unreadable:{token}", 1, 0])
    if launcher is not None:
        try:
            st = launcher.stat()
            entries.append([f"#launcher:{st.st_ino}", st.st_size, st.st_mtime_ns])
        except OSError:
            entries.append(["#launcher:missing", 0, 0])
    entries.sort(key=lambda entry: entry[0])
    return entries


def _subagent_finished_evidence(
    path: Path, agent_id: str, payload: dict, main_transcript: Path, main_cache: dict,
) -> bool:
    """docs/DESIGN.md 7.2's D2 and H1: a subagent unit is finished only with
    authoritative evidence -- its last line is a kill sentinel, or its return
    state (4.13.1, T0 to T5, read from the main transcript and, for a nested
    child, its launcher read as evidence only) is `returned`,
    `reported_failed` or `reported_killed`, or the payload itself lists it as
    finished. An assistant text entry with no tool_use is never evidence: it
    is also the ordinary shape of an agent still waiting for its next turn. A
    payload entry that names this agent id as still running is checked first
    and overrides every other signal -- the runtime's own live view wins."""
    task_status = _background_task_status(payload.get("background_tasks"), agent_id)
    if task_status == "running":
        return False
    if _subagent_ends_with_kill_sentinel(path):
        return True
    # docs/DESIGN.md 7.2 (H1, v0.3.1): finished only when the child's return state (4.13.1,
    # T0 to T5) is terminal; an asynchronous launch with no notification, a resume in
    # progress and an unestablished return are not
    try:
        decision, _digest, _launcher, _unreadable = reader.session_child_return(
            main_transcript, path, main_cache
        )
    except Exception:
        # fail closed: evidence that cannot be read is not evidence of a return
        return task_status == "finished"
    if decision.state in ("returned", "reported_failed", "reported_killed"):
        return True
    return task_status == "finished"


# --- analysis: turning a finished unit into (run_id, findings, status, headline,
# failed), where `failed` is the run's own lifecycle terminal being `failed`
# (docs/DESIGN.md 4.6, 4.13.4: the verdict word's FAILED condition) ------------


def _analyze_finished_workflow(path: Path) -> tuple[str, list, str, str, bool]:
    run = reader.load_run(path)
    findings = detectors.run_detectors(run)
    headline = report.summary_line(run).split(";", 1)[0].strip()
    return run.run_id, findings, run.status, headline, report.lifecycle_failed(run)


def _analyze_finished_subagent(
    path: Path, main_transcript: Path, main_cache: dict
) -> tuple[str, list, str, str, bool]:
    """docs/DESIGN.md 7.2's D6: analyzed in isolation via
    reader.load_session_view(main_transcript, [path], ...) -- main plus only
    this one subagent, never the whole session's subagents/ directory, so an
    unrelated sibling's parse trouble can never leak into this unit's
    status, and analyzing N subagents never costs O(N session-wide parses).
    main_cache lets several subagents analyzed in the same hook invocation
    share one parse of the main transcript."""
    view = reader.load_session_view(main_transcript, [path], main_cache=main_cache)
    attempt = next((a for a in view.attempts if a.transcript_path == path), None)
    if attempt is None:
        raise LookupError(f"no attempt parsed for subagent transcript {path}")
    logical_agent = next((a for a in view.agents if attempt in a.attempts), None)
    # Isolation comes from what load_session_view parsed (main plus this one
    # subagent), not from filtering afterward; the agent_id filter here only
    # keeps a main-thread tooling finding (agent_id is the main uuid, or
    # None) from being attributed to this subagent's own unit.
    findings = [f for f in detectors.run_detectors(view) if f.agent_id == attempt.agent_id]
    # A synthetic single-subagent view of the session so the headline (docs/
    # DESIGN.md 7.2: "the run summary line's first clause") describes this
    # one subagent, not the whole session's totals.
    synthetic = replace(
        view,
        attempts=[attempt],
        agents=[logical_agent] if logical_agent is not None else [],
        started_at=attempt.first_ts,
        ended_at=attempt.last_ts,
    )
    headline = report.summary_line(synthetic).split(";", 1)[0].strip()
    # A session has no lifecycle, so a subagent unit is never failed by one.
    return view.run_id, findings, view.status, headline, report.lifecycle_failed(view)


# --- per-unit state transition ---------------------------------------------


@dataclass
class _NotifyInfo:
    """docs/DESIGN.md 7.2's D-C: the pieces of a would-be notify line, kept
    structured (not pre-formatted into text) so _cmd_check can group every
    unit that shares one run_id into a single line instead of one line per
    unit.

    is_incomplete and quiet_unfinished are deliberately two separate flags,
    not one: is_incomplete means the reader itself found real diagnostics
    (a bad journal, an unreadable transcript, ...) or the forced analysis
    attempt outright raised -- a genuine "could not fully inspect this"
    (docs/DESIGN.md 12's D-C: suffix ' (inspection incomplete)').
    quiet_unfinished means the unit was finalized purely because it went
    quiet (D-A's stable-check + mtime fallback) with no such diagnostics of
    its own -- the content genuinely may still be running, just quiet
    (suffix ' (unfinished)'). The two are mutually exclusive in practice
    (see _check_one_unit) but kept as separate booleans rather than an
    enum so a default of False on the plain, fully-analyzed path needs no
    third case to spell out."""

    run_id: str
    verdict_word: str
    headline: str
    is_incomplete: bool
    finding_count: int
    quiet_unfinished: bool = False
    # N3b P17: the canonical agent ids behind the findings that triggered
    # this notification, in finding order; rendered as stable aliases.
    agent_ids: tuple[str, ...] = ()


@dataclass
class _UnitOutcome:
    entry: dict
    notify: _NotifyInfo | None
    newly_analyzed: bool = False


def _fallback_run_id(kind: str, path: Path) -> str:
    """An identifier to notify with when a unit can never actually be
    analyzed (docs/DESIGN.md 7.2's D6: the forced-INCOMPLETE analysis
    attempt itself raised). A workflow directory's name is used only when it
    passes the reader's validator (N3b C2), else the replacement `run` uses
    for it; a subagent has no such name of its own, so one is
    derived the same way a session's public id is (a short hash), prefixed
    the same way (docs/DESIGN.md 4.2's 's_')."""
    if kind == "workflow":
        return reader.valid_workflow_run_id(path.name)
    return "s_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]


def _entry_skeleton(
    fingerprint: list[list], state_word: str, stable_checks: int, *,
    reported: list[str], priority: bool, transcript_dir: str | None,
) -> dict:
    entry: dict = {
        "fingerprint": fingerprint,
        "state": state_word,
        "stable_checks": stable_checks,
        "reported": reported,
    }
    if priority:
        entry["priority"] = True
    if transcript_dir:
        entry["transcript_dir"] = transcript_dir
    return entry


def _notified_agent_ids(
    shown: list, reported_prev: list[str], threshold_rank: int,
) -> tuple[str, ...]:
    """The canonical agent ids of the findings a notification is about, in
    finding order, each once (a non-canonical or missing id is never named)."""
    ids: list[str] = []
    for finding in shown:
        if _SEVERITY_RANK.get(finding.severity, 0) < threshold_rank:
            continue
        if finding.finding_id in reported_prev:
            continue
        agent_id = finding.agent_id
        if disclosure.is_canonical_id("agent", agent_id) and agent_id not in ids:
            ids.append(agent_id)
    return tuple(ids)


def _check_one_unit(
    kind: str, path: Path, prev: dict | None, payload: dict,
    main_transcript: Path, main_cache: dict, cfg: config.Config,
) -> _UnitOutcome:
    prev_state = prev.get("state") if prev else None
    prev_fp = prev.get("fingerprint") if prev else None
    reported_prev = list(prev.get("reported", [])) if prev else []
    priority = bool(prev.get("priority")) if prev else False
    transcript_dir = prev.get("transcript_dir") if prev else None

    def _skeleton(fingerprint, state_word, stable_checks) -> dict:
        return _entry_skeleton(
            fingerprint, state_word, stable_checks,
            reported=reported_prev, priority=priority, transcript_dir=transcript_dir,
        )

    files = _unit_files(kind, path)
    current_fp = _unit_fingerprint(kind, path, main_transcript, main_cache)
    fp_unchanged = prev is not None and prev_fp == current_fp

    # docs/DESIGN.md 7.2's D1: an analyzed unit is re-statted every check.
    # Unchanged, it is left exactly as it was; changed (appended to,
    # deleted and recreated under the same id, ...), it is re-opened with
    # stable_checks reset, keeping its reported finding ids so a finding
    # already surfaced is never re-notified just because the surrounding
    # content moved.
    if prev_state == "analyzed" and fp_unchanged:
        return _UnitOutcome(entry=dict(prev), notify=None)
    # GA-8: a changed analyzed unit is re-opened below with stable_checks reset, and its finished
    # evidence is evaluated in this same check (it is not parked as `running` first).

    # docs/DESIGN.md 7.2's D7: fingerprint first. An unchanged unit advances
    # stable_checks without reparsing anything, unless this is the one
    # check that crosses the unknown threshold -- that check still needs a
    # real parse so the forced-INCOMPLETE analysis has real findings. D-A:
    # crossing the stable-check count alone is not enough to finalize --
    # the unit must also have gone quiet (_quiet_long_enough); short of
    # that it stays pending, however high stable_checks climbs.
    if fp_unchanged:
        stable_checks = prev.get("stable_checks", 0) + 1
        if stable_checks < _UNKNOWN_AFTER_STABLE_CHECKS or not _quiet_long_enough(files):
            return _UnitOutcome(entry=_skeleton(current_fp, "running", stable_checks), notify=None)
        state_word = "unknown"
    else:
        stable_checks = 0
        state_word = None

    if state_word != "unknown":
        if kind == "workflow":
            content_finished = _workflow_finished(path / "journal.jsonl")
        else:
            agent_id = path.stem.removeprefix("agent-")
            content_finished = _subagent_finished_evidence(
                path, agent_id, payload, main_transcript, main_cache
            )
        state_word = "finished" if content_finished else "running"

    if state_word not in ("finished", "unknown"):
        return _UnitOutcome(entry=_skeleton(current_fp, state_word, stable_checks), notify=None)

    try:
        if kind == "workflow":
            run_id, findings, status, headline, failed = _analyze_finished_workflow(path)
        else:
            run_id, findings, status, headline, failed = _analyze_finished_subagent(
                path, main_transcript, main_cache
            )
    except Exception:
        if os.environ.get("AGENT_POSTMORTEM_DEBUG"):
            # docs/DESIGN.md 7.2's D10: a redacted traceback, never the raw
            # one -- it can quote a path or a session id straight out of
            # the transcript that failed to parse.
            redactor = Redactor()
            print(redactor.scrub_leftover_paths(traceback.format_exc()), file=sys.stderr)
        if state_word == "unknown":
            # docs/DESIGN.md 7.2's D6: this was already the forced,
            # must-produce-INCOMPLETE analysis attempt (stable_checks had
            # already crossed the threshold); if even that raises, there is
            # nothing left to wait for. Finalize as analyzed/INCOMPLETE
            # with no findings rather than retrying the same failure on
            # every future check forever.
            run_id = _fallback_run_id(kind, path)
            verdict = report.verdict_word([], "INCOMPLETE")
            entry = _skeleton(current_fp, "analyzed", 0)
            entry["verdict"] = verdict
            entry["wasted_seconds"] = 0.0
            entry["run_id"] = run_id
            verdict_word = verdict.split(" ", 1)[0]
            # docs/DESIGN.md 7.2's D-C: unlike the pure "still running, no
            # diagnostics, no findings" unknown-fallback case below, this is
            # a genuine failure (the forced analysis attempt itself raised)
            # -- always notify-worthy.
            notify = _NotifyInfo(
                run_id=run_id, verdict_word=verdict_word, headline="unit could not be analyzed",
                is_incomplete=True, finding_count=0,
            )
            return _UnitOutcome(entry=entry, notify=notify)
        # docs/DESIGN.md 7.2's D6: an analysis exception counts as a stable
        # check, not a reset, so a unit whose content genuinely cannot be
        # parsed still reaches unknown/INCOMPLETE within a bounded number of
        # checks instead of being silently stuck forever -- subject to the
        # same D-A quiet gate as the fingerprint-unchanged path above.
        bumped = stable_checks + 1
        next_state = (
            "unknown"
            if bumped >= _UNKNOWN_AFTER_STABLE_CHECKS and _quiet_long_enough(files)
            else "running"
        )
        return _UnitOutcome(entry=_skeleton(current_fp, next_state, bumped), notify=None)

    # docs/DESIGN.md 7.2: re-stat after parsing; a fingerprint that moved
    # while parsing means the unit is still being written, so it stays
    # pending and nothing is emitted this check.
    post_fp = _unit_fingerprint(kind, path, main_transcript, main_cache)
    if post_fp != current_fp:
        return _UnitOutcome(entry=_skeleton(post_fp, "running", 0), notify=None)

    # docs/DESIGN.md 4.6: reader_incomplete is true only when the reader
    # itself found real diagnostics (a bad journal, an unreadable/mostly-
    # undecodable transcript, a timestamp diagnostic); is_incomplete is the
    # broader "the stored verdict should carry (INCOMPLETE)" flag, true
    # whenever this was also forced through the unknown fallback with no
    # such diagnostics of its own.
    reader_incomplete = status == "INCOMPLETE"
    is_incomplete = state_word == "unknown" or reader_incomplete
    effective_status = "INCOMPLETE" if is_incomplete else status
    # docs/DESIGN.md 4.10/7.2: a detector silenced by the user's config still
    # ran and its findings are kept nowhere the hook ever exports them
    # (there is no `hook check --json`), but the hook verdict word and the
    # notify-worthy finding set must never be swayed by one -- the same rule
    # `run`'s own verdict/stories/next follow. wasted_seconds is left over
    # the full finding set, an informational number cached for
    # session-start's digest, not a notify decision.
    shown, _silenced = config.partition_silenced(findings, cfg)
    verdict = report.verdict_word(shown, effective_status, terminal_failed=failed)
    wasted_seconds = sum(
        f.metrics.get("wasted_seconds", 0.0) for f in findings if f.detector == "wasted_time"
    )

    threshold_rank = _SEVERITY_RANK[_notify_threshold()]
    new_finding_ids = [
        f.finding_id
        for f in shown
        if _SEVERITY_RANK.get(f.severity, 0) >= threshold_rank and f.finding_id not in reported_prev
    ]

    entry = _skeleton(current_fp, "analyzed", 0)
    entry["reported"] = sorted(set(reported_prev) | set(new_finding_ids))
    entry["verdict"] = verdict
    entry["wasted_seconds"] = wasted_seconds
    entry["run_id"] = run_id

    notify = None
    # docs/DESIGN.md 7.2's D-C: an INCOMPLETE that comes only from the
    # unknown-finalization fallback -- no reader diagnostics, no findings
    # at or above the notify threshold -- is noise with nothing a reader
    # can act on, so it is not notified. INCOMPLETE is notified only when
    # the reader itself reported diagnostics (reader_incomplete) or when
    # findings actually reach the threshold; either one also still carries
    # a suffix below -- "(inspection incomplete)" when the reader itself
    # found real diagnostics, or "(unfinished)" (docs/DESIGN.md 12's D-C)
    # when the only reason this is incomplete at all is the quiet-unknown
    # fallback finalizing content that may simply still be running.
    if new_finding_ids or reader_incomplete:
        verdict_word = verdict.split(" ", 1)[0]
        notify = _NotifyInfo(
            run_id=run_id, verdict_word=verdict_word, headline=headline,
            is_incomplete=reader_incomplete,
            quiet_unfinished=state_word == "unknown" and not reader_incomplete,
            finding_count=len(new_finding_ids),
            agent_ids=_notified_agent_ids(shown, reported_prev, threshold_rank),
        )
    return _UnitOutcome(entry=entry, notify=notify)


def _process_units_for_session(
    data: dict, session_key: str, session_dir: Path, payload: dict, cap: int | None = None,
    cfg: config.Config | None = None,
) -> list[_UnitOutcome]:
    """Classify (and, when newly finished/unknown, analyze) every unit of
    one session, mutating `data` in place: unit states, and digest.pending
    for every unit that newly became analyzed this call. Returns the
    outcomes for units processed this call; `cap`, when given, stops after
    that many units are newly analyzed (docs/DESIGN.md 7.2's session-start
    digest budget)."""
    sessions = data.setdefault("sessions", {})
    session_entry = sessions.setdefault(session_key, {"units": {}, "last_check": None})
    unit_states = session_entry.setdefault("units", {})
    digest = data.setdefault("digest", {"pending": []})
    pending = digest.setdefault("pending", [])

    main_transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    units = _discover_units(session_dir)
    discovered_ids = {unit_id for unit_id, _, _ in units}
    # docs/DESIGN.md 7.2's D8: workflow-launched may register a unit before
    # its conventional directory exists, storing the transcriptDir it was
    # given; fall back to that stored locator whenever the conventional
    # path was not discovered on disk and the unit is not already settled.
    for unit_id, entry in unit_states.items():
        if unit_id in discovered_ids or entry.get("state") == "analyzed":
            continue
        stored_dir = entry.get("transcript_dir")
        if stored_dir and _kind_from_unit_id(unit_id) == "workflow":
            units.append((unit_id, "workflow", Path(stored_dir)))

    # docs/DESIGN.md 7.2: workflow-launched marks a unit priority so check
    # inspects it first.
    ordered = sorted(
        units, key=lambda u: (0 if unit_states.get(u[0], {}).get("priority") else 1, u[0])
    )

    # docs/DESIGN.md 7.2's D6: one main-transcript parse shared by every
    # subagent unit analyzed within this one call.
    main_cache: dict = {}
    # docs/DESIGN.md 4.10: loaded once per call, never per unit -- every unit
    # of this session checked in this one call sees the same config.
    active_cfg = cfg if cfg is not None else config.load_config()

    outcomes: list[_UnitOutcome] = []
    analyzed_count = 0
    for unit_id, kind, path in ordered:
        if cap is not None and analyzed_count >= cap:
            break
        prev = unit_states.get(unit_id)
        was_analyzed = prev is not None and prev.get("state") == "analyzed"
        outcome = _check_one_unit(
            kind, path, prev, payload, main_transcript, main_cache, active_cfg,
        )
        unit_states[unit_id] = outcome.entry
        outcome.newly_analyzed = outcome.entry.get("state") == "analyzed" and not was_analyzed
        if outcome.newly_analyzed:
            analyzed_count += 1
            qualified = f"{session_key}{_DIGEST_SEPARATOR}{unit_id}"
            if qualified not in pending:
                pending.append(qualified)
        outcomes.append(outcome)

    session_entry["last_check"] = state.now_iso()
    return outcomes


# --- hook check --------------------------------------------------------

_VERDICT_WORD_RANK = {"CLEAN": 0, "DEGRADED": 1, "FAILED": 2}


_MAX_NAMED_AGENTS = 3


def _agents_clause(group: list[_NotifyInfo], redactor: Redactor) -> str:
    """N3b P17: `  agents a:<alias>, ...` after the command, at most three,
    then `+N more`; empty when no finding names a canonical agent id."""
    ids: list[str] = []
    for notify in group:
        for agent_id in notify.agent_ids:
            if agent_id not in ids:
                ids.append(agent_id)
    if not ids:
        return ""
    named = ", ".join(redactor.identity_text("agent", a) for a in ids[:_MAX_NAMED_AGENTS])
    more = f", +{len(ids) - _MAX_NAMED_AGENTS} more" if len(ids) > _MAX_NAMED_AGENTS else ""
    return f"  agents {named}{more}"


_POSTMORTEM_HINT = "/postmortem"


def _format_notify_lines(notifications: list[_NotifyInfo], redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 7.2's D-C: group by run id. A workflow unit's run id
    is unique to it, so it always forms a group of one and keeps the plain
    per-unit line; several subagent units of one session share that
    session's own run id, so they collapse into a single
    '<n> subagents finished, <k> findings' line instead of one line each.

    The lines are returned already redacted, piece by piece: the fixed slash command
    `/postmortem` the plugin tells the user to type is not a path and must not meet the
    free-text path scanner (docs/DESIGN.md 5.1, F fix 4); every other piece does."""
    groups: dict[str, list[_NotifyInfo]] = {}
    order: list[str] = []
    for notify in notifications:
        if notify.run_id not in groups:
            groups[notify.run_id] = []
            order.append(notify.run_id)
        groups[notify.run_id].append(notify)

    lines: list[str] = []
    for run_id in order:
        group = groups[run_id]
        worst = max(group, key=lambda n: _VERDICT_WORD_RANK.get(n.verdict_word, 0))
        # docs/DESIGN.md 12's D-C: a genuine reader diagnostic anywhere in
        # the group outranks a merely-quiet unit (a workflow's own
        # diagnostic finding is never noise even if a sibling subagent in
        # the same group only went quiet).
        if any(n.is_incomplete for n in group):
            suffix = " (inspection incomplete)"
        elif any(n.quiet_unfinished for n in group):
            suffix = " (unfinished)"
        else:
            suffix = ""
        if len(group) == 1:
            only = group[0]
            # each piece is scanned on its own: a path in the headline hides the rest of the
            # text it is scanned with (docs/DESIGN.md 5.1), so the run id must not follow it
            lines.append(
                f"{only.verdict_word}{suffix}  {redactor(only.headline)}  {redactor(run_id)}  "
                + f"{_POSTMORTEM_HINT} {redactor(run_id)}"
                + redactor(_agents_clause(group, redactor))
            )
            continue
        total_findings = sum(n.finding_count for n in group)
        finding_plural = "" if total_findings == 1 else "s"
        lines.append(
            redactor(
                f"{worst.verdict_word}{suffix}  {len(group)} subagents finished, "
                f"{total_findings} finding{finding_plural}  {run_id}  "
            )
            + f"{_POSTMORTEM_HINT} {redactor(run_id)}"
            + redactor(_agents_clause(group, redactor))
        )
    return lines


def _cmd_check(payload: dict) -> None:
    session_dir = _session_dir_from_payload(payload)
    if session_dir is None:
        return
    session_id = _resolve_session_id(payload)
    if session_id is None:
        return
    root = session_dir.parent.parent
    session_key = _session_state_key(root, session_id)
    stop_hook_active = bool(payload.get("stop_hook_active", False))

    notifications: list[_NotifyInfo] = []
    with state.transaction() as data:
        outcomes = _process_units_for_session(data, session_key, session_dir, payload)
        for outcome in outcomes:
            if outcome.notify is not None:
                notifications.append(outcome.notify)

    if not notifications:
        return

    # N3b P17, P8: one registration for every agent the message names (none
    # when it names no agent); always share. If the book is unavailable the
    # names are positional and the diagnostic is part of the message, so it
    # reaches systemMessage and, when emitted, additionalContext, inside the
    # one JSON object.
    identities: list[tuple[str, str]] = []
    for notify in notifications:
        for agent_id in notify.agent_ids:
            if ("agent", agent_id) not in identities:
                identities.append(("agent", agent_id))
    book = disclosure.register(identities) if identities else None
    redactor = Redactor(context=disclosure.DisclosureContext(
        level="share", book=book, identities=tuple(identities),
    ))
    lines = _format_notify_lines(notifications, redactor)
    message = "\n".join(lines)
    if book is not None and not book.available:
        message += f"  ({disclosure.UNAVAILABLE_DIAGNOSTIC})"
    output: dict = {"systemMessage": message}
    if not stop_hook_active:
        output["hookSpecificOutput"] = {
            "hookEventName": payload.get("hook_event_name"),
            "additionalContext": message,
        }
    print(json.dumps(output))


# --- hook session-start --------------------------------------------------


def _cmd_session_start(payload: dict) -> None:
    source = payload.get("source")
    if source not in ("startup", "resume"):
        return
    session_dir = _session_dir_plain(payload)
    if session_dir is None:
        return
    root = session_dir.parent.parent
    # docs/DESIGN.md 7.5's D3: this sweep, and the digest it prints, only
    # ever concerns entries qualified with this root's own key.
    root_prefix = _root_key(root) + _ROOT_KEY_SEPARATOR

    message: str | None = None
    with state.transaction() as data:
        remaining = _DIGEST_ANALYSIS_CAP
        for ref in reader.discover_runs(root, kinds=frozenset({"session"})):
            if remaining <= 0:
                break
            other_session_dir = ref.path.parent / ref.path.stem
            if not (other_session_dir / "subagents").is_dir():
                continue
            other_session_key = _session_state_key(root, ref.path.stem)
            # docs/DESIGN.md 7.2's D2: the cross-session sweep never has a
            # real Stop-hook payload for another session, so it must never
            # use an empty payload as evidence that a subagent is NOT still
            # running; {} simply names no agent id at all, which
            # _background_task_status already treats as "no signal either
            # way", never as "finished".
            outcomes = _process_units_for_session(
                data, other_session_key, other_session_dir, payload={}, cap=remaining,
            )
            remaining -= sum(1 for outcome in outcomes if outcome.newly_analyzed)

        digest = data.setdefault("digest", {"pending": []})
        pending = list(digest.get("pending", []))
        own_root_pending = [qualified for qualified in pending if qualified.startswith(root_prefix)]
        if not own_root_pending:
            return

        summaries = []
        for qualified in own_root_pending:
            session_key, _, unit_id = qualified.partition(_DIGEST_SEPARATOR)
            entry = data.get("sessions", {}).get(session_key, {}).get("units", {}).get(unit_id)
            if entry is not None and entry.get("state") == "analyzed":
                summaries.append(entry)
        # Every gathered entry of THIS root counts as digested now; another
        # root's still-pending entries must survive untouched (docs/
        # DESIGN.md 7.5's D3).
        digest["pending"] = [
            qualified for qualified in pending if not qualified.startswith(root_prefix)
        ]

        if not summaries:
            return

        rank = {"CLEAN": 0, "DEGRADED": 1, "FAILED": 2}

        def _verdict_rank(entry: dict) -> int:
            return rank.get(str(entry.get("verdict", "CLEAN")).split(" ", 1)[0], 0)

        worst = max(summaries, key=_verdict_rank)
        wasted_seconds = sum(entry.get("wasted_seconds", 0.0) for entry in summaries)
        plural = "" if len(summaries) == 1 else "s"
        redactor = Redactor()
        message = (
            redactor(
                f"{len(summaries)} run{plural} analyzed since last digest: "
                f"worst {worst.get('verdict', 'CLEAN')}, "
                f"{report.format_duration(wasted_seconds)} wasted. "
            )
            + f"{_POSTMORTEM_HINT} {redactor(str(worst.get('run_id', '')))}"
        )

    if message:
        print(json.dumps({"systemMessage": message}))


# --- hook session-end --------------------------------------------------


def _cmd_session_end(payload: dict) -> None:
    session_dir = _session_dir_plain(payload)
    if session_dir is None:
        return
    session_id = _resolve_session_id(payload)
    if session_id is None:
        return
    transcript = payload.get("transcript_path")
    main_transcript = Path(transcript)
    root = session_dir.parent.parent
    session_key = _session_state_key(root, session_id)

    try:
        # docs/DESIGN.md 7.2's D5: the main thread only, no children -- a
        # session's own subagents are each their own unit that `check`
        # already analyzes on its own.
        run = reader.load_session_view(main_transcript, [])
    except reader.NotARun:
        return
    findings = detectors.run_tooling_detectors(run)
    # docs/DESIGN.md 4.10/7.2: the same shown/silenced split _check_one_unit
    # applies -- this unit's cached verdict feeds session-start's own
    # "worst verdict" digest ranking, so it must never be swayed by a
    # detector the user silenced either.
    shown, _silenced = config.partition_silenced(findings, config.load_config())
    verdict = report.verdict_word(shown, run.status, terminal_failed=report.lifecycle_failed(run))
    wasted_seconds = sum(
        f.metrics.get("wasted_seconds", 0.0) for f in findings if f.detector == "wasted_time"
    )

    with state.transaction() as data:
        sessions = data.setdefault("sessions", {})
        session_entry = sessions.setdefault(session_key, {"units": {}, "last_check": None})
        unit_states = session_entry.setdefault("units", {})
        digest = data.setdefault("digest", {"pending": []})
        pending = digest.setdefault("pending", [])

        # docs/DESIGN.md 7.2's D5: dedupe against every unit's reported ids
        # in this session, not just main's own -- a finding reachable
        # through two units must notify once.
        already_reported: set[str] = set()
        for unit in unit_states.values():
            already_reported.update(unit.get("reported", []))
        prior_main_reported = set(unit_states.get("main", {}).get("reported", []))
        new_ids = {f.finding_id for f in shown} - already_reported

        unit_states["main"] = {
            "fingerprint": _fingerprint(_unit_files("session_main", main_transcript)),
            "state": "analyzed",
            "stable_checks": 0,
            "reported": sorted(prior_main_reported | new_ids),
            "verdict": verdict,
            "wasted_seconds": wasted_seconds,
            "run_id": run.run_id,
        }
        qualified = f"{session_key}{_DIGEST_SEPARATOR}main"
        if qualified not in pending:
            pending.append(qualified)
        session_entry["last_check"] = state.now_iso()
    # docs/DESIGN.md 7.2: session-end prints nothing.


# --- hook workflow-launched ----------------------------------------------


def _extract_run_id(tool_response: object) -> str | None:
    """docs/DESIGN.md 7.2 (release finding F12): whichever way a candidate
    run id was found -- the structured `runId` field, or the regex fallback
    over free text -- it is accepted only when it full-matches the same
    canonical run id grammar reader.py's own ingestion enforces
    (reader._WORKFLOW_RUN_ID_RE: 'wf_' + 8 hex + '-' + 3 hex). A structured
    `runId` previously accepted any nonempty string verbatim, including one
    carrying a newline or trailing text that could inject content into the
    additionalContext hint; requiring a full match closes that off for
    both extraction paths at once."""
    candidate: str | None = None
    if isinstance(tool_response, dict):
        raw = tool_response.get("runId")
        if isinstance(raw, str) and raw:
            candidate = raw
    if candidate is None:
        text = tool_response if isinstance(tool_response, str) else ""
        if not text and tool_response is not None:
            try:
                text = json.dumps(tool_response, ensure_ascii=False)
            except (TypeError, ValueError):
                text = ""
        match = _RUN_ID_RE.search(text)
        candidate = match.group(1) if match else None
    if candidate is None or not reader._WORKFLOW_RUN_ID_RE.fullmatch(candidate):
        return None
    return candidate


def _extract_transcript_dir(tool_response: object, session_dir: Path) -> str | None:
    """docs/DESIGN.md 7.2's D8: the transcriptDir the Workflow tool's own
    response names, if any -- the locator `check` falls back to when the
    conventional subagents/workflows/<run id> path does not exist yet.

    Release finding F12: an untrusted transcriptDir is honored only when,
    after normalization, it names a location inside this session's own
    subagents/workflows/ directory (the same session_dir `check` derives
    from transcript_path, docs/DESIGN.md 7.2's D4) -- it need not exist on
    disk yet (D8's whole point). Anything else (a relative path, or one
    that normalizes outside that directory) is dropped exactly as if
    transcriptDir had never been given at all: only the untrusted locator
    is refused, never the registration itself."""
    if not isinstance(tool_response, dict):
        return None
    candidate = tool_response.get("transcriptDir")
    if not isinstance(candidate, str) or not candidate:
        return None
    if not os.path.isabs(candidate):
        return None
    normalized = os.path.normpath(candidate)
    boundary = os.path.normpath(str(session_dir / "subagents" / "workflows"))
    if not normalized.startswith(boundary + os.sep):
        return None
    return normalized


def _cmd_workflow_launched(payload: dict) -> None:
    session_dir = _session_dir_plain(payload)
    if session_dir is None:
        return
    session_id = _resolve_session_id(payload)
    if session_id is None:
        return
    run_id = _extract_run_id(payload.get("tool_response"))
    if not run_id:
        return
    unit_id = f"subagents/workflows/{run_id}"
    transcript_dir = _extract_transcript_dir(payload.get("tool_response"), session_dir)
    root = session_dir.parent.parent
    session_key = _session_state_key(root, session_id)

    with state.transaction() as data:
        sessions = data.setdefault("sessions", {})
        session_entry = sessions.setdefault(session_key, {"units": {}, "last_check": None})
        unit_states = session_entry.setdefault("units", {})
        entry = unit_states.get(unit_id)
        if entry is None:
            new_entry = {
                "fingerprint": [], "state": "running", "stable_checks": 0,
                "reported": [], "priority": True,
            }
            if transcript_dir:
                new_entry["transcript_dir"] = transcript_dir
            unit_states[unit_id] = new_entry
        else:
            entry["priority"] = True
            if transcript_dir:
                entry["transcript_dir"] = transcript_dir

    # .reviews/proposals/2026-09-v0.2-guided-rescue.md section 7 (docs/
    # DESIGN.md 7.2): the registration side effect above happens either way;
    # this is the one added sentence, emitted only when a run id could be
    # read (already established above) and not opted out of. It arms
    # nothing itself (R2) -- the session decides whether to actually run the
    # command.
    if os.environ.get("AGENT_POSTMORTEM_WATCH_HINT", "").strip() != "0":
        message = (
            f"Workflow {run_id} launched. To notice a killed or failed agent within "
            f"seconds instead of hours, arm a Monitor now with this command: "
            f"agent-postmortem watch {run_id}"
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": message,
                    }
                }
            )
        )


# --- hook capabilities ---------------------------------------------------


def _cmd_capabilities() -> None:
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        version = metadata.version("agent-postmortem")
    except metadata.PackageNotFoundError:
        version = "0.0.0"
    print(json.dumps({"protocol": 1, "version": version}))


# --- dispatch ------------------------------------------------------------

_EVENT_FOR_COMMAND = {
    "check": ("Stop", "StopFailure"),
    "session-start": ("SessionStart",),
    "session-end": ("SessionEnd",),
    "workflow-launched": ("PostToolUse",),
}


def dispatch(command: str) -> int:
    """Run one `hook <command>`, reading its payload from stdin. Always
    returns 0 (docs/DESIGN.md 7.2: "the hook never fails a turn"); any
    exception, including StateLocked, is swallowed after writing nothing,
    with a traceback to stderr only when AGENT_POSTMORTEM_DEBUG is set."""
    try:
        if command == "capabilities":
            _cmd_capabilities()
            return 0

        payload = _read_payload()
        if payload is None or _is_subagent_context(payload):
            return 0

        event = payload.get("hook_event_name")
        expected = _EVENT_FOR_COMMAND.get(command)
        if expected is None or event not in expected:
            return 0
        if command == "workflow-launched" and payload.get("tool_name") != "Workflow":
            return 0

        if command == "check":
            _cmd_check(payload)
        elif command == "session-start":
            _cmd_session_start(payload)
        elif command == "session-end":
            _cmd_session_end(payload)
        elif command == "workflow-launched":
            _cmd_workflow_launched(payload)
        return 0
    except Exception:
        if os.environ.get("AGENT_POSTMORTEM_DEBUG"):
            # Redacted like the analysis traceback (7.2's D10): share level,
            # a path outside home and /tmp as path-N.
            print(Redactor().scrub_leftover_paths(traceback.format_exc()), file=sys.stderr)
        return 0
