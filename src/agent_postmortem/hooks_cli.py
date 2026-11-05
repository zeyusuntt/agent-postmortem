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
import stat
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    commands,
    config,
    detectors,
    disclosure,
    live,
    reader,
    report,
    stalls,
    state,
    workers,
)
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
    except (json.JSONDecodeError, ValueError, RecursionError):
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
            except (json.JSONDecodeError, ValueError, RecursionError):
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
    except (json.JSONDecodeError, ValueError, RecursionError):
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
    # (session main transcript, live delivery key) for each ending this check tells (4.14.6)
    live_claims: list = field(default_factory=list)


# the detectors whose session findings are the endings the live channels tell (4.14.2 R3 to R6),
# with the delivery keys that tell the same ending: a kill or a failure is a KILL or FAILED line
# (`:end`), or a CLAIM (`:claim`) when a "completed" notice answered it (R3), so either counts
# and Stop claims both
_LIVE_ENDING_KEYS = {
    "mislabeled_kill": ("end", "claim"), "attempt_failed": ("end", "claim"),
    "claim_contradiction": ("claim",), "missing_artifact": ("claim",),
}


def _live_endings(
    main_path: Path, shown: list, threshold_rank: int, reported_prev: list,
) -> tuple[set[str], list[tuple[Path, str]]]:
    """(the ids of shown findings whose ending a live channel already told, the live keys of
    the ones still to tell). A finding names an ending by its agent and that agent's final entry
    time (`live.ending_key`), the key the watch and the post-tool hook claim."""
    reported = live.delivered_keys(main_path)
    told: set[str] = set()
    claims: list[tuple[Path, str]] = []
    endings: dict[str, str | None] = {}
    for finding in shown:
        suffixes = _LIVE_ENDING_KEYS.get(finding.detector)
        if suffixes is None or not finding.agent_id:
            continue
        if finding.agent_id not in endings:
            transcript = main_path.parent / main_path.stem / "subagents" / \
                f"agent-{finding.agent_id}.jsonl"
            facts = live.SubFacts()
            if transcript.is_file():
                facts.update(live.Tail(transcript).read(live._sub_want))
                endings[finding.agent_id] = live.ending_key(finding.agent_id, facts)
            else:
                endings[finding.agent_id] = None
        ending = endings[finding.agent_id]
        if ending is None:
            continue
        keys = [f"{ending}:{suffix}" for suffix in suffixes]
        if any(key in reported for key in keys):
            told.add(finding.finding_id)
        elif _SEVERITY_RANK.get(finding.severity, 0) >= threshold_rank \
                and finding.finding_id not in reported_prev:
            claims.extend((main_path, key) for key in keys)
    return told, claims


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
    live_claims: list[tuple[Path, str]] = []
    if kind == "subagent":
        # 4.14.6 rule 3 (0.4): an ending the live channels (post-tool hook, watch, resume)
        # already told counts as reported here, and one this check tells is claimed for them
        told, live_claims = _live_endings(main_transcript, shown, threshold_rank, reported_prev)
        reported_prev = [*reported_prev, *sorted(told - set(reported_prev))]
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
    return _UnitOutcome(entry=entry, notify=notify, live_claims=live_claims if notify else [])


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


# docs/DESIGN.md 4.14.5 (field report 2026-11-03, item 1 of "no silent failures")
_STOP_BUDGET = 400


_is_timer = commands.is_timer
_is_bounded = commands.is_bounded


def _stop_deadline_notice(payload: dict) -> dict | None:
    """Stop: the session is ending its turn with shell commands still running in the background.
    Claude Code wakes it when one ends and never when one does not, so ask it, once per task, to
    arm a deadline first, unless it already has one armed (a cron, a Monitor, a timer task) or is
    already continuing from a Stop hook. None, or {full, short, keys, main}: the request, a short
    form for when it joins another message, the delivery keys and the main transcript."""
    if payload.get("hook_event_name") != "Stop" or payload.get("stop_hook_active"):
        return None
    transcript = payload.get("transcript_path")
    tasks = payload.get("background_tasks")
    if not isinstance(transcript, str) or not transcript.endswith(".jsonl") \
            or not isinstance(tasks, list) or payload.get("session_crons"):
        return None
    covered = _watcher_covers(transcript)
    shells = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        kind, status = task.get("type"), task.get("status")
        if kind == "monitor" and status in (None, "running", "pending"):
            return None  # a Monitor tells its end or its expiry
        if kind != "shell" or status not in (None, "running"):
            continue
        command = task.get("command")
        if isinstance(command, str) and _is_timer(command):
            return None  # a deadline is armed
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        if isinstance(command, str) and (commands.is_bounded(command, cwd)
                                         or commands.under_its_own_alarm(command, cwd)):
            continue  # under a positive timeout or its own alarm, it ends on its own
        if covered and not (isinstance(command, str) and _watcher_cannot_read(command, cwd)):
            continue
        if isinstance(task.get("id"), str) and live.TASK_ID_RE.fullmatch(task["id"]):
            shells.append(task["id"])
    main_path = Path(transcript)
    if shells and _armed_by_facts(main_path, [
            task.get("id") for task in tasks if isinstance(task, dict)
            and task.get("type") == "shell" and task.get("status") in (None, "running")]):
        return None
    told = live.delivered_keys(main_path) if shells else set()
    untold = [task for task in shells if f"cmd:{task}:stop" not in told]
    if not untold:
        return None
    return {"tasks": untold, "keys": [f"cmd:{task}:stop" for task in untold], "main": main_path}


