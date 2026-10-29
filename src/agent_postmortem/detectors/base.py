"""The contract every detector module must satisfy, plus the small pieces of
logic every detector needs and none of them owns: formatting an evidence
line, deciding whether an event is a Bash call, and summing an attempt's
wall-clock span. These used to be copy-pasted into every detector module;
a fix to the evidence-format contract (docs/DESIGN.md 4.1) had as many call
sites to update as there were detectors, and nothing caught a site that was
missed. A detector module lives in this package and exports exactly one
module-level instance named DETECTOR. detectors/__init__.py finds it by
iterating the package; there is no registry list to edit by hand.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ..models import Attempt, Event, Finding, MarkerHit, Run

# docs/DESIGN.md 4.4: how close a parent-transcript TaskStop or kill sentinel
# has to land to a child's own kill timestamp to count as evidence of that
# cause, rather than coincidence. 30s comfortably covers both of the two
# sources of slop between two files written by two different processes: the
# runtime buffers transcript writes rather than fsyncing per line, and
# timestamps are only as fine-grained as the clock the two writers share.
# Kept as two named constants, not one, because 4.4 evaluates them against
# different things (a TaskStop's timestamp vs. a second file's own kill
# sentinel) and a future revision may need to tune them independently.
PARENT_STOP_WINDOW_SECONDS = 30
PARENT_SENTINEL_WINDOW_SECONDS = 30

# docs/DESIGN.md 4.3: which run kinds a detector applies to. silent_retry is
# workflow-only (a session's subagents are never retried in a way v0.1 can
# observe -- 4.3's own note); every other detector applies to both kinds.
KINDS_WORKFLOW_ONLY: frozenset[str] = frozenset({"workflow"})
KINDS_BOTH: frozenset[str] = frozenset({"workflow", "session"})


class Detector(Protocol):
    id: str
    severity_default: str
    kinds: frozenset[str]  # run kinds this detector applies to (4.3)

    def run(self, run: Run) -> list[Finding]:
        """Inspect one Run and return zero or more Findings."""
        ...


def tooling_attempts(run: Run) -> list[Attempt]:
    """The attempts a tooling detector must scan (docs/DESIGN.md 4.3): a
    session's own main thread first (it is where a session's -p invocation
    and its own tool calls live, and it is excluded from run.attempts by
    design -- see models.Run), then every child attempt. A workflow run has
    no main, so this is just run.attempts for one.

    The seven tooling detectors (bash_backgrounded, bash_timeout,
    tool_json_error, structured_output_failure, codex_write_denied,
    classifier_unavailable, git_cwd_mismatch) all use this instead of
    iterating run.attempts directly, so a marker in a session's main thread
    is never silently invisible to them. mislabeled_kill and missing_artifact
    stay on run.attempts only: a session's main-thread interrupt is not
    attributable (4.3's own documented limit) and a session has no
    'first user event' of the kind missing_artifact looks for."""
    attempts = [run.main] if run.main is not None else []
    attempts.extend(run.attempts)
    return attempts


def relative_path(run: Run, path: Path) -> str:
    """path relative to run.path, or just its name if it is not underneath
    (for example a transcript from a fixture loaded by a bare, non-standard
    path) or is run.path itself -- a session's cause_evidence (docs/DESIGN.md
    4.4) can point at the main transcript, and for a session run.path is
    that same file, so a literal relative_to would collapse to '.', not a
    path a reader could use at all."""
    try:
        relative = path.relative_to(run.path)
    except ValueError:
        return path.name
    return str(relative) if str(relative) != "." else path.name


def evidence_line(run: Run, event: Event, limit: int = 160) -> str:
    """'relative/path:line: excerpt' (<=160 chars), the evidence format
    docs/DESIGN.md 4.1 requires of every Finding."""
    excerpt = " ".join(event.text.split())[:limit]
    return f"{relative_path(run, event.path)}:{event.line_no}: {excerpt}"


def make_finding_id(
    run_id: str, detector: str, agent_id: str | None, first_evidence: str | None,
) -> str:
    """sha256(run_id, detector, agent_id, first evidence path:line)[:12]
    (docs/DESIGN.md 4.1). first_evidence is a full 'path:line: excerpt'
    evidence line, or None; only its 'path:line' prefix goes into the hash,
    so a later change to excerpt wording or truncation does not change a
    finding's identity. Every detector module calls this to fill
    Finding.finding_id; nothing else computes one."""
    path_line = (first_evidence or "").split(": ", 1)[0]
    payload = "\x1f".join((run_id, detector, agent_id or "", path_line))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def single_agent_id(agent_ids) -> str | None:
    """The agent id when every hit of a run-wide tooling finding belongs to
    the one agent, else None (docs/DESIGN.md 4.3, 4.7): a finding that counts
    hits across several agents is not any one agent's story."""
    distinct = set(agent_ids)
    return distinct.pop() if len(distinct) == 1 else None


