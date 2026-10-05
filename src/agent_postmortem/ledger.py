"""The write-evidence ledger, the risk population, and the repository grouping
(docs/DESIGN.md 4.13.1; milestone N4, part N4b-2).

Pure: standard library only, no filesystem access, no subprocess. The reader
fills each attempt's `ToolCall` list and a `TranscriptState` (what reading the
transcript saw); this module turns them into a `Ledger` per attempt, decides
which attempts are in the risk population and whether that population is
complete, and groups the population's paths by repository.

Nothing here renders and nothing here runs git: per-repository observation is
N5's. A ledger claims less, never more: whatever the recorded text cannot prove
becomes a `possible` entry or a partial reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import commands
from .models import (
    LAUNCH_DIAGNOSTIC_PREFIXES,
    Attempt,
    Generation,
    Ledger,
    LedgerEntry,
    Run,
    ToolCall,
)

# --- the truth table's tool names (4.13.1, L6) ----------------------------------------

# Row 1: tool name -> where its target lives on the ToolCall.
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# Row 7: the read-only tools, which never get an entry.
READ_ONLY_TOOLS = frozenset({
    "Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch", "ToolSearch", "TaskOutput",
    "TaskGet", "TaskList", "TaskCreate", "TaskUpdate", "TaskStop", "ListAgents", "SendUserFile",
    "AskUserQuestion", "SendFeedback", "Skill", "StructuredOutput", "SubagentHandback",
    "Artifact", "ScheduleWakeup", "TodoWrite",
})
# The shell tools: a call to either carries `shell_context_unverified` (C-15).
SHELL_TOOLS = ("Bash", "Monitor")

# Completion mapping (4.13.11): ToolCall.completion -> LedgerEntry.completion.
COMPLETION = {
    "completed_ok": "success",
    "completed_error": "error",
    "backgrounded": "no_result",
    "no_result": "no_result",
    "ambiguous": "ambiguous",
}

# The closed partial-reason codes, in the fixed order of 4.13.1.
REASON_ORDER = (
    "transcript_missing",
    "transcript_unreadable",
    "undecodable_line",
    "unexpected_entry_shape",
    "unknown_entry_type",
    "truncated_field",
    "ambiguous_tool_id",
    "unpaired_write_call",
    "shell_context_unverified",
    "possible_opaque_command",
    "possible_delegation",
    "possible_background_process",
    "possible_unknown_tool",
    "unassignable_attempt",
    "discovery_incomplete",
)

# Entry types a transcript may carry that the ledger ignores (4.13.1); anything
# else that is not `user` or `assistant` gives `unknown_entry_type`.
KNOWN_ENTRY_TYPES = frozenset({
    "attachment", "system", "summary", "queue-operation", "last-prompt", "ai-title",
    "atis-latch", "mode", "permission-mode", "pr-link", "agent-name", "bridge-session",
    "custom-title", "file-history-snapshot", "file-history-delta", "cost-state", "frame-link",
})
_ARTIFACT_PREFIX = "artifact-"


def entry_type_known(entry_type: str) -> bool:
    """Whether a transcript entry type other than user and assistant is one the
    ledger ignores (4.13.1's known set, plus any type starting `artifact-`)."""
    return entry_type in KNOWN_ENTRY_TYPES or entry_type.startswith(_ARTIFACT_PREFIX)


@dataclass
class TranscriptState:
    """What reading one transcript observed beyond its tool calls; filled by
    the reader while it streams (reader-private, never exported)."""

    missing: bool = False  # the expected transcript does not exist (or C-5)
    unreadable: bool = False  # the file could not be opened
    undecodable: bool = False  # some line is not JSON
    unexpected_shape: bool = False  # a 4.13.1 shape rule was broken
    unknown_type: bool = False  # an entry type outside the known set
    # docs/DESIGN.md 4.13.6 (N6): the top-level string `cwd` values seen, first-seen order
    # (a dict used as an ordered set); cleared when the read is discarded as unreadable.
    cwds: dict = field(default_factory=dict)


def _ordered(reasons) -> list[str]:
    known = [reason for reason in REASON_ORDER if reason in reasons]
    extra = sorted(reason for reason in reasons if reason not in REASON_ORDER)
    return known + extra


# --- one call -------------------------------------------------------------------------


@dataclass(frozen=True)
class CallLedger:
    """The entries one tool call produces, and the partial reasons that call
    alone gives rise to."""

    entries: list[LedgerEntry]
    reasons: frozenset[str]


def _absolute_cwd(cwd: str | None) -> str | None:
    """A call cwd is a repository candidate only when it is absolute (L9)."""
    if isinstance(cwd, str) and cwd.startswith("/"):
        return commands.normalize_path(cwd)
    return None


def _classification(call: ToolCall):
    if call.classification is not None:
        return call.classification
    if call.tool_name in SHELL_TOOLS or call.tool_name in commands.DELEGATION_TOOLS:
        return commands.classify_call(
            call.tool_name, call.command, run_in_background=call.run_in_background, cwd=call.cwd
        )
    return None


def call_entries(call: ToolCall, transcript_name: str) -> CallLedger:
    """The ledger entries of one call by the per-call truth table (4.13.1, first
    match wins): file tools (row 1), then the classification of N4a for a shell or
    delegation call (rows 2 to 6), then the read-only tools (row 7), then every
    other tool name (row 8)."""
    tool = call.tool_name
    reasons: set[str] = set()
    if call.ambiguous:
        reasons.add("ambiguous_tool_id")
    if tool in SHELL_TOOLS:
        reasons.add("shell_context_unverified")
    completion = COMPLETION[call.completion]
    success = completion == "success"
    result_line = call.result_line if call.result_line is not None else call.line_no
    line = f"{transcript_name}:{result_line}"
    entries: list[LedgerEntry] = []

    if tool in FILE_TOOLS:
        raw = call.multiedit_path if tool == "MultiEdit" else call.path
        cwd = _absolute_cwd(call.cwd)
        path = None
        if raw is None:
            reasons.add(
                "truncated_field" if call._path_state == "truncated" else "unexpected_entry_shape"
            )
        else:
            # S3: the lexical normalization; empty or relative with no
            # absolute cwd is a shape failure, over 1,024 characters truncated.
            resolved = commands.resolve_target(cwd, raw)
            if resolved is None:
                reasons.add("unexpected_entry_shape")
            else:
                path = resolved[0]
                if resolved[1]:
                    reasons.add("truncated_field")
        entries.append(LedgerEntry(
            "observed" if success else "attempted", "tool_result", completion, path, cwd, line,
        ))
    else:
        classified = _classification(call)
        if classified is not None:
            if classified.effect is None:  # row 4: read-only, no entry
                pass
            elif classified.effect == "claimed_target":  # row 5
                for target in classified.targets:
                    if target.truncated:
                        reasons.add("truncated_field")
                    entries.append(LedgerEntry(
                        "claimed_target" if success else "attempted", "command_grammar",
                        completion, target.path, target.repo, line,
                    ))
            else:  # rows 2, 3, 6
                reasons.add(f"possible_{classified.basis}")
                # One entry per call (C3). A call with commit segments takes the
                # single distinct candidate repository of those segments, or
                # null when they name more than one; else the call's candidate.
                repo = classified.repo
                if classified.commit.segments:
                    distinct = {seg.repo for seg in classified.commit.segments}
                    repo = next(iter(distinct)) if len(distinct) == 1 else None
                entries.append(LedgerEntry(
                    "possible", classified.basis, completion, None, repo, line,
                ))
        elif tool in READ_ONLY_TOOLS:  # row 7
            pass
        else:  # row 8: every other tool name, including every mcp__ tool
            reasons.add("possible_unknown_tool")
            entries.append(LedgerEntry(
                "possible", "unknown_tool", completion, None, _absolute_cwd(call.cwd), line,
            ))

    if entries and call.result_line is None:
        reasons.add("unpaired_write_call")
    return CallLedger(entries, frozenset(reasons))


# --- one attempt ----------------------------------------------------------------------


def build_ledger(
    tool_calls: list[ToolCall],
    transcript_name: str | None,
    state: TranscriptState | None = None,
    *,
    run_reasons=(),
) -> Ledger:
    """The ledger of one attempt: every call's entries in tool-use order, the
    transcript-level reasons `state` records, and the run-level reasons the
    caller passes. 'complete' exactly when no reason applies."""
    state = state if state is not None else TranscriptState()
    reasons: set[str] = set(run_reasons)
    if state.missing:
        reasons.add("transcript_missing")
    if state.unreadable:
        reasons.add("transcript_unreadable")
    if state.undecodable:
        reasons.add("undecodable_line")
    if state.unexpected_shape:
        reasons.add("unexpected_entry_shape")
    if state.unknown_type:
        reasons.add("unknown_entry_type")
    entries: list[LedgerEntry] = []
    if not (state.missing or state.unreadable):
        for call in tool_calls:
            produced = call_entries(call, transcript_name or "")
            entries.extend(produced.entries)
            reasons |= produced.reasons
    ordered = _ordered(reasons)
    return Ledger(entries, "partial" if ordered else "complete", ordered)


# --- generation assignment (workflow runs only) -----------------------------------------


def _generations(run: Run) -> list[Generation]:
    """The run's generations of the lifecycle's own parent-index snapshot, in
    launch-line order (never a second parent read)."""
    lifecycle = run.lifecycle
    found = getattr(lifecycle, "_generations", None) if lifecycle is not None else None
    return sorted(found or [], key=lambda generation: generation.launch_line)


def _timestamps_usable(generations: list[Generation]) -> bool:
    """Every generation's launch call timestamp is present, and they strictly
    increase in launch-line order (4.13.11, FA2)."""
    previous = None
    for generation in generations:
        stamp = generation.launch_ts
        if stamp is None:
            return False
        if previous is not None:
            try:
                if not stamp > previous:
                    return False
            except TypeError:
                return False
        previous = stamp
    return True


def _at_or_before(stamp, first) -> bool:
    """Whether a launch timestamp is at or before an attempt's first timestamp;
    two timestamps that cannot be compared (naive against aware) are not."""
    try:
        return stamp <= first
    except TypeError:
        return False


def _timestamp_unplaceable(attempt: Attempt) -> bool:
    """4.13.1 (A5): the attempt has an event-bearing line with no usable `timestamp`
    (absent, not a string, or unparseable) at or before the earliest line carrying
    `first_ts`, so an event of it may precede `first_ts` and the attempt cannot be
    placed in time."""
    unusable = min((event.line_no for event in attempt.events if event.ts is None), default=None)
    if unusable is None:
        return False
    first_line = min(
        (event.line_no for event in attempt.events if event.ts == attempt.first_ts),
        default=None,
    )
    return first_line is None or unusable <= first_line


def generation_of(run: Run, attempt: Attempt) -> Generation | None:
    """The generation a workflow attempt belongs to: the one whose Workflow
    `tool_use` (launch call) timestamp is the latest at or before the attempt's
    `first_ts` (FA2: the call, not its result, bounds the generation). None when
    the attempt cannot be placed: no `first_ts`, before the first launch call, no
    generation at all, or any generation launch timestamp missing or not strictly
    increasing (then nobody can be placed)."""
    generations = _generations(run)
    if not _timestamps_usable(generations):
        return None
    first = attempt.first_ts
    if first is None or _timestamp_unplaceable(attempt):
        return None
    found = None
    for generation in generations:
        if _at_or_before(generation.launch_ts, first):
            found = generation
    return found


def is_unassignable(run: Run, attempt: Attempt) -> bool:
    """A workflow attempt generation assignment could not place; a session
    attempt is never unassignable (assignment applies to workflow attempts)."""
    return run.kind == "workflow" and generation_of(run, attempt) is None


def _launch_found_unused(run: Run) -> bool:
    """R2-4: the run has no generation but its parent snapshot holds launch evidence for it
    that was deliberately not used: an `ambiguous_launch` lifecycle, or a launch diagnostic
    (ambiguous, result-less, rejected, malformed) in the run's parent discovery diagnostics."""
    lifecycle = run.lifecycle
    if lifecycle is not None and lifecycle.source == "ambiguous_launch":
        return True
    parent = (getattr(run, "discovery", None) or {}).get("parent") or []
    return any(
        isinstance(text, str) and text.startswith(LAUNCH_DIAGNOSTIC_PREFIXES) for text in parent
    )


def unassignable_reason(run: Run, attempt: Attempt) -> str | None:
    """Why generation assignment could not place a workflow attempt (docs/DESIGN.md 4.7, field
    milestone F P6, fix round 1 S-m4), checked in the order `generation_of` checks:
    `no_launch_record` (the run has no generation and the parent holds no launch evidence for
    it), `launch_unusable` (no generation, but a launch was found and not used),
    `launch_times_unusable` (a generation's launch timestamp is missing or the
    timestamps are not strictly increasing), then `no_usable_timestamp` (no first timestamp,
    or an early line without one), else `before_launch` (the attempt starts before every
    launch call). None for a placed attempt and for a session attempt."""
    if not is_unassignable(run, attempt):
        return None
    generations = _generations(run)
    if not generations:
        return "launch_unusable" if _launch_found_unused(run) else "no_launch_record"
    if not _timestamps_usable(generations):
        return "launch_times_unusable"
    if attempt.first_ts is None or _timestamp_unplaceable(attempt):
        return "no_usable_timestamp"
    return "before_launch"


# --- the risk population (4.13.1) ---------------------------------------------------------

_OUTCOMES_AT_RISK = frozenset({"killed", "failed", "superseded", "unknown"})
_NEWEST_TERMINALS_AT_RISK = frozenset({"failed", "stopped", "unknown"})
_NOT_ACTIVITY_KINDS = frozenset({"thinking", "attachment"})


def _workflow_attempt_at_risk(run: Run, attempt: Attempt, newest: Generation | None) -> bool:
    # An attempt with no journal key (journal_state None) can never be closed by
    # the journal: it is treated like an open one (4.13.1, S2).
    if (
        attempt.outcome in _OUTCOMES_AT_RISK
        or attempt.journal_state == "open"
        or attempt.journal_state is None
    ):
        return True
    placed = generation_of(run, attempt)
    if placed is None:  # unassignable: never assumed safe
        return True
    return (
        newest is not None
        and placed is newest
        and newest.terminal in _NEWEST_TERMINALS_AT_RISK
    )


def _meaningful(event) -> bool:
    return event.kind not in _NOT_ACTIVITY_KINDS and bool(event.text.strip())


def _main_ends_in_its_interrupt(main: Attempt) -> bool:
    """The interrupt is the final meaningful main-thread event in physical order:
    the main attempt has an interrupt and no meaningful event follows the last
    user event (the kill sentinel)."""
    if main.interrupt is None:
        return False
    last_user = None
    for index, event in enumerate(main.events):
        if event.kind == "user":
            last_user = index
    after = main.events[last_user + 1:] if last_user is not None else main.events
    return not any(_meaningful(event) for event in after)


def risk_population(run: Run) -> list[Attempt]:
    """The attempts whose ledgers must be accounted for, in the total order
    `main` first and then `Run.attempts` index order (4.13.1). Workflow runs:
    outcome killed, failed, superseded, open, or unknown; the newest generation's
    attempts when that generation failed, stopped, or is unknown; and every
    unassignable attempt. Session runs: every subagent with an interrupt, and the
    main attempt when the interrupt is its final meaningful event."""
    members: list[Attempt] = []
    if run.kind == "session":
        if run.main is not None and _main_ends_in_its_interrupt(run.main):
            members.append(run.main)
        members.extend(attempt for attempt in run.attempts if attempt.interrupt is not None)
        return members
    generations = _generations(run)
    newest = generations[-1] if generations else None
    return [
        attempt for attempt in run.attempts if _workflow_attempt_at_risk(run, attempt, newest)
    ]


def _discovery_incomplete(run: Run) -> bool:
    return any(bool(diagnostics) for diagnostics in run.discovery.values())


def risk_population_completeness(run: Run) -> tuple[str, list[str]]:
    """('complete' | 'partial', reasons) for the run's own risk population: the
    reasons are `unassignable_attempt` (some workflow attempt could not be
    placed) and `discovery_incomplete` (any discovery source reported a
    diagnostic), in that order."""
    reasons: list[str] = []
    if run.kind == "workflow" and any(is_unassignable(run, a) for a in run.attempts):
        reasons.append("unassignable_attempt")
    if _discovery_incomplete(run):
        reasons.append("discovery_incomplete")
    return ("partial" if reasons else "complete", reasons)


def _attempt_ledger(run: Run, attempt: Attempt) -> Ledger:
    run_reasons = []
    if is_unassignable(run, attempt):
        run_reasons.append("unassignable_attempt")
    if _discovery_incomplete(run):
        run_reasons.append("discovery_incomplete")
    state = attempt._transcript_state
    if not isinstance(state, TranscriptState):
        state = TranscriptState()
    if attempt.transcript_path is None:
        state = TranscriptState(missing=True)
    name = attempt.transcript_path.name if attempt.transcript_path is not None else None
    return build_ledger(attempt.tool_calls, name, state, run_reasons=run_reasons)


def attach_ledgers(run: Run) -> None:
    """Set every attempt's ledger (the session main thread included) and the
    run's own risk-population completeness. Called by the reader once the run is
    fully assembled (its discovery, lifecycle, and generations are final)."""
    attempts = ([run.main] if run.main is not None else []) + list(run.attempts)
    for attempt in attempts:
        attempt.ledger = _attempt_ledger(run, attempt)
    completeness, reasons = risk_population_completeness(run)
    run.risk_population_completeness = completeness
    run.risk_population_completeness_reasons = reasons


# --- repository grouping (4.13.1, L14) ------------------------------------------------------


@dataclass
class RepoGroup:
    paths: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    refused: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class RepoGroups:
    repos: dict[str, RepoGroup] = field(default_factory=dict)
    unknown: RepoGroup = field(default_factory=RepoGroup)


class _Accumulator:
    def __init__(self) -> None:
        self.paths: set[str] = set()
        self.attempts: list[str] = []
        self.refused = False
        self.reasons: set[str] = set()

    def add(self, ref: str, *, path: str | None, refused: bool, reasons) -> None:
        if ref not in self.attempts:
            self.attempts.append(ref)
        if path is not None:
            self.paths.add(path)
        if refused:
            self.refused = True
            self.reasons.update(reasons)

    def freeze(self) -> RepoGroup:
        return RepoGroup(sorted(self.paths), list(self.attempts), self.refused,
                         _ordered(self.reasons))


_PATH_EFFECTS = frozenset({"observed", "claimed_target", "attempted"})


def attempt_refs(run: Run) -> dict[int, str]:
    """`main` for a session's main thread, otherwise `attempts[<index in
    Run.attempts>]` (it exists for every attempt, canonical id or not)."""
    refs = {id(attempt): f"attempts[{index}]" for index, attempt in enumerate(run.attempts)}
    if run.main is not None:
        refs[id(run.main)] = "main"
    return refs


def _ledger_of(attempt: Attempt) -> Ledger:
    if attempt.ledger is not None:
        return attempt.ledger
    return Ledger([], "partial", ["transcript_missing"])


def _facts_written_targets(attempt: Attempt) -> list[str]:
    """`facts.paths_written` as normalized targets: a raw path a file tool wrote is resolved
    against the call's own absolute cwd (the same placement the rescue plan uses), and the raw
    spelling stands when nothing places a relative one."""
    facts = attempt.facts
    written = facts.paths_written if facts is not None else None
    if not isinstance(written, list):
        return []
    targets: list[str] = []
    for raw in written:
        placed = False
        for call in attempt.tool_calls:
            if call.path != raw:
                continue
            cwd = call.cwd if isinstance(call.cwd, str) and call.cwd.startswith("/") else None
            base = commands.normalize_path(cwd) if cwd is not None else None
            resolved = commands.resolve_target(base, raw)
            if resolved is not None and resolved[0] is not None:
                targets.append(resolved[0])
                placed = True
        if not placed:
            targets.append(commands.normalize_path(raw) if raw.startswith("/") else raw)
    return targets


def attempt_ledger_paths(
    attempt: Attempt, effects: frozenset[str], *, include_facts_written: bool = False,
) -> list[str]:
    """The distinct normalized paths one attempt's ledger names with an effect in `effects`, in
    first-seen order (docs/DESIGN.md 4.13.1, 4.13.8). `include_facts_written` adds
    `facts.paths_written`, normalized the way the rescue plan normalizes it. Each surface that
    counts paths (the AGENT table, scan, the relaunch brief) chooses its effect set and its
    population itself; this is the one place that reads an attempt's paths."""
    found: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            found.append(path)

    if include_facts_written:
        for target in _facts_written_targets(attempt):
            add(target)
    if attempt.ledger is not None:
        for entry in attempt.ledger.entries:
            if entry.path is not None and entry.effect in effects:
                add(entry.path)
    return found


def risk_paths(run: Run) -> tuple[list[str], bool]:
    """(distinct paths, complete) over the run's 4.13.1 risk population: every ledger path of
    effect `observed`, `claimed_target`, or `attempted`, plus `facts.paths_written`. `complete`
    is true only when the population itself is complete and every ledger in it is (a
    population attempt with no ledger is partial); the count is a lower bound otherwise."""
    paths: list[str] = []
    seen: set[str] = set()
    complete = risk_population_completeness(run)[0] == "complete"
    for attempt in risk_population(run):
        if _ledger_of(attempt).completeness != "complete":
            complete = False
        for path in attempt_ledger_paths(attempt, _PATH_EFFECTS, include_facts_written=True):
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths, complete


def group_by_repo(run: Run) -> RepoGroups:
    """The risk population's ledger paths grouped by candidate repository (L14).
    A partial ledger keeps its repositories and paths and marks them refused; a
    shell call, not a ledger entry, sends its attempt to `unknown`."""
    refs = attempt_refs(run)
    repos: dict[str, _Accumulator] = {}
    unknown = _Accumulator()
    for attempt in risk_population(run):
        ref = refs[id(attempt)]
        built = _ledger_of(attempt)
        partial = built.completeness == "partial"
        for entry in built.entries:
            if entry.repo is not None:
                target = repos.setdefault(entry.repo, _Accumulator())
            else:
                target = unknown
            target.add(
                ref, path=entry.path if entry.effect in _PATH_EFFECTS else None,
                refused=partial, reasons=built.reasons,
            )
        if partial and all(entry.repo is None for entry in built.entries):
            unknown.add(ref, path=None, refused=True, reasons=built.reasons)
        if any(call.tool_name in SHELL_TOOLS for call in attempt.tool_calls):
            unknown.add(ref, path=None, refused=True, reasons=built.reasons)
    return RepoGroups(
        {repo: repos[repo].freeze() for repo in sorted(repos)}, unknown.freeze()
    )