def _watcher_covers(transcript: str) -> bool:
    """A background session on a Claude Code that can run the idle watcher has nothing to arm:
    the watcher tells it when a command goes silent, so this request would only ask an agent to
    do by hand what the plugin does (4.14.5). An interactive session is told only about a wait
    on stdin (a server in one is silent by design), so it is still asked, as is any session the
    watcher cannot run in."""
    from . import watcher

    main_path = Path(transcript)
    return os.environ.get(watcher.PLUGIN_MARK) == "1" and watcher.enabled(main_path) \
        and stalls.live_session_kind(
            main_path.stem, stalls.default_sessions_root(main_path)) == "bg"


def _watcher_cannot_read(command: str, cwd: str | None) -> bool:
    """The watcher tells a silence only for a command whose output it can read. Silence is never
    judged for a redirect it cannot place, for one that is no regular file, or for one where it
    may not be, as a relative one after a `cd X;` that may have failed, so the deadline request
    stays for those (0.5.0 review)."""
    outputs, unknown = live.command_outputs(command, cwd)
    if unknown:
        return True
    segments = commands.lex(command).segments
    if segments and [word.text for word in segments[0].words][:1] == ["cd"] \
            and segments[0].sep_after != "&&":
        if any(not redirect.target.startswith(("/", "~"))
               for seg in segments[1:] for redirect in seg.redirects
               if isinstance(redirect.target, str) and redirect.op in live._OUTPUT_OPS):
            return True
    for path in outputs:
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                return True
        except FileNotFoundError:
            if not os.path.isdir(os.path.dirname(path)):
                return True
        except OSError:
            return True
    return False


def _armed_by_facts(main_path: Path, tasks: list[str]) -> bool:
    """Field report 2026-11-04: the payload can list a Monitor as a plain `shell` task, so a
    Monitor is known from the session's own Monitor calls; one of them still listed as running,
    or a wake still able to fire, is the deadline this request would ask for. A read cut short
    says no, and asks."""
    from . import status as _status

    if not main_path.is_file():
        return False
    follower = live.SessionFollower(main_path, caller_session=None)
    _status._restore(follower, state.read_cache(follower.store_path))
    follower._read(deadline=time.monotonic() + _FIRST_READ_BUDGET_SECONDS)
    now = time.time()
    _status._save(follower, {}, now)
    return any(task in follower.main.monitors for task in tasks) \
        or follower.main.wake_pending(now)


def _deadline_texts(tasks: list[str]) -> tuple[str, str]:
    """The request in full and its short form, naming only the tasks it asks about (rendered
    from the keys this call won, inside the claim: review rounds 1 and 2)."""
    ids = ", ".join(tasks[:3]) + (f" and {len(tasks) - 3} more" if len(tasks) > 3 else "")
    full = (
        f"DEADLINE {ids}: still running as this turn ends. Claude Code wakes you when a command "
        "ends, never when it hangs. Before stopping, arm a deadline that wakes you (a Monitor "
        "with timeout_ms, or a background `sleep 1800`); when it fires and the output has not "
        "moved, TaskStop it and rerun with </dev/null and a timeout. A server or watcher needs "
        "none."
    )
    short = (f"DEADLINE {ids}: still running; before stopping, arm a deadline that wakes you (a "
             "Monitor with timeout_ms, or a background `sleep 1800`).")
    return full, short


def _claim_deadline(deadline: dict, room: int | None = None) -> str | None:
    """Claim the request's keys and render it from the tasks this call won; with `room`, the
    text must fit in it, in full or short, else every won key is handed back."""
    handed_back: list[str] = []

    def render(won: list[str]) -> str | None:
        tasks = [task for task in deadline["tasks"] if f"cmd:{task}:stop" in won]
        if not tasks:
            return None
        for text in _deadline_texts(tasks):
            if room is None or len(text) <= room:
                return text
        handed_back.extend(won)  # nothing fits: the next Stop asks
        return None

    _won, text = live.claim_deliveries(deadline["main"], deadline["keys"], then=render,
                                       unclaim=lambda: handed_back)
    return text


def _print_deadline(deadline: dict | None, payload: dict | None = None) -> None:
    """The Stop-time deadline request on its own (no finding to tell), once per task, else the
    reminder to run `status`, which waits for a Stop with nothing else to say."""
    text = _claim_deadline(deadline) if deadline is not None else None
    if not text and payload is not None:
        text = _status_reminder(payload)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop",
                                                 "additionalContext": text}}))


_STATUS_HINT_KEY = "hint:status"


def _status_reminder(payload: dict) -> str | None:
    """Field report 2026-11-04: two of three workers with subagents never ran `status` before
    reporting, since the rule lives only in an instruction file. At a Stop of a background session
    (an interactive one has a person reading), once per session (4.14.6 rule 4), when its subagents
    and workflows have all ended and no `status`, `scan`, `run` or `watch` of this tool ran since
    the last ending. Fixed words and two counts. A session with no subagent reads nothing here."""
    transcript = payload.get("transcript_path")
    if payload.get("hook_event_name") != "Stop" or payload.get("stop_hook_active") \
            or os.environ.get("AGENT_POSTMORTEM_WATCH_HINT", "").strip() == "0" \
            or not isinstance(transcript, str) or not transcript.endswith(".jsonl"):
        return None
    main_path = Path(transcript)
    if not (main_path.parent / main_path.stem / "subagents").is_dir() \
            or stalls.live_session_kind(main_path.stem,
                                        stalls.default_sessions_root(main_path)) != "bg" \
            or _STATUS_HINT_KEY in live.delivered_keys(main_path):
        return None
    from . import status as _status

    snap = _status.snapshot(main_path, background=False)
    counts = snap["counts"]
    if counts["running"] or counts["waiting"] \
            or any("ended" not in flow for flow in snap["workflows"]):
        return None
    subagents, flows = sum(counts.values()), len(snap["workflows"])
    entry = (state.read_cache(live.state.live_store_path()).get("transcripts") or {}).get(
        str(main_path))
    facts = live.MainFacts.from_dict(entry.get("facts")) if isinstance(entry, dict) else None
    if facts is None or not (subagents or flows):
        return None
    # an ending known only from its own transcript has its time on the row; with no time at all,
    # no earlier `status` can be said to have covered it
    times = [facts.last_ending(), *(row.get("at") for row in snap["subagents"])]
    times = [when for when in times if isinstance(when, (int, float))]
    if facts.status_ran is not None and times and facts.status_ran >= max(times):
        return None
    if not live.claim_delivery(main_path, _STATUS_HINT_KEY):
        return None
    what = " and ".join(part for part in (
        f"{subagents} subagent{'' if subagents == 1 else 's'}" if subagents else "",
        f"{flows} workflow{'' if flows == 1 else 's'}" if flows else "") if part)
    return (f"{what} finished and agent-postmortem status has not run since. Run it before "
            f"reporting done: it says which results can be trusted.")