def anchored_marker_hits(attempt: Attempt, marker: str) -> list[MarkerHit]:
    """Every MarkerHit on attempt with this canonical id that is anchored
    and not in_payload (docs/DESIGN.md 4.13.2): the only hits a detector may
    ever treat as evidence. A detector never reads Event.markers directly
    (kept only for compatibility); it reads Attempt.marker_hits through this
    helper instead."""
    return [
        hit for hit in attempt.marker_hits
        if hit.marker == marker and hit.anchored and not hit.in_payload
    ]


def event_for_marker_hit(attempt: Attempt, hit: MarkerHit) -> Event | None:
    """The Event a MarkerHit was built from (docs/DESIGN.md 4.13.2): matched
    by (line_no, tool_use_id) for a tool_result shape -- a single user entry
    can carry more than one tool_result block on the same physical line, so
    line_no alone cannot disambiguate them -- or by line_no plus kind for a
    whole-entry shape (kill_sentinel, the final-assistant-entry markers,
    sandbox_write_denied's assistant form), where the first matching 'user'
    or 'assistant' event is the one the hit's own entry-level flattening
    rule reads from (its first text block)."""
    if hit.tool_use_id is not None:
        for event in attempt.events:
            if event.line_no == hit.event_line and event.tool_use_id == hit.tool_use_id:
                return event
        return None
    for event in attempt.events:
        if event.line_no == hit.event_line and event.kind in ("user", "assistant"):
            return event
    return None


def is_bash_event(event: Event) -> bool:
    """A tool_result the reader resolved to the Bash tool. A generic
    substring match on an attachment's text (a hook result, not necessarily
    tied to Bash at all) is not reliable evidence of a Bash call."""
    return event.kind == "tool_result" and event.tool_name == "Bash"


_COMMAND_EXCERPT_LIMIT = 160


def preceding_bash_command(attempt: Attempt, index: int) -> str:
    """The command text of the Bash tool_use immediately before
    attempt.events[index], or "" if none is found. Shared by
    bash_backgrounded (which quotes it, redacted, in a finding's metrics)
    and codex_write_denied (docs/DESIGN.md 4.3's D-C: a marker only counts
    on a Bash tool_result whose own command mentions "codex")."""
    for event in reversed(attempt.events[:index]):
        if event.kind == "tool_use" and event.tool_name == "Bash":
            try:
                data = json.loads(event.text)
            except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
                return event.text[:_COMMAND_EXCERPT_LIMIT]
            command = data.get("command") if isinstance(data, dict) else None
            return command if isinstance(command, str) else event.text[:_COMMAND_EXCERPT_LIMIT]
    return ""


def attempt_seconds(attempt: Attempt) -> float:
    """An attempt's wall-clock span, or 0.0 if either endpoint is unknown."""
    if attempt.first_ts is None or attempt.last_ts is None:
        return 0.0
    return max((attempt.last_ts - attempt.first_ts).total_seconds(), 0.0)


# docs/DESIGN.md 4.3 (FA5): the outcomes of an attempt that ended without a result.
NO_RESULT_OUTCOMES = frozenset({"killed", "failed", "superseded"})


def wasted_attempts(attempts: list[Attempt]) -> list[Attempt]:
    """The attempts of ONE logical agent whose wall-clock is wasted (docs/DESIGN.md
    4.3, FA5), each once: every attempt but the last (a retry, whatever its
    outcome) plus the last one when its outcome is killed, failed, or superseded.
    An open final attempt that may still be running is not counted (its outcome
    is completed or unknown)."""
    ordered = sorted(attempts, key=lambda attempt: attempt.ordinal)
    if not ordered:
        return []
    final = ordered[-1]
    wasted = ordered[:-1]
    if final.outcome in NO_RESULT_OUTCOMES:
        wasted.append(final)
    return wasted


def run_wasted_attempts(run: Run) -> list[Attempt]:
    """wasted_attempts over every logical agent of the run, in agent order."""
    return [attempt for agent in run.agents for attempt in wasted_attempts(agent.attempts)]


