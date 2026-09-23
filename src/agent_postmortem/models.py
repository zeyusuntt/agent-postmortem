"""Data model for agent-postmortem.

reader.py fills these; detectors read them; report.py renders them. Every
dataclass carries a to_dict() so report.py and the --json output do not
re-implement Path and datetime conversion in more than one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def _path_str(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class Event:
    path: Path  # transcript file
    line_no: int  # 1-based
    ts: datetime | None
    kind: str  # 'user' | 'assistant' | 'thinking' | 'tool_use' | 'tool_result'
    # | 'attachment' | 'compaction' | 'unknown'
    raw_type: str  # the transcript's own type field
    tool_name: str | None  # for tool_use / tool_result (resolved via tool_use_id when possible)
    text: str  # excerpt, at most 400 chars, already truncated by the reader; '' for thinking
    exit_code: int | None
    duration_ms: int | None
    is_error: bool
    usage: dict  # empty unless assistant message carried usage
    # Which markers.py fragments (or, for a regex marker, its name) were
    # found anywhere in the field's full decoded text before it was
    # truncated to `text`. A marker landing after the excerpt limit must
    # still be detectable; this is how a detector sees it without the
    # reader holding the full, untruncated text.
    markers: frozenset[str] = field(default_factory=frozenset)
    # A tool_result whose resolved tool_name is Agent, Task, Workflow, or
    # TaskOutput (docs/DESIGN.md 4.2): the parent's copy of a child's own
    # output. markers is always empty on such an event so a child's error
    # text quoted in the parent's result is never counted twice.
    envelope: bool = False
    # A tool_result whose resolved tool_name is Read, Grep, Glob, or
    # NotebookRead, or a Bash tool_result whose command is one of the
    # read-only forms docs/DESIGN.md 4.3 lists (docs/DESIGN.md 4.3's
    # self-reference rule): the marker-based tooling detectors skip such an
    # event, since its content is merely quoting something on disk (a
    # marker string in this repository's own source, for example) rather
    # than reporting a tool failure that actually happened to this agent.
    # bash_backgrounded and bash_timeout never consult this field: a
    # timed-out `cat` is still a timed-out command.
    read_only_output: bool = False

    def to_dict(self) -> dict:
        return {
            "path": _path_str(self.path),
            "line_no": self.line_no,
            "ts": _iso(self.ts),
            "kind": self.kind,
            "raw_type": self.raw_type,
            "tool_name": self.tool_name,
            # text is deliberately absent: the JSON export carries structure
            # and findings, never transcript excerpts (privacy and size).
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "is_error": self.is_error,
            "usage": self.usage,
            "markers": sorted(self.markers),
            "envelope": self.envelope,
            "read_only_output": self.read_only_output,
        }


@dataclass
class Interrupt:
    """Set by the reader on an attempt whose last user event is the kill
    sentinel (docs/DESIGN.md section 4.4). cause and cause_evidence start as
    'unattributed' and None while an attempt's own transcript is read, then
    reader._attribute_interrupts fills them in against the parent transcript
    (ParentIndex) once every attempt of the run has been loaded."""

    kill_ts: datetime | None
    preceding: Event | None  # last meaningful event before the sentinel (4.5)
    silence_seconds: float | None  # None when a timestamp is missing, the
    # delta is negative, or a compaction lies between preceding and the kill
    cause: str  # 'parent_stop' | 'human' | 'runtime' | 'unattributed'
    cause_evidence: Event | None  # the parent TaskStop or parent sentinel, if any

    def to_dict(self) -> dict:
        return {
            "kill_ts": _iso(self.kill_ts),
            "preceding": self.preceding.to_dict() if self.preceding is not None else None,
            "silence_seconds": self.silence_seconds,
            "cause": self.cause,
            "cause_evidence": (
                self.cause_evidence.to_dict() if self.cause_evidence is not None else None
            ),
        }


@dataclass
class Attempt:
    agent_id: str
    key: str | None
    label: str
    phase: str
    agent_type: str | None
    meta: dict
    transcript_path: Path | None
    events: list[Event]
    first_ts: datetime | None
    last_ts: datetime | None
    ordinal: int  # 1-based order among attempts sharing a key
    outcome: str  # 'completed' | 'killed' | 'unknown'
    usage: dict  # summed input/output/cache tokens
    interrupt: Interrupt | None = None  # set by the reader when outcome == 'killed'

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "key": self.key,
            "label": self.label,
            "phase": self.phase,
            "agent_type": self.agent_type,
            "meta": self.meta,
            "transcript_path": _path_str(self.transcript_path),
            "events": [event.to_dict() for event in self.events],
            "first_ts": _iso(self.first_ts),
            "last_ts": _iso(self.last_ts),
            "ordinal": self.ordinal,
            "outcome": self.outcome,
            "usage": self.usage,
            "interrupt": self.interrupt.to_dict() if self.interrupt is not None else None,
        }


@dataclass
class LogicalAgent:
    key: str
    label: str
    phase: str
    attempts: list[Attempt]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "phase": self.phase,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


@dataclass
class Run:
    run_id: str
    kind: str = field(default="workflow", kw_only=True)  # 'workflow' | 'session'
    path: Path
    project_slug: str  # the -Users-x-Documents-y directory name, redacted on output
    session_id: str | None
    claude_version: str | None
    journal: list[dict]
    main: Attempt | None = field(default=None, kw_only=True)  # session kind only: the main thread
    agents: list[LogicalAgent]
    attempts: list[Attempt]  # child attempts only; never includes main
    started_at: datetime | None
    ended_at: datetime | None
    diagnostics: list[str]
    status: str = field(default="COMPLETE", kw_only=True)  # 'COMPLETE' | 'INCOMPLETE' (4.6)
    # docs/DESIGN.md 7.4's D8: whether this run has actually finished, not
    # merely whether it was fully readable. A workflow is finished when its
    # journal has a 'result' for every 'started' key; a session is always
    # finished (there is no journal-level "still running" signal for one).
    # Defaults to True so a Run built directly in a test, without setting
    # this, is never mistaken for "still running".
    finished: bool = field(default=True, kw_only=True)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "path": _path_str(self.path),
            "project_slug": self.project_slug,
            "session_id": self.session_id,
            "claude_version": self.claude_version,
            "journal": self.journal,
            "main": self.main.to_dict() if self.main is not None else None,
            "agents": [agent.to_dict() for agent in self.agents],
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "diagnostics": self.diagnostics,
            "status": self.status,
            "finished": self.finished,
        }


@dataclass
class Finding:
    detector: str  # detector id, e.g. 'silent_retry'
    severity: str  # 'critical' | 'major' | 'minor' | 'info'
    title: str  # one line, verdict style, includes the number
    run_id: str
    agent_id: str | None
    evidence: list[str]  # 'relative/path:line: excerpt' (<=160 chars each), redacted at render time
    metrics: dict  # detector-specific numbers (counts, seconds, tokens)
    rule_id: str | None  # the rule that would have prevented it, if any
    # sha256(run_id, detector, agent_id, first evidence path:line)[:12]
    # (docs/DESIGN.md 4.1), computed by detectors/base.py's make_finding_id.
    # Defaults to '' for a Finding built directly in a test rather than by a
    # detector; every detector module sets a real one.
    finding_id: str = ""

    def to_dict(self) -> dict:
        return {
            "detector": self.detector,
            "severity": self.severity,
            "title": self.title,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "evidence": self.evidence,
            "metrics": self.metrics,
            "rule_id": self.rule_id,
            "finding_id": self.finding_id,
        }