def _cmd_check(payload: dict) -> None:
    # field report 2026-11-03 (item 1): asked for at every Stop, a session with no subagent too
    deadline = _stop_deadline_notice(payload)
    session_dir = _session_dir_from_payload(payload)
    if session_dir is None:
        _print_deadline(deadline, payload)
        return
    session_id = _resolve_session_id(payload)
    if session_id is None:
        _print_deadline(deadline, payload)
        return
    root = session_dir.parent.parent
    session_key = _session_state_key(root, session_id)
    stop_hook_active = bool(payload.get("stop_hook_active", False))

    notifications: list[_NotifyInfo] = []
    live_claims: list[tuple[Path, str]] = []
    try:
        with state.transaction() as data:
            outcomes = _process_units_for_session(data, session_key, session_dir, payload)
            for outcome in outcomes:
                if outcome.notify is not None:
                    notifications.append(outcome.notify)
                    live_claims.extend(outcome.live_claims)
    except (state.StateError, OSError):
        # the unit check could not run now (a locked or unreadable store); the deadline request
        # does not depend on it, and this Stop may be the session's last chance (rounds 2, 3)
        _print_deadline(deadline, payload)
        return
    # 4.14.6 rule 3: the endings told here are claimed in the live gate after the state lock is
    # released, so the post-tool hook and the watch never tell them again; nothing there may
    # stop the notification itself
    by_session: dict[Path, list[str]] = {}
    for main_path, key in live_claims:
        by_session.setdefault(main_path, []).append(key)
    for main_path, keys in by_session.items():
        try:
            live.claim_deliveries(main_path, keys)
        except Exception:
            pass  # a repeat later beats a notification lost now

    if not notifications:
        _print_deadline(deadline, payload)
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
        context = message
        # joined with the finding within the 400 characters, in full or short; if neither fits,
        # nothing is claimed and the next Stop asks (review round 1)
        request = None
        if deadline is not None:
            request = _claim_deadline(deadline, room=_STOP_BUDGET - len(message) - 1)
        if request:
            context = f"{message}\n{request}"
        output["hookSpecificOutput"] = {
            "hookEventName": payload.get("hook_event_name"),
            "additionalContext": context,
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

    resume_sentence: str | None = None
    if source == "resume":
        try:
            resume_sentence = _resume_sentence(_main_path_for(session_dir))
        except (state.StateError, OSError):
            resume_sentence = None

    message: str | None = None
    try:
        message = _session_start_digest(payload, root, root_prefix)
    finally:
        output: dict = {}
        if message:
            output["systemMessage"] = message
        if resume_sentence:
            output["hookSpecificOutput"] = {
                "hookEventName": "SessionStart", "additionalContext": resume_sentence,
            }
        if output:
            print(json.dumps(output))


def _session_start_digest(payload: dict, root: Path, root_prefix: str) -> str | None:
    """The 7.2 digest of this root's newly analyzed units, or None."""
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
            return None

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
            return None

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
    return message


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


# --- hook agent-launched (docs/DESIGN.md 4.14.5) ----------------------------

_SESSION_WATCH_HINT = (
    "Subagents of this session are checked when they return: an interruption nothing explains, "
    "a failure that left files changed, or a result its own transcript contradicts is reported "
    "at your next tool call. For a long background run you can also arm a Monitor (timeout_ms "
    "1800000, re-arm on expiry) with: agent-postmortem watch --session"
)


def _main_path_for(session_dir: Path) -> Path:
    return session_dir.parent / f"{session_dir.name}.jsonl"


def _claim_sentence(agent: str, claim: dict, public_id: str) -> str:
    if claim["check"] == "returned_on_error":
        return (f"Subagent {agent} returned, but its own last entry says: {claim['detail']}. "
                f"Check its work before using the result: agent-postmortem run {public_id}")
    return (f"Subagent {agent} returned, but a file its prompt asked for is missing: "
            f"agent-postmortem status {public_id} --local names it")


_HOOK_BUDGET = 400


def _cmd_agent_launched(payload: dict) -> None:
    session_dir = _session_dir_plain(payload)
    if session_dir is None:
        return
    main_path = _main_path_for(session_dir)
    response = payload.get("tool_response")
    agent_id = response.get("agentId") if isinstance(response, dict) else None
    messages: list[str] = []
    status_word = response.get("status") if isinstance(response, dict) else None
    synchronous = isinstance(response, dict) and (
        status_word == "completed"
        or (status_word is None and response.get("isAsync") is not True))  # T2 (returns.py)
    claim_key = None  # claimed last, once the message is ready (release review, 0.4.0)
    claim_message = None
    if synchronous and disclosure.is_canonical_id("agent", agent_id):
        transcript = session_dir / "subagents" / f"agent-{agent_id}.jsonl"
        if transcript.is_file():
            facts = live.SubFacts()
            facts.update(live.Tail(transcript).read(live._sub_want))
            # docs/DESIGN.md 4.14.6 rule 3: one delivery per ending, shared with the watch
            claim_key = f"{live.ending_key(agent_id, facts)}:claim"
            claim = live.claim_for(transcript)
            if claim is not None:
                names = live._Names()
                claim_message = live.session_redactor(main_path, [agent_id])(_claim_sentence(
                    names(agent_id), claim, live.session_public_id(main_path)))
                messages.append(claim_message)
    record = live.mark_reported(main_path, [])
    hint_wanted = os.environ.get("AGENT_POSTMORTEM_WATCH_HINT", "").strip() != "0"
    fits = len(" ".join([*messages, _SESSION_WATCH_HINT])) <= _HOOK_BUDGET
    # else the claim goes first and the hint waits for the next launch; the hint is claimed
    # through the same locked gate, so two launches at once never both hint
    # 4.14.5 (0.4): the hooks report at the next tool call, so the Monitor is an option offered
    # once, for a background launch only
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    background = tool_input.get("run_in_background") is True or (
        isinstance(response, dict) and (response.get("isAsync") is True
                                        or response.get("status") == "async_launched"))
    hint = hint_wanted and fits and background and not record.get("hinted") \
        and not live.watch_active(record)
    # every claim last, in one locked write right before printing: a call that fails or is
    # killed before this point has claimed nothing, so another channel still tells the ending
    keys = ([claim_key] if claim_key is not None else []) + (["hint"] if hint else [])
    won = live.claim_deliveries(main_path, keys)[0] if keys else []
    if claim_message is not None and claim_key not in won:
        messages.remove(claim_message)  # another channel told this ending first
    if hint and "hint" in won:
        messages.append(_SESSION_WATCH_HINT)
    if messages:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": " ".join(messages),
        }}))