def format_duration(seconds: float) -> str:
    """2h13m for an hour or more, 41m for a minute or more, else 58s."""
    total = max(int(round(seconds)), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def final_event(events: Sequence[Event]) -> Event | None:
    """The last event whose raw_type is not exactly "attachment" (docs/DESIGN.md
    4.13.2). Shared by attempt_failed and watch so both read one cause source."""
    return next((ev for ev in reversed(events) if ev.raw_type != "attachment"), None)


def meaningful_event_before(attempt: Attempt, index: int) -> Event | None:
    """The last event before attempt.events[index] worth calling "preceding"
    (docs/DESIGN.md 4.5): a reader.py-and-detector-shared notion of evidence
    quality, not just reader.py's own interrupt-building.

    Walks backward skipping 'thinking' and 'attachment' events and any event
    whose text is empty once harness noise has been stripped (reader.py
    strips <system-reminder>, <total_tokens>, and [harness: ...] spans before
    an Event's text is set, so this is a plain emptiness check here, not a
    second strip). A 'compaction' event stops the walk outright and this
    returns None: a context reset makes whatever came before it not
    "preceding" in any sense a caller can use (silence across a compaction is
    not silence, and evidence from before one is not evidence of what
    happened after)."""
    for i in range(index - 1, -1, -1):
        event = attempt.events[i]
        if event.kind == "compaction":
            return None
        if event.kind in ("thinking", "attachment"):
            continue
        if not event.text.strip():
            continue
        return event
    return None


def _task_stop_matches(attempt: Attempt, task_stop, kill_ts) -> bool:
    """docs/DESIGN.md 4.4 clause 1: named directly, or landing within
    PARENT_STOP_WINDOW_SECONDS before the kill (a TaskStop with no timestamp
    of its own can still match by name, just never by window)."""
    if attempt.agent_id in task_stop.ids:
        return True
    if task_stop.ts is None or kill_ts is None:
        return False
    delta = (kill_ts - task_stop.ts).total_seconds()
    return 0 <= delta <= PARENT_STOP_WINDOW_SECONDS


def _task_stop_evidence(parent_index, task_stop) -> Event:
    return Event(
        path=parent_index.path, line_no=task_stop.line_no, ts=task_stop.ts,
        kind="tool_use", raw_type="assistant", tool_name="TaskStop",
        text=task_stop.text, exit_code=None, duration_ms=None, is_error=False,
        usage={}, markers=frozenset(),
    )


def _kill_sentinel_evidence(parent_index, sentinel) -> Event:
    return Event(
        path=parent_index.path, line_no=sentinel.line_no, ts=sentinel.ts,
        kind="user", raw_type="user", tool_name=None,
        text=sentinel.text, exit_code=None, duration_ms=None, is_error=False,
        usage={}, markers=frozenset(),
    )


def attribute_interrupt(run: Run, attempt: Attempt, parent_index) -> tuple[str, Event | None]:
    """cause and cause_evidence for attempt.interrupt, evaluated against
    parent_index (a reader.ParentIndex; duck-typed here rather than imported,
    since reader.py imports this module) in the exact order docs/DESIGN.md
    4.4 specifies, first match wins:

    1. parent_stop: a parent TaskStop names this attempt (docs/DESIGN.md 4.2
       flattens every string value of a TaskStop's input into `ids`, so an
       "agent_id" or a "task_id" field both work the same way here), or any
       TaskStop lands within PARENT_STOP_WINDOW_SECONDS before the kill.
    2. human: a parent kill sentinel lands within
       PARENT_SENTINEL_WINDOW_SECONDS of the kill, either side (the owner's
       Escape and the child's own kill timestamp are two different files'
       clocks, not a strict before/after).
    3. runtime: workflow kind only, and this attempt's key has a later
       attempt -- the restart itself is affirmative evidence the runtime
       killed and replaced it, never conjecture.
    4. unattributed: everything else.

    Returns (cause, cause_evidence); cause_evidence is only ever set for
    parent_stop and human (docs/DESIGN.md 4.1's Interrupt.cause_evidence)."""
    interrupt = attempt.interrupt
    kill_ts = interrupt.kill_ts if interrupt is not None else None

    for task_stop in parent_index.task_stops:
        if _task_stop_matches(attempt, task_stop, kill_ts):
            return "parent_stop", _task_stop_evidence(parent_index, task_stop)

    if kill_ts is not None:
        for sentinel in parent_index.kill_sentinels:
            if sentinel.ts is None:
                continue
            if abs((kill_ts - sentinel.ts).total_seconds()) <= PARENT_SENTINEL_WINDOW_SECONDS:
                return "human", _kill_sentinel_evidence(parent_index, sentinel)

    if run.kind == "workflow" and attempt.key is not None:
        siblings = run.attempts
        if any(s.ordinal > attempt.ordinal for s in siblings if s.key == attempt.key):
            return "runtime", None

    return "unattributed", None
