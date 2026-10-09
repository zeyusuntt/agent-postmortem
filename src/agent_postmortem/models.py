"""Data model for agent-postmortem.

reader.py fills these; detectors read them; report.py renders them. Every
dataclass carries a to_dict() so report.py and the --json output do not
re-implement Path and datetime conversion in more than one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the classifier is pure; models only names its result type
    from .commands import CallClass


class KeyedDiagnostic(str):
    """A reader diagnostic that would quote a journal key (N3b P10b, D-h).

    Its plain string is the safe form, which names the journal line instead
    of the key (`key@<line>`); a sink that does nothing special therefore
    can never print a key. `.raw` carries the exact text v0.2 printed, for
    the raw sinks only (raw text and raw JSON ask for it explicitly through
    raw_diagnostic, before any Redactor call). A journal key is arbitrary
    text a run's author chose, and a valid-shaped one (`v2:` plus 64 hex)
    can embed a real agent id, so no other sink may read `.raw`."""

    raw: str

    def __new__(cls, safe: str, raw: str) -> "KeyedDiagnostic":
        obj = super().__new__(cls, safe)
        obj.raw = raw
        return obj


class QualityNote(str):
    """A reader diagnostic that is only a data-quality note (docs/DESIGN.md
    4.13.1, A2): it cannot hide an attempt, a tool call, or a launch, so it
    stays in `Run.diagnostics` and never reaches a `Run.discovery` list. The
    reader tags the note where it raises it; nothing matches message text."""


def discovery_part(diagnostics) -> list[str]:
    """The diagnostics of `diagnostics` that belong in a discovery list: every
    one that is not a QualityNote, in order."""
    return [item for item in diagnostics if not isinstance(item, QualityNote)]


def raw_diagnostic(diagnostic: str) -> str:
    """The raw form of a diagnostic for a raw sink: a KeyedDiagnostic's
    `.raw`, any other diagnostic unchanged. Call it only where the level is
    raw."""
    return diagnostic.raw if isinstance(diagnostic, KeyedDiagnostic) else str(diagnostic)


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
    # The tool_result block's own tool_use_id (docs/DESIGN.md 4.13.2, v0.3
    # N1): set only for a tool_result-kind Event, None otherwise. Reader-
    # internal plumbing, not a documented v0.1/v0.2 field: it exists so a
    # MarkerHit (which carries tool_use_id but no text of its own) can be
    # matched back to the exact Event it was built from even when a single
    # user entry carries more than one tool_result block on the same
    # line_no (two parallel tool calls whose results arrive together).
    tool_use_id: str | None = None

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
            "tool_use_id": self.tool_use_id,
        }


@dataclass
class MarkerHit:
    """One typed marker hit (docs/DESIGN.md 4.1, 4.13.2, v0.3 N1), built by
    the reader from the marker provenance registry (markers.REGISTRY) while
    parsing an attempt's own transcript. Every field here is part of the
    binding model contract; detectors consume a hit only when `anchored and
    not in_payload`."""

    marker: str  # canonical id from markers.REGISTRY
    producer: str  # 'harness_user' | 'harness_tool_notice' | 'command_output'
    # | 'runtime_assistant' | 'codex_companion'
    event_line: int  # physical line of the entry carrying the marker
    tool_use_id: str | None  # set for every tool_result shape; None otherwise
    result_line: int | None  # equals event_line for a tool_result shape; None otherwise
    anchored: bool  # the registry predicate held at its anchor
    in_payload: bool  # the text matched only outside the anchor (never evidence)

    def to_dict(self) -> dict:
        return {
            "marker": self.marker,
            "producer": self.producer,
            "event_line": self.event_line,
            "tool_use_id": self.tool_use_id,
            "result_line": self.result_line,
            "anchored": self.anchored,
            "in_payload": self.in_payload,
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
    # Bash's or Monitor's own command, exact. Longer than 2048 chars: None here, with
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
    # For a Bash call that is a commit call under docs/DESIGN.md 4.13.1's
    # commit grammar (C-9, amended by A1) and completed_ok or
    # completed_error: the last k line-start summary matches of the FULL
    # result text (commands.commit_ids), at most 20.
    commit_ids: tuple[str, ...] = ()
    # docs/DESIGN.md 4.2's v0.2 reader changes (F5 hardening): set on every
    # ToolCall sharing a tool_use_id with at least one other ToolCall in the
    # same transcript (a duplicate tool_use id, which makes it impossible to
    # know which tool_result truly belongs to which call). Checked first by
    # `completion`, ahead of every other state: an ambiguous call's own
    # is_error/exit_code/backgrounded fields can never be trusted, whichever
    # one of the duplicates happened to receive them.
    ambiguous: bool = False
    # docs/DESIGN.md 4.1 (v0.3, N4b-1), reader-private and never in schema 2:
    # the tool_use input's own `run_in_background` value for a Bash or Monitor
    # call, exactly as written (None when absent and for every other tool).
    run_in_background: object = None
    # A MultiEdit call's input `file_path` (at most 1024 chars, else None).
    # Kept apart from `path` so schema-2 paths_written, in_flight, and rescue
    # (which read `path`) never change.
    multiedit_path: str | None = None
    # The 4.13.1 classification of this call (commands.classify_call),
    # computed from the FULL command while streaming (C-11) and with the
    # anchored bash_backgrounded hit of its result; None for every tool
    # outside rows 2 to 6 of the truth table.
    classification: "CallClass | None" = None
    # Reader-private (never in to_dict, out of equality and repr): why `path`
    # (or `multiedit_path`) is None for a file tool -- 'missing' (absent or not
    # a string) or 'truncated' (over 1,024 characters); None otherwise. The
    # ledger (4.13.1) turns it into `unexpected_entry_shape` or `truncated_field`.
    _path_state: str | None = field(default=None, compare=False, repr=False)

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
            "run_in_background": self.run_in_background,
            "multiedit_path": self.multiedit_path,
            "classification": (
                self.classification.to_dict() if self.classification is not None else None
            ),
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
    # (field milestone F, FA6) shell CALLS with completion completed_ok that ran a
    # commit command (commands.commit_command_ran, quiet forms included) and yielded
    # no commit id; FACTS_UNKNOWN when the facts are unknown, never a false 0. Default
    # 0 for a hand-built fact.
    commit_commands_without_id: "int | str" = 0

    def to_dict(self) -> dict:
        return {
            "paths_written": self.paths_written,
            "in_flight": self.in_flight,
            "commits": self.commits,
            "commit_commands_without_id": self.commit_commands_without_id,
            "cwds": self.cwds,
            "background_unfinished": self.background_unfinished,
            "last_tool": self.last_tool,
            "seconds_since_last_tool_at_end": self.seconds_since_last_tool_at_end,
        }


@dataclass
class LedgerEntry:
    """One (tool call, target) entry of an attempt's write ledger (docs/DESIGN.md
    4.1, 4.13.1). The five-valued fields are closed enums; `path` and `repo` are
    None when the call names none (`repo` is a lexical candidate, never a
    trusted location); `line` is `<transcript file name>:<line>`."""

    effect: str  # 'observed' | 'claimed_target' | 'attempted' | 'possible'
    basis: str  # 'tool_result' | 'command_grammar' | 'delegation' | 'opaque_command'
    # | 'background_process' | 'unknown_tool'
    completion: str  # 'success' | 'error' | 'no_result' | 'ambiguous'
    path: str | None
    repo: str | None
    line: str

    def to_dict(self) -> dict:
        return {
            "effect": self.effect,
            "basis": self.basis,
            "completion": self.completion,
            "path": self.path,
            "repo": self.repo,
            "line": self.line,
        }


@dataclass
class Ledger:
    """Attempt.ledger's own shape (docs/DESIGN.md 4.1, 4.13.1): the entries in
    tool-use order, 'complete' only when there is no partial reason, and the
    closed partial-reason codes in the fixed order of 4.13.1."""

    entries: list[LedgerEntry]
    completeness: str  # 'complete' | 'partial'
    reasons: list[str]  # [] when complete

    def to_dict(self) -> dict:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "completeness": self.completeness,
            "reasons": list(self.reasons),
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
    # (docs/DESIGN.md 4.13.2, v0.3 N1) every typed marker hit built while
    # parsing this attempt's own transcript, in the order encountered. []
    # when the attempt carries none; never None -- a transcript that exists
    # but could not be read still produces [], the same way `facts` still
    # produces a FACTS_UNKNOWN record rather than being silently absent,
    # except a MarkerHit is built per event, not derived from a whole-
    # transcript read, so there is nothing to mark UNKNOWN here.
    marker_hits: list["MarkerHit"] = field(default_factory=list)
    # docs/DESIGN.md 4.1 (v0.3, N4b-1): the journal state machine's own
    # closure of this attempt -- 'open' | 'completed' | 'failed' |
    # 'superseded' for a workflow attempt with a journal key; None for a
    # session main thread, a session subagent, and a workflow attempt with no
    # journal key. Separate from `outcome` (a superseded attempt whose
    # transcript ends in the kill sentinel has outcome 'killed').
    journal_state: str | None = None
    # docs/DESIGN.md 4.1 (v0.3, N4b-2): this attempt's write ledger. The reader
    # always sets it once a run is assembled (never None from N4 on; a
    # transcript that is missing or unreadable gives a partial ledger with no
    # entries); a hand-built Attempt may leave it None.
    ledger: "Ledger | None" = None
    # docs/DESIGN.md 4.1 (v0.3, N5c-1, S11): a typed private field, None for a
    # workflow attempt and the session main thread; for a session subagent,
    # whether it returned a result to its parent (reader._LaunchLog.child_returned).
    # Never in to_dict, never exported.
    returned_to_parent: bool | None = None
    # docs/DESIGN.md 4.1, 4.13.1 (v0.3.1, G-A): the return state of a session subagent (T0 to
    # T5), its reason, and the number of delivered resumes; None / None / 0 for every other
    # attempt. `returned_to_parent` is exactly `return_state == "returned"`.
    return_state: str | None = None
    return_reason: str | None = None
    resumes: int = 0
    # Reader-internal (never in to_dict): what decided a `reported_failed` state,
    # (source, transcript path, line) with source `launch_result` or `notification`.
    return_evidence: tuple | None = field(default=None, compare=False, repr=False)
    # docs/DESIGN.md 4.3 (G3, v0.3.1): claims.SelfReport of the journal `result` this attempt
    # was closed with (None for a hand-built attempt or one with no dict result). Reader-internal:
    # in no to_dict, no export; it keeps no key name and no value.
    _self_report: object = field(default=None, compare=False, repr=False)
    # Reader-internal (never in to_dict): what reading this attempt's transcript
    # saw that the ledger's reasons need (ledger.TranscriptState); None for a
    # hand-built attempt.
    _transcript_state: object = field(default=None, compare=False, repr=False)
    # Reader-internal (never in to_dict): the launches and results a session
    # main transcript recorded while it streamed, for Run.discovery's launch
    # reconciliation (reader._LaunchLog); None for every other attempt.
    _launch_log: object = field(default=None, compare=False, repr=False)
    # docs/DESIGN.md 4.13.6 (v0.3, N6): every top-level non-empty string `cwd` on a JSON
    # object line of this attempt's own transcript, first-seen order, deduplicated. Feeds
    # `--here` (reader.run_cwd_set); internal only, in no to_dict, JSON, or rendering.
    entry_cwds: tuple[str, ...] = field(default=(), compare=False, repr=False)

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
            "marker_hits": [hit.to_dict() for hit in self.marker_hits],
            "journal_state": self.journal_state,
            "ledger": self.ledger.to_dict() if self.ledger is not None else None,
            "return_state": self.return_state,
            "return_reason": self.return_reason,
            "resumes": self.resumes,
        }


def effective_end(attempt: "Attempt") -> str:
    """docs/DESIGN.md 5.3b (E1): what the evidence establishes about how an attempt ended. A
    workflow attempt (and the session main thread): its `outcome`. A session subagent: the kill
    sentinel (`outcome` killed) first, then its return state (`returned` completed,
    `reported_failed` failed, `reported_killed` killed, `pending` open, `unknown` unknown).
    The internal `outcome` stays private and keeps driving risk, ledgers, WASTED and rescue."""
    state = attempt.return_state
    if state is None:
        return attempt.outcome
    if attempt.outcome == "killed":
        return "killed"
    return {
        "returned": "completed", "reported_failed": "failed", "reported_killed": "killed",
        "pending": "open",
    }.get(state, "unknown")


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


# R2-4: the launch-evidence diagnostics a parent index builds (reader.ParentIndex._rebuild_
# generations); a run whose Run.discovery['parent'] holds one has a launch that was found and
# not used, which is not the same as no launch record.
LAUNCH_DIAGNOSTIC_PREFIXES = (
    "ambiguous launch", "launch without result", "rejected launch result",
    "malformed launch result",
)


@dataclass
class Lifecycle:
    """Run.lifecycle for a workflow run (docs/DESIGN.md 4.1, 4.13.3, v0.3
    N2); always None for a session (sessions carry no Workflow launch and no
    generation to pair, D11)."""

    terminal: str  # 'completed' | 'failed' | 'stopped' | 'journal_resolved'
    # | 'unknown' | 'none'
    source: str | None  # 'notification' | 'task_stop' | 'launch_result' | 'journal'
    # | 'conflict' | 'unrecognized_status' | 'incomplete_generation'
    # | 'parent_unavailable' | 'ambiguous_launch'; None exactly when terminal
    # is 'none'
    activity: str  # 'recent' | 'stale' | 'unknown' (stat failed)
    generations: int  # >= 0; 0 for a run with no associated launch
    diagnostics: list[str]  # [] when none fired
    # Internal only (N1N2 review finding 2, fixed after cross-model review):
    # not one of the five public fields docs/DESIGN.md 4.1/5.3b document for
    # Lifecycle, so it is excluded from equality/repr (never breaks a test
    # or caller that compares/prints a Lifecycle built without naming it)
    # and from to_dict() below. The parent-transcript line that decided
    # `terminal`, captured in the exact same snapshot that produced this
    # Lifecycle -- reader.lifecycle_terminal_line(run) reads it back so
    # workflow_failed cites the evidence that actually decided the stored
    # terminal, never a fresh re-query that could now see a newer generation
    # appended to the parent transcript after this Lifecycle was computed.
    _terminal_line: int | None = field(default=None, compare=False, repr=False)
    # (v0.3, N4b-1) every Generation of this run in the same parent-index
    # snapshot that produced this Lifecycle, so a later reader of generation
    # facts (result_ts) never needs a second parent read. Internal like
    # _terminal_line: out of equality, repr, and to_dict().
    _generations: list = field(default_factory=list, compare=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "terminal": self.terminal,
            "source": self.source,
            "activity": self.activity,
            "generations": self.generations,
            "diagnostics": self.diagnostics,
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
    # docs/DESIGN.md 7.4's D8 (redefined by v0.3 N2, 4.13.3): whether this run
    # has actually finished. For a workflow, derived from Lifecycle.terminal:
    # true for 'completed'/'failed'/'stopped'/'journal_resolved', false for
    # 'unknown'/'none'. None for a session (there is no lifecycle model to
    # report, D11). Defaults to True so a Run built directly in a test,
    # without setting this, is never mistaken for "still running".
    finished: bool | None = field(default=True, kw_only=True)
    # (v0.3, N2, 4.13.3) None for a session; never None for a workflow run
    # once N2 lands.
    lifecycle: Lifecycle | None = field(default=None, kw_only=True)
    # (v0.3, N4b-1, 4.13.1) exactly the keys journal, parent, transcripts,
    # meta; each holds the diagnostics that source reported, in order; []
    # means that source was clean. Independent of `diagnostics` and `status`.
    discovery: dict = field(
        default_factory=lambda: {"journal": [], "parent": [], "transcripts": [], "meta": []},
        kw_only=True,
    )
    # (v0.3, N4b-2, 4.13.1) 'complete' | 'partial' once the reader has assembled
    # the run (None only on a hand-built Run), and the closed reason codes
    # ('unassignable_attempt', 'discovery_incomplete', in that order; [] when
    # complete). Private: in to_dict(), never in export_run().
    risk_population_completeness: str | None = field(default=None, kw_only=True)
    risk_population_completeness_reasons: list = field(default_factory=list, kw_only=True)

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
            "lifecycle": self.lifecycle.to_dict() if self.lifecycle is not None else None,
            "discovery": {key: list(value) for key, value in self.discovery.items()},
            "risk_population_completeness": self.risk_population_completeness,
            "risk_population_completeness_reasons": list(
                self.risk_population_completeness_reasons
            ),
        }


@dataclass(frozen=True)
class TitleParts:
    """A finding title that names a path, as parts (docs/DESIGN.md 4.13.4, 5.1; F fix 7, W7-2):
    a fixed `template` holding exactly one `{path}` placeholder and the `path` value. A renderer
    formats the template once, with the path rendered by the level's path function, and does not
    scan the result, so the path is never in-band for the free-text scanner to rediscover (a
    backquote, a space or an operator inside it cannot change where it ends)."""

    template: str
    path: str

    def raw(self) -> str:
        """The sentence with the real path (what `raw` prints). One substitution of the first
        placeholder: a path that holds the text `{path}` is data, never a second placeholder."""
        head, _sep, tail = self.template.partition("{path}")
        return head + self.path + tail


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
    # F fix 7 (W7-2, docs/DESIGN.md 4.13.4): set by a detector whose title names a path. `title`
    # then holds the same sentence with the real path (`title_parts.raw()`); a renderer uses the
    # parts, never `title`, at share and local.
    title_parts: TitleParts | None = None

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


@dataclass
class Generation:
    """One Workflow launch associated with an inspected run (docs/DESIGN.md
    4.1, 4.13.3, v0.3 N1), built by reader.ParentIndex from the parent
    transcript's own Workflow tool_use/tool_result pairs."""

    launch_line: int  # the Workflow tool_use's own physical line
    tool_use_id: str  # that tool_use's own 'id'
    resume_from: str | None  # its input.resumeFromRunId, when a string
    result_line: int | None  # the unique matching tool_result line; None for
    # a result-less resume (incomplete generation)
    run_id: str  # the inspected run id this generation belongs to
    task_id: str | None  # toolUseResult.taskId; None when incomplete
    launch_status: str | None  # toolUseResult.status as written
    # ('async_launched' on every launch observed so far); None when incomplete
    terminal: str | None  # 'completed' | 'failed' | 'stopped' | 'unknown' | None
    terminal_source: str | None  # 'notification' | 'task_stop' | 'launch_result'
    # | 'conflict' | 'unrecognized_status' | 'incomplete_generation';
    # None when terminal is None
    terminal_line: int | None  # the accepted notification, TaskStop, or launch
    # result line that set terminal; None otherwise
    diagnostics: list[str]  # each names the physical lines involved
    # (v0.3, N4b-1) the parsed timestamp of the launch result line, taken from
    # the same parent-index snapshot that produced the lifecycle; None when
    # there is no result line, it has no timestamp, or it does not parse.
    result_ts: datetime | None = None
    # (field milestone F, FA2) the parsed timestamp of the assistant entry that
    # holds the Workflow tool_use block (launch_line), from the same snapshot;
    # None when the entry has no timestamp or it does not parse. Generation
    # assignment (ledger.generation_of) keys on it, not on result_ts.
    launch_ts: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "launch_line": self.launch_line,
            "tool_use_id": self.tool_use_id,
            "resume_from": self.resume_from,
            "result_line": self.result_line,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "launch_status": self.launch_status,
            "terminal": self.terminal,
            "terminal_source": self.terminal_source,
            "terminal_line": self.terminal_line,
            "diagnostics": self.diagnostics,
            "result_ts": _iso(self.result_ts),
            "launch_ts": _iso(self.launch_ts),
        }