# --- post-tool / prompt-submit: the return-time checks without a Monitor (4.14.5, 0.4) ---

_HOOK_EVENT_KINDS = ("KILL", "FAILED", "CLAIM")
# 4.14.5: the reading one call may do. A first check of a session with hundreds of subagents
# (2.6 s for 453 in the field) goes on over the next calls instead of meeting run.sh's deadline.
_FIRST_READ_BUDGET_SECONDS = 1.5
# 4.14.5: once one ending is ready, the rest wait for the next call past this point, so a call
# always ends inside run.sh's 3 s deadline with what it found
_CALL_BUDGET_SECONDS = 2.0


def _render_endings(events: list[dict], main_path: Path, follower,
                    budget: int = _HOOK_BUDGET) -> str | None:
    if not events:
        return None
    redact = live.session_redactor(main_path, follower._agent_ids())
    parts: list[str] = []
    for index, event in enumerate(events):
        text = "\n".join(line.rstrip() for line in live.render_text(event, redact))
        rest = len(events) - index - 1
        tail = f"\n... and {rest} more: agent-postmortem status" if rest else ""
        if parts and len("\n".join([*parts, text])) + len(tail) > budget:
            parts.append(f"... and {len(events) - index} more: agent-postmortem status")
            break
        parts.append(text)
    return "\n".join(parts)


