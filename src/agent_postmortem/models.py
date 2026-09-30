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
class ToolCall:
    """One tool_use/tool_result pair, filled by the reader while streaming
    from the FULL decoded transcript entry -- never from Event.text, which is
    already bounded to 400 chars (docs/DESIGN.md 4.2/5.1's v0.2 reader
    changes). Reader-internal and private: report.py's export allowlist
    (5.3) never includes a ToolCall directly (a command is private), only
    the facts AttemptFacts derives from a list of them.

    tool_use_id/tool_name/line_no/ts describe the tool_use block itself
    (line_no and ts are the tool_use entry's own, not the result's). The
    remaining fields start at their 'no result yet' default and are filled
    in when the matching tool_result is seen, looked up by tool_use_id (not
    by position -- docs/DESIGN.md 5.1's v0.2 reader changes: two tool_use
    blocks in one assistant message can have their results arrive in either
    order)."""

    tool_use_id: str
    tool_name: str | None
    line_no: int
    ts: datetime | None
    # file_path/notebook_path of an Edit, Write, or NotebookEdit call, taken
    # from the parsed tool_use input (never the excerpt): at most 1024
    # chars, else None. None for every other tool.
    path: str | None = None
    # Bash's own command, exact. Longer than 2048 chars: None here, with
    # command_sha256 set instead so a long command is never silently
    # truncated into something that reads like a different, shorter one.
    command: str | None = None
    command_sha256: str | None = None
    # The tool_use entry's own top-level 'cwd' field, normalized with
    # os.path.normpath (a pure string operation: no symlink resolution, no
    # filesystem access).
    cwd: str | None = None
    # The line number of the tool_result carrying this call's tool_use_id,
    # or None when no such result was ever seen in this transcript.
    result_line: int | None = None
    is_error: bool | None = None
    # The tool_result carries the BASH_BACKGROUNDED marker (both fragments).
    backgrounded: bool = False
    # Only when the result states one in the runtime's own form -- the same
    # parsing the reader already does for Event.exit_code.
    exit_code: int | None = None
    # For a Bash call whose command contains 'git commit': every match of
    # reader._COMMIT_ID_RE on the FULL result text, at most 20.
    commit_ids: tuple[str, ...] = ()
    # docs/DESIGN.md 4.2's v0.2 reader changes (F5 hardening): set on every
    # ToolCall sharing a tool_use_id with at least one other ToolCall in the
    # same transcript (a duplicate tool_use id, which makes it impossible to
    # know which tool_result truly belongs to which call). Checked first by
    # `completion`, ahead of every other state: an ambiguous call's own
    # is_error/exit_code/backgrounded fields can never be trusted, whichever
    # one of the duplicates happened to receive them.
    ambiguous: bool = False

    @property
    def completion(self) -> str:
        """'ambiguous' (this tool_use_id was duplicated in the transcript --
        checked first, since nothing else about the call can be trusted
        then), 'completed_ok' (result seen, is_error false, not backgrounded,
        and the parsed exit code -- if any -- is 0), 'completed_error'
        (is_error true, or a parsed nonzero exit code even when is_error is
        false: a contradiction the runtime can produce, and exit code wins),
        'backgrounded', or 'no_result'. Backgrounded is checked before
        is_error: a backgrounded Bash call is a distinct, still-pending
        state, not an ordinary tool error."""
        if self.ambiguous:
            return "ambiguous"
        if self.result_line is None:
            return "no_result"
        if self.backgrounded:
            return "backgrounded"
        if self.is_error:
            return "completed_error"
        if self.exit_code is not None and self.exit_code != 0:
            return "completed_error"
        return "completed_ok"

    def to_dict(self) -> dict:
        return {
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "line_no": self.line_no,
            "ts": _iso(self.ts),
            "path": self.path,
            "command": self.command,
            "command_sha256": self.command_sha256,
            "cwd": self.cwd,
            "result_line": self.result_line,
            "is_error": self.is_error,
            "backgrounded": self.backgrounded,
            "exit_code": self.exit_code,
            "commit_ids": list(self.commit_ids),
            "ambiguous": self.ambiguous,
            "completion": self.completion,
        }


# docs/DESIGN.md 4.1's v0.2 reader changes: the literal value used in
# AttemptFacts.to_dict() for any field the reader could not establish
# because the attempt's transcript exists but could not be read (never for
# a merely missing transcript -- Attempt.facts is None for that instead, see
# Attempt.to_dict()). A plain string, not None, so a caller that only reads
# to_dict()'s JSON-shaped output can tell the two apart from an ordinary
# empty result (an empty list, a zero count) without a side channel.
FACTS_UNKNOWN = "UNKNOWN"