def post_tool_check(main_path_str: str, payload: dict) -> tuple[Callable[[], str | None], bool]:
    """The full check behind hook_gate's fast gate: one watch-style poll of the session, restored
    from the live store as `status` restores it, that speaks only for a new KILL, FAILED or CLAIM
    ending. Returns (deliver, pending). `deliver()` claims every ending's key in one locked
    write (4.14.6 rule 3) and returns the message for the ones this call won, or None; the
    caller runs it last, right before printing, so a call killed earlier claims nothing.
    `pending`: the gate must open at the next call even if nothing changes (a kill inside its
    wait for a native notice, a first read not finished, endings left for later)."""
    from . import status as _status

    started = time.monotonic()
    main_path = Path(main_path_str)
    if not main_path.is_file():
        return _nothing, False
    follower = live.SessionFollower(main_path, caller_session=None)
    cache = state.read_cache(follower.store_path)
    _status._restore(follower, cache)
    complete = follower._read(deadline=started + _FIRST_READ_BUDGET_SECONDS)
    # field reports 2026-11-04: workers first, and told even with no baseline yet: what they say
    # needs none, and the baseline can take many calls in a long session
    worker_check = _worker_check(follower, payload, cache, started)
    if not follower.load_endings(new_baseline=complete or follower.read_once_each()):
        # no baseline yet and a transcript never read: keep the place, and take the baseline
        # only once every transcript was read, so an old ending is never told as new
        _status._save(follower, {}, time.time())
        return _make_deliver([], [], 0, None, {}, worker_check, main_path, follower,
                             None), True
    now = time.time()
    found: list[tuple[dict, str]] = []  # (event, its delivery key): claimed only once rendered
    handled: list[str] = []  # the keys of endings that need no line (4.14.6 rule 1)
    pending = not complete  # the rest of the read goes on at the next call
    judged = 0  # agents whose endings this call worked out
    for agent_id in follower._agent_ids():
        # past the budget, the rest wait for the next call, whether or not anything is told yet:
        # an ending that needs no line can still need a full parse, and a call killed at run.sh's
        # deadline saves nothing, so the next one would redo it (release review, 0.4.0)
        if judged and time.monotonic() >= started + _CALL_BUDGET_SECONDS:
            pending = True  # tell what is ready now and the rest at the next call
            break
        sub = follower.subs.get(agent_id)
        if sub is not None and sub.deferred is not None:
            continue  # its transcript is not read up to date: judged at a later call
        if sub is not None and sub.facts.final_kind == "kill":
            killed_at = sub.facts.final_ts if isinstance(sub.facts.final_ts, (int, float)) \
                else sub.tail.mtime
            if killed_at is None or now - killed_at >= live.KILL_NOTICE_GRACE_SECONDS:
                sub.pending_kill = True  # its wait for a native notice is over (4.14.3)
            elif live.ending_key(agent_id, sub.facts) not in follower.baseline:
                pending = True  # only the wait holds the gate open; after it this same call
                # decides the ending, by a KILL line or by its native notice
        before = len(found) + len(handled)
        for event, key in follower._agent_events(agent_id):
            if event is not None and event.get("event") in _HOOK_EVENT_KINDS:
                found.append((event, key))
            else:
                follower.reported.add(key)
                handled.append(key)
        if len(found) + len(handled) > before:
            judged += 1  # only an agent with an ending counts, so every call makes progress
    if follower.unsettled_meta or follower.rule_pending:
        pending = True  # a meta file to read again, or a baseline to extend (4.14.5)
    # field report 2026-11-03 (item 2): a background command that stopped writing, once each;
    # the commands still clocking say when the gate must open again
    told = live.delivered_keys(main_path, follower.store_path)
    running = stalls.running_commands(follower.main, now)
    waiting = [(c, _command_key(c)) for c in running
               if c["silent"] and _command_key(c) not in told]
    rechecks = [c["recheck_seconds"] for c in running
                if c["recheck_seconds"] is not None and _command_key(c) not in told]
    # with commands to tell, the endings leave room for their summary line (review round 1)
    reserve = {"budget": _HOOK_BUDGET - _COMMAND_RESERVE} if waiting else {}
    message = _render_endings([event for event, _key in found], main_path, follower, **reserve)
    # only the commands whose lines fit are claimed; the rest are told at the next call
    _text, shown = _with_commands(message, [c for c, _key in waiting])
    commands = waiting[:shown]
    overflow = len(waiting) - shown
    _status._save(follower, {}, now)
    follower._save_reported(handled)

    recheck_at = now + min(rechecks) if rechecks else None
    deliver = _make_deliver(found, commands, overflow, message, reserve, worker_check, main_path,
                            follower, recheck_at)
    # endings to deliver keep the gate open for one more call: the gate is written before
    # deliver() runs, so a call killed while it claims is checked again, and a call whose claims
    # went through finds nothing new and closes it
    return deliver, pending or bool(found) or bool(commands) or overflow > 0 \
        or worker_check.pending


def _make_deliver(found: list[tuple[dict, str]], commands: list[tuple[dict, str]], overflow: int,
                  message: str | None, reserve: dict, check: "_WorkerCheck", main_path: Path,
                  follower, recheck_at: float | None) -> Callable[[], str | None]:
    """`deliver()` for one full check: every key claimed and the message rendered in one locked
    write (4.14.6 rule 3). In the 400 characters: a worker in trouble, then endings, then
    commands, then a worker that delivered; what does not fit stays due."""

    def deliver() -> str | None:
        if not found and not commands and not check.observed:
            return None

        dropped: list[str] = []
        trouble: list[dict] = []
        info: list[dict] = []
        quiet: dict[str, str] = {}
        told: list[dict] = []

        def before(record: dict) -> None:
            # a commit that fails runs this again for the fallback (0.5.0 review)
            for held in (trouble, info, quiet, told):
                held.clear()
            lines, silent = workers.due(_told_workers(record), check.observed,
                                        _finishes(record))
            trouble.extend(w for w in lines if w["class"] in workers.TROUBLE)
            info.extend(w for w in lines if w["class"] in workers.INFORMATION)
            quiet.update(silent)

        def message_for(won: list[str]) -> str | None:  # inside the claim's lock, before its write
            events = [event for event, key in found if key in won]
            mine = [(c, key) for c, key in commands if key in won]
            parts: list[str] = []
            for index, worker in enumerate(trouble):
                line = workers.line(worker)
                left = len(trouble) - index - 1 + len(events) + len(mine) + overflow + len(info)
                tail = len(_more_line(left)) + 1 if left else 0
                if len("\n".join([*parts, line])) + tail > _HOOK_BUDGET:
                    break
                parts.append(line)
                told.append(worker)
            room = _HOOK_BUDGET - (len("\n".join(parts)) + 1 if parts else 0)
            unshown = len(trouble) - len(parts)
            if not parts:
                endings = message if all(k in won for _e, k in found) \
                    else _render_endings(events, main_path, follower, **reserve)
            else:
                spare = room - _COMMAND_RESERVE if mine else room
                endings = _render_endings(events, main_path, follower, budget=spare)
                if endings is not None and len(endings) > room:
                    # the first ending always renders whole: it waits for the next call
                    dropped.extend(key for _e, key in found if key in won)
                    unshown += len(events)
                    endings = None
            text, shown = _with_commands(endings, [c for c, _key in mine], more=overflow + unshown,
                                         budget=room)
            # a command whose line no longer fits is not recorded: told at a later call (round 2)
            dropped.extend(key for _c, key in mine[shown:])
            if text:
                parts.append(text)
            if unshown or overflow or shown < len(mine):
                return "\n".join(parts) or None
            for index, worker in enumerate(info):
                line = workers.line(worker)
                left = len(info) - index - 1
                tail = len(_more_line(left)) + 1 if left else 0
                if len("\n".join([*parts, line])) + tail > _HOOK_BUDGET:
                    parts.append(_more_line(len(info) - index))
                    break
                parts.append(line)
                told.append(worker)
            return "\n".join(parts) or None

        def after(record: dict) -> None:
            if not quiet and not told:
                return
            workers.save_told(record, check.table, quiet, told)
            record["seen"] = time.time()

        _won, text = live.claim_deliveries(
            main_path, [key for _event, key in found] + [key for _c, key in commands],
            follower.store_path, then=message_for, unclaim=lambda: dropped,
            before=before, after=after)
        return text

    rechecks = [at for at in (recheck_at, check.recheck_at) if at is not None]
    deliver.recheck_at = min(rechecks) if rechecks else None  # type: ignore[attr-defined]
    deliver.workers = check.table  # type: ignore[attr-defined]
    deliver.stall_turn = check.stall_turn  # type: ignore[attr-defined]
    return deliver


# --- workers this session launched (field reports 2026-11-04, docs/DESIGN.md 4.14.5) -----------

_STALL_RECHECK_SECONDS = 300.0
_STALL_READ_BYTES = 32 << 20
_NOT_THIS_SESSIONS = "-"  # a call named this id but did not create that job
_NO_JOB_YET = "?"
_UNREADABLE = "!"  # never read at this signature, and no use trying until the file changes


@dataclass
class _WorkerCheck:
    observed: list[dict] = field(default_factory=list)  # read this call; told or recorded
    # short id -> [mtime_ns, size, identity]: what the gate compares (hook_gate.py)
    table: dict[str, list] = field(default_factory=dict)
    pending: bool = False  # a line to tell, or job files left for the next call
    recheck_at: float | None = None  # when a working worker can first read STALLED
    stall_turn: int = 0  # the next idle worker's turn at the stall read
    stall_any_size: bool = False  # the watcher's: no 3 s limit kills it, so it reads any session
    names: dict[str, str] = field(default_factory=dict)  # what each launch result printed


def _told_workers(record: object) -> dict[str, str]:
    table = record.get("workers") if isinstance(record, dict) else None
    return {short: value for short, value in table.items() if isinstance(value, str)} \
        if isinstance(table, dict) else {}


def _finishes(record: object) -> dict[str, int]:
    table = record.get("worker_finishes") if isinstance(record, dict) else None
    return {short: value for short, value in table.items() if isinstance(value, int)} \
        if isinstance(table, dict) else {}


def _store_record(cache: dict, main_path: Path) -> dict:
    sessions = cache.get("sessions") if isinstance(cache, dict) else None
    record = sessions.get(live.session_key(main_path)) if isinstance(sessions, dict) else None
    return record if isinstance(record, dict) else {}


def _worker_check(follower, payload: dict, cache: dict, started: float,
                  previous: dict | None = None, stall_turn: int | None = None,
                  stall_any_size: bool = False) -> _WorkerCheck:
    """Reads the job files of this session's workers that changed since the last check, at most
    PARSES_PER_CALL per call; the rest wait for the next one. A worker whose job was not created
    by the launching call is remembered as no worker, so its file is never read again.
    `previous` and `stall_turn` are the gate's, unless the idle watcher keeps its own."""
    from . import hook_gate

    now = time.time()
    if previous is None:
        gate = hook_gate._read_gate(hook_gate._gate_path(str(follower.main_path))) or {}
        previous = gate.get("workers") if isinstance(gate.get("workers"), dict) else {}
        if stall_turn is None and isinstance(gate.get("stall_turn"), int):
            stall_turn = gate["stall_turn"]
    known = {short: launch for short, launch in follower.main.workers.items()
             if isinstance(launch, dict)}
    for short, name in workers.payload_launches(payload).items():
        known.setdefault(short, {"ts": now - workers.PAYLOAD_WINDOW_SECONDS, "end": now,
                                 "name": name})

    def recorded(short: str) -> int:
        row = previous.get(short)
        return row[0] if isinstance(row, list) and len(row) == 3 else -1

    # a row the transcript does not name yet (a launch it lags behind) stays, within the bound
    for short in sorted(previous, key=recorded, reverse=True):
        if len(known) >= workers.WORKERS_KEPT:
            break
        known.setdefault(short, {})
    while len(known) > workers.WORKERS_KEPT:
        del known[min(known, key=lambda short: known[short].get("ts") or 0)]
    check = _WorkerCheck(stall_turn=stall_turn or 0, stall_any_size=stall_any_size,
                         names={short: launch["name"] for short, launch in known.items()
                                if isinstance(launch.get("name"), str)})
    if not known:
        return check
    record = _store_record(cache, follower.main_path)
    told = _told_workers(record)
    parsed = 0
    # the file read longest ago first, so busy workers cannot hide another behind the limit
    for short in sorted(known, key=recorded):
        launch = known[short]
        path = workers.job_file(follower.jobs_root, short)
        signature = workers.job_signature(path)
        seen = previous.get(short)
        seen = seen if isinstance(seen, list) and len(seen) == 3 else None
        if seen is not None and seen[2] == _NOT_THIS_SESSIONS:
            check.table[short] = seen
            continue
        if signature is None:
            # the gate sees it when it appears; a stat that failed once forgets nothing
            check.table[short] = seen if seen is not None else [0, 0, _NO_JOB_YET]
            continue
        if seen is not None and seen[:2] == signature \
                and (told.get(short) == seen[2] or seen[2] == _UNREADABLE):
            check.table[short] = seen
            continue
        if parsed >= workers.PARSES_PER_CALL:
            check.pending = True
            check.table[short] = seen if seen is not None else [0, 0, _NO_JOB_YET]
            continue
        parsed += 1
        data = workers.read_job(path)
        if data is None:  # being replaced, or over the size limit: tried again when it changes
            validated = seen is not None and seen[2] not in (_NO_JOB_YET, _UNREADABLE)
            check.table[short] = [*signature, seen[2] if validated else _UNREADABLE]
            continue
        linked = (seen is not None and seen[2] not in (_NO_JOB_YET, _UNREADABLE)) \
            or workers.created_during(data, launch.get("ts"), launch.get("end"))
        if not linked and launch.get("ts") is None:
            # the job appeared while the transcript lags behind its launch: judged when it shows
            check.table[short] = seen if seen is not None else [0, 0, _NO_JOB_YET]
            continue
        if not linked:
            check.table[short] = [*signature, _NOT_THIS_SESSIONS]
            continue
        worker = workers.view(short, path, data, now, launch.get("name"))
        if worker is None:
            check.table[short] = [*signature, workers.identity(data)]
            continue
        check.table[short] = [*signature, worker["identity"]]
        check.observed.append(worker)
    _worker_stall(follower, check, now, started, cache=cache)
    lines, quiet = workers.due(told, check.observed, _finishes(record))
    # a change recorded with no line waits for its store write as well (review round 3): else a
    # write that fails leaves the old identity there, and the same prompt coming back reads as told
    check.pending = check.pending or bool(lines) or bool(quiet)
    return check