@dataclass
class AttemptFacts:
    """Facts about one attempt derived from its ToolCall list (docs/
    DESIGN.md 4.1/4.2's v0.2 reader changes). Built once, after the
    attempt's transcript is fully read, by reader._build_attempt_facts.

    Every field below is either its documented type or the literal string
    FACTS_UNKNOWN ('UNKNOWN') when the transcript existed but could not be
    read; the type union is left informal (not `| Literal['UNKNOWN']`) since
    nothing in this codebase runs a static type checker over it."""

    paths_written: list[str]
    in_flight: list[dict]
    commits: list[str]
    cwds: list[str]
    background_unfinished: int
    last_tool: str | None
    seconds_since_last_tool_at_end: float | None

    def to_dict(self) -> dict:
        return {
            "paths_written": self.paths_written,
            "in_flight": self.in_flight,
            "commits": self.commits,
            "cwds": self.cwds,
            "background_unfinished": self.background_unfinished,
            "last_tool": self.last_tool,
            "seconds_since_last_tool_at_end": self.seconds_since_last_tool_at_end,
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
    # 'completed' | 'failed' | 'killed' | 'superseded' | 'unknown'. For a
    # workflow attempt this is decided by the per-key journal state machine
    # (docs/DESIGN.md 4.1): a 'result' or 'failed' journal event closes it
    # 'completed' / 'failed' (even over a trailing kill sentinel -- the
    # journal's own report wins, though the sentinel is still kept as
    # Interrupt evidence, see below); being superseded by a later 'started'
    # for the same key closes it 'killed' when its own transcript carries
    # the sentinel (the v0.1 silent retry) or 'superseded' otherwise (a
    # reader diagnostic then forces Run.status INCOMPLETE). An attempt the
    # state machine never closes (still open when the journal ends) falls
    # back to the transcript alone, same as every session attempt always
    # does (session runs have no journal at all).
    outcome: str
    usage: dict  # summed input/output/cache tokens
    # Set by the reader whenever this attempt's own transcript ends in the
    # kill sentinel (docs/DESIGN.md 4.4), independent of outcome above: a
    # 'failed' attempt can still carry an Interrupt as evidence.
    interrupt: Interrupt | None = None
    # docs/DESIGN.md section 3: the agent's own self-reported return value,
    # read only for an attempt the journal state machine closed with a
    # 'result' event (never 'failed', never one still open). None in every
    # other case. For a dict result: {'type': 'dict', 'keys': [sorted key
    # names, at most 40, each truncated to 64 chars], 'claims': {...an
    # allowlisted subset of self-reported claims...}}. For a string result:
    # {'type': 'text', 'text_length': <int>} -- the string itself is private
    # (up to 25k chars observed) and is never stored here or printed.
    reported: dict | None = None
    # The 1-based journal.jsonl line number of the 'result' event `reported`
    # was built from (None when reported is None). Reader-internal: it is
    # how detectors/claim_contradiction.py builds its evidence line without
    # re-scanning the journal; report.py's export allowlist never includes
    # it directly (docs/DESIGN.md 5.3).
    reported_line: int | None = None
    # docs/DESIGN.md 4.1/4.2's v0.2 reader changes: every tool call of this
    # attempt, in transcript order (tool_use order, not result-arrival
    # order). Reader-internal/private (a command is private): the export
    # allowlist (5.3) never includes this, only Attempt.facts.
    tool_calls: list["ToolCall"] = field(default_factory=list)
    # Derived from tool_calls once the transcript is fully read. None only
    # when this attempt has no transcript at all (a missing file); a
    # transcript that exists but could not be read still produces an
    # AttemptFacts, with the fields that could not be established set to
    # FACTS_UNKNOWN (models.py).
    facts: "AttemptFacts | None" = None

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
            "reported": self.reported,
            "reported_line": self.reported_line,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "facts": self.facts.to_dict() if self.facts is not None else None,
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
    # Every rule this finding backs, in priority order (unit M1d). Most
    # detectors back at most one rule, so rule_ids is just (rule_id,); a
    # detector whose one finding backs more than one rule (silent_retry's
    # R-RETRY-SAFE plus R-CAP-CONCURRENCY on a burst) lists every rule here
    # instead of emitting a duplicate Finding per rule. __post_init__ keeps
    # this and rule_id in sync: passing only rule_id fills rule_ids in as
    # (rule_id,); passing only rule_ids fills rule_id in as rule_ids[0].
    # This keeps every caller that only ever set rule_id (every detector but
    # silent_retry, every test fixture) working unchanged.
    rule_ids: tuple[str, ...] = ()
    # sha256(run_id, detector, agent_id, first evidence path:line)[:12]
    # (docs/DESIGN.md 4.1), computed by detectors/base.py's make_finding_id.
    # Defaults to '' for a Finding built directly in a test rather than by a
    # detector; every detector module sets a real one.
    finding_id: str = ""

    def __post_init__(self) -> None:
        if not self.rule_ids:
            if self.rule_id is not None:
                self.rule_ids = (self.rule_id,)
        elif self.rule_id is None:
            self.rule_id = self.rule_ids[0]

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
            "rule_ids": list(self.rule_ids),
            "finding_id": self.finding_id,
        }