def _unread_bytes(main: Path, cache: dict | None) -> int:
    """What a read of this session's transcripts has left to do, by the offsets in the store."""
    main = Path(main)
    transcripts = cache.get("transcripts") if isinstance(cache, dict) else None
    transcripts = transcripts if isinstance(transcripts, dict) else {}
    total = 0
    for path in [main, *sorted((main.parent / main.stem / "subagents").glob("agent-*.jsonl"))]:
        try:
            info = path.stat()
        except OSError:
            continue
        size = info.st_size
        entry = transcripts.get(str(path))
        offset = entry.get("offset") if isinstance(entry, dict) else None
        # what a read would keep (`status._restore`, `Tail.read`): another version, a replaced file
        # or one shorter than the offset is read again from byte 0 (0.5.0 review)
        kept = isinstance(entry, dict) and isinstance(offset, int) and offset <= size \
            and entry.get("inode") in (None, info.st_ino) and (
                entry.get("facts_version") == live.MAIN_FACTS_VERSION
                and isinstance(entry.get("fork_line"), int) if path == main
                else entry.get("sub_facts_version") == live.SUB_FACTS_VERSION)
        total += size - offset if kept else size
    return total


def _worker_stall(follower, check: _WorkerCheck, now: float, started: float,
                  cache: dict | None = None) -> None:
    """A worker whose job still reads `working` can sit idle on a command that will not end, with
    its job file unchanged. At most one such session is read per call, by the rule `status` uses
    (4.14.4), within the time budget; the gate opens again when the next can be due."""
    from . import status as _status

    working = (f"working/{workers.WORKING}/", f"working/{workers.STALLED}/")
    rows = [(short, row) for short, row in check.table.items() if row[2] in working]
    candidates = sorted(short for short, row in rows
                        if now - row[0] / 1e9 >= stalls.IDLE_SECONDS)
    rechecks = [stalls.IDLE_SECONDS - (now - row[0] / 1e9) for _short, row in rows
                if now - row[0] / 1e9 < stalls.IDLE_SECONDS]
    if candidates:
        rechecks.append(_STALL_RECHECK_SECONDS)
    check.recheck_at = now + min(rechecks) if rechecks else None
    if not candidates or time.monotonic() >= started + _CALL_BUDGET_SECONDS:
        return
    # the gate keeps the turn, so every idle worker gets one whatever the cadence of the calls
    short = candidates[check.stall_turn % len(candidates)]
    check.stall_turn += 1
    path = workers.job_file(follower.jobs_root, short)
    data = workers.read_job(path)
    session_id = data.get("sessionId") if data is not None else None
    if not isinstance(session_id, str) or workers.effective_state(data) != "working":
        return
    projects_root = follower.main_path.parent.parent
    main = _status._job_transcript(projects_root, session_id)
    if main is None:
        return
    if not check.stall_any_size and _unread_bytes(main, cache) > _STALL_READ_BYTES:
        return  # run.sh kills a call at 3 s and nothing is saved: the watcher and `status` read it
    stall = _status._job_stall(main, now, stalls.live_sessions(projects_root.parent / "sessions"),
                               deadline=started + _CALL_BUDGET_SECONDS)
    if stall is _status.STALL_UNKNOWN:
        return  # neither a stall nor the end of one: what was told stands
    identity = f"working/{workers.STALLED}/" if stall is not None else f"working/{workers.WORKING}/"
    check.table[short] = [check.table[short][0], check.table[short][1], identity]
    check.observed = [w for w in check.observed if w["short"] != short]
    check.observed.append({
        "short": short, "name": workers.name_of(data, short, check.names.get(short)),
        "label": None,
        "class": workers.STALLED if stall is not None else workers.WORKING,
        "age": float(stall["idle_seconds"]) if stall is not None else 0.0, "identity": identity,
        "stdin": bool(stall.get("waiting_on_stdin")) if stall is not None else False})


def _command_key(command: dict) -> str:
    """One background command's silence, told once (4.14.6 rule 3)."""
    return f"cmd:{command['task_id']}:silent"


def _command_line(command: dict) -> str:
    """docs/DESIGN.md 4.14.5 (field report 2026-11-03): fixed words, the native task id and two
    ages, so it skips the redactor (which would hide `/dev/null` as a path)."""
    from .status import _age

    task = command["task_id"]
    launched = command.get("launched_seconds")
    since = f" (launched {_age(launched)} ago)" if launched is not None else ""
    fix = f"stop it (TaskStop {task}) and rerun it with </dev/null and a timeout"
    if command.get("waiting_on_stdin"):
        return (f"COMMAND {task}{since} waits on stdin, which nothing will close: its output ends "
                f"in codex's \"Reading additional input from stdin...\" and stopped changing. "
                f"{fix[0].upper()}{fix[1:]}.")
    quiet = command.get("silent_seconds")
    return (f"COMMAND {task}{since} has written nothing for {_age(quiet or 0)}. Claude Code "
            f"tells you when it ends, never that it hangs: if it is stuck, {fix}.")


_COMMAND_RESERVE = 45  # room for the summary line below, held back from the endings


def _more_line(n: int) -> str:
    return f"... and {n} more: agent-postmortem status"


def _with_commands(endings: str | None, commands: list[dict], more: int = 0,
                   budget: int = _HOOK_BUDGET) -> tuple[str | None, int]:
    """The endings message, then one line per silent command, all within the 400-character
    budget, and a summary line for the commands left for a later call, whose room is always
    kept (review round 1). Returns (the message, how many command lines it holds)."""
    parts = [endings] if endings else []
    shown = 0
    for index, command in enumerate(commands):
        line = _command_line(command)
        left = len(commands) - index - 1 + more
        tail = len(_more_line(left)) + 1 if left else 0
        if len("\n".join([*parts, line])) + tail > budget:
            break
        parts.append(line)
        shown += 1
    left = len(commands) - shown + more
    if left:
        parts.append(_more_line(left))
    return ("\n".join(parts) if parts else None), shown


def _nothing() -> None:
    return None


# --- SessionStart on resume: own subagents that ended while it was down -------


def _resume_sentence(main_path: Path) -> str | None:
    from . import status as _status

    snap = _status.snapshot(main_path)
    reported = live.delivered_keys(main_path)
    sub_dir = main_path.parent / main_path.stem / "subagents"

    def end_key(row: dict) -> str:
        facts = live.SubFacts()
        transcript = sub_dir / f"agent-{row['agent_id']}.jsonl"
        if transcript.is_file():
            facts.update(live.Tail(transcript).read(live._sub_want))
        return f"{live.ending_key(row['agent_id'], facts)}:end"  # the watch's own key

    rows = [row for row in snap["subagents"]
            if row["state"] in ("running", "interrupted") and end_key(row) not in reported
            and live.claim_delivery(main_path, end_key(row))]  # locked: one sentence per ending
    if not rows:
        return None
    paths = 0
    partial = False
    for row in rows[:20]:
        if "paths" in row:
            count, part = row["paths"], row.get("paths_partial", False)
        else:
            count, _commits, part = live.changes(sub_dir / f"agent-{row['agent_id']}.jsonl")
        paths += count
        partial = partial or part
    partial = partial or len(rows) > 20
    never = sum(1 for row in rows if row["state"] == "running")
    interrupted = len(rows) - never
    clauses = []
    if never:
        clauses.append(f"{never} subagent{'' if never == 1 else 's'} of this session never "
                       "returned")
    if interrupted:
        clauses.append(f"{interrupted} {'was' if interrupted == 1 else 'were'} interrupted")
    paths_text = f"{paths}+" if partial else str(paths)  # a lower bound, also at 0 (4.14.3)
    return (f"{' and '.join(clauses)} before this session stopped; {paths_text} "
            f"path{'' if paths == 1 and not partial else 's'} changed. "
            f"Check them before relying on that work. -> agent-postmortem status")


# --- hook capabilities ---------------------------------------------------


def _cmd_capabilities() -> None:
    from . import entry  # the one definition, also reached without the CLI (4.14.5)

    entry.capabilities()


# --- dispatch ------------------------------------------------------------

_EVENT_FOR_COMMAND = {
    "check": ("Stop", "StopFailure"),
    "session-start": ("SessionStart",),
    "session-end": ("SessionEnd",),
    "workflow-launched": ("PostToolUse",),
    "agent-launched": ("PostToolUse",),
}


def dispatch(command: str) -> int:
    """Run one `hook <command>`, reading its payload from stdin. Always
    returns 0 (docs/DESIGN.md 7.2: "the hook never fails a turn"), except that
    `stop-watch` returns 2 to wake an idle session (4.14.5); any exception,
    including StateLocked, is swallowed after writing nothing, with a traceback
    to stderr only when AGENT_POSTMORTEM_DEBUG is set."""
    try:
        if command == "capabilities":
            _cmd_capabilities()
            return 0
        if command in ("post-tool", "prompt-submit"):
            from . import hook_gate

            return hook_gate.run(command)
        if command == "pre-bash":
            from . import pre_bash

            return pre_bash.run()
        if command == "stop-watch":
            from . import watcher

            return watcher.run()

        payload = _read_payload()
        if payload is None or _is_subagent_context(payload):
            return 0

        event = payload.get("hook_event_name")
        expected = _EVENT_FOR_COMMAND.get(command)
        if expected is None or event not in expected:
            return 0
        if command == "workflow-launched" and payload.get("tool_name") != "Workflow":
            return 0
        if command == "agent-launched" and payload.get("tool_name") not in ("Agent", "Task"):
            return 0

        if command == "check":
            _cmd_check(payload)
        elif command == "session-start":
            _cmd_session_start(payload)
        elif command == "session-end":
            _cmd_session_end(payload)
        elif command == "workflow-launched":
            _cmd_workflow_launched(payload)
        elif command == "agent-launched":
            _cmd_agent_launched(payload)
        return 0
    except Exception:
        if os.environ.get("AGENT_POSTMORTEM_DEBUG"):
            # Redacted like the analysis traceback (7.2's D10): share level,
            # a path outside home and /tmp as path-N.
            print(Redactor().scrub_leftover_paths(traceback.format_exc()), file=sys.stderr)
        return 0
