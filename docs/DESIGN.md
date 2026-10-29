# agent-postmortem design (v0.1 contract)

Status: binding for every implementer. v0 written 2026-09-16; v0.1 revised
2026-09-22 after a cross-model design review (43 findings, dispositions in
section 12). Change this file in the same commit as any code that departs
from it.

## 0. The problem, in one paragraph

Multi-agent coding runs fail silently. On one machine, 110 Claude Code
Workflow runs over five weeks show 39 runs where an agent was killed and
restarted from scratch with no journal event and no notification; the
transcript records the kill as "[Request interrupted by user]" although no
human interrupted. The same audit's scan reported 38.3 hours of wasted
attempt time (the sum of non-final attempt durations), rounded to 38 in the
README. Other runs lost finished review work when a
structured-output payload arrived corrupted, or when a sandboxed reviewer
could not create its report file and the pipeline treated a truthy return as
success. Each lesson was learned by hand, written into a prompt-injection
hook, forgotten, and relearned in the next project. agent-postmortem turns
the run directories the harness already leaves behind into a verdict,
evidence, a rule, and a trend line that shows whether the rule worked, and
a Claude Code plugin reports the verdict inside the session that produced it.

## 1. Scope of v0.1

In:
- Two run kinds. `workflow`: a directory named `wf_*` under
  `<root>/<slug>/<session>/subagents/workflows/`. `session`: a main-session
  transcript `<root>/<slug>/<session>.jsonl` together with the Agent-tool
  subagent transcripts under `<root>/<slug>/<session>/subagents/agent-*.jsonl`.
  Workflow runs nested under a session are separate runs, never folded into
  the session run, so nothing is counted twice.
- Interrupt attribution with four causes (section 4.4).
- Structural privacy: pseudonyms for every user-authored name in every output,
  an export allowlist, a repository privacy audit, and a public tree produced
  from a sanitized export (section 5).
- A Claude Code plugin: hooks on Stop, StopFailure, SessionEnd, SessionStart
  and PostToolUse(Workflow), plus a `/postmortem` skill (section 7).
- Distribution on PyPI, `uvx agent-postmortem`, a `demo` subcommand, CI, a
  release workflow, and a README on the first-screen pattern (section 8).

Out (recorded so nobody drifts into it): Codex CLI sessions and other
harnesses (the reader keeps its adapter boundary), dollar cost, a web UI,
editing settings.json, LLM calls, any network call from the CLI itself. The
single sanctioned network action is the plugin's optional one-time install
of the CLI during SessionStart (section 7.3), and it is opt-out.

Three verbs, in order of value: diagnose (`scan`, `run`, hooks), solve
(`rules`, `apply`, `--bug-report`), keep improving (`trend`).

## 2. Non-negotiables

- Python 3.11+, standard library only at runtime. `uv` for dev, `pytest` for
  tests, `ruff` for lint (dev only, in `[project.optional-dependencies].dev`).
- Read-only over inspected data, repositories included (4.9). The only
  writes: the tool's own state directory. Never inside a run directory or a
  transcript, and never an index, a stash, an object, or a config value
  in a repository it observes.
- R3 (v0.2, unit M6, section 9). The tool has no code path that writes an
  instruction file; rules are printed as text and diff, and a human applies
  them. `apply` prints a rule's managed block as a unified diff against a
  target file and never writes the target; there is no `--write` flag and
  no way to ask it to.
- Redaction on by default in every output that a person or a machine reads:
  text, JSON, bug report, hook messages. `--no-redact` is the only way to see
  raw paths, session ids, and user-authored names. The state directory is
  private local state (owner-only permissions) and is the one place raw
  locators may be stored; it is never printed.
- Fixtures are synthetic. See section 6 and `tests/test_privacy.py`.
- Verdict-first output. The first ten lines of a report carry the verdict and
  the one number a reader would repeat.
- Honesty over drama. A finding claims a cause only when the evidence is
  affirmative; otherwise it says "unattributed" and gives the measurement.
- Cold-run budget: `scan` over about 100 workflow runs plus 50 sessions (a
  few GB) finishes in under 30 seconds. Transcripts are read line by line;
  never `json.load` a whole transcript; never keep raw lines.
- Tolerant parsing: a bad line becomes a diagnostic string on the Run, never
  an exception. A run whose inputs could not be inspected carries analysis
  status INCOMPLETE (section 4.6) and is never reported CLEAN. (0.3.4) A line
  nested deeper than Python's parser can follow raises RecursionError rather
  than a JSONDecodeError; every read of run data (transcripts, journal.jsonl,
  an agent's meta.json, the parent transcript, and the hook, watch and rescue
  readers) treats it exactly like a line that is not JSON, with the
  diagnostic `<file>:<line>: nested too deeply to parse` (the parent
  transcript's is `<file>:<line>: undecodable parent transcript line: nested
  too deeply to parse`). `load_run` counts the physical lines its read
  skipped (not JSON, nested too deeply, not a JSON object; a last line with
  no newline yet is a live write and is not counted) on `Run.skipped_lines`,
  and the meta.json files it could not parse on `Run.skipped_files`; neither
  is exported. A workflow run also counts the parent transcript lines it
  could not parse or that are not a JSON object (`<file>:<line>: parent
  transcript line that is not a JSON object`, which leaves parent inspection
  complete: such a line holds no evidence); a session does not, since its
  parent is its own transcript, counted once already. A RecursionError that still escapes the
  read (the last guard) becomes `RunUnreadable`, which carries the id the run
  would print (never a path; a non-conforming directory name goes through
  `valid_workflow_run_id`). Measured: Python's JSON parser follows a
  5,000-deep object and a 990-deep array but not a 200,000-deep array.
- Test first. Every detector has a fixture that triggers it, a fixture that
  does not, and an evidence line in `path:line` form pointing into the fixture.
- R8 (v0.2, unit M4, section 4.11). The tool never says "verified".
  `rescue status` reports what a transcript shows happened and names what
  it could not see; a passing check is evidence, never a decision. The word
  itself appears nowhere in a command's output or in `rescue.py`'s own
  source, tested directly.
- Uncertainty is reported as uncertainty: no output turns an absence of
  evidence into a positive claim (none, ended, abandoned, wrote nothing),
  and no change widens what the tool executes or discloses by default.
- (v0.3) No severity claims an end-time fact the tool never observed: a
  filesystem check made after the fact is `observed_later`, never
  `observed_at_finish`, and no detector reaches `critical` until a
  future observer earns that value under its own contract (4.13.4). `share`
  disclosure is the invariant default for every command and every sink,
  including a hook message; nothing in the user config (4.10) can widen it
  (4.13.5).

## 3. Data formats (observed on Claude Code 2.1.251 to 2.1.312)

`journal.jsonl` (workflow runs): one JSON object per line. Observed `type`
values: `launched`, `started`, `result`, `failed`. A `started` event carries
`key` (a stable hash per logical agent() call, prefixed `v2:`), `agentId`,
`label`, `phase`. The same `key` appearing in more than one `started` event
means the runtime restarted that logical agent; attempts are ordered by
journal order. A `result` event carries `key`, `agentId`, and `result` (the
agent's return value, a dict or a string; not read in v0.1). v0.2 reads it,
into `Attempt.reported` (4.1), only for an attempt the state machine below
closes with this event: measured on real data, `result` is a dict in 483 of
705 cases and a string in the rest. For a dict, `reported` keeps the sorted
key names (at most 40, each truncated to 64 chars) plus an allowlisted set
of self-reported claims -- `ok` (bool), `ready` (bool), `status` (str, at
most 32 chars), `tests_passed`/`tests_failed`/`suite_passed`/`suite_failed`
(int), `blockers` (a list, kept only as its length under `blockers_count`),
`commits` (a list, kept as `commits_count` -- the claimed list's own length
-- plus a `commits` list of at most 20 entries that look like a commit id,
`^[0-9a-f]{7,40}$`), and `report_path` (str, at most 1024 chars); any other
key (`note`, `summary`, `bytes`, ...) or any value of the wrong shape is
dropped silently and never leaves the key name behind in `claims`. For a
string, `reported` keeps only its length (`text_length`); a real string can
run to 25k characters and is private end to end, never stored or printed.
`claim_contradiction` (4.3) reads `Attempt.reported` to flag a completed
attempt whose own dict result reports a failure it never disclosed any
other way. A `failed`
event carries `key` and `agentId` and marks an attempt the runtime gave up
on (observed causes on real data: usage limits, the machine sleeping, an
API error). The cause is never read from the journal itself: `attempt_failed`
(4.3) reads it from exactly one place, the last entry, not counting trailing attachment entries, of that attempt's own
transcript, never a wider window and never the attempt's first user prompt.
A killed-and-restarted attempt has no terminal event of its
own; its only trace is the next `started` for the same key. Unknown event
types are preserved verbatim on `Run.journal` and ignored by detectors.

Per key, the reader runs the state machine of 4.2 over these events: at
most one attempt is open per key; `started` opens one (closing an already
open one as superseded); `result` and `failed` close the open attempt of
that key when their `agentId` matches it or is absent; a terminal event
with a different `agentId`, or with no open attempt, closes nothing and is
a diagnostic (4.6). A key is resolved when it has no open attempt; the
journal started at least one key and every key is resolved is what
`reader.workflow_journal_finished` reports.

That journal-only reading is not, on its own, the definition of
`Run.finished` (implemented in N2). `Run.finished` is lifecycle-backed: the
one function `reader.workflow_lifecycle` computes a workflow run's
`Lifecycle` from the run directory alone (the parent's own terminal
evidence first, the journal-resolved reading above only as the fallback a
run with no parent-reported terminal gets -- 4.13.3's run lifecycle table),
and `reader.lifecycle_finished` turns a `Lifecycle` into the one finished/
not-finished answer. `_load_workflow` calls both for `Run.lifecycle` and
`Run.finished`, and `hooks_cli._workflow_finished` (7.2) calls the exact
same two functions, so a workflow unit's own "finished" can never again mean
something different in the hook than it does in `Run.finished` (implemented
in N2; see 4.13.3 for the full lifecycle table and the concrete divergence
this closes). `reader.workflow_journal_finished` itself has exactly one
remaining caller, watch's own `DONE` event (4.12), which keeps the
journal-only reading on purpose.

`agent-<id>.meta.json`: flat object. Observed keys: `agentType`,
`description`, `workflowPhase`, `spawnDepth`, `requestShape`,
`requestNonInteractive`. Values are strings. Present for workflow agents and
for Agent-tool subagents.

Transcripts (`agent-<id>.jsonl` and `<session>.jsonl`): one JSON object per
line. Common fields: `type` (`user`, `assistant`, `attachment`, `system`,
`summary`, others), `timestamp` (ISO 8601), `uuid`, `parentUuid`, `agentId`,
`isSidechain`, `cwd`, `gitBranch`, `sessionId`, `version`, `requestId`.
`message` has `role`, `content`, and on assistant entries `id` and `usage`
(`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`). Content is a string or a list of blocks.
Assistant blocks: `text`, `thinking`, `tool_use` (`name`, `input`, `id`).
User blocks: `tool_result` (`tool_use_id`, `content`, optional `is_error`)
or a plain string. `attachment` entries include hook results (`hookName`,
`hookEvent`, `command`, `stdout`, `stderr`, `exitCode`, `durationMs`).
`system` entries with `subtype: "compact_boundary"` mark a context
compaction. The format is internal to Claude Code and may change per
release; the reader tolerates unknown shapes and records them as
diagnostics.

The exact marker strings the runtime emits live in ONE module,
`src/agent_postmortem/markers.py`. Each constant carries the Claude Code
version range it was observed on. Marker comments never name a real run id;
they cite a `tests/fixtures` case that carries the string instead, or, for a
marker no fixture on disk carries yet, the test that exercises it in
memory. Detectors import from `markers`, never inline a literal.

## 4. Modules and semantics

```
src/agent_postmortem/
  __init__.py       __version__
  models.py         dataclasses (4.1)
  reader.py         discover_runs, load_run, load_session, parent-transcript index (4.2)
  redact.py         redact(), Redactor, Pseudonyms (5.1)
  cli.py            argparse: scan | run | rules | apply | trend | demo | hook
  markers.py        exact runtime strings
  detectors/        one module per detector; base.py holds the protocol and shared helpers
  report.py         text, JSON (allowlisted), bug report, hook message renderers
  rules.py          RULES library, managed-block writer
  state.py          locked, versioned state store (7.5)
  trend.py          weekly and before/after aggregation, stratified by kind
  hooks_cli.py      `hook` subcommands (7.2)
  demo/             one canonical synthetic run shipped in the wheel (8.2)
  hooks/            model_allocation_guard.mjs template
plugin/             the Claude Code plugin (7.1)
.claude-plugin/marketplace.json
```

### 4.1 models.py

```python
@dataclass
class Event:
    path: Path; line_no: int; ts: datetime | None
    kind: str        # 'user' | 'assistant' | 'thinking' | 'tool_use' | 'tool_result'
                     # | 'attachment' | 'compaction' | 'unknown'
    raw_type: str; tool_name: str | None
    text: str        # excerpt <= 400 chars, harness noise stripped (4.5); '' for thinking
    exit_code: int | None; duration_ms: int | None; is_error: bool
    usage: dict      # attached to exactly one event per (message.id, requestId)
    markers: frozenset[str]   # scanned on full raw text before truncation;
                              # always empty for envelope events (4.2)
    envelope: bool = False    # tool_result of Agent/Task/Workflow/TaskOutput

@dataclass
class ToolCall:
    # One tool_use/tool_result pair, filled while streaming from the FULL
    # decoded transcript entry -- never from Event.text, which is already
    # bounded to 400 chars. Reader-internal/private: a command is private,
    # so this is never exported directly (5.3); only the facts AttemptFacts
    # derives from a list of them is.
    tool_use_id: str; tool_name: str | None; line_no: int; ts: datetime | None
    path: str | None = None        # Edit/Write file_path, NotebookEdit
                                    # notebook_path; <=1024 chars, else None
    command: str | None = None     # Bash's or Monitor's own command, exact;
                                    # >2048 chars: None (N4b-1)
    command_sha256: str | None = None  # set instead when command is None
    runs_codex_program: bool = False   # reader-internal, never exported: the
                                    # FULL raw command runs codex as a program
                                    # (4.3), so a command over 2048 chars counts
    run_in_background: object = None   # (v0.3, N4b-1) the tool_use input's own
                                    # `run_in_background` value for a Bash or
                                    # Monitor call, exactly as written; None
                                    # when absent and for every other tool
    multiedit_path: str | None = None  # (v0.3, N4b-1) a MultiEdit call's
                                    # `file_path`, <=1024 chars, else None;
                                    # kept apart from `path` so schema-2
                                    # paths_written and in_flight never change
    classification: CallClass | None = None  # (v0.3, N4b-1) the 4.13.1
                                    # classification (commands.classify_call)
                                    # computed from the FULL command while
                                    # streaming (C-11), with the anchored
                                    # bash_backgrounded hit of its result; None
                                    # for every tool outside rows 2 to 6
    cwd: str | None = None         # the tool_use entry's own 'cwd', through
                                    # os.path.normpath (no filesystem access)
    result_line: int | None = None # line of the matching tool_result, or None
    is_error: bool | None = None
    backgrounded: bool = False     # the result carries BASH_BACKGROUNDED
    exit_code: int | None = None   # same parsing as Event.exit_code
    commit_ids: tuple[str, ...] = ()  # Bash commit calls only (4.13.1, C-9); <=20
    ambiguous: bool = False        # this tool_use_id was duplicated in the
                                    # transcript (4.2); checked first by
                                    # completion, ahead of every other state

    @property
    def completion(self) -> str:
        # 'ambiguous' (this tool_use_id was duplicated -- checked first,
        # since nothing else about the call can then be trusted),
        # 'completed_ok' (result seen, is_error false, not backgrounded, and
        # the parsed exit code -- if any -- is 0), 'completed_error'
        # (is_error true, OR is_error false with a parsed nonzero exit code:
        # a contradiction the runtime can produce, and exit code wins),
        # 'backgrounded' (checked before is_error), or 'no_result'.
        ...

FACTS_UNKNOWN = "UNKNOWN"  # AttemptFacts.to_dict()'s value for a field the
                           # reader could not establish (4.2)

@dataclass
class AttemptFacts:
    # Derived from one attempt's tool_calls once its transcript is fully
    # read (4.2). Every field is either its documented type or the literal
    # string FACTS_UNKNOWN when the transcript existed but could not be read.
    paths_written: list[str]  # unique Edit/Write/NotebookEdit paths with
                               # completion completed_ok, first-seen order
    in_flight: list[dict]     # {'tool', 'path', 'line_no'} for a no_result
                               # call on a killed/failed/superseded attempt;
                               # always [] for a completed attempt (4.2)
    commits: list[str]        # every commit id from every call, unique, in order
    commit_commands_without_id: int  # (field milestone F, FA6) shell CALLS with completion
                               # completed_ok that contain at least one commit command
                               # (`commands.commit_command_ran`, 4.13.1) and yielded no
                               # commit id; 0 for a hand-built fact. The shell may never
                               # have reached the command (`git diff --quiet || git
                               # commit -q`): contains, never ran
    cwds: list[str]           # unique normalized cwds seen on the attempt's
                               # tool calls, first-seen order
    background_unfinished: int  # calls with completion 'backgrounded'
    last_tool: str | None       # name of the last tool call
    seconds_since_last_tool_at_end: float | None  # None when either
        # timestamp is missing or the delta would be negative

@dataclass
class Attempt:
    agent_id: str; key: str | None; label: str; phase: str
    agent_type: str | None; meta: dict; transcript_path: Path | None
    events: list[Event]; first_ts: datetime | None; last_ts: datetime | None
    ordinal: int
    outcome: str   # 'completed' | 'failed' | 'killed' | 'superseded' | 'unknown'
    usage: dict
    interrupt: Interrupt | None  # set whenever the transcript ends in the kill sentinel
    reported: dict | None  # the agent's own result (3): {'type': 'dict',
                            # 'keys': [...], 'claims': {...}} or {'type':
                            # 'text', 'text_length': int}; None unless the
                            # journal closed this attempt with a 'result'
    reported_line: int | None  # journal.jsonl line of that 'result' event;
                                # reader-internal, never exported (5.3)
    tool_calls: list[ToolCall]  # every tool call, in transcript (tool_use)
        # order -- never result-arrival order. Reader-internal/private,
        # never exported directly (5.3)
    facts: AttemptFacts | None  # derived from tool_calls once the
        # transcript is read; None only when transcript_path is None (no
        # transcript at all) -- a transcript that exists but could not be
        # read still produces one, every field FACTS_UNKNOWN
    marker_hits: list[MarkerHit]  # (v0.3, N1, 4.13.2) every typed marker hit
        # built while parsing this attempt's own transcript; [] when the
        # attempt carries none. Never empty-vs-absent: an attempt with no
        # transcript at all still gets [], not None, since a MarkerHit is
        # built per event, not derived from a whole-transcript read the way
        # AttemptFacts is
    ledger: Ledger  # (v0.3, N4b-2, 4.13.1) this attempt's own write ledger.
        # The ledger binds to the attempt, not to a readable file: every
        # attempt of a workflow run, every session subagent, and a session's
        # main thread gets one, so from N4 on the reader never leaves it None
        # (a hand-built Attempt may still default it to None). An expected
        # transcript that does not exist, and an attempt whose `started`
        # event has no canonical agent id (C-5, transcript never opened), give
        # Ledger([], 'partial', ['transcript_missing']); an unreadable one
        # gives Ledger([], 'partial', ['transcript_unreadable']). Replaces the
        # earlier "None on the same terms `facts` is". In to_dict(); never in
        # export_run()
    journal_state: str | None  # (v0.3, N4b-1) the journal state machine's own
        # closure of this attempt: 'open' | 'completed' | 'failed' |
        # 'superseded' for a workflow attempt with a journal key; None for a
        # session main thread, a session subagent, and a workflow attempt
        # with no journal key. Separate from `outcome` (a superseded attempt
        # whose transcript ends in the kill sentinel has outcome 'killed' and
        # journal_state 'superseded'). In to_dict(); never in export_run()
    return_state: str | None  # (v0.3.1, G-A, 4.13.1) None for a workflow attempt
        # and the session main thread; for a session subagent exactly one of
        # `returned`, `reported_failed`, `reported_killed`, `pending`,
        # `unknown`: what the parent-side evidence (the launch, the resume
        # requests, the main transcript's accepted task notifications; truth
        # table T1 to T5 of 4.13.1) establishes about its return. Fail
        # closed: whatever cannot be established is `unknown`. In to_dict();
        # exported (5.3b)
    return_reason: str | None  # (v0.3.1) None for a workflow attempt and the main
        # thread; for a session subagent one of `none` (the state is not
        # `unknown`; a `pending` child whose latest round has no notification
        # yet is `pending`/`none`), `no_evidence`, `conflict`, `malformed`,
        # `delivery_unknown`, `ambiguous`, `parent_unreadable`,
        # `parent_missing`, `status_unrecognized`. Exported (5.3b)
    resumes: int  # (v0.3.1) 0 for a workflow attempt and for every child that was
        # never resumed; for a session subagent the number of delivered
        # SendMessage requests that resumed it (4.13.1 T3). Exported (5.3b)
    returned_to_parent: bool | None  # (v0.3, N5c-1; v0.3.1) a typed private field,
        # exactly `return_state == "returned"` for a session subagent, None for
        # a workflow attempt and the session main thread. Not in to_dict()
        # and never in export_run()

@dataclass
class Interrupt:
    kill_ts: datetime | None
    preceding: Event | None      # last meaningful event before the sentinel (4.5)
    silence_seconds: float | None  # None when a timestamp is missing, the
                                   # delta is negative, or a compaction lies between
    cause: str                   # 'parent_stop' | 'human' | 'runtime' | 'unattributed'
    cause_evidence: Event | None # the parent TaskStop or parent sentinel, if any

@dataclass
class LogicalAgent:
    key: str; label: str; phase: str; attempts: list[Attempt]

@dataclass
class Run:
    run_id: str            # public id: 'wf_...' for workflow, 's_' + 10 hex for session
    kind: str              # 'workflow' | 'session'
    path: Path             # wf_* directory or the session .jsonl
    project_slug: str; session_id: str | None; claude_version: str | None
    journal: list[dict]
    main: Attempt | None   # session kind only: the main thread
    agents: list[LogicalAgent]
    attempts: list[Attempt]   # child attempts only; never includes main
    started_at: datetime | None; ended_at: datetime | None
    diagnostics: list[str]
    status: str            # 'COMPLETE' | 'INCOMPLETE' (4.6)
    lifecycle: Lifecycle | None  # (v0.3, N2, 4.13.3) None for a session run
        # (sessions carry no Workflow launch and no generation to pair, D11)
        # -- never derived for one; never None for a workflow run once N2 lands
    finished: bool | None  # (v0.3, N2, 4.13.3) true for completed/failed/
        # stopped/journal_resolved, false for unknown/none, null for a session
    risk_population_completeness: str | None  # (v0.3, N4, 4.13.1)
        # 'complete' | 'partial'; the reader always sets it (None only on a
        # hand-built Run). In to_dict(); never in export_run()
    risk_population_completeness_reasons: list[str]  # (v0.3, N4, 4.13.1)
        # closed reason codes (`unassignable_attempt`, `discovery_incomplete`);
        # [] when 'complete'
    discovery: dict[str, list[str]]  # (v0.3, N4b-1, 4.13.1) exactly the keys
        # `journal`, `parent`, `transcripts`, `meta`; each holds the
        # diagnostics that source reported, in the order reported, and []
        # means that source was clean. A session run's `transcripts` also
        # carries the launch reconciliation diagnostics (4.13.1). In
        # to_dict(); never in export_run(), and independent of `diagnostics`
        # and `status` (a reconciliation diagnostic changes neither)

@dataclass
class Finding:
    detector: str; severity: str   # 'critical' | 'major' | 'minor' | 'info'
    title: str; run_id: str; agent_id: str | None
    evidence: list[str]; metrics: dict; rule_id: str | None
    rule_ids: tuple[str, ...]  # every rule this finding backs, in priority
        # order; rule_id is kept in sync as rule_ids[0] (unit M1d). A
        # detector emits one Finding per fact even when that fact backs more
        # than one rule (silent_retry's R-RETRY-SAFE plus R-CAP-CONCURRENCY
        # on a burst), never a duplicate Finding per rule.
    finding_id: str        # sha256(run_id, detector, agent_id, first evidence path:line)[:12]

# -- v0.3 model additions (D14); each field above that uses one of these
# names the milestone that adds it --

@dataclass
class MarkerHit:  # (v0.3, N1, 4.13.2)
    marker: str              # canonical marker id from the registry in 4.13.2:
                              # 'kill_sentinel' | 'bash_backgrounded' | 'bash_timeout'
                              # | 'tool_input_invalid' | 'structured_output_mismatch'
                              # | 'sandbox_write_denied' | 'shell_guard_block' | 'codex_unavailable'
                              # | 'classifier_unavailable' | 'git_not_repo'
                              # | 'usage_limit_session' | 'usage_limit_weekly'
                              # | 'machine_sleep' | 'api_error'
    producer: str            # 'harness_user' | 'harness_tool_notice' | 'command_output'
                              # | 'runtime_assistant' | 'codex_companion'
    event_line: int          # physical line of the entry that carries the marker
    tool_use_id: str | None  # set for every tool_result shape; None otherwise
    result_line: int | None  # equals event_line for a tool_result shape; None otherwise
    anchored: bool           # the registry predicate held at its anchor
    in_payload: bool         # the text matched only outside the anchor (never evidence)

@dataclass
class LedgerEntry:  # (v0.3, N4, 4.13.1)
    effect: str       # 'observed' | 'claimed_target' | 'attempted' | 'possible'
    basis: str        # 'tool_result' | 'command_grammar' | 'delegation'
                       # | 'opaque_command' | 'background_process' | 'unknown_tool'
    completion: str   # 'success' | 'error' | 'no_result' | 'ambiguous'
    path: str | None  # normalized path; null when the call names no target
    repo: str | None  # the resolved repository (4.13.1); null when unknown
    line: str         # evidence in 'path:line' form

@dataclass
class Ledger:  # (v0.3, N4, 4.13.1) Attempt.ledger's own shape
    entries: list[LedgerEntry]
    completeness: str   # 'complete' | 'partial'
    reasons: list[str]  # closed partial-reason codes (4.13.1); [] when complete

@dataclass
class Generation:  # (v0.3, N1, 4.13.3) one launch associated with a run
    launch_line: int            # the Workflow tool_use's own physical line
    tool_use_id: str            # that tool_use's own 'id'
    resume_from: str | None     # its input.resumeFromRunId, when a string
    result_line: int | None     # the unique matching tool_result line; None for
                                 # a result-less resume (incomplete generation)
    run_id: str                 # the inspected run id this generation belongs to
    task_id: str | None         # toolUseResult.taskId; None when incomplete
    launch_status: str | None   # toolUseResult.status as written ('async_launched'
                                 # on every launch observed so far); None when incomplete
    terminal: str | None        # 'completed' | 'failed' | 'stopped' | 'unknown'
                                 # | None (no terminal evidence yet)
    terminal_source: str | None # 'notification' | 'task_stop' | 'launch_result'
                                 # | 'conflict' | 'unrecognized_status'
                                 # | 'incomplete_generation'; None when terminal is None
    terminal_line: int | None   # the accepted notification, TaskStop, or launch
                                 # result line that set terminal; None otherwise
    diagnostics: list[str]      # each names the physical lines involved
    launch_ts: datetime | None  # (field milestone F, FA2) the parsed timestamp of the
                                 # assistant entry that holds the Workflow tool_use
                                 # block (launch_line), taken from the same parent-index
                                 # snapshot; None when that entry has no timestamp or it
                                 # does not parse. Generation assignment (4.13.1) keys on
                                 # it. In to_dict() (ISO string or null); never in
                                 # export_run()
    result_ts: datetime | None  # (v0.3, N4b-1) the parsed timestamp of the launch
                                 # result line (result_line), taken from the same
                                 # parent-index snapshot that produced the lifecycle;
                                 # None when there is no result line, the line has no
                                 # timestamp, or it does not parse. In to_dict() (ISO
                                 # string or null); never in export_run()

@dataclass
class Lifecycle:  # (v0.3, N2, 4.13.3) Run.lifecycle for a workflow run; None for a session
    terminal: str          # 'completed' | 'failed' | 'stopped' | 'journal_resolved'
                            # | 'unknown' | 'none'
    source: str | None     # 'notification' | 'task_stop' | 'launch_result' | 'journal'
                            # | 'conflict' | 'unrecognized_status' | 'incomplete_generation'
                            # | 'parent_unavailable' | 'ambiguous_launch';
                            # None exactly when terminal is 'none'
    activity: str          # 'recent' | 'stale' | 'unknown' (stat failed)
    generations: int       # >= 0; 0 for a run with no associated launch
    diagnostics: list[str] # [] when none fired
```

`to_dict()` on every dataclass is the private, complete form used by tests.
Public export goes through `report.export_run()` (5.3), never `to_dict()`.

### 4.2 reader.py

`discover_runs(root, kinds={'workflow','session'}) -> list[RunRef]` where
`RunRef(kind, path, run_id)`. Workflow refs come from
`*/*/subagents/workflows/wf_*` directories. Session refs come from
`*/<uuid>.jsonl` files that carry at least one `user` or `assistant` entry
with a parseable timestamp; a file with no such entry (a `bridge-session`
marker with nothing else in it, for example) is not a run and is skipped in
discovery. Session public id: `'s_' + sha256(f"{slug}/{uuid}")
.hexdigest()[:10]`. Discovery detects public-id collisions and records a
diagnostic on both runs with a 12-hex id instead. Results are sorted.

`load_run(ref_or_path) -> Run` dispatches on layout: a directory holding
`journal.jsonl` is a workflow run; a `.jsonl` file whose first parsed line
carries `sessionId` is a session; anything else raises `NotARun`. The CLI
turns `NotARun` into exit 2. `load_session` itself also raises `NotARun`
for a session file with no timestamped `user`/`assistant` entry, the same
rule discovery applies, so a direct `load_session` call is never fooled by
one that slipped past discovery some other way.

`load_session(path)`: `Run.main` is the main transcript as an Attempt with
`label='main'`, `key=None`, `ordinal=1`. Each `subagents/agent-*.jsonl` is
one Attempt with `key=agentId`, one LogicalAgent per attempt, label from
meta `description` (empty string when meta is missing), `agent_type` from
meta. Retries of Agent-tool subagents are not observable in v0.1; the report
says so instead of claiming zero.

Usage deduplication, both kinds: while reading a transcript the reader keeps
a set of `(message.id, requestId)`. An assistant entry whose pair is already
in the set gets `usage={}` on every block it emits. An entry missing either
field is counted (the ccusage convention); a duplicate pair with different
usage numbers adds a diagnostic.

Envelope events: a `tool_result` whose resolved `tool_name` is one of
`Agent`, `Task`, `Workflow`, `TaskOutput` is emitted with `envelope=True`
and `markers=frozenset()`, so a child's error text quoted in the parent's
result is never counted twice.

Compaction: a `system` entry with `subtype == 'compact_boundary'` becomes an
Event of kind `compaction`. `parentUuid: null` mid-file is not evidence of
anything and is ignored.

Attempt outcome for a workflow attempt: `completed` or `failed` when the
journal state machine (section 3) closed it with `result` or `failed`, even
when the transcript also ends in the kill sentinel (the journal's own
report wins; the sentinel is still kept as `Attempt.interrupt` evidence).
An attempt superseded by a later `started` is `killed` when its transcript
ends in the kill sentinel (the silent retry) and `superseded` otherwise,
which is a diagnostic and sets status INCOMPLETE. An attempt the state
machine never closed, and every session attempt, is decided by the
transcript alone: `killed` when its last user event is the kill sentinel,
`completed` otherwise, `unknown` without a transcript.

(v0.3.1, G-A) That transcript-only outcome of a session subagent is the INTERNAL outcome: it keeps
driving the risk population, the ledgers, WASTED and the rescue units. What the report and the
JSON say about whether the child ended is its effective end (5.3b): a function of the kill
sentinel and `return_state` (4.1, 4.13.1), decided from the parent-side evidence, never from a
modification time. The reader reads, besides the main transcript, the launcher transcript of a
nested child and the other session transcripts that may hold a resume request (T3), as
evidence only: they are never analyzed attempts and their diagnostics reach no run.

Parent transcript index (implemented in N1, 4.13.3): for any attempt whose
transcript ends in the kill sentinel, the reader needs the parent main
transcript (`<root>/<slug>/<session>.jsonl`, derived from the run path). A
process-level, incremental cache `ParentIndex.for_path(path)` keeps: the
identity and timestamps of `TaskStop` tool_use blocks (now also their own
`task_id`/`shell_id` input fields, not only the generic set of every string
value in their input), timestamps of kill sentinels, compaction timestamps,
and -- v0.3 N1 -- every `Generation` (4.1) the transcript's Workflow
launch/result pairs and terminal evidence yield, exposed as
`ParentIndex.generations`, a flat list spanning every run any launch here is
associated with (a caller filters by `Generation.run_id`). The cache is no
longer "scan once forever": each access re-validates the file by inode, size,
and a hash of the bytes already parsed, parsing only newly appended complete
lines when nothing earlier changed, and rebuilding from zero on any shrink,
inode change, or a hash mismatch on the already-parsed prefix (a same-size
rewrite). `lines_parsed` and `rebuilds` count respectively every complete
line parsed (the initial build included) and every rebuild after the first.
4.13.3 is the full contract.

**A fork's inherited prefix (field milestone F, FA4).** A forked session's
transcript starts with a block of entries copied from the parent session (417 of
them in the one real fork measured), each carrying the PARENT's `sessionId`.
They are the parent's history, not the fork's, and counting them gave the fork
the parent's findings (same line numbers) and duration. In a session run's MAIN
transcript, the inherited block is the leading span of physical lines up to and
including the last entry whose `sessionId` is a string that differs from the
transcript's own stem, counted only when some later entry carries the
transcript's own `sessionId`; entries lacking `sessionId` inside that span (and
undecodable or blank lines) are inherited with it, entries lacking it after the
last foreign one are not. If no entry carries the transcript's own `sessionId`,
nothing is inherited, and a foreign entry after the first own entry is kept (not
observed in the corpus). The boundary is a raw-line boundary computed once per
main transcript (`reader._fork_boundary`: the number of the last inherited
physical line, 0 for none) and shared by `_read_transcript` and `ParentIndex`,
applied before any accumulator is updated: events, markers, usage, tool calls,
the ledger, launches, notifications, TaskStops, kill sentinels, discovery
diagnostics, identity and version fields, every timestamp (`first_ts`,
`last_ts`, `started_at`), the undecodable-line counts, and the `--here` cwd set.
Physical line numbers of the own entries are unchanged, and `ParentIndex.
lines_parsed` still counts every physical line. A growing fork whose first own
entry has not been written yet inherits nothing; when it appears the index
rebuilds once from zero under the new boundary. Exactly one diagnostic, a
data-quality note (4.13.1, A2: it stays in `Run.diagnostics` and never reaches a
discovery list), says `inherited <n> leading lines from another session (a fork);
they are that session's`, `n` the boundary line number. Agent and workflow
transcripts are untouched.

Tool calls and attempt facts (v0.2, unit M2a): while `_read_transcript`
streams a transcript it also builds `ToolCall` records, one per `tool_use`
block, in transcript order. Each is filled from the FULL decoded JSON entry
-- the parsed `input` object and the entry's own top-level `cwd` -- never
from `Event.text`, which is already bounded to the 400-char excerpt; a
`file_path` or `command` past that limit is still captured whole on the
`ToolCall` (up to its own, larger bound). A `tool_use` id is tracked in an
index so the matching `tool_result`, wherever it appears, updates the right
`ToolCall` by id, never by position: two `tool_use` blocks in one assistant
message can have their results arrive in either order. `path` is read from
`file_path` (Edit, Write) or `notebook_path` (NotebookEdit) and kept only up
to 1024 chars, else dropped to `None`; `command` is Bash's own command kept
exact up to 2048 chars, past which it is dropped to `None` and
`command_sha256` is set instead, so a long command is never silently
truncated into something that reads like a different, shorter one. `cwd` is
normalized with `os.path.normpath` -- a pure string operation, no symlink
resolution, no filesystem access. `backgrounded` is set when the result
carries both `BASH_BACKGROUNDED` fragments; `exit_code` reuses the same
parsing `Event.exit_code` already does. `commit_ids` is set only for a Bash call
that is a commit call under the commit grammar of 4.13.1 (C-9, computed from
the FULL decoded command while streaming, never the 2,048-character stored
copy) and whose completion is `completed_ok` or `completed_error` (A1): the
last k line-start matches
of `^\[[^\]\s]+ ([0-9a-f]{7,40})\]` against the result's FULL text (never the
excerpt), for k commit segments, at most 20; a Bash result that happens to
contain a bracketed hex run is never counted when its command is not a
commit call.

Duplicate `tool_use_id` (v0.2 hardening, F5): a second `tool_use` block
sharing an id already seen in this transcript is a reader diagnostic,
`"duplicate tool_use_id <id> at lines <a> and <b>"` (`a` the first
occurrence's line, `b` the new one), which forces `Run.status` `INCOMPLETE`
the same way any other reader diagnostic does. Every `ToolCall` that ever
shared the id -- the original and every duplicate, whether or not it has
already been paired with a `tool_result` -- is marked `ambiguous`, never only
the newest one (the only one still reachable from the id index a later
`tool_result` is matched against). `rescue status` (4.11) never counts an
ambiguous call as a check run.

Once an attempt's transcript is fully read, `_build_attempt_facts` derives
its `AttemptFacts` from the attempt's `tool_calls` list: `paths_written` is
the unique paths of Edit/Write/NotebookEdit calls whose completion is
`completed_ok`, first-seen order; `in_flight` is every call with completion
`no_result` when the attempt's outcome is `killed`, `failed`, or
`superseded` (never invented from a call whose result was simply never
seen on a `completed` attempt -- that list is always `[]` there, since the
journal's own verdict wins); `commits` and `cwds` are collected from every
call regardless of completion, unique and in order; `commit_commands_without_id`
(FA6) counts Bash CALLS (never segments) whose completion is `completed_ok`, that
contain at least one commit command under 4.13.1's `commit_command_ran` rule (a
placed, foreground one; the transcript cannot prove the shell reached it) and whose
`commit_ids` is empty, and is the unknown value for an attempt whose facts are
unknown, never a false 0; `background_unfinished`
counts calls with completion `backgrounded`; `last_tool` is the last call's
tool name; `seconds_since_last_tool_at_end` is the last call's own `ts` to
the attempt's `last_ts`, `None` when either is missing or the delta would
be negative. `Attempt.facts` is `None` only when the attempt has no
transcript at all (a missing file); a transcript that exists but could not
be read, whether it failed to open or failed anywhere during the read (a
`UnicodeError` from bytes that are not UTF-8, or an `OSError`, after any
number of good lines), still produces an `AttemptFacts`, every field the
literal string `FACTS_UNKNOWN` rather than a misleadingly empty list or zero
count. Such a read-time failure is the same unreadable shape as an open
failure (N4 fix, C2): one diagnostic `<transcript name>: unreadable: <error>`
(in `Run.diagnostics` and the run's `transcripts` discovery list),
`TranscriptState.unreadable` set, every partially accumulated call, event,
marker hit, and launch record discarded, the other transcript-state flags
cleared, and so a ledger of exactly `Ledger([], 'partial',
['transcript_unreadable', ...run-level reasons])`; the run is `INCOMPLETE`
and the read never raises. One exception (G-C, GC-1): a final physical line with no trailing
newline whose bytes are not valid UTF-8 is a half-written line, whether the cut falls between
characters or inside one. It is handled exactly like a half line of ASCII: one `<transcript
name>:<n>: ...` diagnostic that quotes none of the line, one more undecodable line in the
transcript's count, `TranscriptState.undecodable` set, and the transcript stays readable. An
undecodable byte anywhere else, a torn line that is followed by a newline included, keeps the rule
above. A hook's kill-sentinel check (7.2) treats such a tail as a partial write, not finished, and
never raises on it. The journal, the meta files, and the parent
transcript carry their own guards (a journal `UnicodeError` or `OSError` is a
`journal.jsonl: undecodable` or `unreadable` diagnostic, a meta file's is a
`<meta name>` diagnostic, an undecodable parent line is an `undecodable
parent transcript line` diagnostic), and the two sniffs that decide whether a
file is a session (4.2) treat a `UnicodeError` as "cannot tell": the file is a
candidate session, and its own read then reports it unreadable.

### 4.3 Detector protocol

```python
class Detector(Protocol):
    id: str
    severity_default: str
    kinds: frozenset[str]          # run kinds this detector applies to
    def run(self, run: Run) -> list[Finding]: ...
DETECTOR = SilentRetry()
```

`detectors.all_detectors()` returns instances sorted by id. The runner calls
a detector only on runs whose kind is in `detector.kinds`; scan totals and
trend rates use the same filter (4.7).

The `severity` column below is superseded by 4.13.4 (N5b): severity is
computed from the input table and rule rows of 4.13.4 (D3), not from a fixed
per-detector value, and no detector's severity is `critical` any longer
(4.13.4). The column is kept as the pre-N5 record of what each detector
fires on; the table that follows it carries every detector's inputs and
rule, and `severity.py` (N5b) is the one place that table lives in code.

Agent attribution of the six run-wide tooling detectors (`bash_backgrounded`,
`bash_timeout`, `tool_json_error`, `structured_output_failure`, `classifier_unavailable`,
`git_cwd_mismatch`).
Each emits one finding per run with the count of every hit in the run. Its
`agent_id` is that agent's id only when every hit belongs to the same agent;
when the hits span two or more agents, `agent_id` is `None` and the finding
backs the one `run` story (4.7). Titles, metrics, severity, the number of
findings and rule ids do not depend on this. `finding_id` hashes `agent_id`
(4.1), so a multi-agent finding's id is the one computed with no agent.
`permission_denied`, `codex_write_denied` and `codex_unavailable` are already one finding per
attempt and are not affected.

| id | severity | kinds | fires when | metrics | rule_id |
|---|---|---|---|---|---|
| silent_retry | major | workflow | a key has more than one attempt | attempts, keys, wasted_seconds, burst | R-RETRY-SAFE, plus R-CAP-CONCURRENCY when burst (one Finding's rule_ids carries both, never two Findings) |
| mislabeled_kill | see 4.4 | both | an attempt's `interrupt` is set | silence_seconds (nullable), cause, events | R-MONITOR-RETRIES when cause is runtime or unattributed-major |
| wasted_time | info | both | always, one per run | workflow: run_seconds, attempt_seconds, wasted_seconds, wasted_tokens. session: run_seconds, child_seconds, child_count, wasted_seconds (FA5) | none |
| bash_backgrounded | minor | both | Bash tool_result carries the backgrounded marker | count | R-NO-LONG-BASH |
| bash_timeout | minor | both | Bash tool_result shows exit 143 or the timeout marker (title, field milestone F P10: `<n> Bash command(s) hit a hard timeout` only when every hit has the anchored timeout marker; when any hit is the exit code alone, `<n> Bash command(s) ended with exit code 143 (a hard timeout or another termination)`; severity unchanged) | count | R-NO-LONG-BASH |
| tool_json_error | minor | both | tool_result carries the input-validation marker | count, tools | none |
| structured_output_failure | major | both | StructuredOutput tool_result carries a mismatch marker | count | R-SCALAR-SCHEMA |
| codex_write_denied | critical | both | a sandbox denial or shell-guard marker in an assistant event whose text starts with "[codex]" (the companion's output echoed), or in a Bash tool_result whose output starts with "[codex]" or whose command runs `codex` as the first word of a command segment (optionally path-qualified) or names `codex-companion.mjs` (the word inside command data, a heredoc body or a file name such as "review-codex.txt", does not count). Measured on the build machine: 23 of 23 real denials were companion output; assistant prose quoting the marker was never one | count, marker | R-CODEX-CONTRACT, R-ASSERT-ARTIFACT |
| codex_unavailable | major/minor | both | one finding per attempt, the main thread of a session included, that holds at least one anchored `codex_unavailable` hit (4.13.2): a Bash tool_result, paired to a call whose command runs codex as a program or names `codex-companion.mjs`, with a line that starts with the companion's sentence `Codex CLI is not installed or is missing required runtime support`. Assistant text and the output of any other command (a `cat` of a notes file) never count; a `read_only_output` event is skipped. Title, a fixed sentence: `<n> Codex companion call(s) could not start: the Codex CLI is missing or lacks its runtime`. Severity per 4.13.4 | count | R-CODEX-CONTRACT |
| classifier_unavailable | minor | both | tool_result carries both classifier-outage fragments | count | none |
| git_cwd_mismatch | minor | both | Bash tool_result carries the not-a-git-repository marker | count | R-GIT-CWD |
| permission_denied (G7, v0.3.1) | minor | both | one finding per attempt, the main thread of a session included, that holds at least one anchored `permission_denied` hit (4.13.2): an `is_error` tool result whose text starts with a runtime denial sentence | count, auto_mode, user, permission_rule, safety_check | none |
| missing_artifact | critical | both | first user message names an absolute path it must write and the path does not exist at scan time; outside allowed roots: info 'unverified' | paths, verified | R-ASSERT-ARTIFACT |
| attempt_failed | major/minor | both | a workflow attempt's `outcome` is `failed` (major when no later attempt of the key completed -- whether or not one was even tried; minor when a later attempt did complete); a session subagent's `return_state` is `reported_failed` (G1c, v0.3.1: one major finding, title `<label> failed: <cause phrase>`, `launch_result_failed` when a synchronous launch result decided it and `task_notification_failed` when a notification did, `retry_now` `unknown`, `reset_time` null, evidence the deciding result or notification `file:line`; `reported_killed` adds no finding) | cause, retry_now, reset_time | R-USAGE-LIMIT when cause is a usage limit |
| claim_contradiction | major | workflow | a `completed` attempt's own dict `reported` reports a failure: `tests_failed`/`suite_failed` > 0, `ok`/`ready` false, `blockers_count` > 0, or a set `report_path` absent on disk (a relative path is unverified, never counted; the path is checked only when the run ended at most 72 hours ago or its end time is unknown, since temp reports are cleaned up later); (G3, v0.3.1) or, when no tests, suite, ok, ready or blockers clause holds, a returned result whose top level carries a failure count of its own (`self_reported_failure`) or `blocked: true` (`self_reported_blocked`) | clauses, claims, and for the G3 clauses `self_reported_failure_counts`, `self_reported_blocked` | none |
| workflow_failed (v0.3, N2, 4.13.3, implemented in N2) | major | workflow | the run's own `Lifecycle.terminal` is `failed` -- the harness itself reported this workflow failed, independent of whatever the journal says | none | none (section 9 names no rule for this detector) |

**A `reported_killed` child adds no finding, and the session header can stay CLEAN (GA-10, v0.3.1).**
A child whose only evidence of an end is a task notification or launch result saying `killed`,
`stopped` or `cancelled` (no kill sentinel in its transcript) is usually a deliberate stop: a
TaskStop by the parent or the user. A finding would call a requested stop a failure, so the
detector emits none. The facts are not hidden: the AGENT END cell says `killed`, the exported
`outcome` is `killed`, and WORK AT RISK says `unknown` (`<n> reported killed` in its breakdown) because the
tool cannot say what the child wrote before it stopped. The header verdict stays `CLEAN` with such
children; this is the one place where the verdict and the return state can differ, kept on purpose.

**(v0.3, N5b) Inputs and rule per detector.** This table extends the one
above; every row of it is a row of `severity.RULES`, and a detector never
chooses a severity any other way. Input names and their values are those of
4.13.4, plus the three derived inputs the rows below name: `cause`
(`runtime`, `unattributed`, `parent_stop`, `human`, 4.4), `silence`
(`long` for `silence_seconds >= 300`, `short` for less, `unknown` for
`None`; the one 300 s rule of 4.4), and `position` (`final` when the
attempt has no later attempt of its key in `Run.attempts` or has no key,
`non_final` otherwise). A clause of `claim_contradiction` is one of
`tests_failed`, `suite_failed`, `ok_false`, `ready_false`, `blockers` (each
the "tests/ok/ready/blockers" row), `report_path_missing`, or (G3, v0.3.1)
`self_reported_failure` or `self_reported_blocked`.

| id | inputs | rule |
|---|---|---|
| workflow_failed | terminal | `failed`: major |
| attempt_failed | recovered | `no`, `unknown`: major; `yes`: minor (a session subagent that reported failure passes `unknown`: major; `recovered` is never a metric) |
| silent_retry | recovered, work_risk | `no`, `unknown` (any work_risk): major; `yes` with `present` or `unknown`: major; `yes` with `none`: minor |
| mislabeled_kill | cause, silence, position | `parent_stop` or `human`, anything: info; a `short` or `unknown` silence, any cause, any position: info (the literal 4.13.4 row; N5 fix 1 reverses the N5b reading that let a non-final position override a short silence); `runtime` or `unattributed`, `long`, `final`: major; `runtime` or `unattributed`, `long`, `non_final`: minor |
| wasted_time | none | info |
| bash_backgrounded, bash_timeout, tool_json_error, classifier_unavailable, git_cwd_mismatch, permission_denied (G7) | none | minor |
| structured_output_failure | recovered | `no`, `unknown`: major; `yes`: minor |
| codex_unavailable | outcome, later_success | `outcome` is `completed` when the attempt's `outcome` is `completed`, else `other`; `later_success` is `yes` when a Bash call running codex as a program (4.3, judged on the full command even past 2,048 characters) after the first failing one has a result without the marker, else `no`. `completed` with `no`: major; `completed` with `yes`: minor; `other` with either: minor (a killed or failed attempt is carried by other findings) |
| codex_write_denied | none | minor, one finding per attempt, `rule_ids` `["R-CODEX-CONTRACT", "R-ASSERT-ARTIFACT"]` |
| claim_contradiction | clause, absence | a tests, suite, ok, ready, blockers, `self_reported_failure` or `self_reported_blocked` clause (absence `none`, not applicable): major; `report_path_missing` with `absent_now` or `unknown`: info |
| missing_artifact | obligation, absence | `prompt_path` with `absent_now` or `unknown`: info |

Every tuple a row does not list, including a value outside an input's
vocabulary, is a contract error (4.13.4): the detector's finding takes the
highest severity among its rows, the run's `status` becomes `INCOMPLETE`,
and `Run.diagnostics` gains `severity: <detector> has no row for
(<name>=<value>, ...); used the highest row, <severity>` (one line per
distinct detector and tuple, never repeated). A finding with several
clauses (`claim_contradiction`) or several attempts (`structured_output_failure`,
`codex_write_denied` per attempt) takes the highest severity among its
parts.

How the derived inputs are read (N5b):
- `recovered` for an attempt. Candidate attempts are the attempts of the
  same key with a higher ordinal (`silent_retry` and `attempt_failed`; the
  attempt itself is not a candidate, since it did not recover itself) plus,
  for `structured_output_failure` only, the attempt itself. A candidate
  "completed with a result" when the journal closed it with a `result`
  event (`journal_state` `completed`); an attempt with no journal record
  (a session subagent, the session main thread, an unkeyed entry) never
  completes with a result, because nothing recorded one. `yes` when any
  candidate completed with a result. Otherwise look at the key's last
  attempt (the highest ordinal, or the attempt itself): `no` when it was
  closed `failed` or `superseded`, or its outcome is `killed` or `failed`,
  or the run is terminal; `unknown` when it has no terminal record (journal
  state `open`, or no journal record, or no transcript) and the run is not
  terminal. The run is terminal when its lifecycle terminal (4.13.3) is
  `completed`, `failed`, `stopped`, or `journal_resolved`; a session has no
  lifecycle and is never terminal. So every session `structured_output_failure`
  stays major unless a terminal record says otherwise, which none does.
- `work_risk` is run-level (the same for every finding of a run), in this
  order: the risk population (4.13.1) is partial, or any ledger of an
  attempt in the population is partial (or absent): `unknown`; otherwise any
  `observed`, `claimed_target`, or `attempted` entry in a population ledger:
  `present`; otherwise `none`. An attempt outside the population does not
  vote.
- `absence` of a path: `present_now` when `os.stat` succeeds, `absent_now`
  when it raises `FileNotFoundError` or `NotADirectoryError`, `unknown` for
  any other `OSError`, a `ValueError`, or a path that is not absolute.
  `claim_contradiction` still ignores a relative `report_path` (no finding,
  as before); `missing_artifact`'s grammar only matches absolute paths.
- A finding whose absence is `absent_now` titles itself "`<path>` is absent
  now; the tool cannot tell whether it existed when the run ended"; one
  whose absence is `unknown` titles itself "`<path>` could not be checked
  now; the tool cannot tell whether it existed when the run ended". A
  `claim_contradiction` finding with a failing tests, suite, ok, ready, or
  blockers clause keeps its v0.2 title whether or not a `report_path`
  clause is also present. The `WHAT TO DO` `[look]` item 4.13.4 describes
  is not part of N5b (its wording is unspecified and it is rendering).
- `attempt_failed`'s title tail ("the key was not retried", "retried, no
  later attempt completed", "a later attempt completed") uses the same
  "completed with a result" test as `recovered`, so the tail and the
  severity cannot disagree.

**A returned result that reports a failure count (G3, v0.3.1).** One shared predicate,
`claims.self_reported_problems(result) -> SelfReport(failed_counts: int, blocked: bool)` (a stdlib
module used by the reader, the detector, the audit and the corpus check), reads the top level of a
journal `result` dict only: `failed_counts` is the number of keys that end in `_failed` and hold an
`int` (a `bool` is not one) above 0, leaving out the two keys the legacy clauses read
(`tests_failed`, `suite_failed`); `blocked` is true when the key `blocked` is exactly `true`. No
free-text key is read, no nested value is read, and `blockers_fixed`, `blocker_kind`,
`blocked_reason`, `errors`, `failures`, `blocked: "yes"` and a nested `{"x": {"a_failed": 1}}`
count for nothing. The reader keeps only the `SelfReport` on the attempt (a private field, in no
`to_dict`, no export, no JSON). A `completed` attempt with a non-empty `SelfReport` and no tests,
suite, ok, ready or blockers clause gets one `claim_contradiction` with the clause
`self_reported_failure` (`failed_counts` above 0) or `self_reported_blocked` (blocked alone), major
at every level, so an attempt carries exactly one major `claim_contradiction` through the legacy
clauses or this one. Its title is the fixed sentence `completed, but its own returned result reports
failures in <n> field(s)` or `completed, but its own returned result reports it was blocked` (`n` is
`failed_counts`, the number of positive `*_failed` fields, never a sum of their values; failure
counts win when both hold; keys are compared as written, so `X_FAILED` counts for nothing), its metrics add `self_reported_failure_counts`
and `self_reported_blocked`, and its evidence is the one pointer `journal.jsonl:<line>` of the
`result` event, never an excerpt. A `report_path_missing` clause on the same attempt stays in
`metrics.clauses` and the finding keeps this title and severity. Neither a key name nor any result text reaches a finding title,
metric, evidence line, story, rescue brief or bug report at any disclosure level (the
`reported.keys` list of 5.3 is the one place a result key is exported, and at share it lists
pseudonyms, G-B1, GB-8). A story and the rescue brief say `<n> failure count(s)`
or `being blocked`.

Marker provenance (implemented in N1, 4.13.2): every "carries the ... marker"
cell above (bash_backgrounded, bash_timeout, tool_json_error,
structured_output_failure, codex_write_denied, classifier_unavailable,
git_cwd_mismatch) and attempt_failed's four runtime causes plus its kill
sentinel now name a canonical id in the marker registry (`markers.REGISTRY`),
not a raw substring test. The detector fires only on an anchored,
not-in-payload `MarkerHit` from the attempt's own `marker_hits` (never
`Event.markers`, kept only for compatibility); the registry's own permitted
shape per id already encodes what some of these detectors used to check by
hand (codex_write_denied's D-C: a Bash tool_result whose command runs codex
as a program, or an assistant entry's own text when the attempt has such a
call; the five markers restricted to a `tool_result` shape never fire on a
`user` entry or an `attachment`; `tool_input_invalid` and
`structured_output_mismatch` are mutually exclusive by the tool paired to
the result, never both). bash_timeout keeps its second, marker-independent
trigger (`Event.exit_code == 143`) exactly as before: not every bare exit
code 143 carries the explanatory second line the registry's own predicate
requires. 4.13.2 is the full contract.

silent_retry's title counts restarts, not attempts: N attempts is N-1
restarts ("restarted 1 time" for 2 attempts, "restarted 2 times" for 3),
since the thing a reader cares about is how many times the runtime silently
started over, not the length of the attempts list (M1 review finding 5).

attempt_failed's cause (4.2) requires the marker fragment to START the last
assistant entry's own text (`str.startswith`, not membership anywhere in
it): an ordinary sentence that happens to quote a marker in passing ("I saw
API Error: Server error mid-response in a log") must come back
unattributed, never misattributed to that marker's cause (M1 review finding
3). A usage-limit `reset_time` is captured only when the text starts with
that marker, and is kept to at most 40 characters drawn from
`[0-9A-Za-z:., ]`, anything else stripped, so a corrupted or unexpected
remainder can never ride into the title or exported metrics unfiltered.

attempt_failed's title tail names three states, not two (M1 review finding
4): no later attempt exists for the key at all ("the key was not retried",
major); a later attempt exists but none of them completed ("retried, no
later attempt completed", major -- a `failed, started, failed` sequence and
a `failed, started` sequence whose second attempt is still open both land
here, since both really were retried); a later attempt completed ("a later
attempt completed", minor). "Later" is evaluated per attempt (a higher
ordinal of the same key), not per key as a whole, so the last attempt of a
retried key that itself has no successor still says "not retried".

silent_retry's evidence (unit M1d) never quotes a non-final attempt's own
prompt -- that text is user-authored and, for a retried key, is often the
exact same prompt repeated verbatim on every attempt, a privacy residual
rather than useful evidence. For each non-final attempt it instead points
at the kill sentinel line that ended it, via `evidence_line` (4.5), when
that attempt's transcript carries one; an attempt with no sentinel (a
missing transcript) is simply omitted rather than padded with a placeholder.
Then, for the final attempt, one plain pointer line names where it started
with no excerpt at all: `<transcript>:<line>: attempt <ordinal> started`.

The main attempt of a session run is scanned by the tooling detectors
(bash_*, tool_json_error, structured_output_failure, codex_write_denied,
classifier_unavailable, git_cwd_mismatch, permission_denied, codex_unavailable) and never by mislabeled_kill or
missing_artifact. Whether a main-session interrupt was human is not
knowable from the transcript (a `-p` session has no human either); v0.1
records this as a documented limit rather than guessing.

**wasted_time (field milestone F, FA5).** One rule serves `wasted_seconds`, the
AGENT table's WASTED column (4.7), and the header sentence (4.7): the wall-clock
(each attempt's `first_ts` to `last_ts`, 0 when either is unknown) of the
deduplicated union of every non-final attempt of a key (a retry, whatever its
outcome) and every attempt whose `outcome` is `killed`, `failed`, or `superseded`
at any position, each attempt counted once. A final attempt that completed never
counts, and neither does an open final attempt that may still be running (its
outcome is `completed` or `unknown`, not one of the three). Why: a subagent or
a workflow key killed on its only attempt returned no result and burned its
whole span, yet the old rule (every attempt but the last) showed 0s for the one
bad event (a session subagent killed after about 70 minutes). A session run has
no retries, so for it the rule reduces to its killed, failed, or superseded
subagents; its `wasted_time` finding gains `wasted_seconds` (last key, the other
three unchanged). `wasted_tokens` (workflow) sums the same set. The workflow
title says `<W> wasted across <n> retried attempts` unchanged, and appends ` and
<k> final attempt(s) that ended without a result` only when k, the number of
counted final attempts, is above 0. `silent_retry`'s own `wasted_seconds` metric
is unchanged (it measures retries only); scan's workflow totals follow
`wasted_time` as before.

Silence and durations come from transcript timestamps only. A detector never
reads a file outside the run directory except `missing_artifact` and
`claim_contradiction` (`os.path.exists` on the named path -- `report_path`
for `claim_contradiction`, following the same convention: only an absolute
path is ever checked, since neither `Attempt` nor `Event` records a `cwd`
to resolve a relative one against in this milestone, and a relative
`report_path` is therefore never counted as a contradiction either way) and
the parent index (4.2).

**Self-reference rule.** Anyone who works on this repository, or greps their
own logs, has their agent read files that themselves quote a marker string
(this document, `markers.py`, `reader.py`) or run a read-only Bash command
over them. That must never look like the marker actually firing. `Event`
carries `read_only_output: bool`, set by the reader for a tool_result whose
resolved tool is `Read`, `Grep`, `Glob`, or `NotebookRead`, and for a Bash
tool_result whose command text -- anywhere in it, not only as its first
word (D-B: a loop such as `for f in ...; do echo "=== $f ==="; cat "$f";
done` counts too) -- contains one of `cat`, `sed`, `head`, `tail`, `grep`,
`rg`, `nl`, `less`, `more`, `bat`, `wc`, `diff`, `git show`, `git diff`,
`git log`, `git grep`, `git blame` as a whole word, AND contains none of
`node`, `codex`, `python`, `uv run`, `pytest`, `npm`, `npx`, `curl`, `make`,
`tee`, or a `>` redirection. The reader still scans the full text for
markers (section 4.5 is unaffected); the marker-based tooling detectors
(`tool_json_error`, `structured_output_failure`, `codex_write_denied`,
`classifier_unavailable`, `git_cwd_mismatch`) skip an event with
`read_only_output` true. `bash_backgrounded` and `bash_timeout` never
consult it: a timed-out `cat` is still a timed-out command. Tooling-detector
markers only ever count in a `tool_result` event or an `assistant` text
event, never a `user` one (D-B; the `kill_sentinel` of 4.13.2 is the one
marker whose permitted shape is a user entry): a review or spec pasted into a USER prompt that happens
to quote WRITE_DENIED is not a sandbox denying anything, and
`codex_write_denied`, `classifier_unavailable`, `tool_json_error`,
`git_cwd_mismatch`, and `structured_output_failure` all honor this.
`tests/fixtures/runs/self_reference` carries five shapes (a read-only Read,
a read-only Bash `sed`, a genuine denial from a non-read-only Bash `node`
call, a USER prompt quoting WRITE_DENIED, and a read-only Bash loop around
`cat` quoting WRITE_DENIED) and asserts exactly one `codex_write_denied`
finding, attributed only to the genuine denial, and zero
`classifier_unavailable`.

### 4.4 Interrupt attribution

The reader sets `Attempt.interrupt` for every attempt whose last user event
starts with the kill sentinel. Cause, evaluated in this order, first match
wins:

1. `parent_stop`: the parent transcript has a `TaskStop` tool_use whose
   input names this attempt's agent id or task id, or any `TaskStop` within
   30 s before the kill timestamp. Severity info. Title: "<agent> stopped by
   the parent session".
2. `human`: the parent transcript has a kill sentinel within 30 s of the
   kill timestamp. Severity info. Title: "<agent> interrupted together with
   the parent session".
3. `runtime`: workflow kind only, and the same key has a later attempt. The
   restart is affirmative evidence the runtime killed and replaced it.
   Severity major. Title: "<agent> attempt N was killed by the runtime after
   Ns of silence, logged as a user interrupt".
4. `unattributed`: everything else. Severity major when
   `silence_seconds >= 300`, else info. Title: "<agent> interrupted after Ns
   of silence, cause unattributed" (major adds "consistent with a runtime
   kill"). When silence is None the title says "silence unknown" and the
   severity is info.

**(v0.3, N8, 4.13.8) Precursor wording.** For cause `runtime`, and for
`unattributed` with a known silence, when the attempt's last tool activity
before the kill sentinel is a `tool_result` carrying an anchored
`bash_backgrounded` hit (4.13.2) and no later `tool_use` or `tool_result`
exists in the attempt (parallel calls can return out of order), the title
gains `, after a Bash command was moved to the background (line N)` right
after the silence clause, `N` being the marker's transcript line: "impl
attempt 1 was killed by the runtime after 350s of silence, after a Bash
command was moved to the background (line 3), logged as a user interrupt".
The wording never says or implies the backgrounding caused the kill (no
"caused", "because", or "due to" in any title). Severity, rule ids, and cause
are unchanged; a `parent_stop` or `human` title, and an `unattributed` title
with unknown silence, never name a precursor.

The 30 s windows are constants in `detectors/base.py` with the reasoning in
a comment. Timestamps that are missing or out of order produce
`silence_seconds=None`, never 0.

### 4.5 Evidence quality

`meaningful_event_before(attempt, index)` in `detectors/base.py` walks
backwards skipping `thinking`, `attachment`, and events whose stripped text
is empty, and stops (returns None) at a `compaction` event. Before the
excerpt is taken, the reader strips `<system-reminder>...</system-reminder>`,
`<total_tokens>...</total_tokens>`, and `[harness: ...]` spans from the
field text; markers are still scanned on the raw text. Evidence lines are
`relative/path:line: excerpt` with excerpt at most 160 chars, redacted at
render time.

### 4.6 Analysis status and verdict

`Run.status` is INCOMPLETE when: the journal could not be parsed, any
transcript named by the journal was unreadable (not merely absent), more
than 5 percent of a transcript's lines were undecodable, a timestamp
diagnostic affected an attempt's first or last event, a `started` event
carried a non-string `key` or `agentId` (M1 review finding 2), or (v0.3, N2,
4.13.3, implemented in N2) a workflow run's own `Lifecycle.terminal` is
`unknown` -- a conflict, an unrecognized launch status, an ambiguous
launch, an unreadable parent transcript, or an incomplete generation is
exactly the same "the reader could not fully establish what happened" shape
every other condition in this list already is, or (v0.3, N5b) a detector
reached a severity input tuple 4.3's table does not list (a contract error:
the detector's highest row is used and a `severity:` diagnostic names the
detector and the tuple). Otherwise COMPLETE. The contract error is set while
detectors run, so a caller reads `Run.status` after running them.

A `started` event's four fields are validated before an Attempt is built
from them, never after: a non-string `key` or `agentId` (present with the
wrong type, never merely absent or explicit null, which are the existing
"unkeyed"/"blank id" shapes) skips the whole entry -- no Attempt, no
LogicalAgent -- with a line-specific diagnostic
(`journal.jsonl:<line>: started event has a non-string agentId`, or `key`)
and forces status INCOMPLETE; a non-string `label` or `phase` is cosmetic
rather than identifying, so the entry still becomes an Attempt, with the bad
value replaced by `""` (never `None`, which would crash report.py's AGENT
table formatting) and its own diagnostic. Before this fix a non-string
`agentId` raised `TypeError` out of `_valid_agent_id` and could reach
`report._key_digest`'s `.encode()` call the same way for a non-string `key`;
`tests/fixtures/runs/journal_malformed_fields` carries one of each shape.

**(N3b) A `started` event without a canonical agent id (C-5).** A `started`
event whose `agentId` is missing, null, or a string that is not a canonical
17-hex agent id (`a` plus 16 lowercase hex characters) still builds its
Attempt as above (the "blank id" shape and the hash replacement are
unchanged), but it now also forces status INCOMPLETE and records the
diagnostic `journal.jsonl:<line>: started event has no canonical agentId`,
with `<line>` the physical journal line of that event. For such an event
no transcript file and no `.meta.json` file is ever opened (the id would be
spliced into a file name, and `q/../../OUTSIDE` names a file outside the run
directory): the Attempt has no transcript path, no events and an empty meta
(N3b S14). A non-string `agentId` keeps the skip and its own diagnostic above. Measured on the
author's corpus: 0 of 950 real `started` events lack a canonical agent id, so
no real verdict changes.

**(N3b) Journal keys in diagnostics (P10b).** Three reader diagnostics quote
a journal key: a key's attempt "replaced by a later attempt with no recorded
outcome", and a `result` or `failed` event with "no open attempt" or one that
"does not match the open attempt's agent id". A journal key is text the run's
author chose, and a valid-shaped one (`v2:` plus 64 hex) can embed a real
agent id, so these diagnostics are `KeyedDiagnostic` values (a `str`
subclass). Their plain string is the safe form: `key@<line>` replaces the
quoted key, where `<line>` is the physical journal line of the attempt's
`started` event (for the replaced-attempt diagnostic) or of the `result` or
`failed` event. Their `.raw` attribute is the exact text v0.2 printed. Only
a raw sink asks for `.raw`, and it does so before any redaction call: raw
JSON (`--no-redact`) prints the v0.2 text, while schema-2 JSON at share
prints the safe form (the list-of-strings shape does not change). No text
sink prints `Run.diagnostics` today; a sink added later starts safe because
the plain string is the safe form.

Verdict word: `FAILED` (any critical), `DEGRADED` (major, no critical),
`CLEAN` (neither), always suffixed with ` (INCOMPLETE)` when the status is
INCOMPLETE. Exit codes: 0 ok, 1 usage error, 2 no runs or not a run, 3
verdict FAILED, 4 INCOMPLETE without FAILED. `scan --help` lists these codes
(N5a-2), and README's Privacy section says versioned JSON at the default
level is share-safe like text and that `--local --json` is valid.

**(v0.3, N5b) Verdict word changes.** From N5b the verdict word rule
above is replaced by: `FAILED` when the run's own lifecycle terminal
(4.13.3) is `failed` OR any finding is `critical`; `DEGRADED` when any
finding is `major` (and the run is not `FAILED`); `CLEAN` otherwise; the
` (INCOMPLETE)` suffix is unchanged. Exit 3 means verdict `FAILED` (was:
any critical finding) -- the same exit code, a wider and more honest
condition behind it. In practice no detector produces `critical` severity
in v0.3 (4.13.4's severity table has no `critical` row; the value is
reserved, 4.13.4), so from N5 on `FAILED` is reached only through a failed
lifecycle terminal; `critical` stays in the verdict rule's own wording only
so a future detector or observer that does earn `critical` (4.13.4's
unknown-branch rule, or a later end-time observer) is not a second special
case.

Verdict over non-silenced findings only (v0.2, unit M3c, 4.10). When the
user's config file names a detector in `[detectors].off`, `verdict_word` is
still the same function above, but every caller that decides what to show a
person -- `run`'s header and `--json` `verdict`, `scan`'s per-row `verdict`
and its exit-code rollup, `demo` -- calls it over the *shown* findings
(`config.partition_silenced`'s first return value), never the full list a
detector actually produced. A silenced finding still exists (it is still in
`--json`'s `findings` list, marked `silenced: true`, 5.3, and still counted
by `trend`), it just never gets a vote in the verdict. This is exactly why a
critical-severity detector id in `off` is refused rather than honored (4.10):
a silenced critical would silently turn a `FAILED` exit code 3 into `CLEAN`
exit code 0, which is the one thing a config file must never be able to do.

**(v0.3, N5)** Once no detector's `severity_default` is `critical` (4.13.4),
`config.critical_rejected` (4.10) simply never fires -- there is no id left
for it to reject -- and this paragraph's own guarantee is carried instead by
`FAILED` depending on the run's lifecycle terminal (4.13.3), which `[detectors].off`
has no power over at all: a config file was never able to silence a
lifecycle terminal, so this is still the one thing it must never be able to
do, just no longer through a detector severity.

### 4.7 Report contract

`run` text output (rewritten by unit M3a; see `.reviews/proposals/
2026-09-v0.2-guided-rescue.md` section 6.1 for the design this implements)
leads with the verdict and the work at risk, not a findings dump:

```
RUN <id>  <VERDICT>[ (INCOMPLETE)][ (unfinished)]  ended <YYYY-MM-DD HH:MMZ> (<N days|hours|minutes> ago)
<account: 1 to 3+ sentences>

WORK AT RISK  <one line>
  <optional second line>

WHAT TO DO   (heading and items printed only with at least one item)
  1. [look]   <command>   <why, 6 words max>
  2. [decide] <sentence or command>
  (at most 3 items)

AGENT     ATTEMPTS  END                KILLED  PATHS  COMMITS  WASTED
<one row per logical agent>

STORIES (<n>)   details: --verbose   evidence: --evidence
  <agent>  <story text, at most 2 lines, wrapped at 78 columns>
    <evidence pointer lines, at most 2, only with --evidence>
  ...
  run      <story for run-level findings without an agent, if any>

<n> info findings not shown (--verbose)
```

Header: the verdict word rules are unchanged (4.6). A session run's header
word is `SESSION`, not `RUN`. **(v0.3, N2, 4.13.3, implemented in N2)** The
time clause reads `ended <ts> (<age>)`, using `run.ended_at` converted to
UTC (`ended: unknown` when it is `None`), for any run whose own terminal is
certain -- a workflow whose `Lifecycle.terminal` is anything but `none`.
When nothing certifies the run has actually stopped (a session, which
carries no lifecycle at all, D11; or a workflow whose `Lifecycle.terminal`
is `none`), the same value is worded `last activity <ts> (<age>)` instead
-- "ended" would otherwise claim a certainty nothing here actually has --
plus a trailing `possibly running` when activity reads `recent` (a
workflow's own `Lifecycle.activity`; a session's own last recorded event
checked against the same 10-minute freshness rule, since a session carries
no precomputed activity field to read). The relative age ("N days/hours/
minutes ago") is computed against `report._now()` (a thin `datetime.now(
timezone.utc)` wrapper a test monkeypatches, the same pattern `detectors/
claim_contradiction.py` already uses), floor-rounded to the largest unit
that is at least 1; the recent/stale/unknown activity judgment itself is
never part of this injectable clock -- it is a file-freshness check against
the real wall clock, set once at load time for a workflow (4.13.3). The
header carries a trailing `(unfinished)` tag when `Run.finished` is exactly
`false` (12's D-C; never for `null`, a session's own value): `run` always
computes and prints real findings and a real verdict over whatever a
workflow has so far, even one that has not finished, since an unfinished,
retried, or abandoned run is exactly the run a user most wants to inspect;
the tag is the only difference, never a substitute verdict. **(D11)** A
session's `Run.finished` is `null`, not `true`, as of N2 (4.13.3) -- a
model-level fact, true immediately at the Python level, not merely once
schema 3's JSON surface lands in N5: this tag's own renderer-level behavior
(never show `(unfinished)` for a session, since `null` is not `false`) and
the schema's field-level `null` are two different facts about the same
underlying truth -- a session simply has no lifecycle model to report -- not a
contradiction between them. The old v0.1 project line (project slug, claude
version, time range) is dropped from the default report; nothing else in
this milestone needs it.

Account (workflow): "`<A>` agents took `<T>` attempts." always, then any of
the following whose own count is nonzero, each its own sentence, omitted
outright at 0 rather than printed as "0 ...": "`<K>` attempts were killed by
the runtime and restarted." (interrupt cause `runtime`); "`<P>` were stopped
by the parent session." (`parent_stop`); "`<H>` were interrupted with the
parent session." (`human`); "`<U>` were interrupted, cause unattributed."
(`unattributed`); "`<F>` failed (`<causes, comma separated, with counts>`)."
(one `attempt_failed` finding per failure, causes grouped by
`metrics.cause`, first-seen order, each rendered `<phrase> (<count>)`);
"`<W>` was spent on retries and attempts that ended without a result."
(`wasted_time`'s own `wasted_seconds` metric, formatted with
`detectors.base.format_duration`; field milestone F, FA5: omitted at 0, under the
one wasted-time rule stated at 4.3's `wasted_time` row).
Session: "session ran `<T>`; `<N>` subagents, `<K>` interrupted (`<U>`
unattributed); retries not observable." (unchanged from v0.1's session
summary line, `report._session_summary_line`) plus the same failed-sentence
when one applies -- (G1c, v0.3.1) a session subagent that reported failure is one
`attempt_failed` finding, so the two account builders share the one
failed-clause function rather than duplicating it, and likewise the one
wasted-clause function: the same "`<W>` was spent on retries and attempts that
ended without a result." follows when the session's `wasted_seconds` is above 0
(FA5: a subagent killed after 70 minutes is one such attempt).

WORK AT RISK (N5c-1; plan S4, S11): computed from the write ledger and the
repository groups (4.13.1), not from `AttemptFacts`. The block is its first line,
then indented lines in this order. Exact strings (the owner reviews them):

- `WORK AT RISK  none: no attempt was killed, failed, or left unfinished.` when
  the risk population is empty (and complete), and `WORK AT RISK  none: the
  attempts at risk wrote no paths.` when it is not empty (field milestone F, P5):
  in both cases the risk population is complete and every population ledger is
  complete with no `observed`, `claimed_target`, or `attempted` entry
  (`severity.work_risk` is `none`, 4.13.4), and, for a session, every subagent
  outside the population has `return_state` `returned` (4.13.1). Nothing follows
  it. (The old single sentence said "killed or failed attempts wrote no paths"
  even when nothing had been killed or failed.)
- `WORK AT RISK  unknown: the tool could not establish which attempts were at
  risk: some run files could not be read or matched.` when the population is
  empty and its completeness is `partial`; the words for `unassignable_attempt`
  name each attempt and the reason (field milestone F, P6): `<id> could not be
  placed in a launch of this run (<reason>)`, `<id>` as in the per-attempt lines
  below (`a:<alias>` at share), `<reason>` one of `it started before the run was
  launched` (a launch time exists and the attempt's first timestamp is earlier than
  every launch call), `an early line has no usable timestamp` (the attempt has no
  first timestamp, or an event-bearing line at or before its first one has no
  usable `timestamp`), `the launch times are missing or out of order` (a
  generation's launch timestamp is missing or the timestamps do not strictly
  increase), `no launch of this run was found in the parent transcript` (the run
  has no generation and the parent snapshot holds no launch evidence for it, as when
  the parent transcript is absent), or `a launch of this run was
  found but could not be used` (the run has no generation but a launch was found and
  deliberately not used: an ambiguous launch whose tool-use id repeats, or a
  malformed, rejected, or result-less resume launch naming the run, as in the run's
  parent discovery diagnostics or an `ambiguous_launch` lifecycle). The
  no-generation reasons and the launch-times reason are checked first,
  in that order. Attempts with the same
  reason share one phrase, `<id>, <id> and <id> could not be placed in a launch of
  this run (<reason>)` (two: `<id> and <id>`), in `Run.attempts` order, at most
  three named per reason and then `and <k> more` (`a, b, c and 2 more`); phrases
  of different reasons are joined with "; ", in the order each reason first
  occurs. That phrase list is joined with "; " to the discovery words when both
  `unassignable_attempt` and `discovery_incomplete` apply, and the same words end
  the `the list of attempts at risk may be incomplete:` line below.
- `WORK AT RISK  unknown: <k> subagent(s) have no established return (<breakdown>);
  the tool cannot say what they wrote.` (`1 subagent has ... what it wrote`) when
  the population is empty and complete but the session guard fails, followed by
  one line per such subagent, `  <name>  <reason>`; the breakdown and the
  reasons are the W1 words in the paragraph below the numbered list (v0.3.1).
- otherwise `WORK AT RISK  <n> attempt(s) at risk: <p> path(s) named, <c>
  commit(s) reported` ending `.` when every population ledger is complete and the
  population is complete, else `; the tool cannot see all of what was written.`
  (`n` the population size, `p` the distinct ledger paths with effect `observed`,
  `claimed_target`, or `attempted`, `c` the commit ids the population attempts'
  facts report, unverified summary-shaped output lines, 4.13.1). FA6: when the
  population attempts' `commit_commands_without_id` sum `k` is above 0, the
  clause becomes `<c> commit(s) reported (<k> call(s) with a commit command reported
  no id)`: the claim is only what one call's exit status proves, that a call
  containing a commit command completed and reported no id, never that the commit
  ran.

Then, for a non-`none` block, in order:

1. One line per population attempt, `  <id>  <clause>; <clause>`, the id being
   (G6, v0.3.1) the agent as the AGENT table names it at that level: `a:<alias>
   (agent-3)` at share, `<real id> (<label>)` at local and raw (`main` for a session
   main thread; an empty label shows the id alone). The same naming is used by every
   subagent line below. Clauses, each only when its count is above zero: `wrote <n>
   path(s) (file tools)` (distinct `observed` paths), `named <n> path(s) in write
   commands` (`claimed_target`), `tried <n> path(s) with no confirmed result`
   (`attempted`), `ran <k> shell command(s) that may have written more` (Bash and
   Monitor calls), `called <m> unrecognized tool(s) that may have written`
   (`possible` entries of basis `unknown_tool`), `delegated <d> call(s) that may
   have written` (Agent, Workflow, SendMessage, Task calls), and `ledger
   incomplete (<codes>)` for the attempt-level partial reasons (not the shell and
   `possible_*` ones, not the run-level ones). An attempt with no clause reads
   `wrote nothing the ledger names`.
2. When the population is `partial`: `  the list of attempts at risk may be
   incomplete: <words>`.
3. The repository state line: with `--observe`, `  <d> paths dirty now, <m>
   missing, <k> clean` over the checked groups when any is checked (the old
   counts format; `1 path dirty now` for one, N5 fix 1, m8), and for each not-checked group that is not refused `  repo
   state not checked (<reason>)`, once per distinct reason; without `--observe`,
   `  repo state: run with --observe` when some named group is not refused and has
   a path and the run's last activity is within 72 hours (or unknown), `  historical:
   repo state not checked (run older than 72h)` when it is older, and nothing when
   every group is refused.
4. One manual line per named group that is refused or, with `--observe`, not
   checked or holding `unobserved_paths` (N5 fix 1, m3: the person is always told to
   look, whatever the reason, a symlinked cwd included), in sorted candidate order: at
   local and raw `  run yourself: git -C <repo> status --short` (the real path at raw,
   quoted with `shlex.quote`; the P19 spelling at local, which is not always a path
   a shell can use), at share `  run yourself: git status --short in <alias>; --raw
   prints the exact command` (raw is the level that prints a runnable command).
   Then, when the unknown group is exported, `  run yourself: git status
   --short in the repository the agent worked in`, and at local and raw only one
   `    hint: an attempt ran in <cwd>` line per distinct absolute cwd of the
   unknown group's attempts (first seen order, at most three).
5. For a session, when some subagent outside the population has `return_state`
   other than `returned` (W1, v0.3.1): `  <k> subagent(s) outside this list have no
   established return: <names>` (`1 subagent ... has`), names as in item 1.

When the population is empty, complete, and a session subagent has no established return, the
block is `WORK AT RISK  unknown: <k> subagent(s) have no established return (<breakdown>); the
tool cannot say what they wrote.` (`1 subagent has ... what it wrote`), then one `  <name>
<reason>` line per such subagent. The breakdown lists, in this order and omitting zero counts,
`<n> reported failed`, `<n> reported killed`, `<n> possibly running` (`pending` or `unknown`
and the child's transcript modified within 600 seconds), `<n> no return evidence found` (stale
and `pending`, or `unknown`/`no_evidence`: a readable parent with nothing for this child or its
latest round; ` in its parent agent` is appended to the per-child reason of a nested child),
`<n> return could not be established` (stale and any other `unknown` reason). The per-child
reason is the same phrase. The words `never returned` appear nowhere.

No rendering prints a repository path or a real id at `share`.

WHAT TO DO: deterministic, at most 3 items, each tagged `[look]`
(read-only) or `[decide]` (needs a human), evaluated in this fixed order
and appended only while the list has fewer than 3 items already:

1. `[look] agent-postmortem run <id> --observe` ("see which paths are
   dirty") when at least one named repository group is not refused and holds a
   path (N5c-1: only such a group is observed), the run ended within 72 hours (or
   its end time is unknown), and `--observe` was not already given.
2. `[decide] wait until <reset_time> before relaunching` when a major
   `attempt_failed` finding (its key was not yet successfully retried) has
   `retry_now: "no"` and a `reset_time`; otherwise `[decide] relaunch is
   safe now: <cause phrase>` when one has `retry_now: "yes"`.
3. (N5c-1, 4.13.4) `[look] agent-postmortem run <id> --verbose   <n>
   expected file(s) absent now: check whether the run wrote them` (singular
   `file` and `it` for one) when at least one shown info-severity
   `missing_artifact` or `claim_contradiction` finding exists and the run's
   own last activity (`ended_at`) is known and within 72 hours (an age of
   exactly 72 hours counts). One item however many findings exist, no path
   in its text, the same item in JSON `next`. It counts toward the cap of
   three and goes before the rules item below.
4. `[decide] rules that would have prevented this: agent-postmortem rules
   propose` when at least one major or critical finding carries a rule id.
   `run` never scans another run to report a real "N of M runs" rate (that
   would mean scanning the whole fleet from inside `run`), so this always
   points at the fleet-wide proposal command (section 9, unit M6) rather
   than naming this one run; `rules --run <id>` still exists for a
   run-scoped count, it is simply no longer what `run` itself prints.

(v0.3, N8, 4.13.8) There is no generic "see the findings hidden here" item:
the default view hides only info findings, so such an item would add
nothing the trailing `<n> info findings not shown (--verbose)` line does
not say. That line stays the pointer. The `WHAT TO DO`
heading, its items, and the blank line after the section are all omitted
when there is no item, so the text never shows two blank lines in a row;
JSON `next` is `[]` then, as before.

No item ever names an instruction file (CLAUDE.md or otherwise) or prints
`apply`; `rules propose` is the only way `run`'s own output points at
rules. The old NEXT ACTION and SUGGESTED RULES sections are gone from the
text report entirely (`--bug-report`'s own "Suggested fixes" section is a
different renderer, untouched by this unit).

AGENT table: `AGENT`, `PHASE` (kept only when at least one logical agent of
this run actually carries a nonempty phase; a session's subagents never do,
so a session's table never shows it), `ATTEMPTS`, `RESUMES` (G9, v0.3.1: only when some
subagent of a session run has `resumes` above 0; the count, blank for 0), `END` (one of
`completed`, `killed`, `failed`, `stopped by parent`, `interrupted`,
`superseded`, `no result`, `possibly running`, `unknown`, derived from the final attempt's own
`outcome` and, for a `killed` outcome, its `interrupt.cause`: `parent_stop`
reads `stopped by parent`, `human` reads `interrupted`, `runtime`/
`unattributed` read `killed`; (G1, v0.3.1) a session subagent's END comes from its
effective end (5.3b): `completed`, `failed` and `killed` as proven by its return state (a kill
sentinel still reads by its interrupt cause), and `open` or `unknown` read `possibly running`
when the child's transcript was modified within 600 seconds (`UNKNOWN_QUIET_SECONDS`) and
`unknown` otherwise; a proven state is never changed by the modification time; **(v0.3, N2,
4.13.3, implemented in N2)** a
workflow attempt whose own per-key journal occurrence was never closed by a
`result`/`failed` event -- still open at the end of the journal the reader
actually read -- reads `no result` instead of whatever `completed` the
transcript-only outcome fallback would otherwise claim for a transcript
that simply has not (yet) ended in the kill sentinel), `KILLED` (how many
of this agent's attempts carry an
`interrupt` whose cause is `runtime` or `unattributed`; a `parent_stop` or
`human` interrupt is not a kill the runtime may own and is not counted, 4.13.8), `PATHS` (N8b, K4.4: the distinct ledger paths of effect `observed`, `claimed_target`, or `attempted`
over every attempt of this agent, plus `facts.paths_written`, each normalized path counted once;
`N+` when any of the agent's attempts has a partial ledger (a transcript that was deleted or
unreadable included, N8 fix 1, S1), since the count is then a lower
bound, `N` when every ledger is complete, `?` when no attempt has a ledger and no attempt's
facts could be established at all; every shell call leaves its ledger partial, C-15, so a
shell-only write reads `1+`),
`COMMITS` (the union of `commits`; FA6: when any of the agent's attempts has a positive
`commit_commands_without_id`, the cell is `?` if no commit id is known (G4, v0.3.1: it was `0+`)
and `N+` if at least one id is known, since the count is then a lower bound: a completed shell
call contained a commit command but reported no commit id, so a commit may have happened),
`WASTED`
(FA5: the one wasted-time rule of 4.3's `wasted_time` row, per agent: the
wall-clock of every attempt of this agent but the last, plus every attempt of it
whose outcome is `killed`, `failed`, or `superseded` at any position, each
attempt counted once; an open final attempt that may still be running is not
counted). A session's
table lists subagents only, as before.

Column widths (field milestone F, P3): the shown AGENT label is truncated to at
most 40 characters (a longer one keeps its first 37 and ends `...`) and the AGENT
column is `max(10, len(longest truncated label) + 2)` wide, so two separator spaces
always sit outside the longest label; PHASE likewise truncates at 22 (first 19 and
`...`) with width `max(11, len(longest truncated phase) + 2)`. The share labels
(`agent-N`, `phase-N`) stay within 10 and 11, so a share table is byte-for-byte what
it was. Legend (P4, G4): when any PATHS or COMMITS cell of the table ends in `+`, one
line directly under the table reads `+ is a lower bound: incomplete evidence may hide
more paths or commits.`; when any COMMITS cell is `?`, one line after it reads `COMMITS ? a completed
shell call contained a commit command but reported no commit id` (G-B1, GB-4: it names its column). Each is otherwise absent, the
`+` line comes first when both appear, and a `?` in the PATHS column adds no legend. The legend
says what the call contained, never that a commit ran or that the command printed anything: a
command such as `false && git commit -q -m x; true` or `git diff --quiet || git commit -q -m x`
contains a commit command whether or not the commit executed.

Two subagents of a session with the same label share one AGENT-table name at share (`agent-N`,
5.1); the `WORK AT RISK` line's id tells them apart (G-B1: the corpus check of 4.13.9 therefore
compares children by identity, never by that rendered name).

STORIES: one entry per logical agent that has at least one non-info finding
(or, under `--verbose`, at least one finding of any severity), worst
severity first (critical, major, minor, info) then by the agent's own
label, plus one trailing `run` entry when at least one finding carries no
`agent_id` and is non-info (or, under `--verbose`, any such finding at
all). Story text is built deterministically from that agent's relevant
findings and its `AttemptFacts`, never by printing a `Finding.title`
directly except as a last-resort fallback for a finding shape none of the
rules below cover:

- `silent_retry`: "killed `<k>` times by the runtime (`<silences>`s of
  silence)" when every one of its `k` restarted (non-final) attempts has a
  `mislabeled_kill` finding with `cause: "runtime"` (silences drawn from
  those findings' own `silence_seconds`), else "restarted `<k>` times";
  `k` is `metrics.attempts - 1`.
- a parent stop: when the agent's final attempt has a `mislabeled_kill`
  finding with `cause: "parent_stop"`, ", then stopped by the parent" is
  appended to the clause above (or stands alone as "stopped by the parent"
  when there is no silent_retry clause to attach to).
- `attempt_failed`: "attempt `<n>` failed: `<cause phrase>`[, resets
  `<t>`]" per finding, `n` the failing attempt's own ordinal. (G1c, v0.3.1) The cause
  phrases gain `launch_result_failed`: `its launch result reported failure` and
  `task_notification_failed`: `its task notification reported failure`, used by the
  account, the story, WHAT TO DO, the scan reason and the rescue brief exactly as the
  workflow causes are; a session subagent's finding title is `<label> failed: <phrase>`.
- `claim_contradiction`: "attempt `<n>` completed but reported `<clauses>`"
  per finding, clauses rebuilt from `metrics.clauses`/`metrics.claims`
  (the same phrases `detectors/claim_contradiction.py`'s own title uses),
  never by re-parsing the finding's title text.
- facts: "Wrote `<p>` paths, reported `<c>` commits." when `p` or `c` is
  nonzero; "wrote no paths" (no trailing period, a fragment) when both are
  known to be 0 and the agent's final attempt was killed; omitted
  otherwise. (v0.3.1, G4b) The commit part follows the COMMITS cell: when a
  completed call contained a commit command and reported no id it reads
  `reported <c>+ commits` if some id is known and `commit count unknown (a
  commit command reported no id)` if none is, and the clause is then shown
  even when `p` is 0. This holds for every sentence that states a commit
  count: the watch KILL line (4.12) and the scan runtime-kill reason (4.8),
  which say only `commit count unknown` to stay short.
- a tooling detector (`bash_backgrounded`, `bash_timeout`,
  `tool_json_error`, `structured_output_failure`, `codex_write_denied`, `codex_unavailable`,
  `classifier_unavailable`, `git_cwd_mismatch`, `permission_denied`) attributed to the agent:
  "`<n>` `<short phrase>`", every present one joined by `"; "` (`permission_denied`:
  "`<n>` tool call(s) denied"; `codex_unavailable`: "`<n>` Codex call(s) could not start").

A tooling detector's finding is attributed to an agent only when every one of its
hits is in that agent's transcripts (4.3). The six run-wide ones
(`bash_backgrounded`, `bash_timeout`, `tool_json_error`, `structured_output_failure`,
`classifier_unavailable`, `git_cwd_mismatch`) have `agent_id` `None` when their hits span two or more agents, so
no agent story claims the run's total.

A finding with no `agent_id` (`wasted_time` always; `missing_artifact` when
it names no attempt; a run-wide tooling finding whose hits span two or more agents) backs the one `run` story instead, its text simply
every relevant finding's own (redacted) title joined into one paragraph.
Info-severity findings never make a story unless `--verbose`; `wasted_time`
is therefore counted (in the trailing "N info findings not shown"
line) but never turns into a story of its own outside `--verbose`.

Evidence: the default report prints no evidence excerpt anywhere -- not
even the `path:line` pointer. `--evidence` prints, under each story, at
most 2 evidence lines drawn from that story's own findings. `--verbose`
prints, after STORIES, the full v0.1-style `FINDINGS (...)` list (`report.
_findings_section`), including info findings. (N3b, 4.13.5, P16) An
evidence line is an excerpt line (`path:line: excerpt`, the v0.1 format,
redacted the same way) only at `local` or `raw` and only with `--evidence`;
at `share`, and at `local` or `raw` without `--evidence`, it is the pointer
alone, `file:line`, plus the marker code in brackets when the line is a
marker hit (`agent-<alias>.jsonl:5 [bash_timeout]`). At `share` the file
name is aliased and `--evidence` prints one note line saying excerpts need
`--local` or `--raw`.

Silenced detectors (v0.2, unit M3c, 4.10). Every finding whose detector id
is in the user's `[detectors].off` is dropped before any of the above ever
sees it: not a STORIES entry, not in the `--verbose` FINDINGS list, not in
WHAT TO DO, not in the account sentence, not in the header's verdict
(4.6) -- `render_run_text` filters `findings` down to the shown set as its
very first step, the same filter `config.partition_silenced` performs.
When at least one detector is silenced this way, the text report's last
line (after everything else, including the `--verbose` FINDINGS list or the
hidden-info-count line) is:

```
<n> detectors silenced by config: <id>[, <id> ...]
```

(singular "detector" when `n` is 1), `<id>` sorted alphabetically. This line
reflects the config file itself -- it prints whenever `[detectors].off`
silenced at least one valid, non-critical id, whether or not that detector
actually fired on this particular run. `demo` follows the same rule (it is
rendered through the same `render_run_text`). `--json` never drops a
finding this way; see 5.3 for the `silenced` key it gains instead.

`scan` (unit M3b) defaults to a triage view, not a full list:

```
NEEDS ATTENTION (<k> of <N> runs)
  <run id>  <age>  <VERDICT>[ (INCOMPLETE)]  <one-line reason>
                   -> <next command>
  ...
LIVE NOW (<n> of <N> runs)
  <run id>  possibly live  <VERDICT>[ (INCOMPLETE)]  <next command>
  ...
LAST 7 DAYS   <w> workflow runs, <r> with retries, <t> wasted; <s> session runs, <i> subagents interrupted
ALL TIME      <same shape>
  full list: agent-postmortem scan --all
```

A run needs attention when any of (rules (a) to (d)): (a) it is `possibly live`
(`Run.finished` false and the newest mtime among its own files is under
`hooks_cli._UNKNOWN_QUIET_SECONDS`, 600s, old -- the best a cold scan can
say about liveness without hook state) **(field milestone F, FB2: rule (a)
alone no longer makes a candidate; such a run is listed under LIVE NOW, below)**;
(b) it ended within 72 hours with verdict FAILED or DEGRADED; (c) it ended
within 72 hours and a killed, failed, or superseded attempt wrote a path;
(d) its verdict is FAILED regardless of age. A possibly-live run is a NEEDS
ATTENTION candidate only through (b) to (d), so a live session that is
CLEAN is not one. Ranked, in order: possibly live first; paths written by such an
attempt, descending; verdict (FAILED, DEGRADED, CLEAN); wasted seconds,
descending; `ended_at`, descending. At most 10 rows (`--limit N`); the
header's `k` is the true candidate count, never truncated by the limit, and when
the limit shows fewer rows the header ends `showing <k>, use --limit N or --all
for more` inside its parentheses (`NEEDS ATTENTION (<K> of <N> runs; showing <k>,
use --limit N or --all for more)`). A
session with 0 subagents and a CLEAN verdict is never a candidate, only
counted in the totals. The one-line reason is deterministic, first that
applies: an `attempt_failed` clause naming the cause(s) and a reset time
when one exists; a runtime-kill clause naming paths named (R1: the WORK AT RISK count line's own numbers, from
one shared helper that scan's runtime-kill and rule (c) reasons and the run report all read:
ledger paths of effect `observed`, `claimed_target`, or `attempted` only, never
`facts.paths_written`, so a relative-path edit with no cwd counts 0 in both places; `N+`
when its ledgers are partial) and commits (`<c>+ commits` or `commit count unknown` by 4.7's
G4b rule) when a `mislabeled_kill` finding's cause is `runtime`; the top major-or-
critical detector as `<detector> x<n>`; `<n> subagents interrupted` for a
session; `<n> attempt(s) at risk: <paths> named, <commit words>` when the run's risk
population wrote at least one path (a rule (c) candidate; the same counts and wording as the
WORK AT RISK count line: `<p>+ paths` when the ledger is partial, 4.13.8, and 4.8's G4b
commit words), also the `attention_reason` of the scan JSON; else `clean`, a defensive
fallback no candidate reaches. The next command adds `--observe` only when a
killed or failed attempt is known to have written a path and the run ended
within 72 hours (or its end time is unknown); it never names an
instruction file or `apply`.

**Empty groups (field milestone F, P12).** When NEEDS ATTENTION has no candidate its
header is followed by the one line `  nothing needs attention`, so the group is never
a bare header; with no LIVE NOW run the LIVE NOW group is omitted entirely, header
included.

**LIVE NOW (field milestone F, FB2).** After NEEDS ATTENTION and before LAST 7
DAYS, a group lists every possibly-live run that is not a NEEDS ATTENTION
candidate (a live run that is DEGRADED or FAILED, or has a path at risk, stays
under NEEDS ATTENTION only, never in both): one line per run, `<run id>  possibly
live  <VERDICT>[ (INCOMPLETE)]  <next command>`, most recent activity first
(`ended_at`, else `started_at`, then run id). Its header is `LIVE NOW (<n> of
<N> runs)`, `<n>` the true count of such runs, and the group is absent when there
are none. `--limit N` caps its lines too; when it shows fewer, the header ends `showing <k>
of <n>, use --limit N or --all for more` inside its parentheses (`LIVE NOW (<n> of
<N> runs; showing <k> of <n>, use --limit N or --all for more)`). Scan's exit code,
`--all`, and every total are unchanged.

LAST 7 DAYS and ALL TIME are the same
stratified totals `--all` prints, computed over a run's `ended_at` (or
`started_at` when `ended_at` is `None`) falling in the window; the
per-detector breakdown is shown only under `--all`, to keep the default
output within about 25 lines.

Silenced detectors (v0.2, unit M3c, 4.10): `scan` computes a row's own
`verdict`, its NEEDS ATTENTION candidacy (`attention`), its one-line
`attention_reason`, and `--all`'s top-detector column all over the shown
(non-silenced) findings, the same `config.partition_silenced` split `run`
uses -- so the reason line and the top-detector column can never name a
silenced id, and a run whose only findings were silenced can drop out of
NEEDS ATTENTION entirely (its verdict is no longer DEGRADED/FAILED and no
qualifying attempt's paths are otherwise at risk). `totals` and
`findings_per_detector` (the `--all` per-detector breakdown) are counted
over every finding regardless of silencing, the same "still counts" rule
`trend` follows (5.3) -- only the per-run narrative fields are filtered.
Both scan renderers (the default triage view and `--all`) print the same
last line 4.7 already defines for `run`, whenever `[detectors].off` is
non-empty.

`scan --all` prints one line per run (id, date, project, kind, agents,
attempts, verdict, top detector among major and critical findings, `-` for
a CLEAN run -- unit M1d: an info-only finding such as wasted_time's,
present on every run, is never interesting enough to call the "top"
detector of a run with nothing wrong), with the verdict followed by
`unfinished` (not replacing it, 12's D-C) when that run has not finished
(D8: `Run.finished` false), or by `possibly running` when it is a session
(no lifecycle, `Run.finished` null) whose `unfinished_state` is `possibly
running`. Rows, in the text and in `--json`, are ordered by the instant the
run started (in UTC, whatever offset its timestamps carry), oldest first, a
run with no start time before every dated one, ties by run id (release
review of 0.3.0: the discovery path order said nothing about time). Then totals stratified by kind: workflow runs,
runs with retries, attempts, wasted hours; session runs, subagents,
interrupted; findings per detector, each over the runs of the kinds the
detector applies to, introduced (field milestone F, P9) by the line `findings per
detector (one finding can cover several events):` whenever the block is not empty
(one finding can cover several events, so the numbers are not event counts). This is
byte-for-byte what `scan`'s own output was
before unit M3b, plus that line; only the default (no `--all`) changed, plus the silenced-
detectors last line above when applicable.

`--json` rows are unchanged in population (one object per run, every run,
regardless of `--all`) and shape (`finished`, `top_detector` `null` instead
of a detector id when there is no major or critical finding), plus six
keys unit M3b adds to every row: `attention` (bool, the same rule the
triage list uses: true only through rules (b) to (d), so a clean possibly-live run has
`attention` false), `attention_reason` (the same one-line reason, or `null`
whenever `attention` is false, a clean live run included), `age` (`"possibly live"` or the abbreviated
`"<n>m/h/d ago"`, or `"unknown"` when both `attention` and `ended_at` are
absent), `possibly_live` (bool), `paths_at_risk` (int, the same path count
the ranking key uses: the distinct ledger paths of effect `observed`, `claimed_target`, or
`attempted`, plus `facts.paths_written`, over the run's 4.13.1 risk population, so a completed
attempt in a failed generation counts, N8b, K4.4), `paths_at_risk_complete` (bool, true only
when the risk population and every ledger in it are complete; the text shows `N+` when it is
false), and `unfinished_state`. **(v0.3, N2, 4.13.3/5.3b,
implemented in N2)** This field's own rule below replaces the one that
previously stood here entirely -- `"abandoned"` is removed, and the value
is derived only from the run's own lifecycle, never from per-key journal
state (a key whose own attempt ends in a journal `failed` event no longer
shows up here at all once the run's own journal fully resolved; that
per-key story belongs to `attempt_failed`, not this field). For a workflow
row: `live` (unit M3b's own hook-free heuristic) still wins first,
`"possibly running"`; otherwise, from `Run.lifecycle.terminal`: `completed`
or `journal_resolved` gives `null`; `failed` gives `"failed"`; `stopped`
gives `"stopped"`; `unknown` gives `"unknown"`; `none` with activity
`recent` gives `"possibly running"`; `none` with activity `stale` or
`unknown` gives `"no terminal record"`. For a session row: `live` wins
`"possibly running"` the same way; otherwise `null` (a session's own
`Lifecycle` is always absent, D11; `"no terminal record"` is in this
field's vocabulary for a session row too, 5.3b, but N2 does not derive it --
this milestone claims less until a concrete need does). `trend` prints
per-detector
weekly counts and before/after rates with the applicable-kind denominator
beside every rate. There is no cross-kind total.

`--json` emits `{schema, run, findings, rules, verdict, status, stories,
next}` (`observation` too when `--observe` was given) through the export
allowlist (5.3; `stories`/`next` added by unit M3a; `schema` by unit M3c).
`stories` is always computed with `verbose=False` (an info-only agent never
backs a JSON story either, matching the default text report; per-flag JSON
filtering is out of scope for this unit). `--bug-report` renders the v0
section order with pseudonyms for every user-authored name, entirely
unchanged by unit M3a (it has its own `report._bug_*` helpers, never
`render_run_text`'s) -- including a silenced detector's finding, which
still appears there: the bug report is meant to show everything and is
never filtered by config.

### 4.8 CLI

```
agent-postmortem scan   [--root PATH] [--since YYYY-MM-DD] [--kind workflow|session] [--here] [--all] [--limit N] [--json] [--local | --raw | --no-redact]
agent-postmortem run    [RUN_ID|PATH] [--here] [--root PATH] [--json] [--bug-report] [--observe] [--verbose] [--evidence] [--local | --raw | --no-redact]
agent-postmortem rules  [--run <RUN_ID|PATH>] [--root PATH] [--all] [--show RULE_ID] [--json] [--local | --raw | --no-redact]
agent-postmortem rules propose [--root PATH] [--since YYYY-MM-DD | --run <RUN_ID|PATH>] [--json] [--local | --raw | --no-redact]
agent-postmortem rules review  [--root PATH] [--weeks N] [--json] [--local | --raw | --no-redact]
agent-postmortem apply  <RULE_ID> --target FILE [--local | --raw | --no-redact]
agent-postmortem trend  [--root PATH] [--weeks N] [--json] [--local | --raw | --no-redact]
agent-postmortem demo   [--json]
agent-postmortem rescue plan   <RUN_ID|PATH> [--root PATH] [--json] [--evidence] [--local | --raw | --no-redact]
agent-postmortem rescue status <RUN_ID|PATH> [--root PATH] [--json] [--evidence] [--local | --raw | --no-redact]
agent-postmortem watch  <RUN_ID|PATH> [--root PATH] [--interval SECONDS] [--max-hours H] [--quiet-after SECONDS] [--done-grace SECONDS] [--json]
agent-postmortem hook   <check|session-start|session-end|workflow-launched|capabilities>
agent-postmortem --version
```

**(N3b)** `--local`, `--raw`, and `--no-redact` (an alias of `--raw`) are one
mutually exclusive group (two of them: usage error, exit 1); `--local` with
`--json` is valid from N5a-1 (schema 3 names the level in its `disclosure`
key). 4.13.5 states what each level prints.

`postmortem` remains as an alias console script. `RUN_ID` may be a prefix of
either kind; ambiguity is an error listing the candidates.

**(v0.3, N6)** `RUN_ID` resolution for `run`, `rescue plan`, `rescue status`,
and `rules --run` (one shared resolver) takes these forms, checked in this
order, each shape-disjoint from the next: an existing path, returned as is;
`a:` and 12 to 64 lowercase hex characters, an agent alias (4.13.5); a session
UUID (a session transcript's stem is its UUID, so one form covers both),
matching every discovered session whose transcript stem equals it, across
every project under the root; `agent-` and an agent id (`a` and 16 lowercase
hex), an agent transcript stem; anything else, the id prefix rule above, which
is unchanged. The special forms never fall through to the prefix rule, so an
uppercase alias, an alias of fewer than 12 hex characters, and a bare agent id
are not forms and end in exit 2 like any unknown id. A stem or an alias matches
exactly the runs whose loaded attempts hold that agent id (a cheap candidate
set from the journal's `started` events and the transcript file names, confirmed
by loading only the candidate runs), so a transcript named by no journal event
is never an identity. An alias resolves through a read-only lookup of the state
directory (4.13.5, 7.5): it opens the directory with the verified descriptor,
reads the secret and the registry relative to it, takes no lock, and creates,
repairs, and writes nothing; an agent id matches when its registered alias is
exactly the typed one. When that state is unavailable the command prints
"stable aliases unavailable on this machine, so '<value>' cannot be resolved"
and exits 2. Zero matches is exit 2 and more than one is exit 1 listing the
candidate run ids; the advice after the list is "expected a longer prefix or the
path of a run directory" for a prefix and "expected one of these run ids or the
path of a run directory" for a UUID, a stem, or an alias. At `share` and
`local` these messages carry only the structural text and spans (`path-N`,
`value-N`, `uuid:<last4>`); `raw` prints what was typed.

`run --here` and `scan --here` (4.13.6): `RUN_ID` becomes optional on `run`,
and exactly one of `RUN_ID` and `--here` is required (both, or neither, is a
usage error, exit 1). `run --here` loads every discovered run, keeps those
whose cwd set holds the caller's directory or a parent of it, picks one by the
selection tuple 4.13.6 states (the caller's own session, else the most recent
activity), and then behaves exactly as `run <that run's path>` on the run it already
loaded (no second load, so a run that vanishes between choosing and rendering cannot
fail the command); no match is exit 2. When more than one run matched, one stderr
line names the choice (4.13.6); stdout is unchanged.
`scan --here` filters before anything is computed, composes with `--kind`,
`--since`, and `--limit`, and exits 2 when no run passes every filter. Neither
message prints the caller's directory. `rescue` and `rules` take no `--here`.

Default `--root` is `~/.claude/projects`, overridable by `CLAUDE_CONFIG_DIR` (first entry when
comma-separated) for parity with ccusage. `scan --all` (unit M3b, 4.7) prints
the full one-line-per-run list instead of the default NEEDS ATTENTION triage
view; `--limit N` (default 10) caps how many candidate rows the triage view
prints and is ignored with `--all`. `run` on a workflow that has not
finished (D8: `Run.finished` false) computes and prints findings and a
verdict exactly as it does for a finished one (12's D-C); only the header
differs, tagged `(unfinished)` (4.7). Exit codes (below) are unaffected by
finished-ness: 3 for a critical finding, 4 for status INCOMPLETE, apply
the same whether or not the run has finished. **(v0.3, N5)** from N5 on,
read "3 for verdict FAILED" in place of "3 for a critical finding" (4.6);
the finished-ness independence is unchanged. `--observe` (4.9) is off by
default; without it, `run`'s text and JSON output are unchanged from
before this flag existed. `--verbose` (unit M3a, 4.7) shows info-severity
findings and the full v0.1-style FINDINGS list; `--evidence` (4.7) shows up
to two evidence excerpts under each story. Both are off by default and
independent of each other and of `--observe`.

`rescue plan` and `rescue status` (v0.2, unit M4; full contract in 4.11):
`RUN_ID` resolves exactly as it does for `run` above, including the prefix
and ambiguity rules and the default `--root`. Both are read-only and
stateless; neither has a `--observe`, `--bug-report`, `--verbose`, or
`--evidence` flag, and `rescue plan` runs `observe.observe_run` itself,
unconditionally, rather than behind a flag.

`run`, `scan`, and `demo` also read the user config file on every
invocation (v0.2, unit M3c; 4.10 has the full contract): its location is
`${XDG_CONFIG_HOME:-~/.config}/agent-postmortem/config.toml`, overridable
for tests by the `AGENT_POSTMORTEM_CONFIG` environment variable (a path).
No flag controls this; there is no `--config`.

**Exit codes in `--help` (field milestone F, P11).** `scan --help` already ends in an
`exit codes:` epilog; `run --help`, `rescue plan --help`, `rescue status --help`, and
`watch --help` now end in one too, each taken from the contract that owns the codes
(4.6/4.8 for `run`, 4.11 for the two rescue commands, 4.12 for `watch`). The text is
exactly this (`--help` prints it verbatim after the options):

```
run:
exit codes:
  exit 0  the status is complete and the verdict is CLEAN or DEGRADED
  exit 1  usage error
  exit 2  no such run, or not a run
  exit 3  the verdict is FAILED (3 wins when 4 also applies)
  exit 4  the status is INCOMPLETE and the verdict is not FAILED

rescue plan:
exit codes (the first condition that applies wins):
  exit 1  usage error, or a configuration error
  exit 2  no such run
  exit 4  the run may still be running, there is nothing to rescue yet, a finished
          run's risk population is not empty or not complete, the lifecycle
          terminal is unknown, or any UNKNOWN fact is in the packet
  exit 0  otherwise

rescue status:
exit codes (the first condition that applies wins):
  exit 1  usage error, or a configuration error
  exit 2  no such run
  exit 3  any row is NONZERO_OBSERVED
  exit 4  the lifecycle terminal is unknown, any row is NOT_OBSERVED or
          WRITE_AFTER_CHECK, the parent transcript could not be read, any
          NEVER-list match, or no unit at all in a run that is not finished with
          every attempt completed
  exit 0  otherwise

watch:
exit codes (the first condition that applies wins):
  exit 1  usage error
  exit 2  no journal appeared within 60 seconds
  exit 3  DONE with verdict FAILED
  exit 4  LIMIT (--max-hours, or a malformed journal), or DONE with the lifecycle
          terminal unknown and the verdict not FAILED
  exit 0  DONE otherwise (verdict CLEAN or DEGRADED, with or without the
          (INCOMPLETE) suffix)
```

`watch` (v0.2, unit M5; full contract in 4.12): `RUN_ID` resolves against
workflow runs only (a session carries no journal.jsonl for it to follow),
otherwise the same prefix and `--root` rules as `run`. `--interval` (default
5, range 0.2 to 300) is the poll period in seconds. `--max-hours` (default 6,
range 0.01 to 168) is how long `watch` keeps polling an unfinished run
before giving up. `--quiet-after` (default 300, range 10 to 86400) is the
silence, in seconds, an open attempt's transcript must show before the
first QUIET line. `--done-grace` (default 30, range 0 to 3600) is how long
every key must stay resolved, with no new `started`, before DONE is
printed. Every one of these four is validated as a finite float within its
own range (release finding F11); out of range, non-finite (`nan`, `inf`),
or unparsable is a plain usage error -- exit 1, a message naming the flag --
the same as any other bad argument, never a value that reaches `watch_run`
at all. `--json` prints one JSON object per line instead of text; there is
no `--no-redact` for `watch` -- its lines never carry anything but an
agent's own pseudonym and counts, so it always redacts. Read-only and
stateless, like `rescue plan`/`rescue status`.

### 4.9 Repo observation

`observe.py` (v0.2, unit M2b), standard library only, not called by the
reader, a detector, or a hook -- wired only into `run --observe`. Read-only:
it never writes an index, a stash, an object, or a config value, and it
starts no process a repository's own configuration could steer. Known limit
(review 2026-10-26, present since 4.9 was written): in two index modes git's
own `status` still writes, and this module does not detect them. With a
sparse index (`index.sparse`) that `status` must convert, git can store a new
tree object, and with a split index git refreshes the `sharedindex.*` file's
timestamps when it reads it. Both are git's own bookkeeping (a
content-addressed object, file times), not a change to tracked content or
refs; refusing these modes needs reading the index's extensions and is left
for a later change.

`observe_run(run, now=None) -> RepoObservation`. `now` defaults to
`datetime.now(timezone.utc)`; tests pass it explicitly. `RepoObservation`:
`checked: bool`, `reason: str | None`, `repo_root: str | None`,
`head: str | None`, `git_version: str | None`, `paths: dict[str,
PathObservation]`. `PathObservation`: `state` one of `DIRTY_NOW`,
`CLEAN_NOW`, `MISSING_NOW`, `UNKNOWN`; `reason: str | None` (set only for a
path refused as outside the work tree); `later_write_observed: bool`;
`attempts: list[dict]`, each `{"agent_id", "ordinal"}` for an attempt that
wrote or left the path in flight.

Rules:

1. Staleness. `run.ended_at` not None and `now - ended_at` over 72 hours:
   `checked=False, reason="historical: run older than 72 hours"`, no
   process started at all.
2. Candidates. The union of `facts.paths_written` and `facts.in_flight[].path`
   over every attempt whose outcome is `killed`, `failed`, or `superseded`.
   None found: `checked=False, reason="no paths written by a killed or
   failed attempt"`.
3. Repo directory. The most common cwd among those attempts' own
   `facts.cwds`, ties broken by first-seen order. It must be an existing
   directory, else `reason="cwd not found"`.
4. The only argument lists this module ever runs: `git --version`;
   `git -C <dir> rev-parse --show-toplevel`; `git -C <dir> rev-parse HEAD`;
   `git -C <dir> config --show-scope --get-regexp ^filter\..*\.(clean|process)$`;
   `git -C <dir> --no-optional-locks -c core.fsmonitor=false
   [-c filter.<name>.clean= -c filter.<name>.process=
   -c filter.<name>.required=false]... status --porcelain=v2 -z
   --no-renames --ignore-submodules=all -- <paths...>`, with one bracketed
   group per driver the guard of rule 5 switches off, sorted by name.
   `git` itself is resolved to one absolute path with `shutil.which("git")`
   at the very start of `observe_run`, before any of the five calls; that
   same absolute path, never the bare string `"git"`, is argv[0] on every
   one of them, so all five run the identical binary rather than each
   doing its own separate PATH search. Each runs via `subprocess.run` with
   a list (never a shell), `timeout=10`, `stdin=DEVNULL`, and an
   environment built from scratch: `PATH` and `HOME` copied from the
   current process, plus `GIT_OPTIONAL_LOCKS=0`, `GIT_PAGER=cat`,
   `PAGER=cat`, `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, `GIT_ALLOW_PROTOCOL=`
   (empty: no transport protocol is allowed, whatever the repository's own
   `protocol.*.allow` says), and `GIT_NO_LAZY_FETCH=1`, nothing else. The
   last two exist because `status` in a partial clone fetches a missing
   object on demand, through the promisor remote the repository configured,
   and an `ext::<command>` URL runs that command. `GIT_ALLOW_PROTOCOL=`
   refuses the transport in every supported version; `GIT_NO_LAZY_FETCH=1`
   also refuses the fetch on git versions that know it (2.36 does not).
   `status` then fails and the check is reported unchecked (found in review
   2026-10-26, present since 4.9 was written). A
   timeout or non-zero exit (except `config --show-scope --get-regexp`
   exiting 1, which means no filter driver is configured) gives
   `checked=False` with a reason naming the step; no exception ever escapes
   `observe_run`.
5. Guards, in order, first failure wins: git not on `PATH` (`shutil.which`);
   git older than 2.36 or its version unparsable; `rev-parse
   --show-toplevel` failing, meaning the cwd is not inside a git
   repository. A fourth guard, a clean or process filter driver configured
   at a scope the repository can steer, is checked only once there
   is at least one candidate path contained in the work tree (rule 6) --
   i.e. only when `status` is actually about to run -- and as the call
   immediately before `status`, with no other subprocess call in between:
   this is what keeps the window between the guard and the command it
   guards as small as this module can make it. `status` is never run when
   that guard finds such a driver, and `status` never runs any driver at
   all. The guard reads each driver's scope (`--show-scope`, git 2.26 and
   later, inside the 2.36 floor): every output line must be
   `<scope>\t<key>` or `<scope>\t<key> <value>` (git drops the space when the
   key has no value) with a key `filter.<name>.clean` or
   `filter.<name>.process` whose name is letters, digits, `_`, `.`, and
   `-`, starting with a letter or digit. A driver whose scope is `system`
   or `global` is the person's own installation (`git lfs install` writes
   Git LFS's drivers to the global config); it does not stop `status`, but
   it is switched off for that one call with the `-c` group rule 4 shows
   (an empty clean and process command, `required=false`), because a
   repository can still steer a driver it did not configure: its
   `.gitattributes` choose the driver, the driver runs in the work tree
   (a `python3 -m <module>` command imports a module the work tree holds),
   and Git LFS reads `lfs.extension.<name>.clean` commands from the
   repository's `.git/config` and stores objects under `.git/lfs` when it
   cleans. With the driver off, git compares a routed file without the
   external filter (built-in conversions such as `ident` and line endings
   still apply): for Git LFS, a file whose real content is checked out
   where the index holds a pointer reads dirty once its timestamp changes,
   even when its content did not, a conservative answer. The output is
   split on `\n` alone (git ends each record with it; a value may hold other
   Unicode line breaks). Any other scope (`local`, `worktree`, a file either
   of those includes, `command`, `unknown`), any line that does not parse
   (an empty or ordinary continuation line of a multi-line value included),
   and any name outside that character set refuses; a continuation line that
   happens to read like a `system` or `global` driver line only adds a
   switch-off group for a driver name that may not exist, which changes
   nothing. Exit 0 with
   no output line, any exit other than 0 or 1, and a call that did not
   complete are a failed check and refuse too. Before this change any
   configured driver refused, so every check that reached `status` on a
   machine with Git LFS configured was refused, CI's runners included.
   Known limit, documented
   rather than fixed: nothing in the standard library stops a process with
   write access to this repository from editing `.git/config` to add a
   filter driver in the interval between this guard's own `config
   --show-scope --get-regexp` call returning and `status` actually starting -- closing
   that gap completely would need an isolation primitive (a sandboxed or
   otherwise non-repo-steerable git invocation) stronger than `subprocess`
   plus a scrubbed environment can provide. Treated as out of scope for a
   human-run, read-only diagnostic: the attacker already needs local write
   access to the observed repository's `.git` directory at the moment
   `postmortem run --observe` happens to be running.
6. Containment. Each candidate path is made absolute against the cwd when
   relative, then `os.path.normpath`'d; it must start with
   `<toplevel> + os.sep`, else it is `UNKNOWN` with
   `reason="outside the work tree"` and is never passed to git. A
   contained path is passed to `status` relative to the toplevel, after
   `--`.
7. State, from `status --porcelain=v2 -z` output: a path with an entry of
   type `1`, `2`, `u`, or `?` is `DIRTY_NOW`; no entry and the file exists
   on disk is `CLEAN_NOW`; no entry and missing is `MISSING_NOW`. The `-z`
   output is NUL-separated; a type `2` (rename/copy) record carries an
   extra chunk, the original path, which is skipped rather than
   misread as a second path (in practice `--no-renames` means git never
   emits one, but the parser handles it correctly regardless).
8. `later_write_observed`: true when an attempt of the same key with a
   higher ordinal than one of the path's writers also wrote or left that
   path in flight.

`run --observe` (off by default) adds a `REPO STATE` block after the AGENT
table: checked/reason, then per path a `path-N` pseudonym by default (the
real repo-relative path under `--no-redact`, in the same "path" namespace
report_path claims use, 5.1), its state, `later write: yes/no`, and the
ordinals of every attempt that wrote it. `--json` adds an `observation` key
(5.3) built the same way. Without `--observe`, `run`'s text and JSON output
are byte-for-byte what they were before this unit.

**Per-repository observation (N5c-1; plan S5, S6).** `run --observe`,
`rescue plan`, and `rescue status` no longer call `observe_run`, which is kept
only as the single-repository function its own tests exercise.
`observe.observe_groups(run, now=None) -> RunObservation` partitions the risk
population's ledger paths with `ledger.group_by_repo` (4.13.1) before any
process starts, and then:

- a **refused** group (a contributing ledger is partial) and the **unknown**
  group start no process at all: `checked: false`, `reason: "refused"`, their
  ledger paths listed in `unobserved_paths`, no `PathObservation` invented. The
  unknown group is always exported refused, since no repository can be named for
  it, and it is left out of the list when it holds neither an attempt nor a path;
- a group that is not refused runs 4.9's guarded sequence with only its own
  ledger paths: the staleness rule (rule 1, `reason: "historical"`, no process),
  the candidate directory (`git -C` is given the group's candidate `repo`; a
  directory that does not exist is `"not a git repository"` with no process),
  then absolute `git` resolved once, `--version` once for the whole run, and per
  group `rev-parse --show-toplevel`, `rev-parse HEAD`, the filter-driver check
  immediately before `status`, and `status` with `--no-optional-locks`, `-c
  core.fsmonitor=false`, `--no-renames`, `--ignore-submodules=all`, over only the
  paths inside the resolved toplevel (rule 6's containment, relative to the
  toplevel). A path outside the toplevel is listed in `unobserved_paths`; when
  every path is outside, no `status` runs and the reason is `"outside the work
  tree"`;
- the closed reason list is `refused`, `historical`, `not a git repository`
  (candidate directory missing or `rev-parse --show-toplevel` failed), `git
  unavailable` (no git on `PATH`, older than 2.36, or an unreadable version),
  `guard refused` (`rev-parse HEAD`, the filter-driver check, or `status` failed
  or refused), and `outside the work tree`. No exception escapes.

`RunObservation.groups` is the ordered list of `GroupObservation`: named
repositories in sorted candidate order, then the unknown group. A group holds
`repo` (the candidate, `None` for unknown), `refused`, `checked` (true only when
`status` actually ran), `reason`, `repo_root`, `head`, `git_version` (internal,
never exported), `paths` (`{absolute ledger path: PathObservation}`, only for a
checked group), `unobserved_paths` (sorted), and an internal list of attempt
references. A `PathObservation` is as before; its `attempts` are the population
attempts of the group that recorded the path (`observed`, `claimed_target`, or
`attempted`), and `later_write_observed` is true when a later attempt of the same
key also recorded it. `RunObservation.repo_root` is the first checked group's
toplevel (what the disclosure context uses at `local`, 4.13.5) and
`RunObservation.checked` is whether any group was checked.

`run --observe` text adds the `REPO STATE` block after the AGENT table: per group
`  repository <repo>  checked: yes` or `  repository <repo>  checked: no
(<reason>)` (`repository unknown` for the unknown group), then per observed path
`    <path>  <STATE>  later write: yes|no  attempts: <ordinals>` and per
unobserved path `    <path>  not observed`. Paths and repositories use the
level's path namespace (5.1, 4.13.5). `--json` adds `observation: {groups: [...]}`
(5.3b).

(Field milestone F, P7, P8.) A refused named repository (the reason code `refused`,
unchanged in JSON) reads `  repository <repo>  checked: no (its ledger is partial:
incomplete evidence may hide more writes, so the tool does not run status for it)`
and is followed by its manual command, indented four spaces, in the spelling WORK AT
RISK's manual line uses (4.7: `    run yourself: git -C <repo> status --short`, `    run
yourself: git status --short in <alias>; --raw prints the exact command` at share).
The unknown group keeps `checked: no (refused)`. With no group the block reads
`REPO STATE  (no repository holds a path at risk)` on one line.

### 4.10 User configuration

`config.py` (v0.2, unit M3c), standard library only (`tomllib`, Python
3.11+). One file:
`${XDG_CONFIG_HOME:-~/.config}/agent-postmortem/config.toml`, overridable
for tests by the `AGENT_POSTMORTEM_CONFIG` environment variable (a path;
nothing outside the test suite ever sets it). Section `[detectors]`, key
`off`, a list of detector ids:

```toml
[detectors]
off = ["bash_backgrounded"]
```

`load_config() -> Config` reads and parses it once per process, cached by
the resolved path (the same process-level-cache pattern `reader.ParentIndex`
uses for a parent transcript) so a repeated call inside one invocation never
re-reads the file, while a different resolved path (a different test's
`AGENT_POSTMORTEM_CONFIG`) always gets its own fresh parse. `Config` is a
frozen dataclass: `detectors_off: frozenset[str]` (the ids actually
silenced), `warnings: list[str]` (plain sentences for the caller to print,
never containing the config's own file contents beyond an id or a short
parser message), and `critical_rejected: tuple[str, ...]` (any id in `off`
that names a critical-severity detector, kept separate from `warnings` so a
caller can tell "silence this and warn" from "refuse to run").

Never raises. Every failure mode degrades to "nothing silenced" plus a
diagnostic, never an exception and never a changed exit code on its own:

- Missing file: `detectors_off` empty, `warnings` empty, `critical_rejected`
  empty.
- Malformed TOML (or a file that is not valid UTF-8): one warning naming the
  parse error; nothing silenced.
- `[detectors]` present but not a table, or `off` present but not a list of
  strings: one warning; nothing silenced.
- A non-string or empty entry in `off`: one warning per such entry; that
  entry is dropped, every other valid entry in the same list is still
  honored.
- An id not in `detectors.all_detectors()`: one warning naming it; dropped,
  never silenced. This is deliberately only a warning (a detector v0.2 has
  not shipped yet, or a typo, must never stop the tool from running).
- An id whose `severity_default` is `"critical"` (none today since N5b
  (4.13.4): `codex_write_denied` and `missing_artifact` were the two before
  it; determined by asking the detector registry, not a hardcoded list):
  never added to `detectors_off`, recorded in
  `critical_rejected` instead of `warnings`. A silenced critical would
  change the exit code (4.6), which is the one thing this file must never
  be able to do quietly.

`config.partition_silenced(findings, config) -> (shown, silenced)` is the
one split every renderer and every hook uses (4.6/4.7/7.2): findings whose
detector id is in `detectors_off` go to `silenced`, everything else to
`shown`, both in the findings' original order. `run_detectors` itself (4.3)
never sees a `Config` and is completely unchanged: a silenced detector still
runs, every time, over every run; only what happens to its findings
afterward differs.

The CLI and the hooks disagree on what a critical rejection means, on
purpose: `run`, `scan`, and `demo` each load the config once at the top of
the command, print every string in `warnings` to stderr (through the active
Redactor), and, when `critical_rejected` is non-empty, print one more
message naming the id(s) and exit 1 without doing anything else -- a config
file that tried to silence a critical detector is treated as a usage error,
not a partial success. A hook (7.2) never does this: it just never sees
those ids in `detectors_off` (the parser already excluded them), so it
silently proceeds as if `off` had never named them at all, since a hook must
never fail a turn regardless of what a config file says.

`apply`, `trend`, and bare `rules` (and `rules --show`/`rules --run`/`rules
--all`) never call `load_config()` at all: none of them has anything to do
with silencing a detector's findings, and `trend` must keep counting a
silenced detector exactly as before (4.6/5.3) -- there is nothing for it to
filter. `rules review` (section 9, unit M6) is the one exception: it calls
`load_config()` to read `[[adopted]]` below, never to silence anything.

Section `[[repos]]` (v0.2, unit M4, 4.11): one or more tables, each
`path = "..."` and `checks = ["...", ...]`. `Config.repos` is a tuple of
`RepoConfig(path, checks)`, `path` normalized and `~`-expanded once at load
time so `config.checks_for_repo(cfg, repo_root)` (`repo_root` itself already
normalized and `~`-expanded, since it comes straight from `git rev-parse
--show-toplevel`) is a plain string comparison, never a filesystem call of
its own. A declared check that contains `&&`, `||`, `;`, `|`, a newline, or
starts with `cd ` is rejected at load with a warning naming it and is never
added to that repo's `checks`; every other check in the same table is still
kept. A malformed `[[repos]]` table, a missing or non-string `path`, or a
non-list `checks` each add one warning and are dropped, the same
never-raise contract every other part of this file follows. `apply` and
`trend` have no use for `repos` either.

Section `[[adopted]]` (v0.2, unit M6, section 9): one or more tables, each
`rule = "R-..."`, `date = "YYYY-MM-DD"`, `target = "..."` (free text: where
the owner put the rule, never checked against the filesystem). `Config.
adopted` is a tuple of `AdoptedRule(rule, date, target)`. A malformed table,
a `rule` naming an id `rules.RULES` does not have, or a `date` that does not
parse as a calendar date, each add one warning and are dropped; every other
well-formed `[[adopted]]` table is still kept. `rules review` and `rules propose`
read `Config.adopted` (`rules propose` reads only this config source, never the state store's
legacy `applied` entries); `rules review` also combines it with those legacy `applied` entries
(7.5) so a v0.1 user's history still reviews.

### 4.11 Guided rescue

`rescue.py` (v0.2, unit M4), standard library only. Two commands, both
read-only and stateless, neither writing a file, running a check, or
touching git beyond what `observe.py` (4.9) already does:

```
agent-postmortem rescue plan   <RUN_ID|PATH> [--root PATH] [--json] [--evidence] [--local | --raw | --no-redact]
agent-postmortem rescue status <RUN_ID|PATH> [--root PATH] [--json] [--evidence] [--local | --raw | --no-redact]
```

`RUN_ID` resolves exactly as it does for `run` (4.8).

**Who does the rescue.** The tool (deterministic, no model) reads
transcripts and observes repo state; it prints a decision packet and, later,
reads back what a transcript shows was actually run. The main Claude
session follows the printed protocol: it reads the diffs, runs the declared
checks with its own Bash tool, repairs within a 2-cycle budget, and proposes
a commit or a relaunch. The human approves every commit, every relaunch,
and the closing of a rescue. Honest limit: the tool cannot stop a session
from ignoring the protocol; the two guarantees that do not depend on
cooperation are that the tool itself never writes (R1, R3), and that every
action the session takes still goes through Claude Code's own permission
settings.

**`rescue plan`: the decision packet.** Always runs `observe.observe_groups(run)`
first (N5c-1, 4.9), read-only, the same call `run --observe` makes: a refused or
unknown group starts no git process, so a path in one reads `UNKNOWN (refused)`
in the PATHS table and `observation: null` in `--json`, and such a path makes the
packet's UNKNOWN fact true (exit 4). The UNKNOWN fact is also true (N5 fix 1, S1) for
any refused group and for the unknown group (each exists only when it holds an attempt
or a path), and for any group with a non-empty `unobserved_paths`, whether or not a unit
row names the path: a shell write, a MultiEdit, or an attempt with no recorded path can
live in such a group, and the plan never says "no path is dirty now" then. `rescue status` makes the same call, but
counts checks per declared repository, never from an observation (below). A unit of work is one
`LogicalAgent` (4.1) whose attempts include at least one `killed`, `failed`,
or `superseded` one -- `rescue.qualifying_units(run)`. Per unit, in order:

- `UNIT <agent> (<phase>) N attempts, last one <end>`: `<agent>`/`<phase>`
  are the same `agent-N`/`phase-N` pseudonyms (5.1) `run`'s own AGENT table
  uses; `<end>` is the last attempt's outcome, `killed` refined to `stopped
  by parent`/`interrupted` the same way the AGENT table's END column already
  reads `Interrupt.cause` (4.7).
- `TASK`: a pointer, `full text: <transcript file name>:<line>`, to the
  first user message of the unit's own first (ordinal 1) attempt, then
  `prompt length: N chars`, the true length of that same text. (N3b: the
  prompt is an excerpt, and an excerpt prints only with `--evidence`
  (4.13.5, D10, D-o): without the flag the pointer and the length print at
  every level; with `rescue plan --evidence` at `local` and `raw` the line is
  `TASK      "<first 300 characters>"` followed by the `full text:` pointer,
  as v0.2 printed it under `--no-redact` (at `local` through structural
  redaction and the P19 path rule); at `share` the flag adds only the one note
  line `run --evidence` prints and the output is otherwise unchanged. `--json`
  ignores the flag.) At `share` the
  file name is the aliased one (`agent-<alias>.jsonl`, 4.13.11); at `local`
  and `raw` it is the real file name; for an attempt without a canonical
  agent id the pointer is omitted at `share` and only the length prints.
- `REPORTED`: the last attempt's own `Attempt.reported` (section 3): for a
  dict result, every allowlisted claim as `key: value` (a `report_path`
  claim gets its own `path-N` pseudonym, 5.1; a `status` claim -- up to 32
  chars of an agent's own free text, section 3 -- prints as the fixed word
  `set` at `share`, F7 hardening, its real value at `local` and `raw`,
  `--json` likewise at `share`: `"status": "set"`); for a text result, `text result, N
  chars, not kept`; `no result event` otherwise.
- `PATHS`: a table, one row per (target, group) the unit's own
  killed/failed/superseded attempts wrote or left in flight (N5 fix 1, C3 and S1:
  the union of the unit's facts paths, `paths_written` and `in_flight`, and every
  ledger entry path of those attempts, 4.13.1, so a MultiEdit or a path named in a
  shell command has a row; the target is the normalized absolute path the ledger
  gives, never the spelling a call used, so one relative path written from two
  repositories is two rows, and one path spelled absolute and relative from the same
  directory is one row; a group that lists the target gets its own row with only that
  group's ledger entries and attempts; a target no group lists gets one row with group
  null and state `UNKNOWN`): the path (`path-N` at `share`; at `local` the P19
  form, repo-relative when git placed it inside the repository the plan's own
  observation resolved, `~/.../<basename>` inside home otherwise, `path-N`
  elsewhere, never absolute; real repo-relative at `raw`), its observed state, the ordinals of
  the writing attempts, and whether a later attempt of the same key also
  wrote it (`observe._later_write_observed`, unchanged). A path observation
  was not checked at all (`observe_run` never started a process): every row
  reads `UNKNOWN` with the run-level reason attached.
- `COMMITS`: the commit ids `git commit` calls on the unit's own
  killed/failed/superseded attempts reported (4.2), or `0`.
- `IN FLIGHT at the kill`: one line per `no_result` tool call on a
  killed/failed/superseded attempt (`AttemptFacts.in_flight`), `<tool> <path>
  (no result recorded)`; omitted entirely when there is none.
- `UNKNOWN`: always printed, the fixed sentence "whose content is in the
  dirty paths; whether the task's goal was reached; what remains of the
  task"; when the observation was not checked, a second line names the
  reason. A unit with at least one killed/failed/superseded attempt whose
  own `facts` is `None` (no transcript at all, F6 hardening) gets a further
  line, "UNKNOWN facts: transcript missing" -- distinct from an unchecked
  observation, which is about the repo, not the transcript. Such a unit
  also forces the packet's own `has_unknown_fact`/exit 4 (below).
- `CHECKS` (N5c-2): the declared checks of the unit's declared repositories
  (the `[[repos]]` roots that own the unit's paths, lexically, as `rescue
  status` attributes them, below; never taken from an observation, so a refused
  group still finds them), in repository order, each check its own `check-N` pseudonym at
  `share` (5.1, F7 hardening) -- the whole command hidden, not only a path
  embedded inside it -- the real command at `local` (any absolute path inside
  it printed by the P19 rule) and at `raw`; otherwise "no checks
  declared for `<repo>`: add `[[repos]]` to the config, see docs" (`<repo>`
  is the real toplevel path only at `raw`, else the fixed word
  "this repository" -- a repo root has no pseudonym of its own, 5.3, so it
  is never printed at `share` or `local`).
- `STEPS`: two read-only `[look]` items (`git status --short`/`git diff`
  over the `DIRTY_NOW` paths, or every path written when the observation
  was not checked) always printed; four `[decide]` items (run the declared
  check, keep/fix/set aside, commit, relaunch) printed only when the run is
  not possibly live (below). A unit with no path to name at all, while the repository
  state was not checked (N8b, K4.1), gets both look items as whole-tree commands with no
  pathspec, `git status --short` and `git diff`, never `-- <none>`; a possibly-live run
  prints exactly those two, any other run follows them with the four decide items (3 to 6).
  In `--json` the two `look` steps' `command` is a non-null string in every case, the
  whole-tree commands when no path is named. A unit with no path to inspect, when the
  observation was checked, reads `no path is dirty now`
  only when none of its attempts sits in a refused, unknown, or unobserved group (N5 fix
  1, S1); otherwise the first step reads `no path was observed for this unit: a shell
  command or an unobserved repository may hide what it wrote; run git status --short in
  the repository the agent worked in`, and, while some group was checked, its UNKNOWN
  block (and the JSON `unknown` list) gains the line `(repo state not seen for some of
  this unit's attempts: a shell command or an unobserved path may hide what they wrote)`.
- `NEVER`: the fixed line naming `git checkout`/`restore`/`reset`/`stash`/
  `clean` as never to be run on these paths.

After every unit, `BRIEF FOR A RELAUNCH`: which attempts were killed (agent,
attempt, kill time, silence) and which failed (`attempt_failed`'s own cause,
`retry_now`, `reset_time`, 4.2/4.3), then the paths the whole run's attempts
wrote (every attempt's confirmed writes, not only the
killed/failed/superseded ones: its ledger paths of effect `observed`, which a MultiEdit and a
shell-free file tool give, plus `facts.paths_written`; a `claimed_target` or `attempted` path
never enters it, since neither effect proves a write, N8b, K4.4) and the commit ids they
reported, printed
`already exists: <n> paths (...); commits reported: <m> (...)` (N5c-2: a commit
id is an unverified, summary-shaped output line, 4.13.1, so no rendering says a
commit was created or exists; `COMMITS` and "commits reported" are the wording;
the JSON key stays `already_exists.commits`),
then the fixed sentence "start from what exists; do not redo it", and, only for a workflow
run in which two attempts of different keys overlapped in time (both have known `first_ts`
and `last_ts` and the closed intervals intersect; unknown timestamps never trigger it, N8b,
K4.2), the resume/fan-out caveat (`anthropics/claude-code#95076`).

LIVE rule: when the run is possibly live (`report.is_possibly_live`, the
same unfinished-plus-fresh-mtime signal `scan`'s triage already uses, 4.7),
the first line of the packet itself depends on who may still be writing
**(field milestone F, FB3; this is `rescue plan` text only: `rescue status` and
every JSON output, exit codes included, are unchanged)**. Exactly one of four
branches, chosen in this order:

- *Live, no unit* (nothing has been killed or failed so far): `NOTHING TO
  RESCUE YET: the run may still be running; no attempt has been killed or
  failed so far.` There is no do-not-modify sentence, because there is no file
  to protect yet, and never the cheerful `NOTHING TO RESCUE: every attempt
  completed.`, whose claim would be false for a run that may still be running.
  Exit code 4, as before.
- *Live, units, a SESSION run whose transcript stem equals
  `CLAUDE_CODE_SESSION_ID`* (a canonical UUID, compared in lowercase: the
  caller's own session): `YOUR CURRENT SESSION: this run is the session you are
  working in. Check the files listed below yourself before relaunching the
  killed work.`
- *Live, units, any other run*: `POSSIBLY LIVE: the run may still be running, and
  another agent may be writing the files listed below. Do not modify them until it
  has stopped; confirm with the human.`
- *A possibly-live WORKFLOW run*, including one launched by the caller's own
  session, always takes the previous branch: its workers can still be writing even
  though its parent session id matches, so the warning and its no-modification
  rule stay. The own-session branch is for session runs only.

**(N3b, N3c)** At `local` and `raw` the disclosure line (4.13.5) comes before
that line, and at `share` with `--evidence` the excerpt note does; nothing else
precedes it. Every unit still prints its `TASK` through `NEVER` block (the run
may have already killed/failed something even while other agents keep going),
but `STEPS` shows only its two `[look]` items, and `BRIEF FOR A RELAUNCH` is not
printed at all, in the own-session branch as well. The no-unit branch prints its one
sentence and no unit or relaunch-brief line (the disclosure or excerpt-note
prefix above still comes first where it applies). The variable is a hint, not a
security boundary: it only chooses which of the two wordings a session run gets.

No unit at all and the run is not possibly live (F6 hardening): the
cheerful `NOTHING TO RESCUE: every attempt completed.`, nothing else, exit
0, only when `run.finished` is true AND every attempt's own `outcome` is
`completed`. Otherwise (an unfinished run, or one whose only attempt is
`unknown` -- no transcript at all, so not proven completed either) that
claim would be false: `NOTHING TO RESCUE YET: the run has not finished and
no attempt was killed or failed.`, exit 4. `rescue status` with no unit uses the same
predicate (`rescue.nothing_to_rescue_ok`, N8b, K4.3, which closes N5's m5): its sentence is
the one above, and an empty-unit run exits 0 only when `run.finished` is true and every
attempt completed; every other empty-unit run (a fresh or stale unfinished run, an
unknown terminal, a possibly-live run) exits 4, text and JSON alike.

(N8 fix 1, S2.) The cheerful sentence also requires an empty 4.13.1 risk population whose
completeness is `complete` (`ledger.risk_population` and `risk_population_completeness`). A
finished run whose every attempt completed but whose risk population is not empty or not
complete (a failed parent terminal with a completed writer, or no parent transcript so its
attempts cannot be placed) prints `NO KILLED OR FAILED ATTEMPT: <n> attempt(s) may still hold
unchecked work; see WORK AT RISK in agent-postmortem run <run id>.` instead, `<n>` the size of
the risk population, in both `rescue plan` and `rescue status`, exit 4; the plan's JSON has
`nothing_to_rescue` false, `units` empty, `brief` null, and `exit_code` 4 (status JSON: `exit_code`
4). When the population is empty but its completeness is not `complete`, no count is exact, so
the sentence is `NO KILLED OR FAILED ATTEMPT: the tool could not establish which attempts were
at risk; see WORK AT RISK in agent-postmortem run <run id>.` (never `0 attempt(s)`), with the
same exit 4, JSON, and no brief. A truly clean run with a parent transcript still exits 0.

(R1, v0.3.1.) A session with no rescue unit in which some subagent has a `return_state` other
than `returned` (4.13.1) prints, in `rescue plan` and `rescue status` alike and before any other
no-unit sentence, `NO RESCUE UNIT: <k> subagent(s) have no established return (<breakdown>); run
agent-postmortem run <run id> to see WORK AT RISK` (`1 subagent has`), the breakdown being the
one of 4.7's WORK AT RISK over every such subagent, and exits 4; it never prints `NOTHING TO
RESCUE` or `NOTHING TO RESCUE YET`. When real units exist the text keeps its units and prints no
such line. Both JSON shapes carry two top-level keys at every disclosure level: `empty_state`
(null whenever `units` is not empty; otherwise `nothing_to_rescue`, `nothing_yet`,
`no_rescue_unit_unestablished_returns`, or `unchecked_risk`, the last being the branch of the
paragraph above) and `return_breakdown` (null unless `empty_state` is
`no_rescue_unit_unestablished_returns`; else an object of five integers `reported_failed`,
`reported_killed`, `possibly_running`, `no_return_evidence`, `not_established`). A workflow
rescue carries both keys, `empty_state` set by the same rule over its existing branches.

Exit codes, ordered (N5c-2; the first condition that applies wins): 1 a
usage error, or a configuration error (4.10); 2 no such run; 4 a possibly-live
run, a "NOTHING TO RESCUE YET" run, a `NO RESCUE UNIT` session (R1), a finished all-completed run whose 4.13.1 risk
population is not empty or not complete (the `NO KILLED OR FAILED ATTEMPT` sentence), a run
whose lifecycle terminal is `unknown` (4.13.3), or any `UNKNOWN` fact anywhere in the packet (an unchecked
observation, a refused or unknown group's path, a path state of `UNKNOWN`, an
`AttemptFacts` field the reader could not establish, or a
killed/failed/superseded attempt whose `facts` is `None`, 4.1); else 0. The
conditions inside 4 overlap freely and every overlapping pair yields 4; the
process status and the JSON `exit_code` are the same number.

`--json`: `{schema: 2, run: {run_id, kind, finished, possibly_live},
nothing_to_rescue, units: [{agent, phase, attempts, end, task: {pointer,
length, text} | null, reported, paths: [{path, state, reason, attempts,
later_write_observed}], commits, in_flight: [{tool, path}], unknown:
[str, ...], checks: [str, ...], steps: [{grade, text, command}]}], brief:
{killed: [...], failed: [...], already_exists: {paths, commits}} | null,
exit_code}` (`task.text` is `null` unless `--no-redact`; `brief` is `null`
for a possibly-live run, a nothing-to-rescue run, or a run with an unchecked risk
population), through the same export
allowlist and redaction rules as the text report.

**`rescue status`: what was observed.** Reads the run's parent session
transcript with the existing reader (a workflow run: the session `.jsonl`
the run directory sits under, `reader._parent_transcript_path`; a session
run: itself), via `reader._parse_main_attempt` so its main attempt carries
`tool_calls` (4.2's v0.2 reader changes). Considers only tool calls whose
own timestamp is after the run's `ended_at` (every one, when `ended_at` is
unknown).

**Declared repositories (N5c-2; plan S7).** The roots are the `[[repos]]`
paths of the configuration (4.10), already normalized lexically, never
resolved through git and never taken from an observation, so a refused group
(4.9) leaves a declared check counting. Two `[[repos]]` tables with the same
normalized path are a configuration error: `rescue status` prints one line
naming the duplicated root (through the level's path rule) to stderr and exits
1 before it looks for the run. A ledger path, a candidate repository, or a
write's working directory belongs to the longest declared root that contains it
component-wise (`os.path.commonpath`, so `/work/repo2` is not inside
`/work/repo`, and `/work/repo/vendor/x/f` belongs to a declared
`/work/repo/vendor/x`, not to `/work/repo`). A unit's declared repositories are
the owners of its candidate paths (the owner of the path's group candidate when
the path itself has none), in order of first appearance; a unit with none
reports its checks as not declared (`checks declared: 0`, word `NOT_OBSERVED`,
as before). Everything below is computed per (unit, declared repository), never
globally: one row per pair, and the row names its repository.

A Bash call counts as a run of a declared check only when all hold: its
`command` equals one of that repository's declared strings exactly, trailing
whitespace stripped; its own `cwd`, normalized, equals that declared repository's
path (a call made in a subdirectory, or in another declared repository, does not
count, and is listed under "not counted" as `other cwd`); it is paired with
a result by `tool_use_id`; its completion is `completed_ok` or
`completed_error` (never `ambiguous`, F5 hardening: a duplicated
`tool_use_id`, 4.2, can never be trusted as a genuine check run). Every
other Bash call whose command merely contains a declared check string as a
substring is listed under "not counted" with the reason: `ambiguous tool
id` (checked first: nothing else about the call can be trusted once its own
id was duplicated), `no result` (no `tool_result` at all), `backgrounded`,
`other cwd`, or `compound command` (an exact match otherwise, so this is
what a `cd x && <check>` or `<check> || true` gets). A call whose command
names no declared check at all is never mentioned here -- but see
`WRITE_AFTER_CHECK` below, which is not scoped to declared-check text at
all.

Per unit and declared repository (`rescue.qualifying_units(run)`, the same
population `rescue plan` uses): `checks declared: N` (that repository's); the last counted run's own command -- its own
`check-N` pseudonym by default (5.1, F7 hardening), raw under `--no-redact`
-- its exit state (`completed_ok` reads exit 0, `completed_error` reads
non-zero), and its time, mapped to one word -- `EXIT_0_OBSERVED`,
`NONZERO_OBSERVED`, or `NOT_OBSERVED` when the repository declares a check and
no acceptable call of it was counted (it is never a stand-in for a refused
observation group). `WRITE_AFTER_CHECK` (F3 hardening, N5c-2) is tagged whenever,
strictly after that repository's last counted run: a `completed_ok`
Edit/Write/MultiEdit/NotebookEdit call wrote one of the unit's own candidate paths that
this repository owns (N5 fix 1, C2: MultiEdit reads its target from `multiedit_path`
and follows the same owner function and the same completion rule as Write), OR a write-capable Bash or Monitor call belongs to this
repository. A shell call is write-capable unless its command is proven read-only
for this purpose (`rescue._proven_read_only`, deliberately narrower and different
from `reader._is_read_only_bash_command`'s own self-reference rule, 4.3: `git
status` is never a write, and neither is a `sed` invocation without an in-place
flag, both unlike the reader's own heuristic); a command the recorder did not
keep (longer than the recorded limit, so `command` is absent) is write-capable.
It belongs to the one declared repository that owns its working directory,
taken after a literal absolute leading `cd DIR &&` (4.13.1, L9), subdirectories
included. A call whose working directory is missing, relative after the `cd`,
unknown, or owned by no declared root counts as a possible write for every
declared repository of the run (fail closed, never dropped). Such a call is
listed under "not counted" too, reason "a Bash command that may write ran after
the check", unless it is already listed there with one of the check reasons above.
Only a call that was counted as a declared check for some repository is exempt from
this scan (N5 fix 1, C1): a call whose command merely contains a declared check but
was rejected (`other cwd`, `compound command`, `backgrounded`, `no result`, `ambiguous
tool id`) is write-capable like any other call that is not proven read-only, owned by
its working directory as above, and counts in writes-after and in check cycles. `check cycles observed: K of 2` (a cycle is a counted run preceded,
since the previous counted run of the same repository, by at least one write
that repository owns -- the same ownership as above -- `K` is the
true observed count, not capped at the 2-cycle protocol budget, so a
session that kept going past it is visible, not hidden); every "not
counted" line (no excerpt in text, only a line number and reason -- see
`--json` for the excerpt); every NEVER-list match (`git checkout`/
`restore`/`reset`/`stash`/`clean`) found anywhere in the transcript after
`ended_at`, shown as the matched rule's own spelling (`git checkout`, taken
from code, never a transcript word: the first word can be an environment
assignment or an absolute-path binary) in the text at every level unless
`--evidence` is given at `local` or `raw` (N3b, D10, D-o, C1). With `--evidence` at `local` and `raw` the line shows the command's
first 60 characters exactly as v0.2 printed them under `--no-redact`, with
listed paths spelled the way the PATHS table spells them at that level (the
P19 form at `local`, so never absolute); with `--evidence` at `share` the text
gains the one note line `run --evidence` prints at `share` (excerpts need
`--local` or `--raw`) and nothing else changes. `--json` is unaffected by
`--evidence`, and `--json --raw` still prints the 60-char excerpt. Any NEVER-list match anywhere makes the exit
4 unless a `NONZERO_OBSERVED` makes it 3 (below), and appends `NEVER_COMMAND_
OBSERVED` to the printed/exported word (both text and `--json`), alongside
the plain word, never replacing it. The fixed sentence "Writes made outside
this session after the check cannot be seen; freshness beyond the
transcript is UNKNOWN." is always printed last. R8: the word "verified" is
never printed by this command, and never appears in `rescue.py`'s own
source; a test greps both, case-insensitively.

Exit codes, ordered (N5c-2, 4.13.11; the first condition that applies wins):
1 a usage error or a configuration error (including two `[[repos]]` tables with
one normalized path); 2 no such run; 3 any row `NONZERO_OBSERVED` (ahead of every
4 condition, the same precedence `run` gives a critical finding over a
merely-INCOMPLETE status, 4.8; it also outranks a NEVER-list match, which F3
hardening used to let override it); 4 a lifecycle terminal `unknown` (4.13.3),
any row `NOT_OBSERVED`, any `WRITE_AFTER_CHECK`, the parent transcript could not
be read, any NEVER-list match anywhere, or no unit at all in a run that is not finished
with every attempt completed (N8b, K4.3, above); else 0 (every row `EXIT_0_OBSERVED`, or no
unit in such a finished run).
The process status and the JSON `exit_code` are the same number. `--json`
mirrors the text: `{schema: 3, run:
{run_id}, units: [{agent, repo, checks_declared, last_counted_run: {command,
completion, at} | null, word, writes_after, write_after_check, cycles}],
not_counted: [{line, reason, excerpt}], never_matches: [{line, excerpt}],
transcript_unknown, exit_code}` (`command` is the check-N pseudonym by
default, raw under `--no-redact`; every `excerpt`, in both lists, is the
matched NEVER rule's own spelling (`git checkout`; the fixed word `command`
for a `not_counted` row that matches no rule) by default, the full 60-char
excerpt only under `--no-redact` (N3b C1); `word` carries the same `NEVER_COMMAND_OBSERVED` suffix the
text line does when applicable).

**The `/rescue` skill** (`plugin/skills/rescue/SKILL.md`, registered the
same way `/postmortem` is, under the plugin's `skills/` directory): what the
tool can and cannot do in one paragraph, then the 7-step protocol from
`.reviews/proposals/2026-09-v0.2-guided-rescue.md` section 8.4 verbatim in
substance (get the packet with `rescue plan <run-id> --local --evidence`
(N3c; it was `--no-redact` before) and read the line right after the disclosure
line: stop on `POSSIBLY LIVE`; on `YOUR CURRENT SESSION` (FB3) do not stop but check
the listed files itself, read-only, before relaunching; on `NOTHING TO RESCUE YET:
the run may still be running` say so and stop. A test pins the skill's step 1
against the three sentences the tool prints;
read the diffs and report them; run the declared checks with the session's
own Bash tool, or ask the human when none are declared; repair within the
2-cycle budget then stop; never undo, copy a file aside with `cp` before a
risky edit; ask the human before every commit and relaunch, showing `rescue
status <run-id> --local`; never edit an instruction file), then how to read `UNKNOWN` and
`DIRTY_NOW`. The `/postmortem` skill (7.4) gets one line pointing at
`/rescue` for when `WORK AT RISK` names any paths or commits.

### 4.12 Watching a run

`watch.py` (v0.2, unit M5), standard library only. `agent-postmortem watch
<RUN_ID|PATH>` (4.8 has every flag) is a read-only follower for one
workflow run, designed to be the command a Claude Code Monitor runs: it
prints one line per event a person would act on and nothing otherwise, then
a terminal DONE or LIMIT line, and exits. Since v0.3 (N8b) it also prints one `START` line
first and one `OK` line per returned result, so a quiet run is distinguishable from a dead
watcher. It never writes inside the run
directory, arms nothing on its own, and holds no state across invocations.
The run id on a KILL or FAILED line (text and JSON) is the directory name
only when it matches the run id grammar; otherwise it is the replacement
`run` prints for that directory (`wf_<8 hex>-x`), never the raw name (N3b C2).

**Waiting for the run to exist.** The launch hook (below) fires before the
Workflow tool has created the run's own directory or its `journal.jsonl`.
`watch` therefore waits, printing nothing, resolving `RUN_ID|PATH` against
workflow runs only (never sessions, which carry no journal) the same way
`run` does, polling every `--interval` seconds for up to 60 seconds total.
If no journal has appeared by then, it prints `no journal appeared within
60 s` to stderr and exits 2; LIMIT (below) never applies to this wait, since
the run has not even started yet. Each sleep during this wait is capped to
whatever is left of the 60s budget (release finding F11): with `--interval
300` (the maximum allowed), the very first sleep is still only 60s, not
300s, so a large `--interval` can never defeat this limit. The main poll
loop's own sleep (below) is capped the same way against whatever is left of
the `--max-hours` deadline.

**Polling.** Every `--interval` seconds (default 5): stat the journal and
every transcript in the run directory. The journal is re-read only when its
size has changed, and only the bytes appended since the last read are
parsed -- one byte offset per journal, kept for the life of the process; a
file smaller than the stored offset has been truncated or replaced, and
reading restarts from 0. A transcript is never polled incrementally: it is
only stat'd, for its mtime (QUIET), or fully parsed once, the moment a KILL
or FAILED needs its last line or its silence span. **The parent transcript (N8b, K5.4)** is
read through the parent index of 4.13.3, refreshed only when its `(inode, size, mtime_ns,
ctime_ns)` signature changes since the last clean refresh. The signature is cached only after a
refresh that had no I/O failure (the index reports no pending full recheck), so a transient
read failure is retried on the next poll even when the file did not change. An unchanged poll
after a successful refresh reads zero parent bytes; an append parses only the appended lines (the
index rehashes the parsed prefix, N1, so bytes are read, not parsed); a same-size rewrite changes
the mtime and so still refreshes and rebuilds.

**Events**, in the order a poll considers them (START prints once, before anything else; KILL,
FAILED, and OK are one-shot per transition; DONE ends the loop; QUIET recurs; RESET and
MALFORMED can each happen at most once per truncation/malformed-line respectively; lines
are at most 78 columns, a second, indented line is allowed for KILL). Within one poll the order
is KILL, FAILED, OK, then DONE, then QUIET (N8b, K5.3):

```
START  <run id>: <A> agents started so far, <T> attempts.
KILL   <agent> attempt <n> after <s>s of silence; restarted as attempt <n+1>.
       attempt <n> wrote <p> path(s), reported <c> commit(s).  -> agent-postmortem rescue plan <run id>
       (<p>+ in text when the attempt's ledger is partial, N8 fix 1, S-m5; JSON keeps the integer)
FAILED <agent> attempt <n>: <cause phrase>.
OK     <agent> attempt <n> returned a result.
QUIET  <agent> attempt <n> has produced nothing for <s>s.
RESET  journal was truncated or replaced; state cleared.
DONE   <A> agents, <T> attempts, <VERDICT>.  -> agent-postmortem run <run id>
LIMIT  <hours>h reached; run not finished.
```

- `START` (N8b, K5.1): `START  <run id>: <A> agents started so far, <T> attempts.`, `<A>` the
  distinct journal keys started so far and `<T>` the started occurrences. Printed once per
  process, the first byte of output as soon as the journal is found: before a MALFORMED line the
  first poll parsed (the first poll defers it until START is out), before any other event of that
  poll. A first poll that ends on the 1 MB partial-line exit (below) prints no START: it never
  parsed the journal, so it has no counts to give, and the exit is final. A RESET does not
  print it again.
- `KILL`: fires when a `started` journal event for a key that already has an
  open (or just-superseded) attempt appears, and that earlier attempt's own
  transcript ends in the kill sentinel -- the same silent-retry shape
  `mislabeled_kill`/`silent_retry` attribute cause `runtime` to (4.3/4.4).
  `<s>` is the silence from the last meaningful event before the sentinel to
  the sentinel itself, computed the same way the interrupt machinery does
  (`detectors.base.meaningful_event_before`, reader.py's own
  `_build_interrupt`): `unknown` when that delta cannot be established. The
  second line's `<p>`/`<c>` are that earlier attempt's own
  `AttemptFacts.paths_written`/`commits` counts, from one full parse of its
  transcript (never polled incrementally). Release finding F10: the
  transition is detected the poll the second `started` event appears, but is
  only marked handled once actually resolved -- see **Retry of a suppressed
  KILL/FAILED event** below for what happens when the earlier transcript is
  not there yet.
- `FAILED`: fires on a journal `failed` event. The cause phrase is read from
  exactly the same place `attempt_failed` reads it (4.3): the failed
  attempt's own final transcript entry (4.13.2: the last entry whose `type` is not
  `attachment`, found by the same `final_event` helper), never a wider window; `cause not
  recorded` when nothing there is attributable (this does not, on its own,
  delay emission or count as a reason to retry -- only the rate limit does,
  per F10 below). Unlike the `attempt_failed` detector's own title, this
  line names no retry tail -- `watch` is a live feed of what just happened,
  not a verdict over the whole run.
- `OK` (N8b, K5.2): `OK     <agent> attempt <n> returned a result.` once per occurrence
  closed by a journal `result` event, `<agent>` the stable alias. It is subject to the same
  per-agent rate limit and the same bounded retry as FAILED: an OK suppressed by the limit is
  retried every poll and printed once the limit clears, or forced on the 12th unresolved poll.
- `QUIET`: fires when an open (not yet closed by `result`/`failed`/a later
  `started`) attempt's transcript mtime is at least `--quiet-after` seconds
  old. Printed once per attempt, then again only once the silence has
  doubled from the threshold it last fired at (300, 600, 1200, ... seconds
  with the default `--quiet-after`) -- a poll where the doubled threshold is
  not yet reached, or where the per-agent rate limit (below) suppresses it,
  leaves the threshold unchanged, so the next poll can still fire at it.
  Within one poll, KILL, FAILED, and OK are always emitted before DONE and QUIET.
- `RESET` (release finding F8): fires when the journal is found smaller than
  the byte offset `watch` last read from -- truncated in place, or replaced
  by a fresh run reusing the same directory. Every piece of state derived
  from the old content (the parsed journal itself, which KILL/FAILED
  transitions are already handled or still pending, every QUIET doubling
  threshold, every per-key rate-limit timestamp, the DONE-grace clock and
  its own `started`-count baseline, which OK transitions are already handled or still pending
  (N8b), and the malformed-line flag and line
  counter below) is cleared before the replacement content is processed at
  all, so nothing from before the reset can combine with what comes after
  it into a false KILL or a false DONE. The reset also starts a new **journal epoch** (N8b,
  K5.4): watch records the newest parent generation's launch line it knew before the reset as the
  ended epoch's watermark, and in the replacement epoch only a generation whose launch line is
  newer than that watermark can finish the run at once; an older generation's terminal (a
  notification that lands while the journal is being replaced) never overrides the replacement
  journal's own unresolved state. RESET is exempt from the rate limit
  (like DONE and LIMIT) and can fire again later if the journal is
  truncated more than once.
- `DONE` (N8b, K5.4 rewrites the v0.2 journal-only rule): fires from the 4.13.3 lifecycle table
  evaluated with the `journal_resolved` fact computed from watch's own in-memory journal
  occurrences (`reader.lifecycle_from_journal_state`; watch never re-reads the journal or reruns
  its state machine, and refreshes the parent index only on a changed signature, above). Two ways
  to be eligible:
  - a lifecycle terminal of `completed`, `failed`, or `stopped`, whatever its source that is not
    the journal (an accepted task notification, a `TaskStop`, or a synchronous launch result,
    whatever the journal says): eligible at once, with no grace wait;
  - a lifecycle terminal of `journal_resolved` (every key the journal has started is resolved,
    `reader.workflow_journal_finished`'s own rule, from the same occurrences, and no terminal
    evidence decides the run) that has stayed true for `--done-grace` seconds (default 30) with
    no new `started` event: the journal-only reading, kept because a workflow that runs its
    phases one after another shows every key so far resolved in the gap between two phases, and
    the next phase's first `started` lands within seconds. A new `started` resets the grace
    clock. `--done-grace 0` makes it eligible at once.

  A lifecycle terminal of `unknown` or `none` is never eligible: a run whose parent evidence
  cannot be trusted (an unreadable or malformed parent transcript, an ambiguous launch, a
  conflicting or unrecognized terminal) is not declared done, even when its journal is
  resolved; `watch` keeps polling and ends with LIMIT.

  Once eligible, DONE is printed only after every KILL, FAILED, and OK transition already detected
  has been emitted or forced through its bounded retry (this is not a grace wait: it is the 12-poll
  bound of the retry paragraph below), so no event is dropped. `<A>`/`<T>` and `<VERDICT>`
  come from one full `reader.load_run` plus `detectors.run_detectors` at
  that moment, the same verdict `run` would print. Honest limit: a gap
  longer than the grace period still ends the watch early on the journal path; the DONE line
  is then wrong about the run being over, and the launch hint's Monitor
  will need re-arming. Release finding F9: once the journal has carried any
  malformed line (below), DONE can never be printed again for this run --
  LIMIT (with a different message) fires in its place instead, at the exact
  point DONE otherwise would have, an immediate terminal included.
- `LIMIT`: fires when `--max-hours` (default 6) has elapsed since `watch`
  itself started, and the run still has not finished; a timeout is never
  reported as DONE. Also fires, with a different message and independent of
  `--max-hours`, in the two malformed-journal cases below (F9).

**Malformed and oversized journal lines (release finding F9).** A journal
line is read once it is terminated by a newline; a not-yet-terminated
trailing line is withheld and reread whole on the next poll, same as
before, *unless* it has already grown past 1 MB with still no newline --
a stuck or malformed writer, not an ordinary write in flight -- in which
case `watch` prints `MALFORMED journal line longer than 1 MB` and exits 4
immediately, independent of `--max-hours` or run state. Separately, a
complete line (one that is newline-terminated) that fails to parse as a
JSON object -- invalid JSON, or valid JSON that is not an object -- sets a
malformed flag for the rest of this run: `MALFORMED journal line <n> is not
JSON` (`<n>` counting every complete line read since the last RESET) is
printed once, the first time this happens, never again for a later
malformed line in the same run. From that point on DONE can never be
printed for this run; once every key would otherwise have resolved (the
point DONE would fire), `watch` prints `LIMIT  journal has malformed lines;
run state unknown.` instead and exits 4 -- the journal's own account of
what happened can no longer be trusted as complete.

**Retry of a suppressed KILL/FAILED/OK event (release finding F10; OK, N8b).** A KILL,
FAILED, or OK event is marked handled only once it has actually been resolved,
never merely detected: an event this poll cannot resolve is retried on
every later poll rather than being dropped. KILL can be unresolved for two
reasons: the per-key rate limit (below), or the earlier attempt's own
transcript file not existing on disk yet (the journal's `started` line for
the next attempt can land before the previous attempt's transcript write
is visible). Once the transcript does exist, if it does not end in the
kill sentinel, that is conclusive -- not a kill, never printed, never
retried again. FAILED can be unresolved only by the rate limit; a missing
transcript already degrades gracefully to `cause not recorded` rather than
blocking emission, so it is never a reason to retry FAILED; OK, like FAILED, can be unresolved only by the
rate limit. Either kind of
retry is bounded at 12 polls: on the 12th unresolved poll, KILL is
force-printed without its transcript-derived details --
`KILL   <agent> attempt <n> (details unavailable); restarted as attempt
<n+1>.` -- and FAILED and OK are force-printed bypassing the rate limit (FAILED using
whatever cause phrase it already has); either way the event is then marked
handled and never retried further. This is what closes release finding
F10: previously, an event suppressed by the rate limit, or seen before its
transcript existed, was marked handled at the moment it was first
detected and never printed at all.

**Rate limit.** At most one line per agent (per journal key) per 60 seconds,
except DONE, LIMIT, and RESET, which are never rate-limited. A QUIET line
suppressed by the limit leaves its doubling threshold unchanged, so it can
still fire on a later poll once the limit clears. A KILL, FAILED, or OK line
suppressed by the limit is retried, not dropped -- see **Retry of a
suppressed KILL/FAILED/OK event** above.

**Exit codes** (ordered, N5c-2, 4.13.11): 1 a usage error; 2 when no journal
appeared within 60 s; 3 on DONE with verdict FAILED (the verdict word of 4.13.4:
the lifecycle terminal is `failed`, or a finding is critical); 4 on LIMIT (the
ordinary `--max-hours` timeout, or either malformed-journal case above) and on
DONE when the run's lifecycle terminal is `unknown` (4.13.3) and the verdict is
not FAILED (watch's own loop never makes an unknown terminal eligible, so this only happens
when the evidence changes between the eligibility check and the full load that renders the DONE
line); 0 on DONE otherwise (verdict CLEAN or DEGRADED, with or without the
`(INCOMPLETE)` suffix). A DONE line is printed in every DONE case; only the exit
status differs.

**Redaction.** `watch` always renders `share`; it takes no level flag. An
`<agent>` token is the event attempt's own stable alias `a:<alias>` (N3b, P14,
4.13.5), the identity `run` shows for the same attempt, not the journal
label and not a per-watch pseudonym; `--json`'s `agent` field carries the
same text, and `attempt` and `restarted_as` keep their ordinals. The agents
the journal names at the first poll are registered in one call; an agent
first named by a later event is registered then, one registration per new
agent, not per line. If a registration fails (secret or registry
unavailable, lock contended past 2 s, a bound exceeded), that event and
every later one name agents positionally (`agent-N`, numbered within this
watch, the agents named before taking the lower numbers) while the lines
already printed stand (one event is one render, C-1), and `watch` prints
`agent-postmortem watch: stable aliases unavailable` once to stderr (C-6),
in text and in `--json` mode alike, so stdout stays valid JSONL and no event
type is added. An attempt whose `agentId` is missing, null or not a canonical
agent id (C-5) is named by a positional `agent-N` of its own, never by its
label. The run id itself is printed as is (it carries no user text, 5.1).

**`--json`** prints one JSON object per line, `{"event": "START" | "KILL" |
"FAILED" | "OK" | "QUIET" | "RESET" | "DONE" | "LIMIT" | "MALFORMED", ...}` with
the fields named above (START: `run`, `agents`, `attempts`; OK: `agent`, `attempt`; KILL: `agent`, `attempt`, `silence_seconds`,
`restarted_as`, `paths_written`, `commits`, `run_id`, plus
`details_unavailable: true` on the F10 forced-fallback line, with
`silence_seconds`/`paths_written`/`commits` null on that line; FAILED:
`agent`, `attempt`, `cause`, `reset_time`; QUIET: `agent`, `attempt`,
`silence_seconds`; RESET: no fields beyond `event`; DONE: `agents`,
`attempts`, `verdict`, `run_id`, `next_command`; LIMIT: `hours` for the
ordinary timeout, or `reason: "malformed"` in its place for the
malformed-journal case (F9); MALFORMED: `reason: "line longer than 1 MB"`
with no `line`, or `line` (an integer) with `reason: "not JSON"`), through
the same redaction as the text lines.

**The launch hint (7.2).** `hook workflow-launched` hands the session the
`watch` command the moment a workflow is registered, so arming a Monitor
never depends on a person remembering to; `watch` itself arms nothing.

## 4.13 v0.3 evidence model (contract; implemented in N1 to N8, commits 31a2c24..87b4cca; the N9 synthetic release gate is implemented; the advisory corpus check was run with the owner's authorization; N7 step 3 is deferred past v0.3 by the owner's decision C)

Source: `.reviews/proposals/2026-10-01-v0.3-evidence-model.md`, revision 3.
Each subsection below names the milestone that implements it. Until that
milestone lands, the behavior described elsewhere in this document (the
sections above, and section 9) applies unchanged; when a milestone lands it
edits the older sections it replaces in the same commit, so DESIGN.md never
carries two true descriptions of the same behavior at once.

### 4.13.1 Write evidence ledger (N4) (implemented in N4) (commits 707aa96..6e0bc61)

Each attempt gets a write ledger, held alongside its `AttemptFacts` (4.1).
One ledger entry is produced per (tool call, target); a call that names no
target produces exactly one entry with `path` null (D6). An entry has these
orthogonal fields:

| Field | Values |
|---|---|
| `effect` | `observed` (a file tool returned success), `claimed_target` (a command names a target under the tiny command grammar below; this says nothing about whether the write happened), `attempted` (a write-capable call whose result was an error, missing, or ambiguous), `possible` (a call that may write and names no target the tool can trust) |
| `basis` | `tool_result`, `command_grammar`, `delegation`, `opaque_command`, `background_process`, `unknown_tool` |
| `completion` | `success`, `error`, `no_result`, `ambiguous` |
| `path` | normalized path, or `null` |
| `repo` | the repository the path or command resolves to, by this section's own path and repository resolution rules below; `null` when unknown. **Correction (D7):** this is not the resolution 4.9 performs today -- 4.9 selects one most-common cwd for the whole run and never parses a per-command `git -C`. N4 resolves `repo` per ledger entry, and changes 4.9's own observation (the "Observation boundary" bullet below) to observe each distinct `repo` value separately, with every 4.9 guard applied per repository. |
| `line` | evidence in `path:line` form |

**Risk population.** Which attempts must be accounted for depends on the
run's lifecycle (4.13.3), not only on each attempt's own outcome. An attempt
is in the risk population, and its ledger must be accounted for, when any of:
- (workflow runs) its outcome (4.1/4.2) is `killed`, `failed`,
  `superseded`, or has no result -- "has no result" means the attempt's own
  outcome is open (its `journal_state` is `open`: no terminal journal event
  closed it) or `unknown`; it never refers to a `ToolCall`'s own `no_result`
  completion, which is a per-call fact, not an attempt outcome (D4);
- (workflow runs) it belongs to the newest generation (4.13.3, the one with
  the greatest launch line) and that generation's terminal is `failed`,
  `stopped`, or `unknown` -- even when the journal's own state machine
  closed the attempt `completed` (a run that failed after an attempt
  "completed" may still have left that attempt's writes uncommitted);
- (workflow runs) it has no journal key (its `journal_state` is `None`, N4
  fix S2): no journal event can ever close it, so it is treated like an
  `open` one whatever its outcome and generation;
- (workflow runs) it is unassignable (generation assignment below): an
  attempt the tool cannot place in time is never assumed safe;
- (session runs) every subagent with an `interrupt` set, and the main attempt
  when the session has no activity after an interrupt -- "no activity after
  an interrupt" means the interrupt is the final meaningful main-thread event
  in physical order: the main attempt has an `interrupt`, and no event after
  it is a meaningful one (not `thinking`, not `attachment`, and with text once
  harness noise is stripped; 4.5) (D4). A session has no journal and no
  generation, so the first four branches never apply to one (a session
  attempt's `journal_state` is `None` too, and that never selects it).

**Generation assignment (D4, 4.13.11; FA2).** Applies to workflow attempts only.
An attempt belongs to the generation (4.13.3) whose Workflow `tool_use` entry
timestamp (`Generation.launch_ts`, the launch call and not its result, read from
the parent-index snapshot that produced the lifecycle, never from a second parent
read) is the latest one at or before the attempt's own `first_ts` (equal belongs
to that generation; a `first_ts` between two timestamps belongs to the earlier
one). The launch call is the causal boundary: a child can write its first entry
before the parent records the launch result line (0.33 to 0.47 s in the four
observed real runs), and that attempt belongs to the launch. The launch result
timestamp (`result_ts`) plays no part in assignment, and lifecycle evidence
ordering (4.13.3: notifications and TaskStops after `result_line`) is unchanged.
An attempt with no `first_ts`, or whose timestamp precedes the first launch call,
is unassignable.
So is an attempt whose transcript has an event-bearing line at or before the line
of the event that gave it `first_ts` (the earliest line carrying that
timestamp) with no usable timestamp, that is the `timestamp` key absent, not a
string, or not parseable (N4 fix, A5): a write on such a line may have
happened before `first_ts`, so the attempt cannot be placed. A line with no
usable timestamp after that line does not matter. So is every attempt of a run that has no generation, and of a run in which some
generation's `launch_ts` is missing or the timestamps are not strictly
increasing in launch-line order. An unassignable attempt is still included in
the risk population (conservatively -- an attempt the tool cannot place in time
is never assumed safe), its ledger carries the reason `unassignable_attempt`,
and it makes the run's own `risk_population_completeness` (below) `partial`,
reason `unassignable_attempt`.

**Risk population completeness (D4).** Every run carries
`risk_population_completeness: complete | partial`, orthogonal to any one
attempt's own ledger `completeness` below:
- `partial`, reason `unassignable_attempt`, when generation assignment
  (above) could not place some attempt;
- `partial`, reason `discovery_incomplete`, whenever journal, parent,
  attempt-transcript, or meta discovery (`Run.discovery`, 4.1) reported a
  diagnostic, whatever the size of the risk population (4.13.11) -- an empty
  population supports `complete` only when every one of those four discovery
  steps completed cleanly, a named-but-missing session child included;
- `complete` otherwise.
The reasons are listed in that order (`unassignable_attempt` first).

**Ledger construction (N4b-2).** One ledger per attempt, built from the
attempt's own tool calls in the order of the per-call truth table below. An
entry's `line` is `<transcript file name>:<line>`: the line of the call's
`tool_result` when it has one, else the line of its `tool_use`. Entries are in
`tool_use` order, and in target order within one call (a tiny-grammar call
names its targets in order). The completion mapping (4.13.11) is `completed_ok`
to `success`, `completed_error` to `error`, `backgrounded` and `no_result` to
`no_result`, `ambiguous` to `ambiguous`. A file tool names one target: Edit,
Write, and MultiEdit take `input.file_path`, NotebookEdit takes
`input.notebook_path`; a missing or non-string field gives one entry with
`path` null and the reason `unexpected_entry_shape`, and one over 1,024
characters gives `path` null and the reason `truncated_field`. The entry's
`path` is the lexical normalization of the input path (Paths (D7) below, a
leading `//` collapsed to `/`): an absolute input is normalized, a relative
one is joined to the call's own absolute `cwd` first, and a relative input
with no absolute `cwd`, or an empty string, gives `path` null and the reason
`unexpected_entry_shape`; a normalized path over 1,024 characters gives
`path` null and `truncated_field`. The `repo` of a
file-tool or unknown-tool entry is the call's own absolute `cwd`, normalized
(a candidate, like every `repo` below), null when the call has none; the `repo`
of a shell, grammar, or delegation entry is the candidate of the call's
classification, except that a shell call containing commit segments (the commit
grammar below) takes the `repo` of its one entry from the commit segments
instead of from the call's own candidate: the single distinct candidate (C-10:
the segment's candidate base with its `-C` options applied) when every commit
segment yields the same one, else null. A targetless call (rows 2, 3, 6) gives
exactly one `possible` entry, never one per repository. The ledger's `reasons` are the closed codes below, each when
its producer fired, deduplicated, in the fixed order of that list; a ledger
with no reason is `complete`. Run-level producers (`unassignable_attempt`,
`discovery_incomplete`) are added to every attempt's ledger of the run once the
run is assembled.

**Repository grouping (L14, N4b-2).** `ledger.group_by_repo(run)` is a pure
function over a loaded run (no filesystem access, no subprocess) that returns
`RepoGroups`: `repos`, a mapping from every non-null `repo` named by any entry
of any risk-population ledger (complete or partial) to a `RepoGroup`, in sorted
key order, and `unknown`, a `RepoGroup` for entries whose `repo` is null, for
risk-population attempts whose partial ledger names no repository at all, and
for every risk-population attempt that made at least one Bash or Monitor call
(whether or not that call produced an entry: the shell may have run or written
somewhere no recorded path or candidate names). A `RepoGroup` holds `paths`
(sorted, unique; the observed, claimed, and attempted paths of that group),
`attempts` (attempt references, unique, `main` first and then `attempts[<index
in Run.attempts>]` in index order), `refused` (true when any contributing
ledger is partial), and `reasons` (the union of the contributing partial
ledgers' reasons, in the fixed order of the reason list). A partial ledger
keeps its repositories and paths; a consumer must render a refused group as
"run status yourself", never as "nothing to observe".

**Session discovery reconciliation (C-14, N4b-1).** `Run.discovery` (4.1) is
filled while a run is loaded, from the diagnostics each source reported, in
the order reported: `journal` (journal.jsonl reading, its state machine, and
the started-event field checks), `parent` (the parent-index snapshot's
diagnostics), `transcripts` (attempt transcript reading, including a session
subagent file whose name is not a canonical id), and `meta` (agent meta
files). For a session run, `load_session` also enumerates the canonical
`subagents/agent-<id>.meta.json` names of the session directory independently
of its transcripts (a canonical id is 17 lowercase hex characters; a directory
or a non-canonical name names no child); each meta id with no
`agent-<id>.jsonl` file adds one `transcripts` diagnostic, exactly `<meta
name>: child named by a meta file has no transcript`, in sorted id order, after
the reader's own per-file diagnostics and before the launch diagnostics below
(the meta file's content is not read for it). `load_session_view` keeps its own
explicit child list (7.2) and enumerates nothing. The main transcript's child
launches are reconciled against the subagent transcript files the session
directory holds, and each launch that does not reconcile adds one diagnostic to
`transcripts`, exactly `<main transcript name>:<tool_use line>: <Tool>
launch has no discovered child (<code>)`, in tool-use order. The
child-launching tools are **Agent, Task, Workflow** (SendMessage continues an
existing child and launches none). A launch is a `tool_use` block whose
`name` is the tool, in the content of an **assistant** entry of the main
transcript; a `tool_use`-shaped block inside a user entry is not a launch.
The first row that applies gives the code. For Agent and Task (two id
locations: the entry's `toolUseResult.agentId`, and the `tool_result` block's
own `agentId`): 1. the launch block's `id` is missing or not a string:
`malformed_launch`; 2. another `tool_use` block shares its `id` (4.2):
`ambiguous_tool_id`; 3. no `tool_result` block carries that `tool_use_id`:
`no_result`; 4. two or more do and they are not all identical:
`ambiguous_result` (results whose `tool_result` block and entry-level
`toolUseResult` are identical as parsed JSON count as one result, whether a
result is written before or after its launch; a launch logged with its result
written twice reconciles as if written once);
5. either id location is
present and not a string: `non_string_id`; 6. both are strings and differ:
`conflicting_ids`; 7. neither is present: `no_id`; 8. the id names no
subagent transcript file: `undiscovered`; 9. otherwise it reconciles. For
Workflow (no id location of its own; reconciliation is the parent-index
association of 4.13.3): rows 1 to 4 as above; 5. the one result is a
rejected launch (its `tool_result` block has `is_error` true and the entry's
`toolUseResult` is a string; N4 fix, A5): an error result is not proof that no
run exists (Agent and Task have no such row, since an errored Agent call may
still have started a child), so it reconciles only on durable evidence. With a
string `resumeFromRunId` on the launch it reconciles when the named run
directory (a canonical run id, a directory of the session's `workflows`
directory) exists, and is `undiscovered` otherwise; the named run's own load
reports the rejected launch (4.13.3). Without a resume (an initial launch) it is always
`undiscovered`: no producer evidence ties a missing run to that launch, and a
directory named by another generation is not exclusive proof that this launch
created none; 6. no
`Generation` of the same parent-index snapshot whose launch is this `tool_use`
has a run id naming a discovered workflow run directory: `undiscovered`; 7.
otherwise it reconciles. "Whose launch is this `tool_use`" means the same `id`
and the same physical line (N5c-2): the launch log and the parent index are two
reads of the one transcript, not one snapshot (the index is cached and
incremental, and counts a line by `\n` bytes alone where the transcript read
counts lines by universal newlines, so a lone carriage return numbers a launch
one line apart), so the launch's own line is part of what must agree, and a
launch the index places on another line is `undiscovered` rather than matched
by its id alone. `conflicting_ids`, `non_string_id`, and `no_id` do not apply to
Workflow. Any such diagnostic makes the run's discovery not clean, so
`risk_population_completeness` is `partial`, `discovery_incomplete`, even
when the population is empty. A view of named subagents
(`load_session_view`) names its children itself and reconciles nothing.

**Return evidence (S11, N5c-1; v0.3.1 G-A).** `Attempt.return_state`, `return_reason` and
`resumes` (4.1) are the one predicate behind "WORK AT RISK none" for a session (4.7), the
exported `outcome` of a session subagent (E1, 5.3b), the AGENT END word, `attempt_failed` for a
reported failure (4.3), the rescue no-unit state (4.11) and the hook's finished rule (7.2). A
session's none requires every subagent outside the risk population to have `return_state`
`returned`, because a child killed with no kill sentinel is outside the population and can still
have left writes. `returned_to_parent` is exactly `return_state == "returned"`. The kill sentinel,
`interrupt` and the internal whole-transcript `outcome` are unchanged. The table below is the
whole decision; whatever it does not assign is `unknown`.

*T0 measured shapes only.* The reader recognizes exactly the shapes observed in the corpus and
written here; every other presence or type is `unknown`/`malformed` for the child it names (or
for every child named by either target field of a request): subagent meta that is unreadable or
not a JSON object; `parentAgentId` present and not a non-empty string, or equal to the child's own
id; `spawnDepth` present and not an integer of 1 or more (a boolean is not an integer); `toolUseId`
present and not a string; any agent
id, `resumedAgentId` or status that is present and not a string; a launch `toolUseResult`
that is present and not an object, except the exact rejected shape (an error block with a string
`toolUseResult`, itself `malformed` for a child it names); a SendMessage with neither `to` nor
`recipient`, with either present and not a string, or with `to` and `recipient` naming two
different children of the session (GA-7: a value that is not the id of a child of the session is
an agent name, never a contradiction); an Agent, Task or SendMessage `tool_use` block whose `id` is
present and not a string (a number, null or a boolean; GA-4): the id cannot key a launch or a
request, so the block is kept as a bounded malformed-use fact and makes `unknown`/`malformed`
every child named by its `to` or `recipient` and every child named by a string `agentId` or
`resumedAgentId` of a result whose `tool_use_id` is the same non-string value; a
non-rejected SendMessage result whose `toolUseResult` is neither the resume shape (an object with
`success` true and a string `resumedAgentId`) nor the measured queued shape (an object with
`success` true, a string `message` starting `Message queued for delivery`, and no
`resumedAgentId`). (GA-7) A resume's `resumedAgentId` decides the child. A `to` or `recipient`
that differs from it is a contradiction, `unknown`/`malformed` for both children, only when that
value is the id of another child of the session (a subagent transcript or meta file of the
session); any other value is an agent name (SendMessage's own usage says to refer to agents by
name) and is no contradiction. Measured: 45 resumes, every one with `resumedAgentId` equal to `to`.

*T1 launcher.* The child's launch is looked up in its launcher transcript: the main transcript
when `parentAgentId` is absent and `spawnDepth` is absent or 1; the transcript of the subagent
named by a string `parentAgentId` equal to another subagent's id of this session (nested), read as
evidence only and never as an analyzed attempt. A string naming no other subagent of the session,
or `spawnDepth` 2 or more with no `parentAgentId`, is `unknown`/`parent_missing`. A launcher (or
the main transcript) that cannot be read is `unknown`/`parent_unreadable`. Notifications always
come from the MAIN transcript's accepted notifications (4.13.3), for every depth: a nested child's
completion notification is delivered to the main transcript, naming the child as task-id and the
launcher subagent's Agent `tool_use` id as tool-use-id.

*T2 launch.* Candidate launches are the Agent or Task `tool_use` blocks of the launcher whose
result names the child (a string `agentId` in the `tool_result` block or in the entry's
`toolUseResult`), plus those whose id an accepted main notification with task-id equal to the
child names as tool-use-id (a nested launch often has no structured result: 18 of 38 observed),
plus the launcher's Agent or Task `tool_use` whose id equals the child's meta `toolUseId` when that
is a string. Claude Code 2.1.309 returns a nested foreground child's report as a plain
`tool_result` starting `[Subagent hand-back]`, with no `toolUseResult`, no `agentId` and no
notification (3 nested cases, 4 children in one real session), so neither of the first two sources
finds it. Measured: every session subagent meta file carries a string `toolUseId` equal to its
launching `tool_use` id in the launcher transcript, and it agrees with the result-linked launch in
651 of 651 cases where both exist. A string naming no launch `tool_use` of the launcher adds no
candidate (it is not malformed); one naming a launch other than the result-linked or note-linked
one gives more than one candidate. A result with no `toolUseResult` and no `status` is synchronous
unless an accepted main notification names its launch (the asynchronous rule below), so such a
hand-back, which has no notification, is `returned`. The hook's main-side digest (7.2) includes the meta-linked launch.
None is `unknown`/`no_evidence`. More than one, or two distinct results (results identical as
parsed JSON count once), is `unknown`/`conflict`. With the one launch: an error result is
`unknown`/`malformed`; a result naming another agent as well is `unknown`/`conflict`; `isAsync`
true together with a terminal `status` (`completed`, `failed`, `killed`, `stopped`, `cancelled`)
is `unknown`/`malformed`; asynchronous (`status` `async_launched`, `isAsync` true with no
`status`, or a result with no string agent id when an accepted main notification names the launch,
whether or not the child's meta `toolUseId` also names it: the meta link only finds the launch and
never decides synchronicity) leaves round 1 to the notifications; synchronous with `status` `completed` or no `status` key makes round 1
`returned`; `failed` makes it `reported_failed`; `killed`, `stopped`, `cancelled` make it
`reported_killed` (4.13.3's stopped class); any other status string is
`unknown`/`status_unrecognized`.

*T3 resume rounds.* A SendMessage `tool_use` in ANY inspected transcript of the session (main or
a subagent) whose exactly-one non-error result (identical results count once) is the resume shape
with `resumedAgentId` equal to the child resumed it: a new round keyed by the request's `tool_use`
id, and `resumes` counts it. Its `to` and `recipient`, where present, are checked by T0 (GA-7).
A transcript that is a sender of this rule (the main transcript, an analyzed subagent's, the
child's launcher) and cannot be read at all (an open or read failure) makes EVERY child of the
session `unknown`/`parent_unreadable` (GA-1): any child could be the target of a resume request
hidden in it. A malformed or half-written line inside a readable transcript does not: a live
transcript often ends in a half-written line (whether the cut falls between characters or inside
one), and the run is already INCOMPLETE with a diagnostic.
Recorded limits: a resume request inside an unparsable line is invisible, and a request addressed
by an agent name that has no result cannot be attributed to a child and touches none. Every launch and resume key must be carried by exactly one `tool_use` block across the
transcripts read, else `unknown`/`conflict`. Rounds are ordered by the parsed timestamp of their
`tool_use` entry and the launch must be first; a missing or unparsable timestamp on a resume (or
on the launch when the child has a resume), a resume not later than the launch, or two rounds
with the same instant is `unknown`/`ambiguous`. A request that targets the child and was
delivered in the measured queued shape ("message queued for delivery", observed 14 times) joined
the running round: no round, no effect. One whose every result is an error was rejected: no effect.
One targeting the child with no result, or with several distinct results, is
`unknown`/`delivery_unknown`. A request naming another child does not touch this one.

*T4 notifications.* Exactly the 4.13.3 accepted notifications of the main transcript (whole
envelope with one task-id, one tool-use-id and one status; the two delivery forms; the origin
cutoff; triplet dedup) whose task-id is the child. A notification with no tool-use-id is not
accepted there, so it is ignored here. Each must name the launch or one of the child's resume
requests, else `unknown`/`conflict`; it belongs to that round (a resumed round's notification
names the SendMessage's own `tool_use` id). A notification must lie after its round's request: by
physical line when the request is in the main transcript, by parsed entry timestamp otherwise;
before it (or equal to it), or not orderable, is `unknown`/`ambiguous` (FA3 for the launch is the
same-transcript case). Status words: `completed`; `failed`; `killed`, `stopped`, `cancelled` all
mean killed; any other string is unrecognized.

*T5 decision*, on the latest round only: its notifications all `completed` is `returned`; all
`failed` is `reported_failed`; all killed is `reported_killed`; mixed is `unknown`/`conflict`;
any unrecognized is `unknown`/`status_unrecognized`; none is the T2 synchronous value when the
latest round is a synchronous round 1, else `pending`/`none`. When the latest round is a
synchronous round 1 that also has notifications, the T2 value and the notifications' value must
be equal, else `unknown`/`conflict`. Earlier rounds never decide. Any `unknown` from T0 to T4
wins. The decision never reads a file's modification time.

A SendMessage block is never a launch; it is a resume request (T3). Conflicting notifications
are conflicts within the latest round only. For a view of named subagents (`load_session_view`),
the senders of T3 are the main transcript, the launcher and the named subagents; a resume issued
from another transcript is outside the view (a stated limit that only the hook meets).

The row set the tests pin: every T0 shape (unreadable meta, meta not an object, an empty,
non-string or self parent, a string, zero, boolean, fractional, null or negative depth, a
non-string second id, a launch result that is a list or null, a null status, a request with no
target or a null target, a mismatched resume target marking both children, and each non-measured
SendMessage result shape); parent absent, nested readable, nested unreadable, parent naming no
subagent, depth two without a parent; no launch, two launches, two distinct results, an error
result, the rejected shape, a result naming another agent, async with a terminal status,
synchronous completed, no status, failed, killed, stopped, cancelled, unrecognized, asynchronous
with no notification; a delivered resume, a resumed round's notification, an earlier round that
never decides, a queued request, a rejected request, a request with no result, two distinct
results, a request for another child, a resume from a third transcript, a resume key carried by
two blocks, a launch with no timestamp, a resume older than the launch, an unparsable timestamp,
equal timestamps, rounds ordered by time and not by line; a notification naming nothing, naming a
queued request, before its request (main and nested), without a tool-use-id, failed, killed
spellings, mixed, unrecognized, doubled delivery, a quoted envelope, another child's
notification; a synchronous value and a different or the same notification.

*Return-specific notification boundary (field milestone F, FA3).* A notification naming the
launch's own id counts only when its physical line is after the launch `tool_use` line, in the
launcher transcript when that is the main transcript. A fast child can finish before the parent
records the launch result line (observed once in 382 async launches), so a notification between
the `tool_use` and its result line is accepted; one before the `tool_use` line is
`unknown`/`ambiguous` (T4). This is a different boundary from the lifecycle's: 4.13.3's notification boundary for a
Workflow generation's terminal evidence stays after the launch `result_line`, unchanged.

**Workflow discovery reconciliation (S1, N4 fix).** A workflow run's journal
names its attempts; the run directory is also listed for the files it holds.
Every file of the run directory named `agent-<id>.jsonl` or
`agent-<id>.meta.json` (a regular file, with any `<id>`) whose `<id>` no
journal `started` event names as its `agentId` string (a non-canonical id a
`started` event names counts as named, and so does an id of a `started` event
the reader skipped for a non-string key) adds one `transcripts` diagnostic,
exactly `<file name>: transcript named by no journal started event` (the same
text for the meta file), sorted by file name, after the attempt transcripts'
own diagnostics. Such a diagnostic changes neither `Run.diagnostics` nor
`Run.status`; it makes the run's discovery not clean (`partial`,
`discovery_incomplete`). When the journal is missing or unreadable every such
file is unnamed.

**What reaches `Run.discovery` (N4 fix, A2).** A discovery list holds only a
diagnostic that can hide an attempt, a tool call, or a launch: an unreadable,
missing, or undecodable input; an entry or block shape that drops a tool call
or a launch; a duplicate tool id; an ambiguous or malformed launch or launch
result; an unreconciled launch; a child named by a meta file with no
transcript. A data-quality note that cannot hide any of those stays in
`Run.diagnostics` only (it still forces `INCOMPLETE` wherever 4.6 says so). The
reader tags such a note where it raises it, never by matching message text.
A parent launch diagnostic (an ambiguous launch, a launch without a result, a
malformed launch result) reaches a run's `Run.discovery['parent']` only when it
involves that run (N4 fix, A4, corrected by A5): an ambiguous launch whose
candidate run ids (4.13.3, rule 1) include the run; a launch without a result,
with a malformed result, or with a rejected result, whose string
`resumeFromRunId` names the run; or a launch with a malformed result any of
whose results carries a string `toolUseResult.runId` naming the run (a
malformed result that names its own run involves that run, whatever the launch
resumes). A launch that names no run (an initial launch without a result, or
with a malformed or rejected result that carries no string `runId`) reaches no
run's list; the session launch table above still reports it. A parent-file
diagnostic (an undecodable line, a malformed entry, an unreadable parent) still
reaches every run that reads that parent. `Run.diagnostics` is not scoped. The
complete inventory of reader diagnostics that reach a discovery list, in the
order of the sources:

| Source | Diagnostic | Discovery |
|---|---|---|
| journal | `journal.jsonl: missing`; `journal.jsonl:<n>: <JSON error>`; `journal.jsonl:<n>: not a JSON object`; `journal.jsonl: unreadable: ...`; `journal.jsonl: undecodable: ...` | kept |
| journal | state machine: an event with no open attempt, an agentId that does not match, an attempt replaced with no recorded outcome | kept |
| journal | started event: non-string key, non-string agentId, no canonical agentId | kept |
| journal | started event: agent id did not match the expected format (replacement used) | kept |
| journal | started event: non-string `label`, non-string `phase` (cosmetic, never identifying) | excluded |
| parent | undecodable line, line that is not a JSON object (0.3.4), malformed entry, `message is not an object`, parent unreadable | kept |
| parent | ambiguous launch, launch without result, malformed launch result, rejected resume launch result | kept, only for a run it involves (A4 and A5, above) |
| transcripts | `<file>:<n>: <JSON error>`, `<file>:<n>: not a JSON object`, `<file>: unreadable: <error>` (an open failure or a failure anywhere in the read) | kept |
| transcripts | `message is not an object`, `tool_use input is not an object` | kept |
| transcripts | duplicate `tool_use_id` | kept |
| transcripts | session subagent file name that is not a canonical id | kept |
| transcripts | session launch reconciliation (above) | kept |
| transcripts | session: a canonical meta file whose id has no transcript file | kept |
| transcripts | workflow: a transcript or meta file no journal `started` event names | kept |
| transcripts | `timestamp is not a string`, `timestamp could not be parsed` | excluded (its effect on placing an attempt in time is carried by unassignability, 4.13.1, A5) |
| transcripts | `usage is not an object` | excluded |
| transcripts | `attachment is not an object` | excluded |
| transcripts | `block text is not a string` | excluded |
| transcripts | `duplicate usage for a (message id, requestId) pair differs from its first occurrence` | excluded |
| meta | `<meta name>: <JSON error or OSError>`; `<meta name>: not a JSON object` | kept |

Every excluded diagnostic stays in `Run.diagnostics` with its text and order
unchanged.

**Per-call truth table (D6; exact tool names per row and `Task`, C-16, N4a).**
First match wins, in the order of the rows below (4.13.11's row order); this
table is the complete definition of `effect`/`basis`/`completion`, replacing
any looser "write-capable call" test. A row names the tools it applies to:
a tool not named by a row never matches it.

| Call | completion | effect | basis |
|---|---|---|---|
| 1. Edit, Write, MultiEdit, NotebookEdit | success | observed | tool_result |
| same | error, no_result, ambiguous | attempted | tool_result |
| 2. **Bash or Monitor** with input `run_in_background: true`, with a `nohup` token anywhere in its command (any word equal to `nohup`), with a trailing `&` (the command's last token is the operator `&`; an internal `printf x & wait` is not background and falls to row 6), or with an anchored `bash_backgrounded` hit on its result | any | possible | background_process |
| 3. **Bash** (only Bash) with a segment running `codex`, `aider`, `claude` with `-p` or `--print`, or a word ending in `codex-companion.mjs`; and the tools **Agent, Workflow, SendMessage, Task** (`Task` is Agent's legacy name) | any | possible | delegation |
| 4. **Bash or Monitor** command in the closed read-only grammar (below) | any | no entry | -- |
| 5. **Bash** (only Bash: a Monitor command is never a tiny-grammar write) command in the tiny command grammar below | success | claimed_target | command_grammar |
| same | error, no_result, ambiguous | attempted | command_grammar |
| 6. any other **Bash or Monitor** command (a Monitor command that matches the tiny grammar's text is here) | any | possible | opaque_command |
| 7. read-only tools: Read, Grep, Glob, NotebookRead, WebFetch, WebSearch, ToolSearch, TaskOutput, TaskGet, TaskList, TaskCreate, TaskUpdate, TaskStop, ListAgents, SendUserFile, AskUserQuestion, SendFeedback, Skill, StructuredOutput, SubagentHandback, Artifact, ScheduleWakeup, TodoWrite | any | no entry | -- |
| 8. every other tool name, including every `mcp__` tool and the lowercase `bash` | any | possible | unknown_tool |

A backgrounded read-only command is therefore `possible`/`background_process`,
a Monitor call is never `delegation` or `claimed_target`, and `Task` is
`delegation` instead of `unknown_tool` (both are `possible`: the same effect,
a more precise basis). Classification reads the FULL command (C-11) and
never decides completion (the completion mapping is 4.13.11's).

**Closed read-only grammar for Bash and Monitor (C-8, N4a; replaces the
word-list grammar of D6).** What the grammar decides is one syntactic claim:
**the read-only grammar recognizes no write in the recorded command.** It
never claims the command did not write: the transcript shows the text, not
the shell, so aliases, functions, shell options, `CDPATH`, `PATH`, and the
environment stay invisible (C-15 below turns that into a completeness rule).
A command is read-only only when it is placed by the lexer (Tokenization
above) and every segment is read-only; one leading `cd DIR &&` is allowed,
where the segment is exactly `cd` and one plain word that does not start
with `-` (`cd`, `cd -`, `cd --`, `cd -L DIR`, `cd -P DIR`, and `cd` anywhere
but first are not read-only). A segment is read-only when its first word is
one of exactly these programs and every other word fits that program's table
in `commands.py`: `cat`, `head`, `tail`, `nl`, `wc`, `ls`, `pwd`, `echo`,
`printf`, `which`, `stat`, `file`, `du`, `df`, `cut`, `tr`, `basename`,
`dirname`, `realpath`, `grep`, `rg`, `find`, `sed`, `diff`, `cmp`, `jq`. The
tables in code are the contract: each is a finite list of options with their
arity (none or one argument, attached or separate; `jq --arg` takes two) plus
operand rules, and any option outside the table makes the segment
write-capable. Fixed rules inside them:
- `ls`: only `-1 -a -A -l -h -R -t -r -S -d -F -G -i -n -o -p -s` and
  `--color=never`.
- `rg`: only `--no-config` (required) and matching and output options: `-e
  -f -F -i -s -S -w -x -v -c -l -L -n -N -o -A -B -C -g -t -T --type
  --hidden --no-ignore -u -U -m --max-depth --files --files-with-matches
  --count --json`. Never `-z`, `--search-zip`, `--pre`, `--pre-glob`,
  `--hostname-bin`, `--hyperlink-format`, or any option not listed (each of
  these can start another program or read a configuration).
- `find`: paths, then only the tests `-name -iname -path -ipath -regex -type
  -maxdepth -mindepth -newer -mtime -mmin -size -perm`, the flags `-empty
  -prune -print -print0`, and the operators `-not ! -o -a ( )`. No `-exec`,
  `-ok`, `-delete`, `-fprint`, or any action that writes or runs a program.
- `sed`: only `-n`, `-E`, `-r`, `-e SCRIPT`, or one script operand; never
  `-i` or `--in-place` in any spelling, never `-f`, `-s`, or `-z`. A script
  is addresses (a line number, `$`, `/re/`, `first~step`, `a,b`, `a,+n`,
  `a,~n`, `!`) plus one of `p d q = n N` (`q` may carry an exit code) or `s`
  with flags limited to `g p I i` and digits. Anything else, including `w`,
  `W`, `e`, `r`, `R`, `y`, blocks, comments, and `s///w` or `s///e`, is
  write-capable.
- `diff`, `cmp`, `jq`, `grep`: finite tables of comparison, matching, and
  formatting options (no output-file, program-file, or config option).
- Redirects: `2>&1`; and a redirect of fd 1, 2, or both (`>`, `>>`, `1>`,
  `1>>`, `2>`, `2>>`, `&>`) to the literal `/dev/null`, as its own token or
  glued to its target but never glued to the word before it (a `&>` after a
  glued word, `echo foo&>f`, ends that word and is glued like any other
  redirect). An append to `/dev/null` is no more a write than a truncating
  redirect to it, so it never claims a target. Every other redirect,
  including `&>>`, every input redirect and heredoc, is not read-only.
- `git`, `sort`, `uniq`, `date`, `type`, `agent-postmortem`, `postmortem`, and
  every program not listed are write-capable (`git` configuration can run
  programs; `sort` and `uniq` take an output operand or file; a bare numeric
  operand sets the clock with `date` on macOS; the postmortem commands write
  the alias registry).

**Completeness (C-15, N4a states it, N4b implements it).** Any attempt that
ran at least one Bash or Monitor call (the lowercase `bash` tool is not one;
it is `unknown_tool`) carries the closed partial reason
`shell_context_unverified`, whatever the grammars decided, so a ledger is
`complete` only when no shell command ran, and "WORK AT RISK none" can never
rest on a shell-command verdict. The reason sits right after
`unpaired_write_call` in the closed order below.

**Unexpected shape (reason code `unexpected_entry_shape`, D6).** An entry
that is not a JSON object; an entry with no string `type`; a `user` or
`assistant` entry whose `message` is not an object or whose `content` is
neither a string nor a list; a `tool_use` block without string `id` and
`name`; a `tool_result` block without string `tool_use_id`. Entry types
other than `user` and `assistant` are ignored for the ledger when they are
in the known set (`attachment`, `system`, `summary`, `queue-operation`,
`last-prompt`, `ai-title`, `atis-latch`, `mode`, `permission-mode`,
`pr-link`, `agent-name`, `bridge-session`, `custom-title`,
`file-history-snapshot`, `file-history-delta`, `cost-state`, `frame-link`,
the request log Claude Code writes from 2.1.300 on (`api-request`,
`api-request-shape`, `api-request-blob`; a blob echoes earlier messages, so a
`tool_use` inside one is never a call the attempt made), and any type
starting with `artifact-`); any other type gives reason
`unknown_entry_type`. The request log was added to the set on 2026-10-28,
after a scan found it in every transcript written by 2.1.300 to 2.1.312.
Other types first seen in that range (`tools-used`, `relocated`,
`agent-setting`, `history-suppression`, `continued-in`) still give
`unknown_entry_type` until their meaning is established.

**Ledger completeness.** A ledger is `complete` only when the per-call
truth table above produces no `possible` or `unknown_tool` entry, every
write-capable call is paired with a result, the attempt's transcript is
present and fully readable, the attempt made no Bash or Monitor call
(`shell_context_unverified`, C-15), and every one of the run's discovery
inputs was read; otherwise `partial`, carrying every reason below that
applies.
Closed partial-reason codes (D6): `transcript_missing`,
`transcript_unreadable`, `undecodable_line`, `unexpected_entry_shape`,
`unknown_entry_type`, `truncated_field` (a recorded target path over 1,024
characters, stored `null`; a command is never truncated for classification,
C-11), `ambiguous_tool_id` (4.2's duplicate-`tool_use_id` rule),
`unpaired_write_call`, `shell_context_unverified` (C-15), `possible_write`
-- recorded as one of `possible_opaque_command`, `possible_delegation`,
`possible_background_process`, `possible_unknown_tool`, one per `basis` that
produced a `possible` entry -- `unassignable_attempt`,
`discovery_incomplete`.

"WORK AT RISK none" (the existing 4.7 wording) is printed only when the
risk population is empty and `risk_population_completeness` is `complete`,
or every ledger in the population is `complete` and holds no `observed`,
`claimed_target`, or `attempted` entry. In every other case the line states
what is known and what is not, for example: "agent
a:3f9c2e71d0 wrote 2 paths (file tools) and ran 12 shell commands that may
have written more; the tool cannot see those writes." The next step names
the single read-only command a person or session runs to see the rest
(`git -C <repo> status --short`); the tool never runs a whole-repository
status itself.

**Observation boundary (unchanged from 4.9, resolved per entry, D7;
observation moves to N5, C-14).** N4 builds the ledger and the pure grouping
above and observes nothing; the observation described here is N5's. Observation
stays candidate-scoped with every 4.9 guard: absolute `git`, a
scrubbed environment, `--no-optional-locks`, `core.fsmonitor=false`,
`--no-renames`, the filter-driver check immediately before `status`, a
pathspec limited to ledger paths with a non-null `repo`, observed per
repository -- one `rev-parse --show-toplevel` and one guarded `status` per
distinct `repo` value the ledger names, never the single most-common cwd
4.9 selects today for the whole run. Whole-repository status is not added.
When a ledger is `partial`, the output tells the person to run status
themselves rather than running it on their behalf.

**Tokenization (C-7, N4a; replaces `shlex.split` over the whole command).**
One pure function in `commands.py` (standard library only, no filesystem, no
subprocess) lexes the FULL decoded command (C-11: the 2,048-character cap
limits only what is stored, never what is classified). It keeps per-character
quote state and splits into segments and words:
- Separators, also when glued to a word: `&&`, `||`, `;`, `|`, `|&`, `&`,
  and a newline outside quotes. A newline right after `&&`, `||`, or `|` is a
  continuation, a backslash-newline is removed, and trailing newlines are
  dropped. A final `;` or `&` is recorded as the command's trailing operator.
- A comment starts at an unquoted `#` at a word start and is ignored.
- A heredoc (`<<DELIM`, `<<-DELIM`, `<<'DELIM'`, `<<"DELIM"`, also with a
  space after `<<`) is data up to the line equal to `DELIM`; its body is kept
  as data and never lexed.
- The command is **opaque** (no read-only verdict, no write-grammar target,
  no commit, no base directory) when it contains: outside single quotes, `$`
  or a backquote, also after a backslash; unquoted, also after a backslash,
  `*`, `?`, `[`; an unquoted brace expansion `{a,b}` or `{a..b}`; `~` at a
  word start; `<(` or `>(`; a here-string `<<<`; a redirect that duplicates a
  file descriptor other than `2>&1` (`>&2`, `1>&2`, `3>&1`, `<&0`, `>&-`); an
  unquoted parenthesis; an extglob opener (`+(`, `@(`, `!(`, `?(`, `*(`); `;;`;
  an unquoted heredoc delimiter whose body contains `$` or a backquote (the
  shell expands the body); unbalanced quotes; an unclosed heredoc; an empty
  segment or a dangling `&&`, `||`, or `|`. A segment whose first word is
  `{`, `!`, `function`, `alias`, `eval`, `exec`, `source`, `.`, `command`,
  `builtin`, `time`, `xargs`, `env`, `nohup`, or a `NAME=value` assignment
  is opaque.
- Exactly one literal form of command substitution is placed, for the commit
  grammar only (4.13.1's commit grammar): inside a double-quoted word,
  `$(cat <<'DELIM'` or `$(cat <<"DELIM"`, a body, the line `DELIM`, then `)`.
  It is one literal word whose text is the body. Nothing else may be inside
  the substitution (`cat` only, quoted delimiter). For every other purpose the
  same text is the opaque `$(` it is.
The lexer is a table of these rules, not a shell interpreter. The transcript
records a command's text, never the shell it ran in, so what an alias, a
function, `CDPATH`, a shell option, or `PATH` did stays invisible; the
grammars below therefore claim only "no write is recognized in the recorded
command" (C-15).

**The tiny command grammar** (`basis: command_grammar`, `effect:
claimed_target`; D6/D7; C-12, N4a). Recognized only for a command the lexer
places, with no leading `cd` honored, no trailing `;` or `&`, and every path
named by the command resolved (a relative target with no absolute call `cwd`
makes the whole command `opaque_command`). The producer side of every form
must itself be read-only (the closed grammar above), so `rm v > log`,
`rm v | tee log`, and `cp a b > log` are not in it. Exactly these forms, each
giving the complete set of paths it can touch (a form whose affected set the
text cannot bound is `opaque_command`):
- `WORD+ (> | >>) TARGET`: one simple command, the redirect last, its own
  token (a glued `x>f` is not in the grammar), no other redirect. One entry:
  TARGET.
- `PRODUCER | tee [-a] TARGET`: PRODUCER is one or more read-only segments
  joined by `|`, `tee` is last with no other option. One entry: TARGET.
- `sed -i SCRIPT FILE` (GNU, no suffix) and `sed -i '' SCRIPT FILE` (macOS,
  empty suffix): one file operand, SCRIPT under the read-only `sed` script
  rule. One entry: FILE. With a non-empty suffix (`-iSUFFIX`, or `-i
  .SUFFIX` as a separate word): two entries, FILE then the lexical backup
  path FILE+SUFFIX; the suffix uses only `A-Z a-z 0-9 . _ ~ -` (a suffix
  containing `*` or `/`, GNU's name templates, and any other shape is
  `opaque_command`, as is more than one file operand, `-e`, `-f`, `-n`, and
  `--in-place`).
- `touch [-c -a -m] [-t STAMP | -d DATE | -r REF] TARGET+`: one entry per
  target, in order. Options precede the first operand (POSIX getopt, what
  macOS runs): a word starting with `-` after the first operand, `--`
  included, would be a file name there, so the command is `opaque_command`
  (`touch a -t 202601010000 b`); only a `--` before the first operand ends
  the options. The same ordering holds in every other form, whose grammar
  already fixes the position of each option (`sed -i s/x/y/ f -n`, `tee f
  -a`, and a word after a redirect target are all `opaque_command`); the
  read-only grammar is unaffected, since an operand there is only read.
- `cat > TARGET <<DELIM` with nothing after the `DELIM` line: one entry.
`cp` and `mv` are always `opaque_command`: whether the destination is a
directory (so the write lands at `DEST/SRC`) and what a directory source
touches depend on filesystem state the classifier never reads, and no
portable syntax forces a non-directory destination on both macOS and GNU
(C-12 removes the earlier `cp|mv` form). No other form qualifies. No shell
interpreter is built; the grammar is a fixed set of patterns over the
lexed command and nothing more.

**Paths (D7).** Lexical resolution only, never a filesystem access and
never symlink resolution: `os.path.normpath(os.path.join(call_cwd,
target))` with a leading `//` collapsed to `/` (macOS reads `//x` as `/x`;
`normpath` keeps exactly two), where `call_cwd` is the tool call's own
normalized `cwd` (used only when it is absolute; an absolute target needs
none); every candidate base and `-C` directory is spelled the same way. A relative target
with no absolute call `cwd` makes the entry `possible`/`opaque_command`. A
recorded target path over 1,024 characters is stored as `path` null with the
reason `truncated_field` (C-11).

**`repo` resolution (D7; C-10, N4a).** `repo` is the lexical directory a
command operates in, and every `repo` value derived from a shell command is a
**candidate**, never a trusted location (the entry's `repo` holds the
candidate; observation and grouping treat it as one). The candidate base of a
segment is the call `cwd` (when absolute), replaced by the operand of a
leading `cd DIR &&` only when DIR is an absolute plain word (`CDPATH` never
applies to an absolute path). A relative `cd name` can resolve through
`CDPATH`, and `cd` itself can be a function, both invisible; so a relative
operand, every form the lexer lists (`cd`, `cd -`, `cd --`, `cd -L DIR`,
`cd -P DIR`, any expansion), a `cd` joined by `;`, `||`, or `|`, and any
later `cd`, `pushd`, or `popd` make the base unknown for every later segment.
`-C DIR` options of a git segment apply in order to the candidate base (an
absolute DIR replaces it, a relative one is joined to it); a relative `-C`
with an unknown base, and any `--git-dir` or `--work-tree`, give `repo` null.
Repository identity (the toplevel) is established only at observation time,
through the guarded `rev-parse --show-toplevel` above, per distinct `repo`
value, and N5 treats a candidate as one.

**The commit grammar (C-9, N4a; amended by A1, N4b-1).** A call is a commit
call only when the lexer places the command (Tokenization above, with the
literal commit-message form) and at least one segment is a commit segment.
Segments may be joined by `&&`, `||`, `;`, a newline, `|`, or `|&`, and the
command may end with a `;`. The command may start with a literal `cd DIR &&`
(which sets the candidate base as `repo` resolution says), and a segment of
exactly the three words `bash -c STRING`, `bash -lc STRING`, or `sh -c
STRING` (no redirect) is searched through its STRING, at most one level deep:
STRING must itself be placed and follow these same rules; a deeper wrapper,
or a STRING that is not placed, makes the call not a commit call. A commit
segment is a `git` segment whose only words before the subcommand `commit`
are `-C DIR` (any number, in order), `-c K=V`, `--git-dir=...`, and
`--work-tree=...`. Poisoning: if any commit segment carries `--dry-run`, `-q`,
`--quiet`, `--porcelain`, `--short`, `--long`, or `-z` (the last four imply a
dry run or suppress git's own summary, so a hook's forged line would be the
only match; an unambiguous abbreviation of a long one and a short-option
cluster containing `q` or `z` count too; words after `--` are pathspecs), the
call counts no id at all, even when another commit segment qualifies.
Everything else is an ordinary segment. No substring fallback: `commit_ids` is
empty for every call that is not a commit call.

Background: the call counts no id when a commit segment is backgrounded in
the text, that is, when the commit segment, the pipeline (segments joined by
`|` or `|&`) that contains it, or the whole `&&`/`||` list that contains it,
is ended by `&` (shell semantics: `&` applies to the whole and-or list, so
`git commit -m x &`, `git commit -m x | tail -1 & wait`, and `git commit -m
x && git push &` all count none), and when the call's completion is
`backgrounded`, `no_result`, or `ambiguous`. The completions that count are
exactly `completed_ok` and `completed_error` (an `is_error` result, or a
parsed nonzero exit code). For k commit segments, the ids are the last k
line-start matches of `^\[[^\]\s]+ ([0-9a-f]{7,40})\]` in the FULL result
text, in order (git prints its summary after any hook output), at most 20.
Not counted: a poisoned call, a backgrounded commit segment, a backgrounded,
missing, or ambiguous result, and a result with fewer matches than commit
segments. Summary lines git prints with a space in the branch part (`[main
(root-commit) abc1234]`, `[detached HEAD abc1234]`) do not match, as in v0.2.

**Calls with a commit command that reported no id (field milestone F, FA6).** A1's id grammar
deliberately rejects `-q` and `--quiet` (a quiet segment poisons the call's id
count), so it cannot see a quiet commit: `git commit -q` prints no summary line,
and an agent that committed read `0 commits`, an exact-looking zero. A second,
separate grammar fact in `commands.py`, `commit_command_ran(segment)`, recognizes
the command's presence (the name is historical: it says the text contains a commit
command, never that the shell reached it): a `git` segment whose subcommand is `commit` (the same global
options as the commit segment above: `-C DIR`, `-c K=V`, `--git-dir=`,
`--work-tree=`), INCLUDING `-q`, a short cluster containing `q`, and `--quiet`
(and its unambiguous abbreviations), and still excluding `--dry-run`,
`--porcelain`, `--short`, `--long`, `-z` (and abbreviations and clusters of
those; words after `--` are pathspecs). A1's id extraction is unchanged.
Placement is judged for the candidate commit segment and its containing and-or
list, not by the whole call's `Lexed.placed` bit: an expansion, substitution,
glob, assignment, or other opaque trigger in an EARLIER segment does not hide a
literal foreground `git commit -q` segment (`cd $WT && WT=$PWD zsh run_quick.sh x
| tail -4; git add a b; git commit -q -m msg` has one), while any opaque or
malformed syntax, quoting, or heredoc data in the candidate segment itself fails
closed (the lexer attributes each reason to the segment being built when it was
raised): `git commit -q -m "$MSG"` and an unbalanced quote are not counted, and
words inside quoted or heredoc data are never segments. The containing and-or
list must not be ended by `&` (the same backgrounding rule as above), and the
call must not be `run_in_background` or contain `nohup` (a background call
is classified row 2 and carries no such fact). The fact on the call's classification
is `commit_command_present`: the transcript cannot prove reachability from one
call's exit status (`git diff --quiet || git commit -q -m x` in a clean repository
and `false && git commit -q -m x; true` both complete OK without the commit having
run), so presence is all that is claimed, and every public sentence says
"contains a commit command", never "ran" or "printed". One level of
`bash -c STRING` is searched as above, the wrapper segment and the inner
candidate each judged the same way. The attempt fact is
`commit_commands_without_id` (4.1, 4.2): the count of shell CALLS, never
segments, with completion `completed_ok` that contain at least one such segment and
whose `commit_ids` is empty (two quiet segments in one call count one; a
`completed_error` call counts none; a call with a summary line has its id and
counts none).

What an id means: a commit id in `AttemptFacts.commits` is an **unverified,
summary-shaped output line** selected from a completed result whose placed
command text contains a qualifying commit segment. It is not attributed to
git, and nothing more is claimed: not that the commit segment ran, not that
it succeeded. An id can be false in every way text-only evidence allows: the
commit segment may never have run (short-circuited by `&&` or `||`, or
skipped by `exit`, `exec`, `return`, or any other control flow before it); it
may have run and failed; and any command, pipeline stage, hook, or what the
name `git` resolved to (an alias, a function, a `PATH` entry, the invisible
shell state) may have printed, changed, or reordered the selected line
(`git commit -m x && printf '[main deadbee] f'` reports `deadbee`; so does
`false && git commit -m x ; printf '[main deadbee] fake\n'`). N5's guarded
observation can at most show that an object with that id exists; that says
nothing about which program printed the line or whether this call created
the object, and no output ever claims more than "a summary-shaped line
appeared in the result of a command whose text contains a commit".

`repo` of a commit segment is the candidate of `repo` resolution above (C-10:
the segment's candidate base with its `-C` options applied in order).

### 4.13.2 Marker provenance (N1) (implemented in N1) (commits 31a2c24..7f3a643)

The reader builds typed `MarkerHit` records while parsing, after tool calls
are paired (4.2):

| Field | Meaning |
|---|---|
| `marker` | the canonical id from the marker registry below |
| `producer` | `harness_user`, `harness_tool_notice`, `command_output`, `runtime_assistant`, `codex_companion`, as the registry assigns |
| `event_line: int`, `tool_use_id: str \| None`, `result_line: int \| None` | identity (D8) |
| `anchored` | true when the registry's anchored predicate for that id holds on the permitted shape |
| `in_payload` | true when a fragment of the id occurs in a permitted shape but the anchored predicate does not hold (never evidence) |

**Text flattening.** For a `tool_result` block: when `content` is a
string, that string; when it is a list, the `text` of the first element
whose `type` is `text` (other elements are ignored); otherwise the empty
string. For a user or assistant entry: `message.content` when it is a
string, else the `text` of the first `text` block. Then strip leading
whitespace (spaces, tabs, carriage returns, newlines); no other
normalization. `line1` is the flattened text up to the first newline,
`line2` the next line (or the empty string).

**Marker registry (N1).** One canonical id per row; the predicate is
evaluated on the flattened text of the permitted shape only. A shape that
is not listed for an id is never a hit for it. The unit of evaluation is
one permitted block: each `tool_result` block of a user entry separately
(identified by its `tool_use_id`), or the whole text of a user or
assistant entry. Each (block, id) pair yields at most one `MarkerHit`.
A block is a candidate for an id when any of the id's fragment strings
(the constant or constants `markers.py` already defines for it; for
`shell_guard_block`, a match of `SHELL_GUARD_BLOCK_REGEX` anywhere) occurs
anywhere in its flattened text. A candidate yields one `MarkerHit` with
`anchored = true, in_payload = false` when the anchored predicate holds,
and `anchored = false, in_payload = true` otherwise. A block that is not a
candidate yields nothing. The anchored predicates were measured on the build
machine's transcripts: for every id, every real occurrence matched the
predicate, and every other occurrence of the same fragment was file content
printed by a command (a source listing or a grep), which the predicate
excludes.

| Canonical id | Producer | Permitted shape | Anchored predicate |
|---|---|---|---|
| `kill_sentinel` | `harness_user` | a `user` entry (not a `tool_result` block) | the whole flattened text, after also stripping trailing whitespace, equals one of `KILL_SENTINEL_VARIANTS` |
| `bash_backgrounded` | `harness_tool_notice` | a `tool_result` paired to a `Bash` tool_use | `line1` starts with `Command did not complete within its ` and contains `moved to the background` |
| `bash_timeout` | `harness_tool_notice` | same | `line1` equals `Exit code 143` and `line2` starts with `Command timed out after` |
| `tool_input_invalid` | `harness_tool_notice` | a `tool_result` paired to any tool except `StructuredOutput` | `line1` starts with `<tool_use_error>InputValidationError:` |
| `structured_output_mismatch` | `harness_tool_notice` | a `tool_result` paired to a `StructuredOutput` tool_use | `line1` starts with `Output does not match required schema`, or `line1` starts with `<tool_use_error>InputValidationError:` and contains `could not be parsed as JSON` |
| `classifier_unavailable` | `harness_tool_notice` | a `tool_result` paired to a `Bash` tool_use | `line1` contains `is temporarily unavailable` and `cannot determine the safety of Bash` |
| `git_not_repo` | `command_output` | a `tool_result` paired to a `Bash` tool_use | `line1` starts with `fatal: not a git repository`, or `line1` matches `^Exit code [0-9]+$` and `line2` starts with `fatal: not a git repository` |
| `sandbox_write_denied` | `codex_companion` | a `tool_result` paired to a `Bash` tool_use whose command runs codex as a program (4.3); or an assistant `text` block of an attempt that has such a call | some line of the flattened text starts with `[codex]` and contains `WRITE_DENIED` or `read-only sandbox`; or (tool_result shape only) `line1` starts with `WRITE_DENIED` |
| `shell_guard_block` | `codex_companion` | a `tool_result` paired to a `Bash` tool_use whose command runs codex as a program | some line matches `SHELL_GUARD_BLOCK_REGEX` from its first character |
| `codex_unavailable` | `codex_companion` | a `tool_result` paired to a `Bash` tool_use whose command runs codex as a program (4.3) or names `codex-companion.mjs` (the same shape as `shell_guard_block`; no assistant or user shape) | some line of the flattened text starts with `Codex CLI is not installed or is missing required runtime support`; the same sentence inside a line, or in an assistant entry or in the output of another command, is not a hit |
| `permission_denied` (G7, v0.3.1) | `harness_tool_notice` | a `tool_result` block with `is_error` true, paired to any tool (a result without `is_error` true yields no hit, not even a negative one) | the flattened text starts with exactly one of the four sentences below; no wrapper is accepted |
| `usage_limit_session`, `usage_limit_weekly` | `runtime_assistant` | the attempt's own final transcript entry, an `assistant` entry | the flattened text starts with the marker string |
| `machine_sleep`, `api_error` | `runtime_assistant` | same | the flattened text starts with the marker string |

The attempt's own final transcript entry is its last entry whose `type` is not
exactly `attachment`. Trailing `attachment` entries (any `attachment.type`) do
not count: since Claude Code 2.1.305 a configured SubagentStop hook makes a
subagent transcript end with an `attachment` entry of type `hook_success` after
the final `assistant` entry (163 of 205 such transcripts measured on 2.1.305 to
2.1.312). Any other entry type after the `assistant` entry, `user` and `system`
included, still makes it not final.

Negative hits (`anchored = false`) are kept for diagnostics and are never
evidence. Detectors consume a `MarkerHit` only when `anchored and not
in_payload`. A command that printed a file is not suppressed as a whole: if
`cat` itself was backgrounded, the notice on `line1` of its result is still
an anchored hit; the file's contents after it are payload. The result of an
`agent-postmortem`, `postmortem`, or `python -m agent_postmortem` command
never yields an anchored hit for any id except `bash_backgrounded` and
`bash_timeout` (the harness can background or time out the tool itself).
`markers.py` keeps the existing string constants and gains a `REGISTRY`
mapping each canonical id to its producer, permitted shape, and predicate
function; detectors import ids, never fragments. `Event.markers` (4.1)
stays for compatibility and is no longer read by any detector; every
detector that reads `Event.markers` today (4.3's table) migrates to
`MarkerHit` in N1.

**`permission_denied` grammar (G7, v0.3.1).** After the leading whitespace is stripped, the text
starts with exactly one of: `Permission for this action was denied by the Claude Code auto mode
classifier.` (kind `auto mode`); `The user doesn't want to proceed with this tool use.` (kind
`user`); `Permission for this command was denied by a built-in Claude Code safety check, not by the
user.` (kind `safety check`); or `Permission to use ` followed by a tool name matching `[A-Za-z0-9_]+` (so `mcp__x__y` counts; an empty name does
not), ` with command `, any text that may span lines, and ` has been denied.` where that last
period ends a line (the first such line; kind `permission rule`; a command of any length counts,
and so does a multi-line command, G-B1, GB-3; a text whose sentence is followed by more words on
the same line does not). Not counted: a result without `is_error` true;
the sentence anywhere but the start, `<tool_use_error>` wrapped, after `Exit code N`, or as part of
a Bash output or a file; `Blocked: sleep`; hook errors; the `granted`, `required` and `unavailable`
variants. A genuine denial of a read-only command counts: unlike the self-quote guard of 4.3, the
text here is the runtime's own reply to the call, so `read_only_output` is not consulted. One
`MarkerHit` per candidate block; the detector reads anchored hits and classifies each by the start
of the event's text. Evidence is up to 5 pointers `<transcript>:<line>`, printed with the code
`[permission_denied]`, never an excerpt: the permission-rule sentence carries the denied command,
which no level prints. The session-end hook does not run this detector (it is not in the eight
tooling detectors of 7.2) and it does not notify.

**N1 acceptance tests.** For every canonical id: one positive case and one
payload-negative case (the fragment inside file content printed by `cat` or
`grep`), each asserting the complete `MarkerHit` (all seven fields) and,
for the negative case, no finding. Plus one entry with two `tool_result`
blocks, one positive and one negative, yielding two hits with distinct
`tool_use_id`.

### 4.13.3 Run lifecycle (N1 parent index, N2 lifecycle) (implemented in N1 and N2) (commits 31a2c24..7f3a643)

**Launches and results (N1).** (implemented in N1.) A launch is an assistant `tool_use` block
with name `Workflow` and a string `id`. Its result is the `tool_result`
block, in a `user` entry, whose `tool_use_id` equals that id; matching is by
id only. The parent index reads the whole parent transcript once (with the
caching rule below) and builds, per launch: its line, id, `resumeFromRunId`
when the input carries a string one, and the line and `toolUseResult` of
its result.

**Association to the inspected run X (N1).** (implemented in N1.) Evaluated per launch, first
rule that applies:
1. Two or more `tool_use` blocks share the id, or two or more different
   `tool_result` blocks name it (results whose block and entry-level
   `toolUseResult` are identical as parsed JSON are one result, the first
   line standing for them): the launch is ambiguous. If any of the involved results
   carries `toolUseResult.runId == X`, or the launch's `resumeFromRunId` is
   X, X's lifecycle terminal becomes `unknown` with source
   `ambiguous_launch` and a diagnostic naming every line; otherwise the
   launch is ignored for X.
2. The result exists, `toolUseResult` is a dict, `runId` is the string X,
   and both `taskId` and `status` are strings: a complete generation of X.
3. The result exists and its `runId` is a string other than X: not X's.
4. The result exists but `toolUseResult` is missing, not a dict, or lacks
   a string `runId`, `taskId`, or `status`: if the launch's `resumeFromRunId` is X, an
   incomplete generation of X (diagnostic "malformed launch result at line
   N"); otherwise a parent-level diagnostic, not a generation of any run. The
   diagnostic also reaches X's discovery (4.13.1) when any of the launch's
   results has a string `toolUseResult.runId` equal to X (N4 fix, A5), even with
   no resume and a valid earlier generation of X; that adds no generation.
5. No result exists: if `resumeFromRunId` is X, an incomplete generation of
   X (diagnostic "launch without result at line N"); otherwise a
   parent-level diagnostic only. A result-less initial launch never belongs
   to any run, so it cannot affect runs it has no evidence for.

A launch whose one result is a rejected launch (the `tool_result` block has
`is_error` true and the entry's `toolUseResult` is a string; N4 fix, A4, A5) is
neither malformed nor result-less and builds no generation (rules 2 to 5 do not
apply). An initial rejected launch builds no diagnostic and involves no run. A
rejected launch with a string `resumeFromRunId` of X keeps the one diagnostic
"rejected launch result at line N" and it reaches X's discovery: an error result
is not proof that no run exists, so X is not complete.

Results are matched to a launch by id only, in either physical order: a
result's identity for the duplicate rule (rule 1) is computed for every result
with a string `tool_use_id` whether or not its launch precedes it (N4 fix, A5).

The "parent-level diagnostic" of rules 4 and 5 involves no run: the parent
index keeps it, but it reaches no run's `Run.discovery['parent']` (4.13.1,
A4). An incomplete generation's diagnostic (rules 4 and 5 with a resume) and
an ambiguous launch's diagnostic reach the run they involve.

Generations of X are ordered by launch line. `Lifecycle.generations` is
their count (0 when none). An ambiguous launch (rule 1) produces no
generation of any run.

An incomplete generation (rules 4 and 5) has: `launch_line`,
`tool_use_id`, `resume_from` from the launch; `run_id` X; `result_line` the
malformed result's line under rule 4, None under rule 5; `task_id` None;
`launch_status` None; `terminal` `unknown`; `terminal_source`
`incomplete_generation`; `terminal_line` None; `diagnostics` holding the
one diagnostic of its rule.

**Launch status (N1).** (implemented in N1.) `toolUseResult.status` is recorded as written.
`async_launched` (every launch observed on the build machine, 174 of 174)
is nonterminal. A result whose status is `completed`, `failed`, `killed`,
`stopped`, or `cancelled` (a synchronous launch) is terminal evidence with
source `launch_result`, mapped through the status vocabulary below. Any
other string: the generation's terminal is `unknown`, source
`unrecognized_status`, diagnostic "unrecognized launch status".

**Status vocabulary (N1).** (implemented in N1.) For notifications and launch results: `completed`
-> `completed`; `failed` -> `failed`; `killed`, `stopped`, `cancelled` ->
`stopped`. Any other notification status: terminal `unknown`, source
`unrecognized_status`.

**Notification delivery forms (N1).** (implemented in N1.) A terminal notification is accepted from
either delivery form (on the build machine 22 of 150 paired Workflow
terminal notifications exist only in the queue, anthropics/claude-code#88423):
- a `queue-operation` entry with `operation` equal to `enqueue` whose
  `content` is the envelope;
- a `user` entry whose `origin` is a dict with `kind` equal to
  `task-notification` and whose `message.content` is the envelope. A user
  entry without `origin` counts only when its physical line number is lower
  than the first line in the same transcript that carries `origin`.
Compaction summaries, attachments, assistant text, tool results, and every
other copy never count.

**Envelope (N1).** (implemented in N1.) After stripping surrounding whitespace, the content starts
with `<task-notification>`, ends with `</task-notification>`, and contains
exactly one each of `<task-id>`, `<tool-use-id>`, and `<status>`. It
counts for a generation only when its task id equals the generation's
`task_id`, its tool-use id equals the generation's `tool_use_id`, and its
line is after the generation's result line. The two delivery forms of one
notification (same task id, tool-use id, and status) collapse into one
record whose line is the earlier one.

**Terminal evidence per generation (N1).** (implemented in N1.) The evidence set is: accepted
notifications, `TaskStop` tool_use blocks whose input `task_id` (or
`shell_id`) equals the generation's `task_id` and whose line is after the
result line, and the launch result when it is terminal. Rules: an empty set
gives terminal, source, and line None; one value, or several equal values,
gives that value with the source and line of the earliest record; a
`TaskStop` followed later in physical order by a `completed` or `failed`
notification (and no other record) gives the notification's value, source,
and line; any other combination of different values gives `unknown`,
source `conflict`, and as line the first record (in physical order) whose
value differs from the earliest record's, and makes the run INCOMPLETE.

**Run lifecycle (N2).** (implemented in N2.) Parent inspection is complete
when the parent transcript is readable, has no undecodable line, and every
entry the index reads has this shape: a `user` entry has a dict `message`
whose `content` is a string or a list; an `assistant` entry has a dict
`message` whose `content` is a list; a `queue-operation` entry has a string
`operation`, and a string `content` when `operation` is `enqueue` -- except
that an `enqueue` entry whose key set is exactly `type`, `operation`,
`sessionId`, and `timestamp` (no `content` key, and no field of any kind that
could carry a notification) is benign and keeps parent inspection complete (N4
fix, M-N4-1 b; a bare queue marker the harness writes that carries nothing; any
other key set without a string `content`, whether it has an extra key or lacks
one of the four, is malformed as before); every
`tool_use` block has string `id` and `name`; every `tool_result` block has
a string `tool_use_id`. Any other shape among these entry types makes
parent inspection incomplete -- but no parent path at all, or a parent
transcript that simply does not exist yet, is complete vacuously (section
2/4.6's "unreadable, not merely absent" convention, applied the same way
here: a legacy run with no companion session transcript left on disk is not
thereby uninspectable). Then, from the newest generation N
of X:

| Condition | `terminal` | `source` |
|---|---|---|
| parent inspection incomplete | `unknown` | `parent_unavailable` |
| an ambiguous launch involves X | `unknown` | `ambiguous_launch` |
| N exists and is incomplete | `unknown` | `incomplete_generation` |
| N exists with terminal `completed`, `failed`, `stopped`, `unknown` | that value | N's `terminal_source` |
| N exists with no terminal, or no generation exists, and every journal key is resolved | `journal_resolved` | `journal` |
| otherwise | `none` | null |

An earlier generation's terminal never decides the run while a later one
exists. `activity`: `recent` when the newest mtime among the run
directory's files is under 10 minutes old, `stale` when older, `unknown`
when stat fails. Display words: `possibly running` (terminal `none`,
activity `recent`), `no terminal record` (terminal `none`, activity
`stale` or `unknown`). "ended", "abandoned", and "running" are never printed
from mtime alone. `Run.finished` is true for `completed`, `failed`,
`stopped`, and `journal_resolved`; false for `unknown` and `none`; null for
a session. A terminal `unknown` makes the run INCOMPLETE.

New detector `workflow_failed` (N2). (implemented in N2.) `major` severity,
workflow only; terminal `failed`; evidence the `terminal_line` as
`path:line` of the parent transcript (no excerpt: the line itself is a
structured handshake, not prose worth quoting). No rule id (section 9 names
none for it).

**Sessions (N2).** (implemented in N2.) `Run.lifecycle` and `Run.finished`
are null for a session. `activity` is `recent`, `stale`, or `unknown` (a
session carries no precomputed activity field the way a workflow's own
Lifecycle does; it is derived at render time from the session's own last
recorded event against the same 10-minute freshness rule). `unfinished_state`
is `possibly running` when `scan`'s own hook-free `live` heuristic says so,
else null -- 5.3b also allows a session row's own `no terminal record`, but
N2 does not derive it (a session has no per-run terminal concept to be
"recorded" or not the way a workflow's lifecycle does; this milestone claims
less rather than invent one). The header reads "last activity HH:MMZ (N
minutes ago)" always for a session, never "ended", plus "possibly running"
when activity is recent. An attempt still open in a workflow journal reads
END `no result` instead of whatever `completed` the transcript-only outcome
fallback would otherwise claim (4.7), never a transcript that already ended
in the kill sentinel: that is authoritative evidence of a kill and keeps
reading `killed`, not overridden to `no result` **(fixed after cross-model
review, N1N2 review finding 4: this summary sentence previously read as an
unconditional override, which 4.7's own fuller wording never claimed)**.

**Parent index cache (N1).** (implemented in N1.) Per process, keyed by the parent path. The
index stores: the byte offset just after the last newline it parsed
(`parsed_offset`), the SHA-256 of bytes `[0, parsed_offset)`, the inode,
and the file size it last saw (`seen_size`); the unfinished tail after
`parsed_offset` is never parsed. On the next access with the file at size S
and inode I:
- I differs from the stored inode, or S < `seen_size` (any shrink, even
  of the unfinished tail): rebuild from zero.
- Otherwise re-hash bytes `[0, parsed_offset)`; if the digest differs:
  rebuild from zero.
- Otherwise parse only the complete lines in `[parsed_offset, last newline
  before S)` and advance.
Hashing reads the prefix but does not parse it; parsing cost stays
proportional to new lines. After every access `seen_size` becomes S. The index
exposes two counters for tests, both 0 when the index object is created:
`lines_parsed` adds one per complete line parsed (the initial build
included), and `rebuilds` adds one per rebuild from zero after the initial
build (the initial build is not a rebuild). N1 acceptance tests: a
suffix-only append parses only the new lines; a rewrite inside the prefix
followed by an append rebuilds; a partial final line is not parsed until its
newline arrives; truncation rebuilds; replacement (new inode) rebuilds; a
same-size rewrite rebuilds. **(fixed after cross-model review, N1N2 review
findings 1/7)** The index also carries `inspection_complete` (the Run
lifecycle paragraph's own completeness flag below), updated inside this
same incremental pass rather than by a second, independent full read: a
per-line decode failure or `_entry_shape_ok` violation flips it False as
that line is parsed, and a read failure anywhere in this cache's own
rebuild/rehash/parse steps (a `Path.open` raising, for example a permission
error on an existing, previously-readable file) flips it False and leaves
the index's prior state otherwise untouched, exactly like the existing
"file missing or unreadable at `stat()`" branch already did -- `ParentIndex`
itself never raises for a parent read or decode failure. `_reset_state`
resets `inspection_complete` to True before a rebuild-from-zero reparses
the whole file, the same way it resets the other per-content accumulators.
**(fixed after cross-model review round 2, new finding 1)** A read failure
in any of those same steps also sets an internal, non-public flag that
forces the *next* access to do a full reinspection from byte 0, regardless
of what the ordinary inode/size/prefix-hash comparison above would
otherwise conclude. This matters because that comparison can only ever
detect a structural change to the file itself; a one-time, non-recurring
I/O hiccup against a file that never actually changed leaves nothing for it
to notice on the next access, so without this flag `inspection_complete`
would stay False for the rest of the process even once the file is readable
again. Persistent content validity (`inspection_complete` and every
per-line diagnostic, both scoped to bytes this index has actually read and
checked) stays separate from this transient I/O-availability flag: the flag
clears the moment a reinspection's own read succeeds, and
`inspection_complete` is restored to True only once that full reinspection
has checked every retained line again and found nothing wrong.
**(fixed after cross-model review round 2, original finding 1)** Per
section 2's "a bad line becomes a diagnostic string on the Run, never an
exception", every line that flips `inspection_complete` False also records
one stable diagnostic on the index's own `diagnostics` (folded into the
consuming Run's diagnostics the same way every other `ParentIndex`
diagnostic already is): `"<name>:<line>: undecodable parent transcript
line"` for a decode failure, `"<name>:<line>: malformed parent transcript
entry"` for an `_entry_shape_ok` violation. A read failure in the cache's
own rebuild/rehash/parse steps records one file-level diagnostic instead,
`"<name>: parent transcript unreadable"` -- a single value, not a growing
list, since the same ongoing failure recurring across refreshes is one
fact. None of these ever include the transcript's own text, only the
filename, a line number where the failure is line-specific, and the fixed
reason above.

**N1 and N2 acceptance tests.** N1 asserts complete `Generation` records
(all eleven fields) for: a single launch; a resume; a result-less resume;
a malformed result of a resume; a result-less initial launch (no
generation, parent diagnostic); a result with another run id (not X's);
duplicate launch ids and duplicate results (zero generations, the exact
parent diagnostic with every line, and in N2 lifecycle `unknown` with
source `ambiguous_launch`); a malformed `toolUseResult`; queue-only,
user-only, both-forms (deduplicated), quoted (ignored), mismatched task id,
and mismatched tool-use id notifications; a TaskStop then a later
notification; two conflicting notifications. N2 asserts the complete
`Lifecycle` (all five fields) for each row of the run lifecycle table, a
zero-generation legacy run (resolved and unresolved journal), an older
failed generation followed by an unfinished resume, and a session (null).

### 4.13.4 Severity inputs and rule table (N5) (implemented in N5) (commits fdcdc37..73149a6)

Severity becomes a function of recorded inputs, each with an explicit
unknown value, never a function of which detector fired alone. Inputs
(D3):

| Input | Values |
|---|---|
| `terminal` | `completed`, `failed`, `stopped`, `journal_resolved`, `unknown`, `none` (4.13.3) |
| `recovered` | `yes` (a later attempt of the same key completed with a result); `no` (the key's last attempt did not complete); `unknown` (the key's last attempt has no terminal record and the run is not terminal) |
| `work_risk` | `none` (the risk population is complete and every ledger in it is `complete` with no `observed`, `claimed_target`, or `attempted` entry, 4.13.1); `present` (an `observed`, `claimed_target`, or `attempted` entry exists); `unknown` (anything partial) |
| `obligation` | `none`; `prompt_path` (`missing_artifact`'s existing grammar: a verb in `{write, save, append, create, persist}`, then up to 80 characters, then an absolute path ending in `.md`, `.json`, `.txt`, or `.jsonl`, with no negation word in the 20 characters before the verb); `claimed_path` (a `report_path` claim in the attempt's own dict result) |
| `absence` | `present_now`; `absent_now`; `unknown` (the path is not checkable: not absolute, or the stat call failed) |
| `outcome` | `completed` (the attempt's `outcome` is `completed`); `other` (any other outcome) |
| `later_success` | `yes` (a Bash call running codex as a program, judged on the full command, after the attempt's first failing one has a result without the `codex_unavailable` marker); `no` |

`unknown` means a required input could not be established; `none` means not
applicable or affirmatively absent. **Consequence (D2).** Every filesystem
existence check the tool makes, CLI or Stop hook, is `observed_later`: the
file may have existed at the end and been moved or removed since, and the
tool never claims to know which. Nothing in v0.3 produces an end-time
snapshot (`observed_at_finish`); that value stays in the vocabulary,
reserved for a future end-time observer that needs its own contract
(4.13.10). Consequence: no detector produces `critical` severity in v0.3;
`critical` is reserved.

**Severity truth table (D3).** This table replaces the severity table that
previously stood here entirely. Every detector; any input tuple not listed
below is a contract error: severity is the highest row the detector has,
and the run's own status becomes `INCOMPLETE` (4.6). No row uses a time
threshold except the existing 300 s silence rule of 4.4, kept unchanged and
cited where it applies.

| Detector | Rule |
|---|---|
| `workflow_failed` | `terminal failed`: major |
| `attempt_failed` | `recovered no` or `unknown`: major; `recovered yes`: minor (a reported failure of a session subagent: `recovered unknown`, major) |
| `silent_retry` | `recovered no` or `unknown`: major; `recovered yes` and `work_risk present` or `unknown`: major; `recovered yes` and `work_risk none`: minor |
| `mislabeled_kill` | cause `runtime` or `unattributed` with silence >= 300 s (the existing 4.4 rule) and the attempt is the key's last attempt: major; the same cause and a long silence on a non-final attempt: minor (`silent_retry` carries the loss); `parent_stop`, `human`, or a short or unknown silence: info at any position (position never lifts a short or unknown silence above info; N5 fix 1) |
| `claim_contradiction` | the `tests`/`ok`/`ready`/`blockers`/`self_reported_failure`/`self_reported_blocked` clause: major; the `report_path` clause with `absence absent_now`: info (D2); `absence unknown`: info |
| `missing_artifact` | `obligation prompt_path` with `absence absent_now`: info (D2); `absence unknown`: info |
| `structured_output_failure` | `recovered no` or `unknown`: major; `recovered yes`: minor |
| `codex_unavailable` | `outcome completed` and `later_success no`: major (the attempt returned a result though its Codex call never started); `outcome completed` and `later_success yes`, or `outcome other`: minor |
| `codex_write_denied` | minor, one finding per attempt, `rule_ids` exactly `["R-CODEX-CONTRACT", "R-ASSERT-ARTIFACT"]` |
| `bash_backgrounded`, `bash_timeout`, `tool_json_error`, `classifier_unavailable`, `git_cwd_mismatch`, `permission_denied` (G7, v0.3.1) | minor |
| `wasted_time` | info |

An info-severity `missing_artifact` or `claim_contradiction(report_path)`
finding's title is "<path> is absent now; the tool cannot tell whether it
existed when the run ended" (D2), or "<path> could not be checked now; the
tool cannot tell whether it existed when the run ended" when the check could
not be made. The title is built from structured parts (F fix 7, W7-2; the
literal backquotes of fix 6b are gone, because a backquote is a legal
character of a file name): the `Finding` carries `title_parts`, a fixed
template with one `{path}` placeholder and the path value, and `title` holds
the same sentence with the real path (what `raw` prints). A renderer formats
the template once, with the path rendered by the level's path function (at
`raw` the real path; at `share` and `local` the registered pseudonym or, for a
path nothing registered, the one the free-text scanner gives it, without the
omission marker because the value is one path; so it is the same `path-N` as in
the finding's `metrics.paths` or `metrics.claims.report_path`), and never scans the result: the tool's own words
survive whatever the path holds (a space, a quote, one or several backquotes,
an operator). The text findings list, the bug report, the JSON `title` and the
text and JSON stories all render a finding's title this way. Rendering adds a WHAT TO DO `[look]` item
for it only when the run's own last activity is within 72 hours -- a
rendering hint driven by recency, never a severity change. Exactly one such
item exists however many findings qualify (N5c-1, 4.7 item 3 carries the
wording): grade `look`, text `<n> expected file(s) absent now: check whether
the run wrote them` (`1 expected file absent now: check whether the run wrote
it`), command `agent-postmortem run <run id> --verbose`, where `n` counts the
shown info-severity `missing_artifact` and `claim_contradiction` findings.
No path appears in it at any disclosure level. A run with no known last
activity gets no such item (nothing says it is recent), and an age of exactly
72 hours still counts as within.

`codex_write_denied` becomes one finding per attempt carrying both rule ids
(R-CODEX-CONTRACT, R-ASSERT-ARTIFACT, 4.3), always `minor`: a denial alone
does not show that a required write was blocked; when a required output
actually is missing, `missing_artifact` or `claim_contradiction` carries
the severity instead, never `codex_write_denied` itself.

**Verdict consequence (D2; 4.6 carries the full rule).** `FAILED` when the
run's own lifecycle terminal (4.13.3) is `failed` or any finding is
critical; `DEGRADED` when any finding is major and the run is not `FAILED`;
`CLEAN` otherwise. Since no detector above ever reaches `critical`,
`FAILED` in practice follows only from a failed lifecycle terminal -- this
is what keeps `FAILED` reachable and honest: it means the harness itself
reported the run failed, not that some detector's severity table happened
to say so.

One table in 4.3 (the existing detector table) is extended in N5b to hold
every detector's own severity inputs and rule alongside its existing row
(`severity.py` holds the same rows in code); tests pin each row and each
`unknown` branch, and a mutation script kills a change to any of them.

### 4.13.5 Disclosure levels (N3) (implemented in N3) (commits 2082790..f7f5ae9)

| Level | Names | Paths | Evidence | Structural redaction |
|---|---|---|---|---|
| `share` (default, always) | stable aliases: agent `a:<alias>`, session `s:<alias>`, project `project:<alias>` (an alias is at least 12 hex, from the registry below); label `agent-N` and phase `phase-<n>` per render | `path-N`; a reported commit id prints as `commit-N` | pointers `file:line` with aliased file names and marker codes, no excerpt | on |
| `local` (per invocation, `--local`) | real description, label, phase title, project directory name, agent id | repo-relative real; outside both the repository and home, `path-N`, never absolute (D10) | excerpts with `--evidence` | on |
| `raw` (per invocation, `--raw`, alias `--no-redact`) | real | absolute | excerpts with `--evidence` (D10: `raw` is not an automatic excerpt) | off |

- **Commit ids (M9).** A short hash of a public repository's commit can identify the
  repository and its author, so at `share` every reported commit id prints as `commit-N`, numbered
  per rendering in order of first appearance, the same id the same `N` within one output, in text
  and in JSON alike (JSON keys unchanged). The sinks are `rescue plan` (the COMMITS line, the
  `commits reported` brief line, `units[].commits`, `brief.already_exists.commits`) and the `run
  --json` attempt `facts.commits`. A result's own commit list (the `commits` claim the reader
  keeps from a dict result) is a sink too: `reported.claims.commits` in `run --json` and in
  `rescue plan --json`, the REPORTED line of `rescue plan`, and, when the result also has
  `ready: false`, `findings[].metrics.claims.commits`; the keys stay, only the ids change.
  Numbers follow first appearance in the output: `rescue plan --json` exports the units before
  the brief, so the first `commit-N` in it is `commit-1`. Counts stay. `local` prints the real id scrubbed like a name (a value that holds a path or a
  control character is scanned as it was before M9, S1); `raw` prints it unchanged. A commit list is
  rendered through the mapping exactly once and is never rescanned as a path, so a written file
  literally named `commit-1` stays a `path-N` and the commit stays `commit-1` in text and JSON (A1). The number
  comes from the same per-render pseudonym mechanism as `path-N` (the `commit` category of the
  Pseudonyms of 5.1).

- **Names at `local` (N3b S9, C3).** A real label, description, phase title,
  agent type and project name print as written except that every C0 control
  character (and DEL and C1) becomes a space, an absolute path inside the name
  goes through the P19 path function, and a `~/...` path collapses to
  `~/.../<basename>`. Each fully assembled local line (the AGENT table row, the
  bug-report reproduction line) is scrubbed again as a whole, and a finding
  title or story at `local` carries no control character.
- **Path spellings at `local` (N3b S11).** The P19 path function collapses a
  leading `//` to `/` before the containment check, treats a `scheme:` prefix
  (`file://...`, `https://...`) as outside the repository and home (`path-N`),
  expands `~` and `~/...` against the context's home before the rule, and shows
  the home directory itself as `~`.
- **Duplicate evidence lines (N3b S13, R2-3).** At share and local, within one
  finding, one story or one bug-report finding, a rendered evidence line
  identical to an earlier one is not repeated. raw keeps v0.2's count and
  order and never deduplicates. First order is kept; a story still shows up to
  two distinct lines. Two entries that differ only by an excerpt a level does not print
  render identically and so show once.
- **Share invariant.** `share` is the effective default for every command
  and every sink, with no configuration (4.10) that changes it. Hook
  messages (7.2) are always `share`; there is no path by which a hook can
  emit `local` or `raw`.
- **Alias registry (D10, folded from 4.13.11 in N3c).** Three identity
  kinds have stable aliases: `agent` (the 17-hex agent id), `session` (the
  session UUID), and `project` (the project directory name under the
  projects root); a phase and a label never do (`phase-<n>` numbers the
  phase's position in the run, `agent-N` the label, both per render as in
  5.1). An identity's alias is a prefix of HMAC-SHA256 of its real id
  under an installation secret (32 random bytes, `secret` in the state
  directory, 7.5). The registry `aliases.json` beside it records, per
  kind, the full HMAC (never the real id) and the prefix length assigned:
  the first time an identity is rendered it gets the shortest prefix, at
  least 12 hex characters, that no other registered identity of the same
  kind holds, and that assignment never changes. A recorded alias therefore
  never depends on what a later output contains; a first assignment is
  deterministic from the registry as it stood and the registration order
  (in a forced prefix collision, the identity registered first keeps the
  shorter prefix). A person can follow one agent's alias across `run`,
  `rescue`, `watch`, and hook messages, and one project's alias across
  `scan --all` and `run` (`rescue` text names no project); nobody without the secret can reverse
  an alias or match it to a transcript file name. Equal aliases across kinds are
  allowed. Validation, initialization, the lock, and the bounds are 7.5's
  (implemented in N3a).
- **Registration (P9, N3b).** A one-shot command collects every identity
  its text may show and registers them in one locked call before
  rendering; an identity the renderer meets that was not collected takes
  one more locked registration (the "slow path") and is counted, and tests
  hold that count at 0 over the demo and every fixture. Registration
  happens only at `share`: `local` and `raw` print real names and register
  nothing. A command whose text shows no identity (`scan` triage, `rules`
  in every form, `trend`, `apply`) register nothing and never print the
  diagnostic below. From N5a-1 a versioned `--json` output registers at
  `share` exactly as the text of the same command does (the same collected
  identities, the same guarded helper), and prints the diagnostic below on
  stderr under the same conditions; `local` and `raw` register nothing.
- **Fallback (D10, P8, N3b).** When the secret or the registry is
  unavailable (7.5), the lock is contended past its timeout, or a needed
  write fails, `share` names identities by per-render positional
  pseudonyms (`agent-N`, `session-N`, `project-N`, numbered per kind in
  first-use order) instead of failing or ever printing a real id. A
  one-shot command renders its whole output first and, if its book became
  unavailable while rendering (a slow path failed), renders it once more
  entirely positionally, so one output never mixes stable and positional
  names; it then prints "agent-postmortem <command>: stable aliases
  unavailable" once on stderr, never on stdout. `watch` treats one event
  as one render (C-1): an agent first seen after a failed registration is
  positional from that event on, earlier lines stand, and the single
  stderr line is "agent-postmortem watch: stable aliases unavailable"
  (C-6, 4.12), in text and `--json` mode alike. A hook appends
  "(stable aliases unavailable)" inside its own JSON message (P17, 7.2).
  Recovery is manual (7.5): remove both `secret` and `aliases.json` to
  start over with new aliases.
- **Rendered forms and evidence pointers (P10, P16, N3b).** At `share` an
  agent id renders `a:<alias>`, a session id `s:<alias>` (the run id
  `s_<10 hex>` is unchanged: it carries no user text), a project
  `project:<alias>`. Evidence pointers keep `file:line` with the file name
  aliased: `agent-<alias>.jsonl`, `agent-<alias>.meta.json`,
  `session-<alias>.jsonl`, `session-<alias>.meta.json` (bare alias hex, no
  `a:`), `journal.jsonl` unchanged; a pointer whose file names no canonical
  identity, or an evidence string that does not parse as a pointer, prints
  a numbered `evidence-N` placeholder. When the evidence came from a marker
  (4.13.2) the marker code follows in brackets (`[kill_sentinel]`). No
  excerpt prints at `share`, with or without `--evidence`; `--evidence` at
  `share` prints one note line instead ("note: excerpts need --local or
  --raw; the default level prints pointers and marker codes only"). In a
  fallback render the file stem is the positional name (`agent-3.jsonl`):
  it numbers agent ids, that is attempts, in first-use order, and is not
  the AGENT table's per-label `agent-N`, so `agent-2.jsonl` can belong to
  the label shown as `agent-1`. An attempt whose `started` event has no
  canonical agent id (C-5, 4.6) is named positionally, never by its label
  or an HMAC of an invalid value, and has no pointer at all: its transcript
  is never opened.
- **Project slugs (P11).** At `share` the run's own project slug is
  replaced by `project:<alias>` before structural redaction; any other
  slug that reaches text keeps the structural `project:<sha1[:6]>` form of
  5.1 as a backstop.
- **Journal keys (P10b).** At `share` and `local` a diagnostic or evidence
  text never quotes a journal key; it says `key@<line>` (4.6). `raw` quotes
  the key as v0.2 did.
- **Levels and flags (P12, P14).** Every command that takes `--no-redact`
  (`scan`, `run`, `rules`, `rules propose`, `rules review`, `apply`,
  `trend`, `rescue plan`, `rescue status`) also takes `--local` and
  `--raw`; `--no-redact` is an alias of `--raw`; any two of the three
  together are a usage error (exit 1). `--local` with `--json` is valid
  from N5a-1 (it was a usage error until schema 3). `watch`, `demo`, and
  every `hook` subcommand take no level flag and always render `share`.
- **Paths at `local` (P19, D-n).** A path is made absolute against its
  documented base (the run's cwd for a relative transcript path) and
  normalized with `os.path.normpath`, without resolving symlinks. It is
  inside the repository when `os.path.commonpath([repo_root, path])` equals
  the repository root (a `ValueError` means outside): inside, it prints
  repo-relative; inside home but outside the repository, `~/.../<basename>`;
  outside both, `path-N`; never absolute. The repository root comes only
  from an observation the command already makes (`run --observe`,
  `rescue plan`, `rescue status`); `local` never runs git on its own, and
  without an observation every path counts as outside.
- **First-line label (P15).** A command run with `--local` or `--raw` that
  prints text on stdout prints, as the very first line of that stdout,
  "disclosure: local (real names and paths; do not paste)" or "disclosure:
  raw (real names, absolute paths; do not paste)"; a command run at the
  default `share` level prints no such line, since `share` carries nothing
  that needs the warning. **(N3c)** A command that writes only an error on
  stderr prints no label: at `local` its error already uses the `share`
  replacement (P13), and at `raw` an error prints as v0.2 did (it can name
  the default root, which the person did not type); the label is a
  stdout-text rule only. **(D10)** The warning is text-only and never a line of `--json`
  output. Schema-3 JSON (N5a-1) names the level in its top-level
  `disclosure` field (5.3b), so a JSON consumer branches on that field,
  never on a prose line that would corrupt the payload.
- **The /rescue skill** runs `rescue plan --local --evidence` (never
  `--no-redact`, which Claude Code's auto-mode safety classifier blocks),
  and its own text states that this puts real names, paths, and the task
  prompt into the session transcript on purpose, because carrying out the
  rescue needs them to act on.
- **`rescue --evidence` (N3b, D-o).** `rescue plan` and `rescue status` take
  `--evidence`, which combines with every level flag and with `--json`. At
  `share` it prints the same one note line as `run --evidence` and nothing
  else changes. At `local` and `raw` the plan text quotes the TASK prompt and
  the status text shows the NEVER-list command excerpt as v0.2 printed them
  under `--no-redact` (`local` through structural redaction and the P19 path
  rule). With `--json` the same flag decides whether the excerpt keys exist
  (5.3b: `task.text`, `not_counted[].excerpt`, `never_matches[].excerpt`, at
  `local` and `raw` only). Without the flag no excerpt prints or exists at
  any level.
- **Paths in tool-built text (N3b, Part 3).** Text the tool builds from a
  path it did not read out of a run (a config warning, `trend`'s state-file
  error, the hook template location in `apply`'s note and in `rules --show`)
  goes through structural redaction, and any absolute path that leaves
  (outside home and `/tmp`) then prints as `path-N` at `share` and by the P19
  rule at `local` (outside home and the repository: `path-N`), never
  absolute; a path inside home keeps its `~/...` form; `raw` prints the real
  path. The hook debug tracebacks (`AGENT_POSTMORTEM_DEBUG`, stderr only) use
  the same rule. A path the tool itself knows (the config file and each
  `[[repos]]` path in a config warning, an `OSError`'s file name in the
  state-file error, the hook template path) is redacted as one value before
  it is joined into the sentence, so a space in it leaks no tail. For free
  text (a transcript excerpt at `local`, exception text, a rescue excerpt, a
  declared check at `local`) the one scanner of 5.1 finds a path inside a
  complete shell word (quotes, escapes, a backslash-newline continuation,
  adjacent quoted fragments, typographic quotes) and renders the whole rest of
  the word as one path, so no private segment survives a quote or an escape;
  a run after `:` or `-` counts only with two segments or a file extension, so
  `and/or` and `read-/write` stay prose. An unquoted path with a space and no
  registered value is cut at the space, as a shell would. The `/tmp` collapse
  begins only at a path token (not inside a longer path) and never crosses
  whitespace.
- **Error rendering.** An error message renders its own offending argument
  through the active disclosure level -- a path becomes `path-N`, a UUID
  becomes `uuid:<last 4>` -- and separately says what kind of argument was
  expected, so an error at the default level never echoes a raw path or id
  back.
  Handler-level errors use the same replacement (N3b Part 2): "no run
  matches" (exit 2), an ambiguous prefix (exit 1, candidates listed by run id
  when it has a run id's shape), "not a run" (exit 2), "no runs found under
  <root>" (exit 2), a bad `--since` date (exit 1), and an unknown rule id
  (exit 1) print fixed text around spans of what the user supplied (`path-N`,
  `uuid:<last 4>`, `agent-N`, `value-N`, numbered across one command) and
  say what was expected, at `share` and `local`; at `raw` they print as v0.2
  did.
- **First-run empty state (0.3.4).** "No runs found under <root>" is the first
  thing a stranger on a fresh machine sees, so it says where the tool looked,
  what it reads and what to run next, and keeps exit 2 ("no runs or not a
  run", 4.6), so a script still tells "nothing to judge" from "all clean".
  Its first line is `postmortem: no runs found under <root>`, with ` (it does
  not exist yet)` when the root is missing; three fixed lines follow at every
  level: what it reads (Workflow runs, and sessions with the subagents they
  launched), `next: agent-postmortem demo`, and that `--root` or
  `CLAUDE_CONFIG_DIR` points elsewhere and Claude Code deletes transcripts
  after 30 days by default. The standard location `~/.claude/projects` is
  the same on every machine and not private, so every error names it
  home-relative at `share` and `local` (absolute at `raw`); any other root,
  typed or set through `CLAUDE_CONFIG_DIR`, stays `path-N` there. `scan`,
  `trend` and `rules` print it; `run`, `rescue` and `rules --run` print it
  instead of "no run matches" or the `--here` message when the root holds no
  run candidate at all (`reader.has_run_candidates`: a workflow run directory
  or a UUID-named session transcript, found by glob; it never opens a file, so
  the error path cannot fail on a transcript's content). `run --here` with
  runs elsewhere adds one line pointing at `scan`. A `scan` of both kinds
  that found no subagent transcript and no Workflow run adds one line under
  "nothing needs attention": "this scan found no subagent transcript and no
  Workflow run, which is what agent-postmortem checks; agent-postmortem demo
  shows a sample report". It states only what the scan observed (a launch
  whose transcript is missing is not "no launch"), and a scan limited with
  `--kind` never prints it. `watch`'s 60 s timeout says it follows a
  Workflow run while it is running and points at `scan`.
- **What scan could not read (0.3.4).** Discovery reports a UUID-named
  transcript whose every line failed to parse (it is not a run, but it is not
  silently gone either). After its text output, `scan` prints one line when
  anything was skipped: `SKIPPED       <n> unreadable line(s) in <m> run(s)
  (<run ids>), still scanned; <k> transcript file(s) with no readable line;
  <j> agent meta file(s) that could not be read; <i> run(s) that could not
  be read (<run ids>); agent-postmortem run <run-id> --json lists each line
  under diagnostics`, each clause only when nonzero, at most 3 run ids then
  `and N more`, no path. With `--json` the same line goes to stderr and the
  JSON document is unchanged. A run that cannot be read at all
  (`RunUnreadable`, the last guard; every parse site already skips such a
  line) is counted there and makes scan exit 4, like an INCOMPLETE run, also
  when no other run is left to show (the empty state names it; `scan --here`
  adds the left-out line below). `rules`, `rules propose`, `rules review`,
  `trend` and `run --here` leave such a run out and say so on stderr:
  `postmortem: <n> run(s) could not be read and was/were left out (<run
  ids>)`. A command asked about that one run (`run`,
  `rules --run`, `rules propose --run`, `rescue`, `watch`; an `agent-<id>` or
  `a:<alias>` whose candidate run cannot be read still resolves to it) prints
  `postmortem: <run id> could not be read (its data is nested too deeply to
  follow); nothing was judged` and exits 4. A transcript file with no
  readable line does not change the exit code. The empty state adds `<k>
  transcript file(s) there had no readable line (not JSON, or nested too
  deeply)` when discovery found one. Each run's skipped lines are in its
  JSON diagnostics.
- **Bug report.** At `share`, `--bug-report` carries pointers and marker
  codes only, never excerpts, and its closing note says so (5.2).
- **`apply` (P18, C-4).** At `share`, `apply` prints no byte derived from
  the target file: the target as `path-N`, the rule's canonical directive,
  and a note that the patch needs `--local` or `--raw`; `local` prints the
  diff with a P19 header, `raw` today's `git apply`-able diff (9).
- **Hooks (P17).** A hook always renders `share`. `hook check` names the
  agents behind its findings by alias (at most three, then `+N more`), the
  one hook output that carries an identity; the other hooks name only run
  ids and register nothing (7.2).
- **Versioned JSON (C-3, D-p; retired in N5a-1).** Between N3b and N5a-1,
  versioned schema-2 JSON carried raw agent ids, the one known exception to
  the share invariant, and the release gate kept it from shipping. Schema 3
  (5.3b) carries aliases at `share`, so there is no exception: every
  versioned JSON output obeys the share invariant, and `--local --json` and
  `--raw --json` print real values only at their own level.
- **`demo` (D-i).** `demo` renders `share` with a fixed, public demo
  secret and an in-memory registry: it never reads or writes the state
  directory, never takes the lock, and never prints the diagnostic, so its
  text (and the README block, 8.2) is the same on every machine.

### 4.13.6 Identity (N6) (implemented in N6) (commits 9f0cdec..6d930f8)

- `scan` rows: a project column, rendered `project:<alias>` in `share`
  (4.13.5's registry alias, at least 12 hex), the real directory name in
  `local`. (Done in N3b for `scan --all` text.)
- **`--here` precision (D18).** `scan --here` and `run --here`: the run's
  own cwd set is every top-level string `cwd` value, non-empty, on every
  JSON object line of every transcript of the run: a session's main
  transcript and every child attempt's transcript, or a workflow run's
  attempt transcripts. It never includes the journal, a meta file, or a
  workflow's parent session transcript. A line that does not parse, a
  nested `cwd`, an empty string, and a value that is not a string
  contribute nothing. Containment is lexical, after
  `os.path.normpath` of both the caller's own cwd and each candidate cwd,
  with no symlink resolution (a relative candidate is first made absolute
  against the process's working directory, as 4.13.11's N6 item says); an
  entry without a `cwd` contributes nothing
  to the set; a run whose own cwd set is empty never matches `--here`, for
  either command. A run matches when the caller's own cwd equals, or is
  lexically inside, any member of the run's cwd set.
- **`run --here` selection (field milestone F, FB1).** One normative tuple,
  referenced by 4.8. Among the runs `--here` matches: (1) when the environment
  variable `CLAUDE_CODE_SESSION_ID` is a canonical UUID and a matching session
  run's transcript stem equals it (compared in lowercase), that run: the
  caller's own session; (2) otherwise the run with the most recent activity,
  the maximum of (an activity time exists, the activity time, the run id, the
  path), where the activity time is `ended_at`, or `started_at` when there is
  no `ended_at`. A workflow run is never the caller's own session. The
  variable is a hint, not a security boundary: it only chooses among runs that
  already matched the caller's directory. When more than one run matched, one
  stderr line, at every disclosure level, before the report:
  `postmortem: <n> runs worked here; showing <id> (<your current session | most
  recent activity>); also: <id> (<age>), ...`, listing up to three others in
  the same order, ages abbreviated as `scan` does (`<n>m|h|d ago`, or
  `unknown`); run ids are printed as the id-shaped values `scan` prints and
  never a directory, a path, or a session UUID. With one matching run there
  is no line. Stdout is identical to `run <selected path>`.
- **The caller's own workflows (G5, v0.3.1).** One shared resolver
  (`reader.caller_workflow_paths`) serves `run --here` and `scan --here`. When
  `CLAUDE_CODE_SESSION_ID` is a canonical UUID, exactly one session run under the
  root has a transcript stem equal to it (compared in lowercase), AND that session
  run itself passes the containment test above for the caller's directory, the
  workflow runs in that session's own directory (`<project>/<session>/subagents/
  workflows/wf_*`) join the set of runs `--here` matches, whatever their own cwd
  sets hold. In `run --here` they count in the `<n> runs worked here` line and
  appear in its `also` list (the own session is still the selection); in
  `scan --here` they are rows, with `--kind workflow` too. With the variable
  absent or malformed, naming no session or two sessions, or naming a session
  that does not itself work here, nothing changes. Another session's workflows
  never join. The variable stays a hint: it adds only runs owned by a run that
  already matched the caller's directory.
- `run` accepts a session UUID, a transcript stem, or an agent alias
  (4.13.5) as its `RUN_ID` argument, in addition to every form 4.8 already
  accepts. **(D18)** Resolution is across every run under the root: an
  agent alias that matches more than one run is an error listing the
  candidate run ids; an alias matching no run is exit 2, the same as any
  other unresolved `RUN_ID`.

### 4.13.7 Performance (N7) (implemented in N7, step 1 only; step 3 deferred past v0.3 by the owner's decision C, 2026-10-05) (commits 67f231a..2975843)

Measured under the profiler on this machine: `scan` over 197 runs took
22.6 s, of which 14.4 s was transcript parsing and 4.5 s was detector
discovery repeated once per run. Steps, in order:

1. Discover detectors (`detectors.all_detectors()`, 4.3) once per process
   rather than once per run; re-profile after. **(Implemented in N7.)** The
   sorted list is cached in the module on the first call, each call hands out
   a copy, and `detectors._reset_registry()` is the documented test hook that
   clears it.
2. **(D9)** N7 step 2 is a measurement, not work: the parent index's own
   fingerprint and incremental-read behavior is specified and owned by N1
   (4.13.3's "Parent index caching"), never duplicated here. This step is
   re-profiling scan's cold time with N1's fingerprint already in place,
   to see whether step 3 is still needed.
3. Deferred past v0.3 (owner's decision C in N7-plan.md, 2026-10-05: a cold
   scan of 238 real runs takes about 26 s, but a safe cache is a full
   subsystem and a defect in it shows a stale verdict without any sign; the
   targeted cold-path work C names, such as parsing a parent transcript once
   instead of twice, is a follow-up after the v0.3 field round). The design
   it would follow: only if `scan` is still above 8 s cold after steps 1 and
   2, add a semantic run cache, separate from hook state (7.5) with its own lock,
   keyed by every input that can change a run's own analysis (journal,
   every transcript, meta, parent transcript -- each by `size, mtime_ns,
   ctime_ns, inode` -- plus the parser, detector, lifecycle, and cache
   schema versions, and a hash of the active config). Only terminal runs
   (`Run.finished`, 4.13.3) older than 72 hours are cached; rendering by
   disclosure level (4.13.5) always happens after retrieval, never cached
   itself. Tests: append, replacement, truncation, same-size rewrite,
   parent change, config change.
4. No process pool in v0.3.

Performance tests assert operation counts (for example, transcripts parsed
on a warm `scan` is 0 for every cached run) rather than wall-clock seconds;
wall-clock time is reported by the corpus check (4.13.9) but never gates a
test.

### 4.13.8 Smaller items (N8) (implemented in N8) (commits 2672ecc..87b4cca)

- `rescue plan` (4.11): units and the relaunch brief are built from the
  unit's own ledger (4.13.1); `STEPS` names no `<none>` command; the
  fan-out caveat is printed only for workflow runs where two attempts of
  different keys overlapped in time; the path summary (`PATHS`) counts
  every ledger state, not only the four `observe.py` states. N8b (K4) fixes
  the two N5 release blockers: `rescue status` with no unit follows `rescue
  plan`'s sentence and exit rule (4.11), and each PATHS surface has its own
  population and wording (4.7: the AGENT column, `N+` for a partial ledger;
  5.3b: scan `paths_at_risk` over the risk population with
  `paths_at_risk_complete`; 4.11: the brief's `already exists` is confirmed
  writes only).
- `mislabeled_kill` (4.3/4.4) names an observed precursor without claiming
  the cause: "after a Bash command was moved to the background (line N)",
  rather than asserting that the backgrounding caused the kill.
- `WHAT TO DO` (4.7): no `[look]` step is printed when every hidden
  finding is `info`; the heading itself is never printed empty.
- `watch` (4.12), exact `START`/`OK` text and shape (D18): `START` --
  `START  <run id>: <A> agents started so far, <T> attempts.` printed once,
  first, as soon as the journal is found; JSON `{"event": "START", "run":
  str, "agents": int, "attempts": int}`. `OK` -- `OK     <agent> attempt
  <n> returned a result.` once per closed attempt whose outcome is
  `completed`, subject to the existing 60 s per-agent rate limit (4.12) and
  retried like `KILL` when suppressed (4.12's retry rule); JSON
  `{"event": "OK", "agent": str, "attempt": int}`. Ordering within one poll:
  `KILL`, `FAILED`, `OK`, `DONE`, `QUIET`. `DONE` fires from lifecycle terminal
  evidence (4.13.3) at once, rather than waiting out the grace period; the
  grace period (`--done-grace`) applies only to a `journal_resolved`
  terminal, never to an accepted notification, a `TaskStop`, or a synchronous
  launch result. N8b (K5) implements this with the lifecycle evaluated from watch's own
  occurrences, a stat-signature gate on the parent index, journal epochs on RESET, and DONE
  held back until every detected KILL, FAILED, and OK transition has resolved (4.12). 5.3b lists
  both `START` and `OK` alongside watch's other events.
- Rules (section 9): an `[[adopted]]` rule shows "adopted <date>; still
  violated N times in M runs since" instead of being proposed again by
  `rules propose`; `R-NO-LONG-BASH` gains the macOS note (no `timeout(1)`;
  a `python subprocess` or `perl alarm` form instead); a new target kind
  `"agent prompt"`; `rules propose --run <id>` scopes proposal to one run.

### 4.13.9 Verification (synthetic release gate implemented in N9; the advisory corpus check run with the owner's authorization on 2026-10-04 and 2026-10-05) (commits f58f8b2..7fa6516)

Release gate: synthetic fixtures and deterministic fake-git boundary
tests. Each field-verified case from the triage becomes a fixture named
after it: `field_shell_writes_invisible`, `field_git_c_commit`,
`field_self_quote_backgrounded`, `field_cat_backgrounded_still_counts`,
`field_workflow_failed_reported_clean`,
`field_failed_generation_completed_attempt`,
`field_resumed_run_unfinished`,
`field_resume_after_failure_possibly_running`,
`field_queue_only_notification`, `field_quoted_notification_ignored`,
`field_conflicting_notifications`, `field_parent_same_size_rewrite`,
`field_readonly_codex_denial`, `field_session_brief_other_agents`,
`field_live_session_wording`, `field_unknown_mcp_tool_partial`,
`field_first_agent_before_launch_result` (added by the field milestone F, FA2).

**Two tests per field case (D14/D17).** Where both levels apply, each field
case gets two tests, not one: a model-level test, at the milestone that
introduces the model fact, asserting the exact model values the contract
names (`Run.lifecycle` with `terminal`, `source`, `activity`,
`generations`; `Run.risk_population_completeness`; `Attempt.ledger` with
entries `{effect, basis, completion, path, repo, line}`, `completeness`,
`reasons`; `Attempt.marker_hits`; the parent index's own accepted
generations) -- never a looser proxy such as "one English line no longer
contains the word none", which an arbitrary wording, or a run marked
globally `unknown`, could also satisfy; and a public test, labeled N5,
asserting the schema-3 JSON first (the exact lifecycle/ledger/severity
shape), then the text consequence, and, for a detector case, the exact
parent `path:line` evidence section 2 requires. DESIGN 4.1 names the v0.3
model fields for exactly this reason: so a model-level test binds to the
contract itself, never to an accident of one implementation's own internal
shape.

**`test_field_session_brief_other_agents` (D15).** Built on an on-disk
synthetic session, invoked through the real CLI entry point --
`cli.main(["rescue", "plan", <path>, "--json", "--raw"])` -- never by
constructing `AttemptFacts` directly or calling `observe_run`/`build_plan`/
`render_plan_text` in isolation, which would pin today's internal helper
design rather than the command's own contract. Agents X and Y each get
their own distinct sentinel path; the test asserts that Y's own unit
contains Y's path and does not contain X's path (a positive assertion for
Y, not merely the absence of X). Labeled N5.

**`test_field_readonly_codex_denial` (D15).** Asserts one public JSON
finding with severity `minor`, `rule_ids` exactly
`["R-CODEX-CONTRACT", "R-ASSERT-ARTIFACT"]` in that order, and the exit
code that a run with only this finding produces -- exit 0, since
`codex_write_denied` is always `minor` (4.13.4).

**New tests, one per triage clause not yet covered by the fixtures above
(D16):** a `cat` payload case where ordinary output precedes the marker
(negative: not anchored, never a hit) alongside a harness notice at byte
zero (positive: anchored); cross-command alias stability (the same agent's
alias is identical across `scan`, `run`, and `rescue`, 4.13.5); per-sink
share output -- `run` text, `run --json`, `scan`, `rescue plan`, `rescue
status`, `watch`, `rules review`, and hook messages -- never containing a
real label or path; the `/rescue` skill's own invocation uses `--local` and
never `--no-redact`; operation-count performance (a warm `scan` parses zero
transcripts for every cached run; detector discovery runs exactly once per
process, 4.13.7); a live workflow attempt shows `END no result`; `KILLED`
excludes parent stops and human interrupts; no `<none>` command appears in
any rescue step; the fan-out caveat is printed only when two attempts of
different keys actually overlapped in time; no `[look]` item is printed
when every hidden finding is `info`, and the `WHAT TO DO` heading is never
printed empty; `mislabeled_kill`'s precursor wording (4.13.8); `watch`'s own
`START` and `OK` lines (D18, 4.13.8); `scan --help` documents exit 3;
`rules propose` marks an already-adopted rule instead of proposing it
again; `R-NO-LONG-BASH`'s text carries the macOS note (4.13.8).

**Milestone labels (D17).** N1 tests assert accepted generations and
parent-index cache invalidation directly, never through `workflow_failed`
(which does not exist until N2). N2 tests assert lifecycle and
`workflow_failed`. N4 tests assert the ledger model at the model level
(entries, completeness, reasons). Every public rendering assertion --
`run`/`rescue`/`watch`/`scan` text and JSON reflecting the new model -- is
labeled N5, including the session-brief test above. Parent fingerprinting
(4.13.3's "Parent index caching") is owned by N1 only; N7 does not
re-implement or re-own it (4.13.7's own step 2 is a measurement of N1's
behavior, D9).

**The strict-xfail rule.** "Every currently failing field case is
`xfail(strict=True, ...)`. A case already correct under v0.2 is an unmarked
regression pin and must pass both the normal and `--runxfail` runs." (D19)

**The `--runxfail` evidence.** N0's own acceptance runs `uv run pytest
--runxfail -k field_` and records, per test, that the assertion the
fixture is named after -- not a fixture-setup error -- is what fails; a
fixture that errors during setup rather than failing its own assertion
does not count as acceptance evidence for that fixture.

**The advisory corpus check.** A private corpus check over this machine's
own runs, asserting the governing invariants: no detector hit from payload
(4.13.2); no `CLEAN` run whose newest generation has an accepted failed
notification (4.13.3); no "none" `WORK AT RISK` line with a `partial`
ledger (4.13.1); no "ended" or "abandoned" word printed from mtime alone
(4.13.3); no raw path in `share` output (4.13.5). It informs; it never
gates a release.

(v0.3.1, G8) Eight more invariants, each reported with its checked and violation counts:
(6) an independent five-valued return state (all children, nested, resumed; both directions)
equals the reader's; (7) the exported `outcome` of a session child equals the independent E1
mapping and `counts.interrupted` equals the number of kill sentinels; (8) no child, compared by
identity and not by its rendered name, has an exported outcome `completed` (its effective end) while
the W1 guard lists it (return state not `returned`, outside the risk population); (9) no session `WORK AT RISK  none` while a
child is not `returned`; (10) the denial sentences a raw walk of the transcripts finds (through the
shared `markers.denial_kind`, and separately by independent regexes) equal the `permission_denied`
counts; (11) an attempt with a G3 `SelfReport` or a legacy major clause has exactly one major
`claim_contradiction`, and one with neither has none; (12) `rescue` prints no `NOTHING TO RESCUE`
for a session with a child not `returned`; (13) every workflow of a session the caller's variable
names is in `run --here` and `scan --here`, reported with the count already included without the
variable. The independent side walks raw transcripts with its own code and imports only the shared
G3 and G7 predicates; its permission-rule regex is written independently and reads the same
multi-line form. The invariant logic lives in `tests/g_corpus_support.py` and the independent return
implementation in `tests/g_independent_return.py` (code only, tracked, exercised by
`tests/test_g_corpus.py`); the real-corpus runner stays in the maintainer's private tooling. It is a
maintainer check, not a shipped feature. The check stays advisory; the synthetic suite is the
release gate.

### 4.13.10 Risks (standing table; each risk is covered by the tests of the milestone that names it, no code of its own)

| Risk | Mitigation |
|---|---|
| Notification format drifts | both delivery forms parsed; unknown shapes ignored and counted as a diagnostic; lifecycle falls back to `journal_resolved` or `none`, never to `completed` |
| A write path the ledger does not know | closed tool allowlist; unknown tools and truncated fields make the ledger `partial`; "none" requires completeness |
| Severity built on a proxy | every input has an `unknown` value that raises severity and marks `INCOMPLETE`; no time thresholds |
| An end-time absence gets claimed as `observed_at_finish` | nothing in v0.3 produces it; `critical` is reserved and no v0.3 detector reaches it; a future end-time observer needs its own contract before the value is ever assigned (D2) |
| Alias collision or reversal | HMAC prefixes of at least 12 hex under a secret, assigned once per identity by the registry so no two identities of a kind share an alias, never reassigned; per-render positional fallback when the secret or registry is unavailable; tests with forced 12-, 13-, and 14-character collisions (D10, 4.13.5, 7.5) |
| Disclosure leak through errors, hooks, or a skill | the share invariant; hook output fixed to `share`; error rendering through the active level; `/rescue --local` documented as deliberate; rendered-output privacy fixtures per sink |
| Stale parent index in a long-lived `watch` | fingerprint with ctime; append offset; tests for append, replacement, truncation, same-size rewrite |
| Partial ledgers everywhere make the output noisy | the line says what the person can do in one command; the corpus check reports how often each reason fires so the grammar can be extended where it pays |
| Schema 3 breaks consumers | the `schema` key; the only known consumer is this repo's own skills and tests, updated in the same milestone |

### 4.13.11 Contract corrections from review round 3 (binding; each milestone folds its items into the sections they name when it lands) (folded: N3 in N3c, N4 in N4, N5 in N5, N6 in N6, N7 step 1 in N7; N7 step 3 and its warm-scan test are deferred past v0.3 by the owner's decision C)

These items answer `.reviews/v0.3/N0r-codex.txt` findings 6 to 14. Where
an item and any earlier sentence of this document disagree (including
4.11, 4.12, 4.13, and 5.3b), the item wins until its milestone folds it in.

**N4, ledger (findings 6, 7).** (Folded into 4.13.1 in N4, commits 707aa96..6e0bc61.)
- The simple write form requires the redirect: `simple := WORD+ redirect`.
  A command with no redirect, no final `| tee`, and none of the other named
  forms is `opaque_command`.
- Row order in the ledger table is: file tools; then background (Bash or
  Monitor with `run_in_background: true`, a `nohup` word, a trailing `&`,
  or an anchored `bash_backgrounded` hit on its result); then delegation;
  then the closed read-only grammar; then the tiny write grammar; then
  `opaque_command`. A read-only command that was backgrounded is therefore
  `possible / background_process`.
- Completion mapping from `ToolCall.completion`: `completed_ok` ->
  `success`; `completed_error` -> `error`; `backgrounded` and `no_result`
  -> `no_result`; `ambiguous` -> `ambiguous`.
- File tool targets: Edit, Write, and MultiEdit take `input.file_path`
  (one entry); NotebookEdit takes `input.notebook_path` (one entry). A
  missing or non-string field gives one entry with path null and reason
  `unexpected_entry_shape`.
- Risk population completeness is `partial` whenever journal, parent,
  attempt-transcript, or meta discovery reported a diagnostic, whatever the
  population size. Generation assignment applies to workflow attempts only.
  Unless every generation's result line carries a timestamp that parses
  and the timestamps are strictly increasing in launch-line order, every
  attempt of the run is unassignable (reason `unassignable_attempt`).

**N3, disclosure (findings 8, 9).** (Folded into 4.13.5 and 7.5 in N3c, commits 2082790..f7f5ae9;
kept here as the record of the correction.)
- Aliases come from a persistent registry `aliases.json` in the state
  directory (mode 0600, written under the state lock, beside the
  installation secret). The first time an identity is rendered it is
  assigned the shortest prefix of its HMAC, at least 12 hex characters,
  that no other registered identity of the same kind holds; the assignment
  never changes afterwards. A rendered alias therefore depends only on the
  identity, the secret, and the registry, never on the other identities in
  one output. When the registry cannot be read or written, rendering falls
  back to per-render positional pseudonyms and prints "stable aliases
  unavailable" (D10).
- In `share`, transcript file names in evidence pointers are aliased too:
  `agent-<alias>.jsonl` for an agent transcript and `session-<alias>.jsonl`
  for a session transcript; journal and meta names follow the same rule.
- watch uses the same stable aliases as every other command in `share`.
- In `local`, an evidence or path value outside both the repository and
  the home directory renders as `path-N`.

**N5, schema and rendering (findings 10, 11, 12, 14).** (Folded into 4.13.4, 4.7, 4.11, and 5.3b in N5, commits fdcdc37..73149a6.)
- Schema 3 types: `finished` is `bool | null` on every row type (null for a
  session); workflow `activity` is `recent | stale | unknown`;
  `agent_type` is `str | null`; `lifecycle.generations` is `int >= 0`;
  `lifecycle.source` follows the enum of 4.1.
- rescue `paths[]` entries carry `observation: {state, reason}` and
  `ledger: [ {attempt, effect, basis, completion, line} ... ]`, one element
  per ledger entry for that path, in attempt order; nothing is collapsed.
- Exit codes for rescue and watch, ordered: rescue plan: usage 1; no run 2;
  lifecycle `unknown` or any UNKNOWN fact or possibly running 4; else 0.
  rescue status: usage 1; no run 2; any unit `NONZERO_OBSERVED` 3;
  lifecycle `unknown`, `NOT_OBSERVED`, `WRITE_AFTER_CHECK`, or any NEVER
  match 4; else 0. watch: usage 1; no journal within 60 s 2; DONE with
  verdict FAILED 3; LIMIT, MALFORMED, or lifecycle `unknown` at DONE 4;
  DONE otherwise 0.
- Privacy tests for every sink (run text, run --json, scan text and json,
  rescue plan, rescue status, watch text and json, rules review, hook
  messages) use a scenario that must emit the sensitive item, assert that
  the expected structural line exists, then assert the alias is present and
  the real value (a planted label, phase title, repo path, raw agent id, and
  session UUID) is absent. A sink that emits nothing fails the test.

**N6, identity (finding 14).** (Folded into 4.13.6 in N6, commits 9f0cdec..6d930f8.) Containment for `--here`: `caller_cwd` is
the process's working directory; each `candidate_cwd` is one value from
the run's cwd set (4.13.6). Both are made absolute and normalized with
`os.path.normpath`; the run matches when any candidate satisfies
`os.path.commonpath([candidate_cwd, caller_cwd]) == candidate_cwd` (the
caller's directory equals or is below a run cwd, as 4.13.6 states); a
`ValueError` (different drives) means no match for that candidate. No
symlink resolution.
(Implemented in N6.) Both paths go through `os.path.abspath` and then
`os.path.normpath`, with no `~` expansion: a literal `~` in a recorded cwd is a
relative name, made absolute against the process's working directory like any
other relative value. A candidate that raises `ValueError` or `OSError` is no
match for that candidate, and an empty cwd set never matches. One function,
`reader.run_matches_cwd`, serves `scan --here` and `run --here`. A run whose cwd
set holds `/` or the home directory matches every caller below it; that is the
rule as written here, and whether it should stay is a product question, not
something N6 changes.

**N7, performance (finding 13).** (Folded into 4.13.7 in N7 step 1, commits 67f231a..2975843; step 3 and its `RUN_CACHE_ENABLED` flag are deferred past v0.3 by the owner's decision C (N7-plan.md, 2026-10-05): no `agent_postmortem.perf` module or flag exists, and the warm-scan test is held as one strict xfail that names that decision.) The warm-scan cache test is to run only when
`agent_postmortem.perf.RUN_CACHE_ENABLED` is true (N7 sets it only if it
builds the step-3 cache) and is skipped otherwise; step 3 is decided by the
measurement N7 records. The detector-discovery count test calls
`detectors._reset_registry()` (a documented test hook) before counting.

## 5. Privacy

### 5.1 Redaction and pseudonyms

`redact(text)` keeps the v0 rules: home directory and truncated spellings
to `~`, bare account name (3+ chars, whole word) to `user`, emails to
`email:` plus a 6-char hash, UUIDs to `uuid:` plus last 4 hex, project slugs
`-Users-...`/`-home-...` to `project:` plus a 6-char hash. This is
structural redaction: it runs on every string a renderer prints -- including
an evidence line, a diagnostic, and a metrics value -- and it never
substitutes a pseudonym.

Commit ids (M9): a reported commit id is its own pseudonym category. At `share` it renders as
`commit-1`..`commit-N`, numbered in order of first appearance within one render, the same id the
same number, in text and JSON, and is never scanned again as a path (A1); at `local` it renders as
the real id scrubbed like a name (`local_text`, S1) and at `raw` as written. See 4.13.5.

M8 review's D-A: after the home substitution above, a home-relative path
still names every directory between `~/` and the file itself -- `~/
Documents/<client>/<sub>/notes.md` leaks the client name even once the home
directory becomes `~`. Every directory segment beyond `~/` collapses to a
single `...`, keeping only the basename: `~/.../notes.md`, regardless of
how many directories sat in between (one or five collapse to the same
shape). A path with nothing between `~/` and the file (`~/notes.md`) is
already minimal and is left alone, as is a bare `~` with no slash at all
(shell shorthand, not a path) and a URL's `~user` spelling (no slash
immediately follows the `~`). The same collapse applies to a `/tmp` or
`/private/tmp` path, but keeps its first segment -- a scratch directory
name, not routinely a project or client name the way a home directory's
children are -- collapsing only what comes after it:
`/tmp/<first>/.../<basename>`. `--no-redact` disables this along with every
other rule here, since it is part of `redact()` itself.

`Pseudonyms` is a per-render mapping. Every public renderer entry point
(`render_run_text`, `render_bug_report`, `export_run`, `render_scan_text`,
`render_trend_text`, `trend.to_json`) builds a fresh one for its own render
before printing anything, so numbering restarts at `agent-1` every time and
two reports can never be linked by a repeated pseudonym. Before rendering,
`render_run_text`, `render_bug_report`, and `export_run` register every
child attempt's label, meta description, phase, and agent_type not in the
built-in set `{general-purpose, Explore, Plan, code-writer, code-reviewer,
test-writer, test-runner, debugger, doc-scribe}` (label and meta description
are two spellings of the same user-chosen name for one agent and share the
`agent-N` namespace; phase gets `phase-N`; a custom agent_type gets
`type-N`); `render_trend_text` and `trend.to_json` register each row's rule
target as `target-N`. They also register every child attempt's own
`report_path` claim (`reported.claims.report_path`), when it is a non-empty
string, as `path-N` (M1 review finding 1) -- see below for why this
namespace is substituted differently from the other five. `export_run`
(v0.2, F4 hardening) additionally registers every attempt's own
`facts.paths_written`, `facts.in_flight[].path`, and `facts.cwds` as
`path-N`, the same namespace, before building the JSON export -- a
deliberately separate pass (`report._register_facts_paths`) from the one
above, called only by `export_run` itself: `report._register_names` is also
called directly by `rescue.py`, which registers its own, repo-relative
`display_key` form of the very same underlying paths, and folding facts
registration into the shared function would burn `path-N` ordinals on the
raw absolute form first, leaving the same file with two different
pseudonyms in one rescue render.

`rescue.py` (4.11, F7 hardening) registers a third, non-name, non-path
category of its own: `check`. A declared check command is neither a label
a detector wrote a sentence about, nor a path this tool ever substring-
matches inside other prose -- it is a whole, arbitrary shell command that
can carry flags, arguments, or an embedded path, and by default it must
disappear wholesale, not merely have an embedded path replaced. `rescue
plan`'s CHECKS/STEPS lines and `rescue status`'s "last counted run" line
call `redactor.names.check(value)` directly at the point of rendering (the
same pattern `path` already uses for a report_path claim or an observation
path), so it needs no `apply()`/`apply_paths()`-style substring pass of its
own; `--no-redact` shows the raw command as always.

Pseudonym substitution applies to three places, not two. A structured name
field a renderer reads directly off `Pseudonyms` (the AGENT table, a bug
report's reproduction line, an exported attempt's label/phase/agent_type, a
trend row's target) and `Finding.title`, rendered through `Redactor.title()`
(a word-boundary anchored substitution,
`(?<![A-Za-z0-9_])name(?![A-Za-z0-9_])`, over every name registered so far,
followed by structural `redact()`; **(N3b S1)** names come first, so a label
that holds a UUID, an email, or a `-Users-...` slug is replaced whole
instead of being half-rewritten by the structural pass and then missed)
work exactly as before. Evidence, a diagnostic, and a metrics value are
never fed to the five *name* categories (agent/desc/phase/type/target): they
pass through structural `redact()` alone (plain `Redactor.__call__`),
because they are a faithful quote of transcript text (4.5), not a rendered
sentence a detector built from a label -- a phase named "Implement" must
never turn "Implement the fix" inside an evidence line into "phase-1 the
fix". The third place is the exception: a registered `path-N` value IS
substituted by plain `Redactor.__call__` itself -- before structural
`redact()` runs, not after, so a report_path under the home directory is
replaced by its pseudonym wholesale rather than first being collapsed into
an unmatchable `~/.../basename` shape -- reaching evidence, a diagnostic,
and a metrics value the same as everywhere else `redact()` is called. A
report_path is a self-reported claim value, not a faithful transcript
quote, and today's only two renderers of it (`reported.claims.report_path`
in an exported attempt, and `claim_contradiction`'s
`metrics.claims.report_path`, always the identical string copied from the
same `reported` dict) both went through plain `redact()` before this fix,
which left a relative path or an absolute path outside home and `/tmp`
unchanged. This is why `report_path` needed its own namespace instead of
joining the other five: it must never appear raw by default in any of the
places a metrics value or a reported field can render, which the five
title-only categories are deliberately never asked to guarantee.
**One free-text path scanner built on shell words (field milestone F, FA1 and
fix round 4).** A path in free text is found inside a complete shell word, and
once a word holds a path every character of the word from the path start to its
end is rendered as one path. No regular expression splits a path at a quote, an
escape or a space. `redact.py` holds one scanner; every free-text output
(below) calls it, at `share` and at `local`. `raw` is a no-op.
- *Words (the lexer).* A streaming lexer splits the text into words and keeps,
  for each decoded character, the raw span it came from. Outside quotes a word
  ends at an unescaped space, tab, newline or carriage return (a no-break or
  ideographic space is part of a name, as in a shell) or at the unquoted
  operators `; | & < > ( )` (a run of them is one boundary, copied back
  unchanged around a rewritten word). An unescaped newline always ends a word, and no quote crosses it: an
  unclosed quote takes the rest of its line into the word (fail closed). A
  backslash before a non-newline character makes it literal; a backslash and a
  newline (or a carriage return and a newline) is a line continuation that joins
  the next line into the same word (outside single quotes and inside double
  quotes). An unescaped `"` opens a
  double-quoted fragment (inside it a backslash escapes `"`, `\`, `$`, a
  backquote and a newline; before any other character it stays literal). An
  unescaped `'` opens a single-quoted fragment (literal to the next `'`, where
  `\'` and `\\` inside it are read as the quote and the backslash, the union of
  the shell rule and the Python `repr` rule, so a `repr` of a name that holds
  both quote kinds is one fragment whichever sink prints it and the rule can
  only widen a span), except
  an in-word apostrophe, one with a word character immediately on each side
  (`it's`, `O'Neil`), which is an ordinary character; `'O'"'"'Neil'` is
  adjacent fragments of one word. Inside a single-quoted fragment an in-word
  apostrophe is name text and never closes it (F fix 8, W8-1, fail closed since
  F fix 8b): Node, git and GNU tools print a name in single quotes with no
  escaping, so `open '/srv/Bob's Docs/x'` is one fragment, and a fragment whose
  closing quote was cut off (an excerpt truncated mid-path) runs to the end of
  its line. A single-quoted fragment, and a fragment opened by U+2018, is
  PATH-BEARING (F fix 10, W10-3) when its text holds an absolute path start, or
  when a registered value occurs between its opener and the end of its line (the
  raw line and, when it holds a backslash, the line with `\'` and `\\` decoded,
  both in NFC form; checked at most once per line; when the NFC form of the line
  differs from the raw line and a value hits it, every fragment on the line is
  path-bearing, because indexes are not mapped back). A path-bearing fragment
  closes at the LAST quote of its line that is not an in-word apostrophe (a word
  character on each side; U+2019 for a U+2018 opener, which also stops before the
  next U+2018 opener of its line), or at the end of the line (F fix 9, W9-3,
  fail closed): a possessive apostrophe before a space is indistinguishable from a
  closing quote (`open '/srv/James' Notes/x.md'`, `‘/srv/Clients’ Files/x.md’`),
  so the first such quote never ends the span. Not even the last quote closes it
  when that quote follows a word character and is followed by one or more spaces
  or tabs and then a token (the characters up to the next whitespace) that holds
  `/`, `\` or a `.` followed by a word character: the quote may be a possessive of
  a path cut off before its real closing quote (`ENOENT: open
  '/srv/James' Notes/private.md`, `open '/srv/x/James' Notes.md`), and the
  fragment then runs to the end of the line. The cost is accepted: a later
  quoted phrase on the same line is hidden with the path (`open '/srv/x' and
  'git init'` is `open 'path-1 [text hidden]'`), and so is a file name after a
  closing quote (`cat '/srv/x' notes.txt`). A fragment that is only the bare root
  (`'/'`, as in `split('/')`) names no directory and is not such a fragment; a
  fragment with neither an absolute start nor a registered value closes at its
  first quote (`it's 'README' now`, `'git init' and 'git add'`). Inside a double-quoted fragment the JSON
  escapes `\/`, `\uXXXX` (a surrogate pair is one character, a lone surrogate
  stays as its escape text), `\n`, `\t`, `\r`, `\b` and `\f` are decoded as well
  for detection (a truncated JSON string such as `"/srv/x y"` is
  still a path; decoding more can only widen a span). The typographic quotes U+201C and U+2018 open
  a fragment anywhere outside another fragment, closed by U+201D and U+2019; a
  U+2019 between two word characters is an ordinary character; an opener with
  no closer takes the rest of the line. Further paired display wrappers are
  lexed the same way (a literal fragment, body may hold spaces, anywhere outside
  another fragment, an unpaired opener takes the rest of the line): the
  backquote (a markdown code span), a German low quote (U+201E) closed by
  U+201C or U+201D, a left or right double curly quote closed by either curly
  double quote (so `“...“`, `”...”` and `”...“` are all fragments), the
  guillemets `«...»` and `‹...›`, and the CJK corner brackets `「...」` and
  `『...』`. A right double curly quote that has a right double curly quote
  later on its line as a partner opens a fragment even directly after a word
  character (`cat /srv/data/Client” Secret.txt” now` is the smart-quote spelling
  of a half-quoted name); one that follows a non-word character opens a
  fragment when a left or right double curly quote follows on its line; a lone
  one, with no partner, is not an opener and stays an ordinary closing quote
  (`cat /srv/x/y” now` keeps the quote as a suffix). A repr such as
  `PosixPath('/srv/O\'Neil "x" y/f')` is one fragment at every sink, by the
  single-quote rule above.
- *Which words hold a path.* A word is rewritten when its decoded text holds
  (a) a registered report_path value, compared in NFC form (an NFD spelling
  matches an NFC registration and the reverse; no case folding), or (b) an
  absolute-path start by the structural grammar (`_ABS_PATH_RE`: a `/` that is
  not preceded by a word character, `/`, `.`, `~` or `$` (a character of a script
  written without spaces between words, one class: Chinese, Japanese and Korean
  characters, fullwidth forms, Thai, Lao, Tibetan, Myanmar and Khmer, is not a word
  character here: it is a boundary, F fix 9, W9-1; F fix 10, W10-1), a leading `//`,
  a `file://` URL (the scheme is case-insensitive and may carry the host
  `localhost`: `FILE:///srv/x`, `file://localhost/srv/x`), a short option glued
  to a path, a run after `:`, `-` or a word character of an unspaced script only with two
  segments, a file extension or a `/` right after it, F fix 10, W10-2; F fix 11, W11-1,
  W11-2: one rule for the raw text and for the decoded text of an escaped word),
  or a `~/`, `~user/` (another account's home: a `path-N`, never the
  `~/.../name` form), `$HOME/` or `${HOME}/` (the home directory, shown as `~`
  and then like any `~/` path: `~/.../name` when no whitespace was hidden) that
  no word character (a character of an unspaced script excepted), `/`, `.`, `~` or `$` precedes (an `@`
  does not block it: `curl -d @$HOME/x`, F fix 9, W9-2), at the start of a word or
  anywhere inside one, inside a quote included (`sh -c "cd $HOME/x"`, F fix 8,
  W8-2), or `$PWD/`, `${PWD}/`, `$OLDPWD/`, `${OLDPWD}/`, `$CLAUDE_PROJECT_DIR/`
  or `${CLAUDE_PROJECT_DIR}/` under the same condition (F fix 9, W9-6; the working directory is an absolute place in all but spelling:
  an opaque `path-N`, never a repo-relative form, F fix 8, W8-8), or a path
  after `@` (`curl -d @/srv/x`; `@scope/pkg` and an e-mail address have no
  slash right after the `@` and stay prose, F fix 8, W8-4),
  or (F fix 7, W7-3) a Windows start: a drive letter (one ASCII letter not
  preceded by a word character, then `:` and `\` or `/`: `C:\Users\x`,
  `c:/x`) or a UNC share (two backslashes not preceded by a word character or
  a backslash, a host and a backslash: `\\server\share\x`; a CJK character before
  either is a boundary too), or a Windows namespace
  path (`\\?\UNC\server\share\x`, `\\?\C:\x`, `\\.\C:\x`: two backslashes, `?` or
  `.`, a backslash; F fix 8, W8-8), or a `file://` URL
  with any host (`file://server/share/x`). A Windows start is found on the raw
  text (an unquoted backslash is a shell escape, so the decoded text no longer
  holds it) and, for a word that decoded an escape, on the decoded text, so a
  quoted path and a JSON-escaped or truncated JSON string are found too. Its
  span is always an opaque `path-N` (never the repo-relative or the structural
  form), with the marker where whitespace was hidden. Both the decoded
  and the raw spelling are searched for registered values, so a registered name
  that holds a quote or a backslash is found whichever way it is written. A
  registered value that holds whitespace or an operator is found across word
  boundaries and the words it touches are joined into one word. A registered
  value that ends in a slash also matches without that slash. `and/or`,
  `read-/write`, a URL's `://` and a lone `/` between spaces are prose and keep
  their bytes (`Pass / Fail rate is 3/4`, `input / output via stdin/stdout`).
  A character of a script written without spaces (Chinese, Japanese, Korean,
  Thai, Lao, Tibetan, Myanmar, Khmer) before a slash is not a word character
  (F fix 9, W9-1; F fix 10, W10-1): a path typed right after a word is the normal
  spelling in those scripts (`无法读取/srv/x/y.md`, `อ่าน/srv/x/y.md`). After such a
  character a POSIX start counts only with two segments or a file extension, the
  rule that already holds after `:` and `-` (F fix 10, W10-2), so a slash between
  two words stays prose (`测试通过/失败 共 12 项`, `成功/失败比例`, `读/写`, `和/或`).
  The test is made on the matched value with its trailing `.` and `,` removed (the
  sentence punctuation the path alphabet admits: `读取/Zqa.md, failed` has the
  extension `.md`, F fix 11, W11-1), and a `/` directly after the match is a further
  segment (`读取/Zqa/`, `读取/srv/(Zqa)/x.md`, whose next segment the path alphabet
  cannot read, F fix 11, W11-2). The strict test applies only after a WORD
  character of the class: punctuation and spaces of the class (`：`, `「`, `。`, the
  ideographic space) are ordinary boundaries, so `错误：/Zqa` and `读取失败：/Zqa Zqb/x.md`
  hide as they did before F fix 9 (F fix 11, W11-2). A combining mark of the class
  (a Thai, Lao or Myanmar vowel, tone or asat mark) ends a word like a letter does,
  so `ใช่/ไม่ใช่` and `ဟုတ်/မဟုတ်` stay prose (F fix 12, W12-1). `通过/失败。`,
  `通过/失败, 共 3 项` and `是/否.` stay prose. The privacy side of the same rule is a
  recorded limit: after a word of the class, a one-segment value with no extension
  and no `/` right after it is prose, so a root-level name prints (`读取/Zqa`, and
  `读取/Zqa Zqb/x.md`, whose first segment holds a space), as does one whose name
  goes on past a shell operator (`读取/Zqa&Zqb/x.md`, `读取/Zqa(1)/x.md`) or past a
  path-alphabet character after its extension (`读/Zqa.md=1`). Each needs a private
  root-level directory glued to such a word; a path with two segments, an extension
  or a following `/` hides.
  The cost is accepted: a list of single characters or short words joined by
  slashes with three or more parts (`增/删/改/查`) and a URL path whose segment is
  written in such a script, followed by two more segments or a file name, are
  hidden. An explicit anchor (a drive, a UNC share, a namespace path, `file://`,
  `~/`, `$HOME/`, `$PWD/` and the other variables above) is a start after any such
  character with no further condition. `naïve/simple` and `Zürich/Bern` stay prose.
  A terminal
  control sequence in front of a path start is not a word character: one parser
  reads `ESC [`, parameter bytes (`0-9 : ; < = > ?`), intermediate bytes (space
  to `.`; never `/`) and a final byte, so colon-form colour (`ESC[38:2::255:0:0m`),
  erase (`ESC[K`), cursor and private sequences all count; the same text with the
  ESC byte already stripped counts only as a colour sequence (`[38:2::255:0:0m`:
  digits, `:` and `;`, then `m`, with no word character before it, so `array[0m/x`
  and `[K] done` are prose). The start grammar sees the sequence as a boundary,
  the sequence is a proven prefix of the word and is kept, and a sequence at the
  end of the span is a raw suffix (F fix 8, W8-3; F fix 9, W9-5). A word with no
  path is left byte for byte as it is.
- *How the word is rewritten.* The path starts at the start of the word, moved
  right only past a proven syntactic prefix: leading colour sequences, `NAME=` (a shell identifier and
  `=`), `--option=`, a glued short option `-X`, a rule id (`R-` and one or more
  hyphen-joined upper-case or digit segments, then a hyphen, directly before
  the slash: a rule id is never a path), and leading `[` or `{`. Nothing
  else before the path start is kept (a space or any other character never ends
  the path early). The path ends at the end of the word, minus a raw suffix:
  trailing sentence punctuation (`. , ; : ) ] } ! ?`, a closing typographic
  quote, an escaped trailing space), a colour sequence and a `:<line>` or
  `:<line>:<column>` evidence suffix (digits only: `:abc` is name text); the suffix is
  searched in the last 512 characters of the span only (F fix 10, W10-5), so a longer
  run of suffix characters is shortened and its extra characters stay inside the
  hidden path (fail closed; it keeps the search linear). A colon inside the word is path text (`PATH=/a:/b` is one
  path). The path text is rendered once by the single-path renderer of the level
  (the next paragraph). The quote delimiters that enclose the path start are
  kept around the result (`"path-1"`). When the decoded text of the span
  holds whitespace (a space, tab, newline or carriage return), whether the span
  is closed, anchored, quoted, escaped or exactly a registered value, it is
  rendered as `path-N` followed by ` [text hidden]`, never as a structural
  form (`~/.../name`, the `/tmp` collapse) and never as the local repo-relative
  form: no rule can tell a space in a filename from prose, so a span with
  whitespace may hold more than a path (`error="/srv/p/f failed with EACCES"`
  is `error="path-1 [text hidden]"`), and the reader is told so. A span with no
  whitespace (an escaped or continued name without a space) keeps the level's
  own path form and has no marker. Decision-critical facts live in structured
  fields (verdicts, codes, counts), never only inside such a word.
- *The end-of-line rule (fail closed).* An unquoted name with a space is
  indistinguishable from a path followed by prose, and a directory name can
  hold any number of words and any character: a semicolon, a pipe, an
  ampersand, a parenthesis, a colon and a number (`Meeting 10:30 Notes`), a
  closing curly quote (`Workers’ Comp`). So in unquoted context a path
  start (an absolute anchor, a Windows start, `~/`, `~user/`, `$HOME/`,
  `${HOME}/`, or a registered hit) hides everything to the end of its LINE: the
  span runs over every following word and separator on the same line and
  renders as one `path-N` with the marker (`cannot open /srv/data/Client
  Secret/deep/n.md now` is `cannot open path-1 [text hidden]`, and a second path
  on the same line is inside the same span). There is NO other stop (F fix 7,
  W7-1): text that came from a transcript, a file, an exception or the
  environment is untrusted, and no operator, closing wrapper or line-number
  shape in it is proof that the path ended. The span ends only (a) at the end
  of the line (a newline always ends it), or (b) at the closing delimiter of a
  fragment that was opened before the path start, the one case where the text
  itself says where the path ends: an ASCII quote, or the partner of a
  typographic or display opener (a path start inside a closed fragment ends with
  that fragment, so `cat "/srv/a b/c" now` keeps `now`). A word that holds both
  a path start inside a closed fragment and one outside every closed fragment
  (`PATH="/usr/bin":/srv/a b/bin`, `"/srv/a b"/c d`) is an unquoted start: the
  span runs to the end of the line. A stray closer after the path start
  (`’`, `”`, `»`, `」`, `›`, `)`, `;`) is name text; a
  trailing one at the very end of the line is kept as a raw suffix, like
  sentence punctuation and a `:<line>` or `:<line>:<column>` pointer at the
  end of the line. The cost is accepted: trailing prose and shell syntax on the
  same line are hidden and marked (`/srv/private; echo failed` is `path-1 [text
  hidden]`, `/srv/My Dir/test_x.py:12: AssertionError` is `path-1 [text
  hidden]`, `cd /srv/x && make` is `cd path-1 [text hidden]`); `--raw` shows
  everything, and verdict-bearing facts live in structured fields. A span
  that starts with a registered value followed by a plain separator (a space or a
  tab, optionally after `.,;:!?`; not an escaped or quoted space, which
  belongs to a longer name) keeps that value's own pseudonym, so one path is one
  pseudonym (`report at clients/x.md was missing` is `report at path-1 [text
  hidden]`); any other span is its own opaque `path-N`. The display forms this
  tool prints for a collapsed path, `~/.../name` and `/tmp/dir/.../name`, are a
  fixed point: a word that is exactly one keeps the words after it, so a line
  scanned a second time is not hidden by its own output.
- *Tool-built text is not scanned as a line (F fix 7, W7-2, W7-4).* A sentence
  this tool builds from its own fixed words and values it already knows is built
  from pieces, and each piece is rendered or scanned where it enters the
  sentence; the assembled sentence is not scanned again, because a path in one
  piece would hide the pieces after it. The finding titles that name a path
  (4.13.4) render a fixed template with the path rendered once; a story that
  joins such a title with other clauses renders the title as a piece of its own
  and scans the clauses around it. The rescue plan
  and status text scan every piece of a line separately: a claim value (the
  `status` claim included, at `local`), the in-flight tool and path, a commit id,
  a time, a cause, a `reset_time`, a path, a reason. A config warning is built
  as typed parts when the parser creates it (F fix 8, W8-10): fixed words (kept),
  the config file path and a `[[repos]]` path field (each rendered once by
  `display_path`), and every value that came from the file (a rule id, a detector
  id, a date, a parser message, a rejected check command), each one untrusted
  piece through the free-text scan; the finished warning is printed as it is. No
  warning is split on the known paths after it was assembled, so a repository
  path that is a prefix of a longer untrusted path leaves no tail unscanned. A
  warning without parts (a hand-built one) is one untrusted piece. The same holds for an exception text (the filenames of an `OSError`
  are rendered once and the message around them is scanned), the `rules --show`
  text and the hook install note (the template path is rendered once) and the
  `apply` already-present line.
- *JSON.* A text that parses as JSON as a whole, and every maximal `{...}` or
  `[...]` substring that does, is redacted value by value in place: each string
  value and each key goes through the one scanner (a string that does not
  change keeps its exact spelling; a changed one is re-encoded as a JSON
  string), while numbers, booleans, null, punctuation and spacing are kept, so
  `{"path":"/srv/x","status":"failed","count":7}` is
  `{"path":"path-1","status":"failed","count":7}`. A bracket span is a JSON
  document only when it stands as its own token: it is preceded by the start of
  the text, whitespace, `:`, `,`, `[` or `{`, and followed by the end of the
  text, whitespace, `,`, `]` or `}`. A span glued to a word character is part of
  a name (`Acme[2024]/Q1 Report`, `notes[1].md`, `Client{}Secret`) and is
  scanned with the word. A bracket span that follows a path start on the same line is
  part of what that path hides (the end-of-line rule), so it is left to the word
  scanner too; a document before the path, or on another line, is read value by
  value. "A path start" is decided by the same predicate the word scanner uses
  (F fix 8, W8-9): an absolute, Windows, namespace, UNC or `file://` start, a
  home or `$PWD` anchor, a registered value, and, for a double-quoted word that
  decodes an escape, a start in its decoded text, so `open C:\Clients\Acme X
  {"relative":"Private\\notes.md"}` is `open path-1 [text hidden]`. A changed string is written back with every non-ASCII
  character and every lone surrogate as an ASCII `\uXXXX` escape, so the
  result always encodes and prints. A text that does not parse keeps the
  fail-closed free-text path, in which a double-quoted fragment decodes JSON
  escapes (above), so a truncated document with `\/` or `\u002f` separators is
  still found. The scan is not skipped for a text with no literal `/` or `~`
  when the text holds a backslash (an escaped slash is a slash). The attempt to
  parse is bounded by the length of the text, so a hostile run of brackets
  cannot make it quadratic, and the check that a path precedes a span on its
  line reads each character of the text at most a bounded number of times (a
  span that follows a path is skipped without a new scan of its line).
- *Which outputs.* The plain `Redactor.__call__` (evidence lines, diagnostics,
  metrics values, JSON strings, exception text), `Finding.title`,
  `scrub_leftover_paths`, the local name and line scrubs, a rescue excerpt, a
  hook debug traceback and every declared check value at `local` all run this
  one scanner; a declared check is `check-N` at `share`. A whole argument an
  argument error quotes goes through `argerrors.ErrorRenderer` instead, as one
  value, and `watch` prints only fixed words and validated identifiers. A hook
  notification is fixed words, a run id and agent aliases: its pieces go through
  the scanner one by one, and the fixed `/postmortem` command the notification
  tells the user to type is not a path and is never given to it. A local
  title and a local name scan the text first and flatten control characters
  after, so a backslash and a carriage return and a newline stay a
  continuation. The exception text of a failure lexes Python reprs (above), and
  an OSError's own filenames are replaced by their one-value display first.
  An exception that no command handles is caught by `cli.main` (F fix 8, W8-5):
  it prints one line, `agent-postmortem: unexpected <ExceptionClass>: <text>`,
  where the text is `Redactor.exception_text` at the active level, and exits 1;
  if even that fails it prints the class name alone. An interrupt and an exit
  are not caught. The reader never raises for an unreadable run directory or
  file (a directory without the execute bit, a journal it cannot open): the
  condition is a reader diagnostic (`journal.jsonl: unreadable: ...`), scanned
  like every diagnostic, and the run is INCOMPLETE; discovery lists the other
  runs. The one executable source that is not Python and writes user-visible
  output, `hooks/model_allocation_guard.mjs`, never echoes a path or a raw
  error message: its failure reason is a fixed sentence plus the class of the
  failure from a closed list (`ENOENT`, `EACCES`, `SyntaxError` and the like,
  else `error`), whatever the payload or `scriptPath` held.
- *Recorded limits (not fixed, by decision).* A single quote spliced between
  two letters of a name (`Cli'ent Sec'ret`) is read as an in-word apostrophe,
  because that rule protects real names (`O'Neil`, `it's`); a shell never
  needs that spelling for a path. The opposite cost is accepted: a possessive
  plural before a path (`the clients' files in /srv/x`) or a glob or operator
  word (`src/**/*.py`, `2>/dev/null`) is over-redacted, and where whitespace was
  hidden the marker says so. (F fix 7 removed two earlier limits: a name that
  holds an operator followed by a space is no longer cut, and Windows paths are
  anchors.) A relative name the scanner never sees (`./Client Secret/x`,
  `../x`, `src/a.py`, `modified:   Client Secret/x.md`, `pathspec 'x'`) is shown,
  by design: only an absolute, a Windows, a namespace, a home, a `$PWD` or a
  registered path is a start, because hiding every relative name would black out
  most command excerpts. The owner confirmed this contract on 2026-10-05: `share`
  prints no transcript text (no excerpt and no command; 4.13.5), so a relative
  name reaches only `local` and `raw`, the levels the owner reads, and `local`
  already shows paths relative to the repository (F fix 8, W8-8). Also recorded (F fix 8, W8-7): a possessive plural before a
  path that holds an in-word apostrophe (`the clients' files at /srv/a Bob's x`);
  a single segment under the root after a colon (`Error:/Client Docs/x`); a doubled
  separator (`\\server\\share`); a drive-relative name (`C:x`); a name that holds a
  newline; and the registered-path lookup, which costs O(L*k) for a line of L
  characters and k registered values with one first character (an availability
  note: evidence lines are short). Recorded in F fix 9 (W9-4, W9-8): a root-level
  directory whose name starts with a space (`cannot read / Client Folder/x.md`)
  is not recognized: such a directory does not occur on real systems, and a rule
  for it hid ordinary prose (`Pass / Fail rate is 3/4`), so the W8-8 recognizer
  is removed; Windows and PowerShell home
  spellings (`%USERPROFILE%\`, `$HOME\`, `~\`, `$env:USERPROFILE\`; the tool
  targets macOS and Linux); `$TMPDIR/` and other variables that are not the home
  or the working or project directory. The relative-name contract above stands.
  Recorded in F fix 10 (W10-1, W10-3, W10-6): a path glued to a word of a script
  written WITH spaces (Latin, Cyrillic, Arabic, Greek) is not a start, the same
  construction as English `read/srv/x`, which the base grammar leaves visible so
  that relative names (`src/a.py`) and prose (`and/or`) stay readable
  (`читать/srv/x/y.md`, `اقرأ/srv/x/y.md` and `read/srv/x/y.md` print raw; write
  a space before the path); a cut-off possessive whose following token holds no
  separator and no extension (`open '/srv/x/James' Notes` with the closing quote
  cut off) reads that quote as the closing one; an ESC-stripped colour sequence
  glued to a word (`Fail[31m/srv/x`); a non-CSI escape (`ESC(B`, `ESC7`) and the
  one-byte C1 CSI directly before a path; a percent-encoded slash; an ssh-style
  remote (`git@host:Org/Repo.git`); a Unicode space in the shown first `/tmp`
  segment; an unexpanded variable other than the home and working-directory family
  (`$TMPDIR/`, `$XDG_CONFIG_HOME/`). A word glued before a path start is hidden
  with the path (the whole-word rule); keeping the raw prefix before an
  unregistered start is not adopted: with relative names visible it would be
  safe, but its only gain is readability at `local` (`share` prints no
  transcript text) and it changes pinned rows that would need their own review
  round. It stays a possible later improvement.
  Recorded in F fix 11 (W11-4, W11-5): a fragment made path-bearing only by a
  registered value runs to the last quote of its line, which can be the opener of a
  later quote. When that later quote is closed, everything from the first quote is
  hidden (`x 'a b' then 'Zqa Zqb/deep/notes.md' y` with `deep/notes.md` registered
  becomes `x 'path-2 [text hidden]' y`). When its closing quote was cut off, the
  first fragment closes at that opener and the word between it and the registered
  value prints: `x 'a b' then 'Zqa Zqb/deep/notes.md` becomes
  `x 'a b' then 'Zqa path-2`. This needs an earlier quoted phrase, a cut-off quote and
  a registered relative value on one line, and is a recorded limit. Not changed: skipping a
  last-quote candidate that follows whitespace would close `'/srv/James' Notes x.md '`
  at the possessive and show `Notes x.md`, a worse leak than a hidden word. The
  cut-off possessive limit is as wide as its rule: any continuation whose FIRST
  token holds no separator and no extension (`'/srv/James' Private Notes/x.md`
  included) reads the quote as the closing one. Scripts written without spaces that
  are not in the class print a glued path raw (Javanese, Balinese, Sundanese,
  Batak, Tai Viet, Tai Tham, Buginese, Tai Le, Yi). Rule (b) has a cost: after a
  path-bearing quote that no possessive follows, the text to the last quote of the
  line is hidden, so `cp '/srv/a/x.md' backup/x.md && echo done` hides the rest of
  the line.
Then, in one pass over the original text (a pseudonym is never matched again by
a shorter registered value), for the path text of a word (decoded, from the path
start to the end of the word, minus the raw suffix); an unregistered absolute
path is scrubbed the same way at `share` and at `local` by the plain
`Redactor.__call__` (before this round a plain share call left it unchanged):
- the path text is exactly the registered value: its pseudonym, as before;
- the path text is longer, whether the value sits inside it (`/tmp/claude-501` inside
  `/private/tmp/claude-501/...`, inside `../tmp/claude-501/...`, or a relative
  `cards/x.md` inside `scratchpad/cards/x.md`) or is a prefix of it (including a
  registered value that contains a space): the COMPLETE token is redacted as one
  path through the single-path renderer of the active level, absolute or
  relative, so neither the registered value nor any continuation survives raw
  and no pseudonym is glued to a raw tail. A registered occurrence is never
  deferred to a later scrub that could not recognize its enclosing token. At
  `share` the renderer is: the run's identities, then structural `redact()`
  (home to `~`, `~/.../<basename>`, the `/tmp` collapse above, so a leftover
  basename such as `x.md` is the only thing that may remain); a token that
  structural redaction leaves unchanged becomes its own `path-N` (a relative
  token included). Before the structural form is computed, every interior run of
  slashes in the token collapses to one (`/home/user/Documents//Acme/plan.md` is
  `~/.../plan.md`), and the structural form must fully match one of the shapes this
  contract shows, `~`, `~/name`, `~/.../name`, `/tmp`, `/tmp/first`,
  `/tmp/first/name`, `/tmp/first/.../name`, each also with `/private` in front and
  an optional trailing slash (F fix 10, W10-4); any other form falls back to the
  token's own `path-N`. The structural form stands only when it hides what was
  registered: after it is computed for a token that encloses one or more
  registered values, if any of those values (or the part a collapse keeps of
  one: its last two segments, and, for a value inside home, its `~`-relative
  spelling) still occurs verbatim in the shown form, the whole token becomes
  its own `path-N` instead (`/tmp/acme-client/notes.md` with `acme-client/notes.md`
  registered, `/tmp/claude-501/<tail>` and the collapsed
  `/private/tmp/claude-501/.../x.md` with `/tmp/claude-501` registered,
  `/tmp/out.md.bak` with `/tmp/out.md` registered; a rendered display form
  `~/.../SECRET.md` with `SECRET.md` registered). There is no exemption for a
  token that is already a display form: an unregistered `~/.../<name>` stays
  unchanged only because no registered value selects it. A token under home
  whose structural form hides every registered value stays `~/.../<name>`. At `local` it is the P19 path function (repo-relative,
  `~/.../<name>`, else `path-N`). `raw` is untouched. The root `/` is never a
  registered substring.
`--no-redact` disables both `redact()` and `Pseudonyms`, `path-N` included.
Run ids are not redacted: they carry no user text (a directory name that is
not a valid run id prints the `wf_<8 hex>-x` replacement, 4.12). **(N3b)**
Agent ids, session ids, and the project are no longer left raw in text:
from N3b they render as the stable aliases of 4.13.5 at the default level
(versioned JSON follows from N5a-1, schema 3).

### 5.2 What the tool prints

Every public renderer entry point calls the plain Redactor (structural
`redact()`) on every string it prints -- an evidence line, a diagnostic, a
metrics value, a project slug, a trend target's raw form -- and
`Redactor.title()` (structural `redact()` plus pseudonym substitution) on
exactly one thing, `Finding.title`. A structured name field (an AGENT table
row, an exported attempt's label/phase/agent_type, a trend row's target)
goes through `Pseudonyms` directly instead, via the same `Redactor.names`
registered before the render started (5.1). This holds for every renderer:
text, JSON, bug report, hook systemMessage, additionalContext. Evidence
excerpts are the only transcript text that leaves the tool.

`render_bug_report`'s output ends with a footer stating plainly what that
level prints (N3b, 4.13.5): at `share`, "Redacted: paths, ids, emails,
account names, agent names. Evidence lines are pointers and marker codes
only; no transcript excerpts are included."; at `local` and `raw`, a
sentence naming what is shown ("Local level: real names, ids, and the
project directory name are shown; paths are repo-relative or numbered." or
"Raw level: real names, ids, and paths are shown; nothing is redacted."),
then either "Evidence lines are pointers and marker codes only; add
--evidence for transcript excerpts." or, with `--evidence`, "Evidence lines
include transcript excerpts (--evidence); read them first.", then "Do not
paste this report." A footer never calls the output safe to paste unread;
a reader must still check evidence lines themselves before filing when
excerpts are present.

### 5.3 Export allowlist

`report.export_run(run, findings, redactor)` returns `{run, findings}` built
from an explicit allowlist: Run: run_id, kind, status, claude_version,
started_at, ended_at, counts (agents, attempts, interrupted), diagnostics
(redacted), and attempts (a list of child attempts, each in the Attempt
shape below; main is never exported -- a session's own numbers already
surface through its wasted_time finding). Attempt: agent_id, key digest
(sha256 first 8, unconditional on --no-redact: a workflow key routinely
embeds its label verbatim), label pseudonym, phase pseudonym, agent_type
(built-in or pseudonym), ordinal, outcome, first_ts, last_ts, usage totals,
event_count, interrupt {cause, silence_seconds}, reported (null, or {type:
'dict', keys, claims} / {type: 'text', text_length}; at share (G-B1, GB-8) `keys` lists the
pseudonyms `key-1`..`key-N`, numbered in order of first appearance within one render and stable
within it (a result key is a user-authored identifier like a label); at local and raw `keys` lists
the real names; `reported` is not a structured name field, so the whole thing goes through the plain Redactor
via `redact_value`, the same as an evidence line or a metrics value -- a
`report_path` claim is the one exception (M1 review finding 1): the plain
Redactor call itself substitutes it with its `path-N` pseudonym (5.1),
registered by `_register_names` before export runs, so it never appears raw
either; `reported_line`, being reader-internal only, is never
exported), and facts (v0.2, unit M2a: null, or {paths_written, in_flight,
commits, commit_commands_without_id (FA6), cwds, background_unfinished, last_tool,
seconds_since_last_tool_at_end} built from `AttemptFacts`' own fields --
never its private complete-dict form, the same way `interrupt` above is
built from `Interrupt`'s fields. `facts` is not a structured name field
either, so it goes through the plain Redactor via `redact_value` exactly
like `reported`: `paths_written`, `in_flight[].path`, and `cwds` are each
registered as `path-N` by `export_run` before this runs (F4 hardening, 5.1)
and substituted wholesale the same way a `report_path` claim is -- a
repo-relative path, or an absolute path outside home and `/tmp`, used to
survive `redact_value` unchanged (this is what the review's F4 finding
caught: only a `/tmp`- or `~`-rooted facts path happened to get partially
collapsed by structural `redact()`'s own rules; everything else leaked raw).
`tool_calls` is reader-internal/private (a command is
private) and is never exported, under any flag). Finding: all fields, title
through `Redactor.title()` (a title routinely embeds a label verbatim),
evidence and metrics through the plain Redactor, structural redaction only,
never a *name* pseudonym substitution (5.1) -- a metrics value can in
principle carry raw text of its own, which is not a rendered sentence built
from a label the way a title is, so it is never fed to the five name
categories even when present. `claim_contradiction`'s
`metrics.claims.report_path` is the one field that still gets a pseudonym
here, the same `path-N` substitution `reported.claims.report_path` above
gets, since it is always the identical string copied from that same
`reported` dict (5.1). In practice no v0.1 detector's metrics carry any
other transcript-derived free text at all (D3: bash_backgrounded reports
only a count, never the commands themselves -- the evidence lines already
show the marker line each one fired on): a finding's evidence and an
attempt's facts (v0.2) are the only other places transcript-derived text
leaves the tool, both redacted. No meta, no cwd, no branch, no event list,
no tool_calls, no transcript text beyond evidence and facts. `silenced`
(v0.2, unit M3c, 4.10): `bool`, needs no redaction (it carries no user
text). `export_run`/`_export_finding` take a `silenced_detectors` frozenset
(the caller's `config.detectors_off`, empty by default) and set this true
on every finding whose `detector` is in it -- a silenced finding is never
removed from `findings`, only marked; a caller that must not act on one
filters with `config.partition_silenced` before it ever reaches a text
renderer, the verdict, `rules`, `stories`, or `next` (4.6/4.7).

`observation` (v0.2, unit M2b, 4.9): present in the payload only when the
caller passed a `RepoObservation` (i.e. `run --observe` was given); absent
entirely otherwise, so a plain `run`'s JSON is unchanged. When present:
`{checked, reason, paths}`, `paths` keyed by each path's `path-N`
pseudonym (the real repo-relative path under `--no-redact`, the same
"path" namespace a `report_path` claim uses) mapping to `{state, reason,
later_write_observed, attempts}`, `attempts` a list of `{agent_id,
ordinal}`. `repo_root`, `head`, and `git_version` are reader-internal and
never exported: a `repo_root` is a real absolute path with no pseudonym
defined for it, and neither the commit id nor the git version is a fact
this milestone's report needs to expose.

`to_json(run, findings, redactor, observation=None, silenced_detectors=
frozenset())` wraps export_run with `schema`, rules, verdict, and a
top-level status, matching 4.7's `{schema, run, findings, rules, verdict,
status, stories, next}`, plus `observation` when one was passed. `findings`
is `export_run`'s output over the full, unfiltered `findings` list (every
finding, `silenced` marked where it applies); `verdict`, `rules`, `stories`,
and `next` are instead all derived from the shown (non-silenced) subset
(`config.partition_silenced`'s first return value) -- the same "never lose
it from JSON, never let it drive a decision" split 4.6/4.10 describe.
Neither ever reaches for a dataclass's own private, complete dict form
(that form exists for tests only).

`stories` (unit M3a, 4.7): a list of `{agent, severity, text, finding_ids}`,
one per `report._story_records` entry (`verbose=False`, matching the
default text report, computed over the shown findings only -- unit M3c).
`agent` is the same `agent-N` pseudonym (or the literal `run` for the
run-level story) the AGENT table and text STORIES section use; `text` goes
through `Redactor.title()` (a story sentence routinely embeds a label
verbatim, same as `Finding.title`); `finding_ids` carries no user text and
needs no redaction. `next` (4.7): a list of `{grade, text, command}`, one
per `report._what_to_do_items` entry, also computed over the shown findings
only; neither field is a structured name field (a command names only a run
id), so both go through the plain Redactor, structural `redact()` only, the
same as a metrics value.

### 5.3a JSON schema 2: top-level key sets (v0.2, unit M3c)

**(Superseded by 5.3b in N5a-1: no command emits schema 2 any more. This
section stays as the record of the shape schema 3 extends.)**

`schema: 2` is a plain integer, the same key and value on every command's
`--json` output below; nothing in this milestone changes what an existing
key means, only what is added. `null` means not applicable to this run/row;
the literal string `"UNKNOWN"` means the reader could not establish it
(4.1's `FACTS_UNKNOWN`). This table is generated by reading `report.py`,
`trend.py`, and `cli.py` directly, not by design intent; if code and this
table ever disagree, run the read again before trusting either one.

- **`run --json` / `demo --json`** (`report.to_json`): top-level
  `{schema, run, findings, rules, verdict, status, stories, next}`, plus
  `observation` (`run --observe` only; `demo` never passes one). `run`:
  `{run_id, kind, status, claude_version, started_at, ended_at, counts,
  diagnostics, attempts}`; `counts`: `{agents, attempts, interrupted}`.
  Each `attempts[]` entry: `{agent_id, key, label, phase, agent_type,
  ordinal, outcome, first_ts, last_ts, usage, event_count, interrupt,
  reported, facts}`. `interrupt` is `null` or `{cause, silence_seconds}`
  (`silence_seconds` nullable). `reported` is `null`, or `{type: "dict",
  keys, claims}`, or `{type: "text", text_length}`. `facts` is `null` (no
  transcript at all) or `{paths_written, in_flight, commits,
  commit_commands_without_id, cwds, background_unfinished, last_tool,
  seconds_since_last_tool_at_end}` -- every one of those eight fields is
  independently either its normal shape (`commit_commands_without_id` an int,
  FA6) or the literal string `"UNKNOWN"` (a transcript that exists but could not
  be read); `last_tool` and `seconds_since_last_tool_at_end` are nullable on
  their own terms too. `paths_written`, `in_flight[].path`, and `cwds` are
  each their own `path-N` pseudonym by default (F4 hardening, 5.1/5.3),
  raw only under `--no-redact`; the same raw path anywhere else in the
  payload (a `report_path` claim, an `observation` path) gets the identical
  pseudonym in one render. Each `findings[]` entry: `{detector, severity,
  title, run_id, agent_id, evidence, metrics, rule_id, rule_ids,
  finding_id, silenced}` (`agent_id` and `rule_id` nullable; `silenced` new
  in this unit, 5.3). `rules[]`: `{rule_id, count}`. `stories[]`: `{agent,
  severity, text, finding_ids}`. `next[]`: `{grade, text, command}`
  (`command` nullable). `observation`, present only under `--observe`:
  `{checked, reason, paths}` (`reason` nullable), `paths` keyed by a
  `path-N` pseudonym mapping to `{state, reason, later_write_observed,
  attempts}` (`reason` nullable; `attempts[]`: `{agent_id, ordinal}`).
- **`scan --json`** (`cli._cmd_scan`): top-level `{schema, rows, totals}`.
  Each `rows[]` entry: `{run_id, kind, date, project, agents, attempts,
  verdict, top_detector, finished, attention, attention_reason, age,
  possibly_live, paths_at_risk, unfinished_state}` (`top_detector` and
  `attention_reason` nullable). `totals`: `{workflow, session,
  findings_per_detector}`; `workflow`: `{runs, runs_with_retries, attempts,
  wasted_hours}`; `session`: `{runs, subagents, interrupted}`;
  `findings_per_detector`: `{<detector id>: count}`.
- **`trend --json`** (`trend.to_json`): top-level `{schema, weeks,
  weekly_counts, before_after}`. `weekly_counts`: `{<detector id>:
  {<ISO week>: count}}`. Each `before_after[]` entry: `{rule_id, target,
  applied_at, kinds, before, after}`; `before`/`after`: `{runs, fired,
  rate}` (`rate` nullable, `null` when `runs` is 0).
- **`rules --json`** (`cli._cmd_rules`, the plain listing, not `--show`):
  top-level `{schema, rules}` (unit M3c: this used to be a bare list; the
  list is now the `rules` key so this command can carry `schema` too).
  Each `rules[]` entry: `{rule_id, title, count}`. `rules --show --json`
  is unchanged and out of scope for this table: `{rule_id, title,
  enforcement, detectors, target_kind, text}`, no `schema` key.
- **`rules propose --json`** (`cli._cmd_rules_propose`, v0.2, unit M6):
  top-level `{schema, proposals}`. Each `proposals[]` entry: `{rule_id,
  target_kind, findings, runs, first, last, adopted, since, text}`
  (`findings`/`runs` the finding and distinct-run counts backing the
  proposal; `first`/`last` the earliest/latest backing run's own start date,
  `"YYYY-MM-DD"` or null; `adopted`/`since` as in 5.3b). `proposals` is `[]`,
  never an error, when no rule clears the threshold (section 9).
- **`rules review --json`** (`cli._cmd_rules_review`, v0.2, unit M6):
  top-level `{schema, reviews, consider_removing}`. Each `reviews[]` entry:
  `{rule_id, target_kind, detectors, adopted_at, target, before, after}`
  (`target` is `target-N` alone by default, F13 hardening -- the raw string,
  basename included, only under `--no-redact`; see section 9 for why this
  differs from `trend --json`'s own `target-N (basename)` rendering.
  `detectors`: the rule's own backing detector ids; `before`/`after`: the
  same `{runs, fired, rate}` shape `trend --json`'s own `before_after[]`
  uses, windowed to `--weeks` on each side of `adopted_at` instead of
  unbounded). `consider_removing`: the `rule_id`s whose `after.rate` is not
  lower than `before.rate` with both windows non-empty. `reviews` is `[]`
  (and `consider_removing` `[]`) when the owner has adopted nothing, in
  config or in the state store's legacy `applied` entries.
- **`rescue plan --json`** (`rescue.plan_to_json`, v0.2, unit M4): top-level
  `{schema, run, nothing_to_rescue, units, brief, exit_code}`. `run`:
  `{run_id, kind, finished, possibly_live}`. Each `units[]` entry: `{agent,
  phase, attempts, end, task, reported, paths, commits, in_flight, unknown,
  checks, steps}` (`phase` nullable). `task` is `null` (no transcript for
  the unit's first attempt, or no user event on it) or `{pointer, length,
  text}` (`text` is `null` unless `--no-redact`). `reported` is `null`, or
  `{type: "text", text_length}`, or `{type: "dict", claims}` (a
  `report_path` claim substituted with its `path-N` pseudonym the same way
  5.3 already describes; a `status` claim is the fixed string `"set"` by
  default, F7 hardening, its real value only under `--no-redact`). Each
  `paths[]` entry: `{path, state, reason,
  attempts, later_write_observed}` (`reason` nullable, `attempts[]` a list
  of ordinals, not `{agent_id, ordinal}` objects -- every path here belongs
  to the one unit already named by the entry, so there is nothing else to
  say about a writer but which attempt it was). `in_flight[]`: `{tool,
  path}` (`path` nullable). `unknown` is a list of one to three sentences
  (4.11: the fixed sentence, an unchecked-observation reason, and/or
  "UNKNOWN facts: transcript missing" when a risk attempt's own `facts` is
  `None`, F6 hardening).
  `checks` is a list of declared checks, each its own `check-N` pseudonym by
  default (F7 hardening; the whole command hidden, not only an embedded
  path), raw under `--no-redact`; at `local` each is the real command after the
  free-text path scanner of 5.1 (round 4), so `checks[]`, `steps[].text`,
  `steps[].command` and `last_counted_run.command` show no absolute path.
  Each `steps[]` entry:
  `{grade, text, command}` (`command` is null only for a `"decide"` step that names no
  command; the two `"look"` steps always carry a non-null string, the whole-tree `git status
  --short` and `git diff` when no path is named, N8b K4.1; only the two `"look"` steps
  are present at all for a possibly-live run). `brief` is `null` for a
  possibly-live run, a nothing-to-rescue run, or a run with an unchecked risk population, else `{killed, failed,
  already_exists}`: `killed[]`: `{agent, ordinal, time, silence_seconds}`
  (`time` a `"HH:MMZ"` string or `null`); `failed[]`: `{agent, cause,
  retry_now, reset_time}`; `already_exists`: `{paths, commits}` (`commits`: ids of unverified, summary-shaped output lines, 4.13.1; the key's name does not say a commit exists).
- **`rescue status --json`** (`rescue.status_to_json`, v0.2, unit M4):
  top-level `{schema, run, units, not_counted, never_matches,
  transcript_unknown, exit_code}`. `run`: `{run_id}` (nothing else about a
  run this command inspects is exported: 4.11 already covers its own
  privacy). Each `units[]` entry: `{agent, checks_declared,
  last_counted_run, word, writes_after, write_after_check, cycles}`.
  `last_counted_run` is `null` (no counted run at all, `word` then
  `NOT_OBSERVED`) or `{command, completion, at}` (`command` is the check's
  own `check-N` pseudonym by default, F7 hardening, raw under `--no-redact`;
  `completion` is `"completed_ok"`/`"completed_error"`; `at` an ISO
  timestamp or `null`). `word` carries the same `NEVER_COMMAND_OBSERVED`
  suffix the text render's word does whenever a NEVER-list match was
  observed (F3 hardening), appended after a space, never substituted. Each
  `not_counted[]` entry: `{line, reason, excerpt}` (`reason` includes
  `"ambiguous tool id"`, F5 hardening, and `"a Bash command that may write
  ran after the check"`, F3 hardening, alongside the pre-v0.2 reasons). Each
  `never_matches[]` entry: `{line, excerpt}`. An `excerpt` here, in both
  lists, is by default only the matched rule's fixed spelling or the word `command` (N3b C1; F7 hardening had printed the first word: a
  full 60-char excerpt, even path-N-substituted, still carried private flags
  and arguments); the full transcript quote (the first 60 characters of the
  Bash command, redacted the same structural way an evidence line is,
  4.5/5.2, with any listed path substituted by its `path-N` pseudonym) is
  shown only under `--no-redact`.

### 5.3b JSON schema 3 (contract; N3 to N5)

**Schema 2 is retired (N5a-1; it was C-3, D-p, folded in N3c).** Between
N3b and N5a-1, versioned schema-2 JSON carried raw agent ids as v0.2 did,
the one exception to the share invariant (4.13.5), frozen by byte goldens
(`tests/test_schema2_goldens.py`). N5a-1 switched every versioned JSON
command to the complete schema 3 below in one commit and replaced those
goldens with schema-3 goldens (`tests/test_n5_schema3_goldens.py`,
`tests/fixtures/golden/schema3/`: `share`, `local`, `raw` over the same
fleet, a frozen clock, and a fixed installation secret). The exception is
gone: schema-3 JSON at `share` carries aliases, never a raw agent id.
`--local --json` is valid. Schema 3 is not frozen the way schema 2 was: a
later N5 part that changes behavior (severity, observation groups) changes
the goldens it affects, each change listed with its cause in that part's
commit and report.

**Exception for `AttemptFacts.commits` (C-14, N4a).** The one content
change schema 2 accepts before N5 is the correction of the existing
`AttemptFacts.commits` value (and the commit counts that render from it) to
the commit grammar of 4.13.1: no new key, no other content change, and every
golden it would change is named. The golden fleet expected none (its
one commit, `git commit -m 'synthetic change'` with the result `[main
abc1234] synthetic change`, was kept; the mid-line `[x 1234567]` after an
`echo` stayed excluded).

**Activation (D1; landed in N5a-1).** "Every versioned JSON command continues to emit
schema 2 through N4. N3 and N4 add internal primitives and text behavior
without changing versioned JSON. N5 atomically changes every versioned JSON
command to the complete schema-3 shape below. No released payload uses
schema 3 before all required schema-3 fields are present." Concretely:
every command below keeps the literal integer `schema` key in the same
top-level position, so a consumer branches on its value rather than
guessing the shape from which keys happen to be present; every command's
own `--json` carries `schema: 2`, in the exact shape 5.3a describes, for
the whole of N3 and N4 (N3's disclosure primitives and N4's ledger are
built and tested beneath the JSON surface, but do not yet reach it); on the
commit that lands N5, every command's `schema` becomes `3` and every
schema-3 key below becomes present, in the same commit, never a
command-by-command rollout and never a `schema: 3` payload missing a
required schema-3 field. This section lists the complete top-level key set
for schema 3, built the same way 5.3a was: by reading `report.py`,
`trend.py`, `rescue.py`, `watch.py`, and `cli.py` directly, not by design
intent.

**Conventions shared by every command below, new in schema 3:**

- **Disclosure field.** Every `--json` payload below (including
  `rules --show --json`, newly brought into this section's scope, D12)
  carries a top-level `disclosure` key, one of `"share"`, `"local"`,
  `"raw"` (4.13.5), naming the level that produced the rest of the payload;
  a consumer never has to infer the level from which fields happen to be
  null or missing. `watch --json` is the one exception: 4.12 gives it no
  `--no-redact` at all, so it always runs at `share` and prints no
  `disclosure` key -- there is only ever one level for it to name.
- **Aliases instead of display names (D10).** An `agent_id` field, and a
  scan row's `project` field, carry the registry alias of 4.13.5
  (`a:<alias>`, `project:<alias>`, at least 12 hex) under `share`, the
  real id or directory name under `local` and `raw`. This replaces the old
  per-render, reset-each-render `agent-N`/`project:<6-char hash>` pseudonym
  of 5.1/5.3a: an alias is now the same string across `scan`, `run`, and
  `rescue` on one machine, never renumbered from 1 on every render; when
  the installation secret is unavailable, it falls back to that same
  per-render positional pseudonym instead (4.13.5's "Secret unavailable"),
  never to a raw name. At `share`, a JSON command registers the identities
  its payload may carry (the same collection its text render uses) in one
  locked registration through the same guarded helper text uses, so one
  output never mixes stable and positional aliases; when the book is
  unavailable it renders once more positionally and prints the one stderr
  diagnostic of 4.13.5, never on stdout. An attempt or finding whose agent
  id is not canonical is named positionally (`agent-N`), never by its label
  or an HMAC of the invalid value. `local` and `raw` register nothing. A `phase` field keeps its existing `phase-N`
  positional pseudonym unchanged (4.13.5: a phase has no stable identity of
  its own to alias). A `path-N` pseudonym (any `path`-shaped field already
  in this namespace per 5.1/5.3a -- `paths_written`, `in_flight[].path`,
  `cwds`, a `report_path` claim, and now a ledger entry's own `path`/
  `repo`) is unchanged by this milestone; it is not folded into the
  agent/project alias scheme.
- **No excerpt fields in share.** In `share`, no payload below carries an
  excerpt-shaped key at all, not merely a null one: `rescue plan`'s
  `task.text`, `rescue status`'s `not_counted[].excerpt` and
  `never_matches[].excerpt`, and every other excerpt field are present as
  keys only under `local` or `raw` (and, per 4.13.5/D10, only when the
  command was also given `--evidence`), and absent from the object
  entirely under `share` -- a change from 5.3a, where the same fields were
  present and `null` by default under `--no-redact`'s single on/off switch.
  Evidence strings (`findings[].evidence`) follow the text renderer's
  evidence view (4.13.5): at `share` each is a pointer with an aliased file
  name plus its marker code, never an excerpt; at `local` and `raw` a
  pointer, and `file:line: excerpt` only when the command was given
  `--evidence` (`run` and the two `rescue` commands take it; every other
  JSON command has no evidence strings). `rescue plan`'s `task.text` and
  `rescue status`'s two `excerpt` keys are absent unless the level is
  `local` or `raw` and `--evidence` was given.
- **`null` versus `"UNKNOWN"`.** Unchanged from 5.3a: `null` means not
  applicable to this run or row; the literal string `"UNKNOWN"` means the
  reader could not establish it (4.1's `FACTS_UNKNOWN`). Schema 3 adds one
  more place this pair applies: a ledger entry's own `path` and `repo`
  (4.13.1) are `null` when the entry never named one; they are never the
  string `"UNKNOWN"`, since an entry that cannot establish a path is still
  a known `possible`/`opaque_command` entry, not an unreadable one --
  unreadability makes the whole ledger `partial` instead (its
  `completeness`/`reasons` fields, below), never a per-field `"UNKNOWN"`.
- **`line` rendering (D12, corrected in N5a-1).** Every `path:line`
  evidence pointer (a ledger entry's own `line`; a rescue task `pointer`; an
  evidence string already covered by 5.3) renders its file half per the
  active disclosure level and 4.13.5's pointer rules: under `share`, a
  transcript file name is its aliased spelling (`agent-<alias>.jsonl`,
  `session-<alias>.jsonl`; positional `agent-N.jsonl` on fallback), never
  the real `agent-<17 hex>.jsonl` (that name embeds the raw agent id), and
  `journal.jsonl` is kept; a name that is no canonical identity renders an
  `evidence-N` placeholder, and an attempt with no canonical agent id (no
  pointer, 4.13.5) renders `evidence-N` for a ledger `line` and `null` for a
  rescue task `pointer`; under `local`/`raw`, every file name renders real.
  The line number is never redacted.
- **Exit-code precedence (D13).** `rescue plan` and `rescue status` keep
  carrying their own `exit_code` key, unchanged in meaning; `run`, `demo`,
  `scan`, `trend`, and every `rules` subcommand still communicate their
  exit code only through the process's own exit status, never duplicate it
  as a JSON key. One ordered table, replacing any looser cross-reference:

  | Command | exit 1 | exit 2 | exit 3 | exit 4 | else |
  |---|---|---|---|---|---|
  | `run`, `demo`, `scan` | usage or configuration error | no run | verdict FAILED | any INCOMPLETE | 0 |
  | `trend`, `rules` (list), `rules propose`, `rules review` | usage error | no runs | n/a | n/a | 0 |
  | `rules show` | usage error, or an unknown rule id | n/a | n/a | n/a | 0 |
  | `rescue plan`, `rescue status`, `watch` | their own 4.11/4.12 tables, unchanged, except that a lifecycle `terminal: unknown` (4.13.3) now counts wherever that table already has an INCOMPLETE-shaped code | | | | |

**Per command (D12).** Every key below is given its type, enum,
nullability, and disclosure transform in full; a key already in 5.3a is
repeated here rather than cross-referenced, so this section stands alone.

**`run --json` / `demo --json`:**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `run` | object | below | no | -- |
| `findings` | array | `Finding` objects, below | no (`[]` allowed) | -- |
| `rules` | array | `{rule_id: str, count: int}` | no | none (no user text) |
| `verdict` | str | `FAILED`\|`DEGRADED`\|`CLEAN` | no | none |
| `status` | str | `COMPLETE`\|`INCOMPLETE` | no | none |
| `stories` | array | `{agent: str, severity: str, text: str, finding_ids: [str]}` | no | `agent` alias (above); `text` through `Redactor.title()` |
| `next` | array | `{grade: str, text: str, command: str\|null}` | no | plain `redact()` |
| `observation` | object | `{groups: [group]}` (N5c-1, below); each `paths` value's `attempts[]` entry is `{agent_id, ordinal}` with `agent_id` through the alias rule (`main` for a session main thread) | absent unless `--observe` | `repo`, every `paths` key, and every `unobserved_paths` element use the path namespace of the level |

`observation.groups[]` (N5c-1, plan S6): exactly `group` (int, the index in this
list), `repo` (str, the candidate repository; null for the unknown group),
`refused` (bool), `checked` (bool), `reason` (str or null, from the closed list
`refused`, `historical`, `not a git repository`, `git unavailable`, `guard
refused`, `outside the work tree`; null for a checked group), `paths`
(`{path: {state, reason, later_write_observed, attempts}}`, empty unless the group
was checked) and `unobserved_paths` (sorted list of the group's ledger paths that
no process looked at: all of a refused, unknown, or unchecked group, and a checked
group's paths outside its toplevel). Named repositories come first in sorted
candidate order, the unknown group last and only when it holds an attempt or a
path. `repo_root`, `head`, and `git_version` never leave the tool. With no risk
population the list is empty.

`run` object:

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `run_id` | str | `wf_...` \| `s_<10 hex>` | no | none (carries no user text) |
| `kind` | str | `workflow`\|`session` | no | none |
| `status` | str | `COMPLETE`\|`INCOMPLETE` | no | none |
| `claude_version` | str | -- | yes | none |
| `started_at` / `ended_at` | str | ISO 8601 | yes | none |
| `counts` | object | `{agents: int, attempts: int, interrupted: int}`; `interrupted` counts every attempt with an `interrupt` (the kill sentinel), whatever its cause and whatever its `outcome` (E1, v0.3.1) | no | none |
| `diagnostics` | array[str] | -- | no (`[]`) | plain `redact()` |
| `attempts` | array | `Attempt` entries, below | no (`[]` allowed) | -- |
| `lifecycle` | object | below | yes -- null for a session row (D11); never null for a workflow row once N2 lands | -- |

`lifecycle` object (workflow rows only, D11/4.13.3):

| Key | Type | Enum | Nullable | Disclosure |
|---|---|---|---|---|
| `terminal` | str | `completed`\|`failed`\|`stopped`\|`journal_resolved`\|`unknown`\|`none` | no | none |
| `source` | str | the enum of 4.1: `notification`\|`task_stop`\|`launch_result`\|`journal`\|`conflict`\|`unrecognized_status`\|`incomplete_generation`\|`parent_unavailable`\|`ambiguous_launch` | yes -- null iff `terminal` is `none` | none |
| `activity` | str | `recent`\|`stale`\|`unknown` (the stat failed) | no | none |
| `generations` | int | >= 0 (0 for a run with no associated launch) | no | none |

`attempts[]` entry:

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `agent_id` | str | `a:<alias>`, at least 12 hex (share) \| real 17-hex id (local/raw) | no | alias (D10) |
| `key` | str | sha256 first 8 hex | no | none (hashed, unconditional even under `raw`, 5.3) |
| `label` | str | -- | no | pseudonym (`agent-N` / built-in literal / `type-N`), 5.1 |
| `phase` | str | `phase-N` | no | positional, unchanged |
| `agent_type` | str | built-in literal or `type-N` (share); the real type (local/raw) | yes -- null when the attempt has no type | pseudonym, 5.1 |
| `ordinal` | int | >= 1 | no | none |
| `outcome` | str | `completed`\|`failed`\|`killed`\|`superseded`\|`unknown`\|`open`. A workflow attempt: the journal state machine's outcome (unchanged). A session subagent (E1, v0.3.1): its effective end, one public meaning, what the evidence establishes: the kill sentinel -> `killed`; else `returned` -> `completed`, `reported_failed` -> `failed`, `reported_killed` -> `killed`, `pending` -> `open`, `unknown` -> `unknown`. The internal whole-transcript outcome stays private and keeps driving the risk population, ledgers, WASTED and rescue units | no | none |
| `return_state` | str | `returned`\|`reported_failed`\|`reported_killed`\|`pending`\|`unknown` for a session subagent (4.13.1); null for a workflow attempt and the main thread | yes | none |
| `return_reason` | str | `none`\|`no_evidence`\|`conflict`\|`malformed`\|`delivery_unknown`\|`ambiguous`\|`parent_unreadable`\|`parent_missing`\|`status_unrecognized` for a session subagent; null otherwise | yes | none |
| `resumes` | int | >= 0; delivered resume requests (4.13.1 T3); 0 for a workflow attempt | no | none |
| `first_ts` / `last_ts` | str | ISO 8601 | yes | none |
| `usage` | object | token totals | no | none |
| `event_count` | int | -- | no | none |
| `interrupt` | object | `{cause: str, silence_seconds: float\|null}` | yes | plain `redact()` |
| `reported` | object | `{type:"dict", keys, claims}` \| `{type:"text", text_length: int}` | yes | plain `redact()`; `claims.report_path` -> `path-N`; at share `keys` lists `key-1`..`key-N` (G-B1, GB-8) |
| `facts` | object | the 7-field `AttemptFacts` shape of 5.3a | yes -- null only when no transcript exists | `paths_written`/`in_flight[].path`/`cwds` -> `path-N` |
| `ledger` | object | `{completeness, reasons, entries}`, below | no -- every attempt has one (4.1, 4.13.1) | -- |

`ledger` object (new, D12/4.13.1):

| Key | Type | Enum | Nullable | Disclosure |
|---|---|---|---|---|
| `completeness` | str | `complete`\|`partial` | no | none |
| `reasons` | array[str] | the closed D6 reason codes (4.13.1) | no (`[]` when complete) | none |
| `entries` | array | `LedgerEntry` objects, below | no (`[]` allowed) | -- |

`LedgerEntry` (D12/4.13.1):

| Key | Type | Enum | Nullable | Disclosure |
|---|---|---|---|---|
| `effect` | str | `observed`\|`claimed_target`\|`attempted`\|`possible` | no | none |
| `basis` | str | `tool_result`\|`command_grammar`\|`delegation`\|`opaque_command`\|`background_process`\|`unknown_tool` | no | none |
| `completion` | str | `success`\|`error`\|`no_result`\|`ambiguous` | no | none |
| `path` | str | normalized path | yes | `path-N` |
| `repo` | str | resolved repository directory | yes | `path-N` (same namespace) |
| `line` | str | `path:line` | no | per the `line`-rendering convention above |

`findings[]` entry (unchanged in shape from 5.3a; repeated in full):
`{detector: str, severity: str, title: str, run_id: str, agent_id: str\|null,
evidence: [str], metrics: object, rule_id: str\|null, rule_ids: [str],
finding_id: str, silenced: bool}`. `severity` is one of
`major`\|`minor`\|`info` in practice once N5 lands (`critical` stays in the
value's own vocabulary for a future observer, 4.13.4, but no v0.3 detector
produces it). `agent_id` here follows the same alias transform as the
`attempts[]` entry's own `agent_id`; `evidence` and `metrics` through the
plain Redactor (5.3).

**`scan --json`:**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `rows` | array | below | no (`[]` allowed) | -- |
| `totals` | object | `{workflow, session, findings_per_detector}`, 5.3a, unchanged | no | none |

`rows[]` entry:

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `run_id` | str | -- | no | none |
| `kind` | str | `workflow`\|`session` | no | none |
| `date` | str | `YYYY-MM-DD` | no | none |
| `project` | str | `project:<alias>`, at least 12 hex (share) \| real directory name (local/raw) | no | alias (D10) |
| `agents` | int | -- | no | none |
| `attempts` | int | -- | no | none |
| `verdict` | str | `FAILED`\|`DEGRADED`\|`CLEAN` | no | none |
| `top_detector` | str | detector id | yes | none |
| `finished` | bool | lifecycle-backed (4.13.3/D11), not mtime-backed | yes -- null for a session row | none |
| `attention` | bool | true only through 4.7 rules (b) to (d); a clean possibly-live run is false (FB2) | no | none |
| `attention_reason` | str | -- | yes -- null whenever `attention` is false (FB2) | plain `redact()` |
| `age` | str | `"possibly live"`\|`"<n>m/h/d ago"`\|`"unknown"` | no | none |
| `possibly_live` | bool | -- | no | none |
| `paths_at_risk` | int | distinct ledger paths over the 4.13.1 risk population (N8b, K4.4: effects `observed`, `claimed_target`, `attempted`, plus `facts.paths_written`); a lower bound when `paths_at_risk_complete` is false | no | none |
| `paths_at_risk_complete` | bool | true only when `risk_population_completeness` is `complete` and every risk-population ledger is `complete` (N8b, K4.4); every shell call leaves a ledger partial (C-15) | no | none |
| `unfinished_state` | str | **workflow row (D11):** `possibly running`\|`no terminal record`\|`failed`\|`stopped`\|`unknown`\|null -- `abandoned` is removed; derived only from the lifecycle, never from per-key journal state: terminal `completed` or `journal_resolved` gives null (even when an older generation left a journal key without a result), `failed` gives `failed`, `stopped` gives `stopped`, `unknown` gives `unknown`, `none` with activity `recent` gives `possibly running`, `none` with activity `stale` or `unknown` gives `no terminal record`; **session row (D11):** `possibly running`\|`no terminal record`\|null, never `abandoned` | yes | none |

**`trend --json`:**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `weeks` | int | the number of ISO weeks requested (`--weeks`); the weeks themselves are the keys of each `weekly_counts` row (corrected in N5a-1: the code and schema 2 always emitted the count) | no | none |
| `weekly_counts` | object | `{<detector id>: {<ISO week>: int}}` | no | none |
| `before_after` | array | below | no (`[]` allowed) | -- |

`before_after[]` entry: `{rule_id: str, target: str, applied_at: str,
kinds: [str], before: {runs, fired, rate}, after: {runs, fired, rate}}`
(`rate` nullable, null when `runs` is 0). `target` is unaffected by the
agent/project alias change -- it already names a rule's target file, not an
identity -- and keeps the existing `target-N (basename)` rendering of 4.7/9.

**`rules --json` (the plain listing):**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `rules` | array | `{rule_id: str, title: str, count: int}` | no (`[]` allowed) | none |

**`rules --show --json`** (newly brought into this section's scope, D12):

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `rule_id` | str | -- | no | none |
| `title` | str | -- | no | none |
| `enforcement` | str | -- | no | none |
| `detectors` | array[str] | detector ids | no | none |
| `target_kind` | str | `"workflow prompt"`\|`"agent prompt"`\|`"hook"`\|`"project instructions"` (4.13.8, section 9) | no | none |
| `text` | str | the rule's managed-block text | no | none |

No `schema` key: unchanged from 5.3a, this command was never versioned the
way the others are.

**`rules propose --json`:**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `proposals` | array | `{rule_id, target_kind, findings: int, runs: int, first: "YYYY-MM-DD"\|null, last: "YYYY-MM-DD"\|null, adopted: "YYYY-MM-DD"\|null, since: {findings: int, runs: int}\|null, text}`; `adopted` is the earliest `[[adopted]]` date for the rule, `since` is null exactly when `adopted` is null, and an adopted row's `findings`, `runs`, `first`, `last` count the since-adoption population (section 9); `first`/`last` are null when no dated run backs the row | no (`[]` when nothing clears the threshold) | none |

**`rules review --json`:**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `reviews` | array | `{rule_id, target_kind, detectors: [str], adopted_at, target, before, after}` | no (`[]` allowed) | `target` keeps section 9's `target-N`-alone rendering, unaffected by the agent/project alias change |
| `consider_removing` | array[str] | rule ids | no (`[]` allowed) | none |

**`rescue plan --json`:**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `run` | object | `{run_id, kind, finished, possibly_live}` -- `finished` is `bool \| null` (null for a session), lifecycle-backed (4.13.3), not mtime-backed | no | `run_id` none |
| `nothing_to_rescue` | bool | -- | no | none |
| `empty_state` | str | null whenever `units` is not empty; else `nothing_to_rescue`\|`nothing_yet`\|`no_rescue_unit_unestablished_returns`\|`unchecked_risk` (4.11, R1) | yes | none |
| `return_breakdown` | object | null unless `empty_state` is `no_rescue_unit_unestablished_returns`; else `{reported_failed, reported_killed, possibly_running, no_return_evidence, not_established}`, five ints (4.7's breakdown classes) | yes | none |
| `groups` | array | the `observation.groups[]` objects of `run --observe` (N5c-1, above), the unknown group left out when it names no path; each `paths[]` row names its group by index in this array | no (`[]` allowed) | the path namespace of the level |
| `units` | array | below | no (`[]` allowed) | -- |
| `brief` | object | `{killed, failed, already_exists}`, 5.3a, unchanged | yes -- null for a possibly-live run, a nothing-to-rescue run, a session with no rescue unit and an unestablished return, or a run with an unchecked risk population | per 5.3a |
| `exit_code` | int | 4.11's own table, D13's lifecycle-unknown addition | no | none |

`units[]` entry keeps 5.3a's shape (`{agent, phase, attempts, end, task,
reported, paths, commits, in_flight, unknown, checks, steps}`); `paths[]`
changes per D12's split below. `task` is `null` or `{pointer, length}` plus
`text` under the evidence rule above; `pointer` is `str | null` per the
`line` convention (null for an attempt with no canonical agent id at
`share`). `commits` (and every `commits` count in a schema-3 payload, the
`facts.commits` list, and `brief.already_exists.commits`) documents unverified,
summary-shaped output lines (4.13.1): a value says a line of that shape appeared
in a command result, never that a commit was created or exists. Every other key
is unchanged from 5.3a.

`paths[]` entry (D12: observation state and ledger facts in separate
fields, replacing the single ambiguous `state`/`reason` pair):

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `path` | str | -- | no | `path-N` |
| `group` | int | the index of the path's group in the top-level `groups` array (N5c-1); null when no group lists the path | yes | none |
| `observation` | object | `{state, reason}`, below | yes -- null when the path's group was refused, unknown, or not checked, or the path was never observed (N5c-1; the `observe_run` single observation is retired) | -- |
| `ledger` | array | one `{attempt, effect, basis, completion, line}` element per ledger entry for that path, in attempt order (4.13.11); nothing is collapsed | no (`[]` when no ledger entry names the path) | `line` per the `line` convention |
| `attempts` | array[int] | ordinals | no (`[]` allowed) | none |
| `later_write_observed` | bool | -- | no | none |

A consumer reads `observation` and `ledger` independently and never
coerces one vocabulary into the other -- this is what closes finding 12's
precedence ambiguity. `observation.state` is one of `DIRTY_NOW`\|
`CLEAN_NOW`\|`MISSING_NOW`\|`UNKNOWN` (not nullable once `observation`
itself is non-null); `observation.reason` is `observe.py`'s own reason
string (for example "outside the work tree"), nullable, through plain
`redact()`. A `ledger` element's `attempt` is the attempt's ordinal (int);
its `effect`/`basis`/`completion` are `LedgerEntry`'s own values above,
none nullable; its `line` is the entry's `line`. An element belongs to a
path row when its entry's normalized `path` equals the row's raw path
normalized against the call's working directory, for an attempt of the
row's unit that is killed, failed, or superseded.

**`rescue status --json`:**

| Key | Type | Enum / shape | Nullable | Disclosure |
|---|---|---|---|---|
| `schema` | int | `3` | no | none |
| `disclosure` | str | `share`\|`local`\|`raw` | no | none |
| `run` | object | `{run_id}` | no | none |
| `units` | array | one element per (unit, declared repository), N5c-2: `{agent, repo, checks_declared, last_counted_run, word, writes_after, write_after_check, cycles}`; `repo` is `str \| null` (null for a unit with no declared repository containing its paths), the declared root in the path namespace of the level (`path-N` at share, the P19 spelling at local, the declared path at raw) | no (`[]` allowed) | per 5.3a; `repo` as stated |
| `not_counted` | array | `{line: int, reason: str, category: str}` plus `excerpt: str` under the evidence rule | no (`[]` allowed) | `excerpt` absent unless `local`/`raw` with `--evidence` (not merely null); `category` is the matched rule's own fixed spelling (`git checkout`) or the word `command` (N3b C1), never transcript text |
| `never_matches` | array | `{line: int, category: str}` plus `excerpt: str` under the evidence rule | no (`[]` allowed) | same rules as above |
| `transcript_unknown` | bool | -- | no | none |
| `empty_state` | str | as in `rescue plan --json`, from the same rule over `units` | yes | none |
| `return_breakdown` | object | as in `rescue plan --json` | yes | none |
| `exit_code` | int | 4.11's own table, D13's lifecycle-unknown addition | no | none |

**`watch --json`:** carries no `schema` key and no `disclosure` key, in
schema 2 or schema 3: each line is a single event object (4.12), not a
versioned payload, and 4.12 gives `watch` exactly one redaction level
(there is no `--no-redact`), so there is nothing for either key to
distinguish. Its event shapes, including the two new ones D18 adds:

| Event | Fields |
|---|---|
| `START` | `{"event": "START", "run": str, "agents": int, "attempts": int}` |
| `OK` | `{"event": "OK", "agent": str, "attempt": int}` |
| `KILL` | `{"event": "KILL", "agent": str, "attempt": int, "silence_seconds": float\|null, "restarted_as": int, "paths_written": int\|null, "commits": int\|null, "run_id": str, "details_unavailable": bool}` (`details_unavailable` true only on the F10 forced-fallback line, which also nulls `silence_seconds`/`paths_written`/`commits`) |
| `FAILED` | `{"event": "FAILED", "agent": str, "attempt": int, "cause": str\|null, "reset_time": str\|null}` |
| `QUIET` | `{"event": "QUIET", "agent": str, "attempt": int, "silence_seconds": float\|null}` |
| `RESET` | `{"event": "RESET"}` |
| `DONE` | `{"event": "DONE", "agents": int, "attempts": int, "verdict": str, "run_id": str, "next_command": str}` -- `verdict` reflects 4.13.4's severity model as of N5 |
| `LIMIT` | `{"event": "LIMIT", "hours": float}` (ordinary timeout) or `{"event": "LIMIT", "reason": "malformed"}` (the two malformed-journal cases) |
| `MALFORMED` | `{"event": "MALFORMED", "reason": str, "line": int\|null}` |

`watch`'s own `agent` field is the event attempt's stable agent alias
`a:<alias>` (N3b, P14), the cross-command identity of 4.13.5, so a person can
carry it from a `watch` line to `run`; on an alias fallback it is a
positional `agent-N` and the one stderr line of 4.12 says so. `attempt` and
`restarted_as` keep their ordinals.

A consumer that reads `schema` before anything else is unaffected by any of
this: `scan --json`'s `totals`, for example, parses identically whether
`schema` is 2 or 3, since `totals` itself does not change shape in N3 to
N5.

### 5.4 Repository hygiene

- `tests/test_privacy.py` scans every tracked file (`git ls-files`) and, in
  CI, the built wheel and sdist contents. Rules: no UUID other than the fake
  `00000000-0000-4000-8000-000000000000`; no absolute path outside `/tmp`,
  `/home/user`, `~`, or `${CLAUDE_PLUGIN_ROOT}`; no `/Users/` (Unicode
  account names included); no email outside `example.com` or a domain
  carrying `noreply` as a label (not merely as a substring of the local
  part); every `wf_` id must appear in `tests/synthetic-ids.txt`; no line
  over 2000 chars in fixtures. A tracked symlink is audited by its target
  string (what git actually stores and `git archive` ships), not by
  resolving through it. A local, gitignored `tests/privacy-denylist.local`
  adds literal terms when present. The `/Users/` and non-`/home/user` path
  rules skip `redact.py` and its three test files (`test_redact.py`,
  `test_bug_report.py`, `test_detectors_core.py`), which must hold literal
  macOS- and Linux-style example paths (synthetic account names such as
  "alice", "bob", "tester") to test path redaction itself; nothing else in
  the tree is exempt. A wheel or sdist member gets the same exemption and
  fixture-line-length treatment as the repo-relative path it was built
  from. A violation message never repeats the value it caught: it carries a
  redacted label, a line number, a rule id, and a short digest of the
  match, never the UUID, path, email, wf id, denylist term, or identity
  itself, so a real regression cannot turn pytest or CI output into a
  second copy of the private data. The rule functions live once in
  `tests/privacy_rules.py`, imported by both `tests/test_privacy.py` and
  `scripts/check-privacy.sh`; it is a dev-only module and
  `src/agent_postmortem` never imports it.
- `scripts/check-privacy.sh` runs the same rules over the tree; over every
  commit reachable from any ref, each split into one per-file patch so the
  same path exemption and fixture rule apply there too; over every ref
  name; over every annotated tag object (tagger identity and message); and
  over commit author fields against the configured public identity. It
  additionally greps for `$(id -un)`. CI runs it on the tree; the author
  runs it before every push.
- Public history is produced by `scripts/export-public.sh`: the destination
  is resolved to an absolute path through its (already-existing) parent
  directory before the script changes to the repository root, so a relative
  destination stays relative to where the script was invoked rather than
  silently resolving inside the repository once the script has moved; a
  destination that already exists, or that resolves inside the source
  repository, is refused. `git archive` of HEAD then goes into that new,
  non-recursively-created directory, `git init`, one commit "Initial
  release v0.1.0" with the configured public identity, then
  `check-privacy.sh` on that repository. The private working copy is never
  pushed; its remote is unset and `push.default` is `nothing`.
- Review artifacts written during development live in `.reviews/`, which is
  gitignored.
- The demo run lives once, in
  `src/agent_postmortem/demo/-home-user-project/00000000-0000-4000-8000-000000000000/subagents/workflows/wf_00000000-d30/`,
  next to its parent session transcript
  `src/agent_postmortem/demo/-home-user-project/00000000-0000-4000-8000-000000000000.jsonl`
  (the standard layout, 4.2; M8 review D8: a conforming `wf_` run id, not the hash-fallback id a
  non-conforming directory name would produce), and the detector tests load
  that case from the package. No second copy.
- `tests/test_no_write_surface.py` (release finding F14) proves the tool's
  own non-negotiable ("read-only over the data it inspects") structurally,
  not by grepping for a handful of string literals a call could easily be
  spelled around: it parses the AST of every module under
  `src/agent_postmortem/` and lists every call matching one of the write- or
  process-capable sink shapes -- `open()`/`Path.open()`, `write_text()`,
  `write_bytes()`, `os.replace`, `os.rename`, `shutil.*`, `subprocess.*`,
  `os.system`, `os.exec*`/`os.spawn*`, `os.popen` -- with its module and
  line. The exact set found must equal an explicit, justified allowlist (a
  handful of `state.py` calls confined to the state directory, observe.py's
  one documented `subprocess.run` plus its `shutil.which`, and read-only
  (`'r'`/`'rb'`) `Path.open()` calls everywhere else); anything found that is
  not on the list fails the test, and an allowlist entry the sweep no longer
  finds fails it too, so the list can never silently go stale. A second test
  structurally confirms no call outside `state.py` ever opens a path in a
  writing mode, or calls anything other than observe.py's own justified
  `subprocess.run`/`shutil.which` -- a write to a caller-named path (a
  run/transcript path) outside the state directory therefore cannot exist
  without also breaking it.
- `tests/test_rendered_privacy.py` (release finding F14) renders one
  distinctive marker string -- planted in a repo-relative path, a first
  user message, a declared check command, and a self-reported `status`
  claim -- through five public commands end to end (`run --json`,
  `rescue plan`, `rescue status`, `rules review`, `scan`) and asserts the
  marker never appears in default output and appears only under
  `--no-redact` (except `scan`, whose own row/triage renderers never carry
  a prompt, raw path, check, or reported claim into their output at all --
  pinned down as a structural absence, not a redaction case, since there is
  no `--no-redact` counterpart to check). This complements, rather than
  duplicates, the narrower per-field redaction tests already in
  `tests/test_rescue.py` and `tests/test_rules_review.py`: its job is to
  catch a renderer that forgets one field while getting its neighbor right,
  the kind of gap a field-by-field unit test alone cannot see.

## 6. Fixtures

`tests/fixtures/runs/<case>/` with the same layout as a real run. Existing
workflow cases: clean_run, bash_backgrounded_timeout,
structured_output_corrupt, codex_write_denied, missing_artifact,
reader_minimal. silent_retry_kill now lives in the package instead
(`src/agent_postmortem/demo/-home-user-project/<session>/subagents/workflows/wf_00000000-d30/`,
section 5.4 and section 8.2's packaged demo): it is both the `agent-postmortem demo` run and the
case the detector, attribution, redaction, and export tests load, in one
place, never copied. silent_retry_kill is also the runtime-cause case for
section 4.4: each killed attempt's key has a later attempt (the restart)
and the parent transcript (a Workflow launch, its result and a completed
notification; no interrupt marker, no TaskStop) names no stop for either, so cause runtime, major, wins
by the time attribution reaches clause 3. New cases:

- `session_clean`: a main transcript with two subagents, no markers.
- `session_parent_stop`: child ends with the sentinel; the main transcript
  has a TaskStop naming the child's agent id 5 s earlier. Expected cause
  parent_stop, info.
- `session_human_interrupt`: child and main both carry the sentinel within
  10 s. Expected cause human, info.
- `session_unattributed_long`: child silent 400 s, main has neither.
  Expected cause unattributed, major.
- `session_unattributed_short`: child silent 20 s. Expected unattributed, info.
- `session_dedup`: two assistant entries sharing (message.id, requestId);
  usage counted once.
- `session_envelope`: the main transcript's Agent tool_result quotes
  WRITE_DENIED from a child; codex_write_denied fires once, on the child.
- `session_compaction`: a compact_boundary between the last real event and
  the sentinel; silence None.
- `evidence_noise`: a thinking block and a system-reminder tag directly
  before a sentinel; evidence is the last real assistant text.
- `incomplete_journal`: a journal with an undecodable line; status
  INCOMPLETE, verdict suffix present, exit 4.
- `hook_session/`: a fake `<root>/<slug>/<session>.jsonl` tree with one
  finished workflow run, one running one (journal without result), and one
  finished subagent; used by the hook tests.
- `session_bridge_only`: a one-line main transcript, `type` `bridge-session`,
  no `timestamp`, no `user`/`assistant` entry anywhere. Proves
  `discover_runs` skips it and `load_session` raises `NotARun` for it.
- `redaction_adversarial`: a workflow attempt whose phase
  (`adversarial-phase-never-a-label`) and agent_type
  (`adversarial-type-never-a-label`) never occur as any fixture's label.
  Used by tests/test_redact.py's fixture privacy sweep to prove it actually
  checks phase and agent_type, not only label.
- `self_reference`: a workflow run (4.3's self-reference rule) with three
  attempts: a `Read` of `markers.py` quoting `WRITE_DENIED` and "read-only
  sandbox", a Bash `sed -n '1,40p' src/x.py` quoting the classifier-outage
  sentence, and a Bash `node codex-companion.mjs task run` carrying a real
  `WRITE_DENIED`. Expected: `codex_write_denied` fires only on the third
  attempt; `classifier_unavailable` never fires.

Every fixture uses the fake session id, fake cwd `/home/user/project`, fake
slug `-home-user-project`, synthetic wf ids listed in
`tests/synthetic-ids.txt`, and obviously synthetic prompt text.

## 7. Claude Code plugin

### 7.1 Layout and install

```
.claude-plugin/marketplace.json    {name: "agent-postmortem", description, owner, plugins: [{name: "agent-postmortem", source: "./plugin", description}]}
plugin/.claude-plugin/plugin.json  name, version (== package version), description, author, homepage, repository, hooks, skills
plugin/hooks/hooks.json
plugin/hooks/run.sh
plugin/skills/postmortem/SKILL.md
plugin/README.md
```

Install: `/plugin marketplace add zeyusuntt/agent-postmortem`, then
`/plugin install agent-postmortem@agent-postmortem`. The plugin manifest and its marketplace
entry carry the same one-line description (the positioning line), and the marketplace has a
description of its own. `claude plugin validate --strict ./plugin` and `claude plugin validate
--strict .` (the marketplace file) pass in CI when the `claude` binary is available; the one
remaining plugin directory note, no icon, does not change the result (the icon is an owner
launch step, because the directory fixes it at the first portal save).

hooks.json, every command quoted with `${CLAUDE_PLUGIN_ROOT}`:

| event | matcher | command | timeout |
|---|---|---|---|
| SessionStart | (none) | `"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" session-start` | 90 |
| Stop | (none) | `"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" check` | 10 |
| StopFailure | (none) | same as Stop | 10 |
| SessionEnd | (none) | `"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" session-end` | 20 |
| PostToolUse | `^Workflow$` | `"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" workflow-launched` | 10 |

### 7.2 Hook protocol and behavior

`run.sh` derives one data directory up front (D9): `$CLAUDE_PLUGIN_DATA` when
set, else `${XDG_STATE_HOME:-$HOME/.local/state}/agent-postmortem/plugin`.
Never `/tmp` -- shared by every unrelated tool on the machine and often
cleared on reboot, which would silently re-arm the once-a-day gates far more
often than intended. This one `$DATA_DIR` is used for resolution step (1)
below, the 7.3 install, the once-a-day marker files, and the install log.

`run.sh <event>` then resolves a CLI in this order and passes the original
stdin through untouched -- byte for byte, trailing newline included, never
captured into a shell variable along the way (D10):
1. `$DATA_DIR/bin/agent-postmortem` (installed by 7.3);
2. `agent-postmortem` on PATH, only if `agent-postmortem hook capabilities`,
   run with stdin `/dev/null` (D10: a capability probe must never consume
   the real payload), prints a single well-formed JSON object whose
   `protocol` field is the integer `1` (not `1.5`, not `"1"`, not `true`)
   within 2 s;
3. otherwise exit 0 with, once per day, `{"systemMessage": "agent-postmortem
   CLI not found; run: uv tool install agent-postmortem"}`.
Any non-zero exit, or output that is not exactly one well-formed JSON object
(garbage, a stack trace, or two objects concatenated), from the CLI is
swallowed into exit 0 with no output. The hook never fails a turn.

JSON is validated with `python3 -c 'import json,sys; ...'` when a *working*
python3 is available (D2) -- presence on PATH is not enough, since a
restricted PATH can resolve nothing but macOS's `/usr/bin/python3` license
stub, which refuses to run at all; run.sh actually invokes it once and
caches whether it worked -- and with a strict regex otherwise (a single
`^\{.*\}$` object with no embedded `}...{ ` sequence, i.e. no concatenated
object, plus, for the capabilities probe specifically,
`"protocol"[[:space:]]*:[[:space:]]*1([^0-9.]|$)`).

The real hook call (and the 7.3 install) run under an internal deadline
shorter than this event's own outer hook timeout from the table above (D3):
check/workflow-launched 8 s, session-end 18 s, session-start 80 s, install
60 s -- overridable to any value by setting
`AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS` (12's D-B), which nothing outside
the test suite ever sets; this is what lets a test drive the real
deadline-kill path against a fake CLI sleeping a couple of seconds past a
1 s deadline instead of the real default. A deadline kill targets the whole
process group the launched command
was given (`setsid` when available makes it a new session/group leader
outright; otherwise a `set -m` subshell does the same for its own
backgrounded job), plus a `pkill -P` sweep by parent pid as a second net,
so a CLI (or `uv`) that itself shells out and backgrounds something of its
own is never left running as an orphan after run.sh has exited (D4). A
deadline kill is swallowed into the same silent exit 0 as any other
non-zero exit or malformed output.

The once-a-day missing-CLI hint and the 24 h install gate each take an
atomic, non-blocking `mkdir`-based lock under `$DATA_DIR` before
re-checking freshness (D5): two hooks racing the same gate can never both
see it as due, and a process that loses the race just skips acting this one
time rather than blocking. `run.sh` never writes to stderr unless
`AGENT_POSTMORTEM_DEBUG` is set (`exec 2>/dev/null` near the top otherwise,
before anything is written); every marker/log/install write is gated on
`mkdir -p "$DATA_DIR"` succeeding first, and when `$DATA_DIR` cannot be
created at all, run.sh prints nothing whatsoever -- no hint, no marker, no
log -- and exits 0 (D6).

Every `hook` subcommand reads the hook JSON from stdin and:
- exits silently when `agent_id` is present in the payload at all --
  regardless of its value or type (an empty string, `null`, or a number all
  still mean "this payload names a subagent") -- or when `hook_event_name`
  does not match the subcommand;
- resolves the session id from `transcript_path`'s own filename stem only.
  A payload `session_id` field that disagrees is ignored: the transcript
  stem is the one value every subcommand agrees on, and trusting the
  payload instead would let a mismatched payload create or overwrite the
  wrong session's state.
- `check` alone derives the session directory by additionally requiring
  `<slug>/<session>/subagents/` to already exist, and exits silently
  without it. `session-start`, `session-end`, and `workflow-launched` need
  only `transcript_path` to name an existing file -- plain
  `parent`/`stem` arithmetic, no subagents/ check -- since each of them
  must still act on a session that has never spawned a subagent at all
  (the common case for session-end and session-start), or must register a
  workflow unit before its own subagents/ directory even exists
  (workflow-launched, below).

State is namespaced by root identity plus session id, never session id
alone (7.5): `session_key = sha256(str(root.resolve()))[:8] + ":" +
session_id`, where `root` is two directories above the session directory
(`<root>/<slug>/<session>/...`). Two different roots that happen to reuse
the same session id (two machines' state merged, two checkouts) can never
collide or share a unit's analyzed state.

`hook check` (Stop, StopFailure):
- Units: each `subagents/workflows/wf_*` directory and each
  `subagents/agent-*.jsonl`, plus (see workflow-launched, below) a workflow
  unit registered but not yet found on disk, addressed by its stored
  locator. A unit's fingerprint is the sorted list of `(relative name,
  size, mtime_ns)` of its files (journal, meta, transcripts).
- State per session (7.5) holds `units: {unit_id: {fingerprint, state,
  stable_checks, reported: [finding_id]}}` with state in `running`,
  `finished`, `unknown`, `analyzed`. An `analyzed` unit additionally
  carries `verdict`, `wasted_seconds`, `run_id`; a workflow unit registered
  by workflow-launched before its directory existed also carries
  `transcript_dir`.
- Every unit is re-statted on every check, including one already
  `analyzed`: unchanged, it is left exactly as it was; changed (appended
  to after being analyzed, or deleted and recreated under the same unit
  id), it is re-opened with `stable_checks` reset to 0 and (GA-8, v0.3.1)
  its finished evidence is re-evaluated in the same check: evidence that is
  still terminal (a rejected, queued or result-less request that left the
  child `returned`, a status word replaced by another terminal one) analyzes
  the unit again at once, evidence that is not terminal leaves it `running`.
  It keeps its `reported` ids, so an already-surfaced finding is never
  re-notified just because the surrounding content moved. Fingerprint comparison
  always happens first: an unchanged `running`/`unknown` unit only advances
  `stable_checks` and is never reparsed, unless this is the one check that
  crosses the unknown threshold below (that check still needs one real
  parse, to give the forced-INCOMPLETE analysis real findings).
- A workflow unit is `finished` iff `Run.finished` would be true for it
  (implemented in N2): `hooks_cli._workflow_finished` calls
  `reader.workflow_lifecycle` on the unit's own run directory, then
  `reader.lifecycle_finished` on the result -- the same lifecycle rule
  section 3 and 4.13.3 define, never `reader.workflow_journal_finished`'s
  narrower journal-only reading on its own (that reading is still correct
  wherever the run carries no parent-reported terminal at all, since it is
  exactly the fallback row of the lifecycle table; it is wrong whenever the
  parent's own terminal evidence disagrees with where the journal currently
  stands, for example a run the parent already reports failed while the
  journal's own key is still open). The fingerprint gate above still comes
  first, so an unchanged unit is never re-evaluated by this rule at all. A
  subagent unit is `finished` only with authoritative
  evidence: its own last transcript line is the kill sentinel (a trailing
  line that fails to parse as JSON at all means not finished, never
  "finished as of the last valid line"), or (H1, v0.3.1) the child's `return_state`
  (4.13.1, T0 to T5, computed from the main transcript and, for a nested child, its launcher
  transcript read as evidence only) is `returned`, `reported_failed` or `reported_killed`, or the
  payload's `background_tasks` names it with a status meaning done. (GA-2, plan H1 amended) The
  payload's finished status stays positive hook evidence: it is the runtime's own live view, newer
  than the transcript, and the unit's analysis (the child's own transcript, for findings) does not
  have to wait for the notification. The stored `analyzed` state is internal (a hook only notifies
  new findings; `run` is authoritative). When the notification arrives, the main-side digest in
  the fingerprint changes and the unit is re-opened and re-evaluated at once, so a later
  `reported_failed` is analyzed and notified. A payload entry naming the child as running still
  wins over every other signal. The launcher is never added
  to the analyzed view's attempts, and its diagnostics and findings never reach the child's unit.
  The unit fingerprint is the child transcript and meta (as before) plus one digest entry of the
  appended evidence for this child in the main transcript (its launch and resume `tool_use` ids,
  lines, timestamps and result classes, and its accepted notifications' ids, statuses and lines),
  (GA-3) the child's current `(return_state, return_reason)`, a stable token for each of the main
  transcript, the meta file and a nested launcher that cannot be read, and one token when the
  decision itself raised (a read failure that leaves every file's size and mtime unchanged still
  re-opens the unit),
  and, when nested, the launcher transcript's size, `mtime_ns` and inode. A resumed child writes
  to its own transcript, so a resume always re-opens its unit; a round still in progress then
  reads `pending` and stays open until its notification. Transcripts are append-only logs:
  rewriting an existing line is not a runtime shape and is out of scope for hooks (`run`
  recomputes from scratch every time). A resume issued from a third transcript (observed once)
  is invisible to the hook's reads: the child's own writes re-open the unit and the main
  notification naming that request is an unknown id (T4 `conflict`), so the unit stays open;
  `run` is authoritative. An assistant text entry with no `tool_use` is never
  evidence on its own: it is also the ordinary shape of an agent still
  waiting for its next turn. A `background_tasks` entry naming the agent id
  as still active is checked first and overrides every other signal. A
  unit whose fingerprint is unchanged for 3 checks without terminal
  evidence becomes `unknown` and is analyzed with status INCOMPLETE
  regardless of what its own detectors found -- but only once it has ALSO
  gone quiet: the newest mtime among its own files at least
  `UNKNOWN_QUIET_SECONDS` (600s = 10 minutes) old (12's D-A). Real data
  showed the plain 3-stable-check rule alone finalizing a workflow that was
  still actively running, because three Stop checks happened to land
  within the same quiet minute; short of both conditions a unit simply
  stays pending, however high its own stable-check count climbs.
- A finished subagent is analyzed in isolation: the main transcript plus
  only this one subagent's transcript, never the rest of the session's
  subagents/ directory, so one sibling's parse trouble can never leak into
  another's status and analyzing N subagents never costs O(N session-wide
  parses). An analysis exception counts as a stable check, not a reset, so
  a unit that genuinely cannot be parsed still reaches unknown/INCOMPLETE
  within a bounded number of checks rather than staying stuck; once the
  mandatory INCOMPLETE analysis attempt itself also raises, the unit
  finalizes as `analyzed`/INCOMPLETE with no findings and a synthesized run
  id (a workflow directory's own name when it matches `wf_` + 8 hex + `-` +
  3 hex, else the `wf_<8 hex>-x` replacement `run` uses for it (N3b C2); or a
  short hash of a subagent's transcript path) rather than retrying the same failure on every future
  check forever. With `AGENT_POSTMORTEM_DEBUG` set, a redacted traceback of
  that exception goes to stderr; the resulting state and exit code are
  unchanged either way.
- After parsing, the reader re-stats the files; if the fingerprint moved,
  the unit stays pending and nothing is emitted.
- Silenced detectors (v0.2, unit M3c, 4.10): before any of the rules below
  ever look at a unit's findings, they are split with
  `config.partition_silenced` into shown and silenced, exactly like `run`'s
  own text/JSON rendering (4.6/4.7). A silenced finding can never become a
  new-finding notification (it is dropped before the notify-threshold
  check below ever sees it, so it is never added to the unit's own
  `reported` list either -- silencing it later never un-silences a finding
  already notified, and un-silencing it later notifies it for the first
  time, same as any other finding that newly crosses the threshold), and
  it can never move the unit's own cached `verdict` (`entry["verdict"]`,
  read back by `session-start`'s "worst verdict" digest and by the grouped
  notify line's own verdict word) -- the same "never lose it, never let it
  decide anything" split 4.6 describes for the CLI. A hook loads the config
  once and, unlike the CLI, never turns a critical id in `off` into an
  error: that id simply never reaches `detectors_off` in the first place
  (4.10), so a hook proceeds exactly as if `off` had not named it, since a
  hook must never fail a turn over a config file's own mistake.
- Notification policy is separate from verdict: emit for each new finding
  with severity at or above `AGENT_POSTMORTEM_NOTIFY` (default `major`), and
  for an INCOMPLETE analysis only when the reader itself reported
  diagnostics that made the status INCOMPLETE, or a finding reaches that
  threshold (D-C) -- an INCOMPLETE that comes only from the
  unknown-finalization fallback (3 stable checks and quiet per D-A, no
  terminal evidence, no reader diagnostics, no threshold finding) is
  recorded (state `analyzed`, its stored verdict still carries
  `(INCOMPLETE)`) but never notified: it is a unit that is simply still
  running, not something a reader can act on. The one exception is the
  forced-INCOMPLETE analysis attempt itself raising (7.2's
  unit-could-not-be-analyzed fallback above): a genuine failure, always
  notify-worthy. Lines are grouped by run id (D-C): a workflow unit's run
  id is unique to it and always forms a group of one; a session's own
  subagent units share that session's run id, so several finishing on the
  same check collapse into one line. Output:
  `{"systemMessage": "<one line per run id: VERDICT[ SUFFIX]  headline  run
  id  /postmortem id>"}` for a group of one, or `{"systemMessage": "<one
  line per run id: VERDICT[ SUFFIX]  N subagents finished, K findings  run
  id  /postmortem id>"}` for a group of more than one (VERDICT is the worst
  verdict word in the group, K the total new findings across it), the bare
  verdict word (not `report.verdict_word`'s own `(INCOMPLETE)` suffix,
  which is a different renderer's contract). SUFFIX (12's D-C) is
  `(inspection incomplete)` when any grouped unit's analysis raised real
  reader diagnostics or the forced analysis attempt itself raised, `(unfinished)`
  when the only reason any grouped unit is incomplete is the quiet-unknown
  fallback finalizing content with no such diagnostics of its own (a unit
  that may simply still be running), or nothing when neither applies; a
  genuine `(inspection incomplete)` anywhere in the group always outranks a
  merely-`(unfinished)` sibling. The same message is added as
  `hookSpecificOutput.additionalContext` only when `stop_hook_active` is
  false and only for finding ids not already in `reported`; reported ids
  are persisted before the output is printed. **(N3b, P17)** After the
  `/postmortem <run id>` command the line, a group's line included (N3b
  C5), may carry `  agents a:<alias>, ...`:
  the canonical agent ids behind the findings that triggered it, as stable
  aliases (4.13.5), at most three, then `, +N more`; omitted when no such
  finding names a canonical agent id. The hook always renders `share`; it
  registers those ids once per invocation, after its state transaction and
  never inside it. When the alias book is unavailable (secret or registry
  unavailable, or the registration lock contended past 2 s) the names are
  positional (`agent-1`, `agent-2`) and the line ends with
  `  (stable aliases unavailable)`, so the diagnostic is in `systemMessage` and
  in `additionalContext` whenever that field is emitted, inside the one JSON
  object, never on stdout beside it and never on stderr. `session-start`,
  `session-end` and `workflow-launched` name only run ids today, register
  nothing, and print no diagnostic.
- Budget: p95 under 500 ms when no unit changed (stats only, no reparse) on
  a session with 20 units; under 2 s when one 10-agent run finishes. A
  benchmark test generates the load in tmp_path and asserts both numbers,
  for both an already-`analyzed` set of units and an unchanged `running`
  set, the latter via a counter on the unit-finished check confirming it
  is never called for an unchanged fingerprint.

`hook session-start` (SessionStart): when `source` is not `startup` or
`resume`, exit silently. Otherwise perform the optional install (7.3), then
sweep every other session under the same root (each gated the same way
`check` is, by its own `subagents/` directory existing) with an empty
payload -- never treated as evidence that a subagent is NOT still running,
only as "no signal either way" -- analyzing at most 20 newly finished units
total. Then print a digest of every unit of this root's own that is
analyzed and still pending in `digest.pending`: count, worst verdict,
wasted time, and the `/postmortem` hint for the worst run; every gathered
entry of this root is removed from `digest.pending`, and any other root's
still-pending entries are left untouched. Silent when there is nothing of
this root pending.

`hook session-end` (SessionEnd): build a main-thread-only session view (no
children at all) and run just the eight tooling detectors from 4.3 over it
-- never `mislabeled_kill` or `missing_artifact`, which need a child
attempt or a first user message naming a path that neither exists for a
main-only view. Store the result as the session's `main` unit, analyzed and
undigested, deduped against every unit's `reported` ids in this session (not
only `main`'s own) so a finding reachable through more than one unit still
notifies once; emit nothing.

`hook workflow-launched` (PostToolUse on Workflow): read `runId` and
`transcriptDir` from `tool_response` (falling back to a `Run ID: wf_...`
match in the text), register the unit as `running` with priority (so
`check` inspects it first) and, when given, store `transcriptDir` on the
unit's own state entry as `transcript_dir` -- registration never requires
the conventional `subagents/workflows/<run id>` directory to exist yet.
`check`, in turn, uses that stored locator for a not-yet-`analyzed` unit
whenever the conventional path is not found on disk.

Both fields are untrusted input from the tool's own response and are
validated before being trusted with anything (release hardening, F12):

- `runId`, however it was found (the structured `tool_response["runId"]`
  field, or the `Run ID: wf_...` regex fallback over free text), is
  accepted only when it full-matches the same canonical run id grammar
  reader.py's own ingestion enforces on a real workflow directory name
  (reader._WORKFLOW_RUN_ID_RE: `wf_` + 8 hex + `-` + 3 hex). Anything else
  -- a value carrying a newline, trailing text, or simply the wrong shape,
  which could otherwise inject content into the `additionalContext` hint
  string below -- means no run id was read at all: the hook emits nothing
  and registers nothing, exactly as when `tool_response` names no run id
  anywhere.
- `transcriptDir` is honored only when, after `os.path.normpath`, it is an
  absolute path inside this session's own `subagents/workflows/` directory
  (`session_dir` derived from `transcript_path` the same way `check`
  derives it, D4) -- it need not exist on disk yet, since D8 exists
  precisely for the case where it does not. A relative path, or one that
  normalizes to somewhere outside that directory, is dropped exactly as if
  transcriptDir had never been given: only the untrusted locator itself is
  refused, never the registration -- a run id that is otherwise valid still
  registers its unit and still gets the watch hint below, just without a
  stored `transcript_dir`.

Unit M5 (.reviews/proposals/2026-09-v0.2-guided-rescue.md section 7; full
contract in 4.12): once a run id could be read (the same `runId`/regex
extraction above), the hook additionally emits, on top of the registration
side effect, exactly

```
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
"Workflow <run id> launched. To notice a killed or failed agent within
seconds instead of hours, arm a Monitor now with this command:
agent-postmortem watch <run id>"}}
```

(one JSON object, `additionalContext` as one unbroken sentence, wrapped
above only for this document). Opt out with
`AGENT_POSTMORTEM_WATCH_HINT=0` -- registration still happens, only the
hint is suppressed, back to the pre-M5 "emit nothing". No run id could be
read: emit nothing, exactly as before M5 (`tests/test_hooks_cli.py`'s
pre-M5 workflow-launched tests around lines 750-777 asserted `out == ""`
for what is now the hint case; the reason for each changed expectation is
listed alongside it). The hint itself arms nothing (R2 of the proposal): it
only hands the session the command a person, or the session itself, can
choose to run. Every other rule of this section still applies verbatim: silent
on `agent_id` in the payload, silent on a mismatched `hook_event_name` or
`tool_name`, JSON validated the same way, never fails a turn, the same 8 s
internal deadline as `check`.

`hook capabilities`: print `{"protocol": 1, "version": "<package>"}`.

### 7.3 One-time install

On SessionStart, if no compatible CLI resolves and `uv` is on PATH and
`AGENT_POSTMORTEM_NO_AUTO_INSTALL` is unset, `run.sh` runs `uv tool install
"agent-postmortem==<version in plugin.json>" --python 3.11+` from PyPI, and only if that
install fails, `uv tool install
"git+https://github.com/zeyusuntt/agent-postmortem@v<version in plugin.json>" --python
3.11+` (0.3.5: PyPI first; the tag covers the minutes between a release reaching GitHub
and reaching PyPI, and needs git besides uv; both attempts run inside the one 60 s
deadline below; the missing-CLI hint names `uv tool install agent-postmortem`) (`UV_TOOL_DIR`
and `UV_TOOL_BIN_DIR` both pointed at `$DATA_DIR`, 7.2's one data
directory, so the installed shim lands at `$DATA_DIR/bin/agent-postmortem`,
exactly where resolution step (1) looks) under the same process-group-kill
60 s deadline every other subprocess call gets (7.2's D3/D4), records the
attempt timestamp in `$DATA_DIR/install.log`, and does not retry for 24 h on
failure -- the 24 h gate itself lock-protected the same way as the missing-
CLI hint (7.2's D5). This is the plugin's only network action and the
README says so. After the install, `run.sh` resolves the CLI again in the
same invocation, so a session's very first hook can still use it.

### 7.4 /postmortem skill

`plugin/skills/postmortem/SKILL.md`: user-invocable and model-invocable.
`/postmortem` with no argument runs `agent-postmortem scan --since <7 days
ago>`; with `latest` runs `agent-postmortem scan --since <today>` and takes
the newest row whose verdict is not marked `unfinished` (docs/DESIGN.md
7.4's D8: `Run.finished` is false for a workflow with a started key and no
`result` or `failed` yet, and a scan row for one carries that trailing marker, 12's
D-C), falling back to the newest `unfinished` row only when it is at least
10 minutes old -- `latest` should not hand back a unit that is probably
still actively running just to have something to show, but an unfinished
row old enough to likely be stalled or abandoned is still a real answer;
with a run id runs `agent-postmortem run <id>`. The skill pastes the
verdict block (noting a `(unfinished)` header tag when present), and when
`WHAT TO DO` names `agent-postmortem rules propose` (unit M6, section 9),
runs it and shows the result -- it only prints rule text and diffs, never
writes anything; if the user wants to adopt one, the skill runs `apply
<rule-id> --target <file>` and asks the user before pasting the printed
block into their file, since `apply` itself never writes it. For a workflow
that is still running (an unfinished
row, or the launch hint), point at `agent-postmortem watch <run id>` (4.12)
instead of re-running `scan`/`run` on a loop: it is the command that notices
a kill or a failure within seconds. `WORK AT RISK` naming any paths or
commits still points at `/rescue`, unchanged (4.11).

### 7.5 State store

Directory: `$XDG_STATE_HOME/agent-postmortem/` or
`~/.local/state/agent-postmortem/`, created with mode 0700; files 0600. The
directory and `state.lock` are lstat-checked before every use and rejected
(`StateError`) if either is a symlink or the wrong kind of thing (a
directory expected for one, a regular file for the other); `state.lock`
opens with `O_NOFOLLOW` where the platform supports it. `state.transaction()`
verifies the final 0700/0600 modes by stat before releasing the lock and
raises `StateError`, fail closed, if they cannot be enforced.

`state.json` schema version 2:
```
{"version": 2, "min_reader": 2,
 "applied": [{"rule_id", "target", "applied_at"}],          # read-only legacy (v0.2, unit M6)
 "sessions": {"<session-key>": {"units": {...}, "last_check": iso}},
 "digest": {"pending": ["<session-key>::<unit-id>", ...]}}
```

`applied` (v0.2, unit M6, section 9): nothing writes a new entry here any
more -- `rules.apply_rule()` only ever reads a target file, it never opens a
`state.transaction()`. Existing entries are still read, migrated, and kept
(a v1 file still migrates to v2 the same way), so a v0.1 user's history
keeps working: `rules review` treats each one as an adopted rule (id, date,
target) exactly like a `[[adopted]]` config row (4.10), and `trend` keeps
counting before/after it the same way it always did.

`<session-key>` is `sha256(str(root.resolve()))[:8] + ":" + session_id`
(section 7.2), never a bare session id: two different roots that happen to
reuse the same session id (two machines' state merged, two checkouts) would
otherwise collide, letting one root's analyzed unit silently suppress the
other's notification, or one root's `session-start` digest and clear the
other's still-pending entries.

A `units` entry holds `{fingerprint, state, stable_checks, reported}`
(section 7.2); once `state` is `analyzed` it additionally carries `verdict`,
`wasted_seconds`, and `run_id`, cached from that analysis so `session-start`'s
digest can summarize a unit without re-parsing its run from disk. A workflow
unit `workflow-launched` registered before its directory existed also
carries `transcript_dir`, the locator `check` falls back to until the
conventional path exists. A `digest.pending` entry is a unit id qualified
with its full session key, joined as `"<session-key>::<unit-id>"`: a bare
unit id is only unique within one session, and the digest is a sweep across
every session under one root -- `session-start` reads and clears only the
entries whose session-key root-hash prefix matches its own root, leaving
another root's entries untouched.

`state.transaction()` is the only write path: acquire `fcntl.flock` on
`state.lock` (exclusive, 2 s timeout, then give up silently in hooks and
raise in the CLI; only `BlockingIOError`/`EAGAIN`/`EACCES` count as
contention and get retried, any other `OSError` from `flock` propagates
right away as `StateError`), load, migrate, yield the state dict to the
caller's block, then on normal exit write temp, fsync, rename, fsync the
containing directory, verify permissions, release. Nothing is written if
the block raises. `state.update(fn)` is a thin wrapper around it for the
common read-mutate-write case. Because the caller's own code runs under
the lock, `apply` (section 9) uses `state.transaction()` directly so
writing its target file and recording that write into state are one
atomic unit; the state lock therefore also serializes concurrent applies
to the same target, not only concurrent state.json writers.

Migration v1 to v2 copies `applied` entry for entry and adds empty
`sessions` and `digest`; it is idempotent. A file whose `version`, or whose
`min_reader`, is above what the reader supports is left untouched and
reported as an error (`StateTooNew`) before any migration or repair is
even considered, never backed up or overwritten. A file that is not valid
UTF-8, is not valid JSON once decoded, or is not the right shape at all is
corrupt: it is copied to `state.json.corrupt-<timestamp>` (`O_CREAT |
O_EXCL`, mode 0600, a numeric-suffix retry on a same-microsecond
collision, never check-then-create) and a fresh v2 state is used in its
place. Namespace validation is recursive: `applied` must be a list of
dicts with string `rule_id`, `target`, `applied_at`; `sessions` a dict of
dicts; `digest` a dict whose `pending` is a list of strings. A malformed
namespace is reset to empty; a malformed entry inside an otherwise
well-formed namespace (one bad dict in `applied`, one non-dict value in
`sessions`, one non-string item in `digest.pending`) drops only that
entry, backed up the same way, with everything well-formed surviving
untouched. If the backup itself cannot be created or written, that raises
`StateError` and nothing is reset. Only `state.transaction()`/`update()`,
under the lock, ever create a backup or persist a migration or a repair;
`load_state()` (what `trend` calls) runs the same load-and-migrate path
read-only and never writes anything, not even a corrupt-file backup.
The session id half of a session key always comes from `transcript_path`'s
own filename stem (section 7.2), never a payload `session_id` field; unit
ids are relative to the session directory; nothing in the state file is
printed by default; `--no-redact` may show an apply target, which the user
named.

### 7.5a The disclosure secret and alias registry (implemented in N3a)

Two more files live in the same state directory as `state.json` and share
its lock: `secret` (the installation secret, 7.5/4.13.5's D10: 32 random
bytes from `secrets.token_bytes`, mode 0600, created once) and
`aliases.json` (the alias registry, 4.13.11's "N3, disclosure" items, mode
0600, written under the lock). Neither is part of `state.json`'s own
schema and neither is ever loaded or written by `state.transaction()`;
`state.lock_state_dir()` holds `state.lock` (the identical lock file in
the identical directory, the same `_acquire_lock` with `StateLocked` after
`LOCK_TIMEOUT_SECONDS`) without touching `state.json` at all, so
`agent_postmortem.disclosure` can read or write the secret and the
registry under the same serialization `state.json`'s own writers get.
**(N3a Codex round 4, MAJOR 1)** The order on this path is: the state
directory is opened (`O_RDONLY|O_NOFOLLOW|O_DIRECTORY`) and verified first;
`state.lock` is then opened relative to that verified descriptor
(`O_CREAT|O_RDWR|O_NONBLOCK|O_NOFOLLOW`, mode 0600, `dir_fd=`), verified,
and only then locked. Opening the lock by path before the directory let a
principal able to rename entries in the parent swap in a second valid
directory in between, so the held lock and the disclosure files named
different directories; with this order they are always the same one.
**(N3b, main-session finding M1)** On macOS APFS, when two processes create
`state.lock` relative to a directory descriptor at the same moment, the
loser's `openat(O_CREAT)` fails with `ENOENT` (measured: 5 to 15 of 40
pairs; a path-based open never did), which made two concurrent first
registrations disagree. While the directory descriptor is held, the only
other cause of `ENOENT` there is the directory itself having been removed,
which fails on every attempt; so `ENOENT` alone is retried, up to 5
attempts with a pause of 1 ms times the attempt number, and any other
error, or `ENOENT` on every attempt, still raises `StateError` and falls
back.

**Manual recovery (N3c).** Nothing repairs, replaces, or deletes an
unavailable secret or registry automatically (P5, P6): a new secret would
re-key every alias, and a new registry could reassign one. When "stable
aliases unavailable" persists, the person removes both `secret` and
`aliases.json` from the state directory to start over; every alias is new
from then on, and old aliases in earlier output no longer match.

Before either disclosure file is ever probed or written, `lock_state_dir()`
fstats two freshly opened descriptors -- the state directory itself
(`O_RDONLY|O_NOFOLLOW|O_DIRECTORY`, so a symlink or a non-directory raises
instead of silently matching) and the lock descriptor opened relative to
it -- and
requires the directory to be mode exactly 0700 and owned by the current
uid, and the lock file to be a regular file, mode exactly 0600, owned by
the current uid. This runs in addition to, not instead of, `transaction()`'s
own best-effort `chmod`: that call swallows a `chmod` failure by design
(it re-verifies for real only after writing `state.json`), so without this
stricter check a directory or lock file a `chmod` silently failed to fix
would otherwise be used as-is for the secret and the alias registry. Any
mismatch raises `StateError` before either file is touched, which
`agent_postmortem.disclosure`'s fallback turns into a positional render,
exactly like any other unavailable outcome.

**(N3a fix round 3, new Finding 1)** `lock_state_dir()` keeps the verified
state-directory descriptor open for the entire lock scope, instead of
closing it right after this check and yielding only the path: every
secret, registry, temp-file, rename, unlink, and directory-fsync operation
that follows runs relative to that one descriptor (`dir_fd=` on
`os.open`/`os.unlink`, `src_dir_fd=`/`dst_dir_fd=` on the registry's
atomic commit, `os.fsync` on the descriptor itself for the directory
fsync), never a later, separate reopen of the directory by path. A file
descriptor is immune to its own path being renamed or replaced by a
symlink afterward, which a path-based reopen is not: without this, a
same-principal attacker with write access to the state directory's own
parent (for example a misconfigured `XDG_STATE_HOME`) could, in the
window between this check and the first disclosure file operation, rename
the just-verified directory away and put a symlink to a different,
otherwise-valid directory in its place, redirecting every later operation
at that directory instead -- `O_NOFOLLOW` on a child file's own final
path component does not protect against a swapped *intermediate*
directory component. The registry's atomic commit uses `os.rename(...,
src_dir_fd=, dst_dir_fd=)` rather than `os.replace()` in this dir_fd mode:
POSIX `rename()` is already an atomic overwrite, the same guarantee
`os.replace()` gives on every platform this ships for, but `os.replace()`'s
own `dir_fd` support is not reliably reported by `os.supports_dir_fd`
even where it would work, so the dir_fd-relative commit uses the function
`os.supports_dir_fd` actually lists. If this platform's `os` module
cannot honor `dir_fd` for every operation that needs it (`os.open`,
`os.unlink`, `os.rename`), `lock_state_dir()` refuses disclosure file
access outright (`StateError`, fail closed) rather than silently falling
back to the unprotected path-based mode.

A read of either file never trusts a separate `stat()` call: it opens with
`O_RDONLY | O_NONBLOCK | O_NOFOLLOW` (the `O_NONBLOCK` is only to keep a
FIFO from blocking the open; it has no effect on a regular file) and
`fstat`s the already-open descriptor, requiring a regular file, mode
exactly 0600, owner equal to the current uid, and (for the secret) size
exactly 32 bytes or (for the registry) size at most `MAX_BYTES`. Any
violation -- a symlink, a directory, a FIFO, the wrong mode, the wrong
owner, or an oversize registry -- reports that file as unavailable without
reading, repairing, or touching it in any way. The registry's own JSON
parse and schema validation (4.13.11's exact shape, enforced by
`disclosure.validate_registry`) happen one layer up, in
`agent_postmortem.disclosure`, never in `state.py` itself: a file that
passes every structural check above but fails to parse or fails the schema
is unavailable in exactly the same way as a structural violation. A decode
failure includes a `RecursionError` from pathologically nested JSON, not
only `UnicodeDecodeError`/`JSONDecodeError`. The schema's `"version"` key
must be exactly the Python `int` `1`; `true`, `false`, and `1.0` are all
rejected even though each compares equal to `1`, because a bool is a
subclass of `int` in Python and a float silently equal to an int is not
itself an int. An existing registry already over `MAX_ENTRIES` is
unavailable the moment it is read, not only when a new identity's
registration would newly push it over that bound.

A write of either disclosure file verifies everything that can fail --
regular file, mode exactly 0600, owner the current uid, and (after the
write loop below) the written size -- on the still-open descriptor, before
that write's own commit point, never by a later, separate `stat()` of the
path that could observe a file swapped out from under it. The write itself
loops on `os.write()` until every byte is sent and treats a zero-length
return as an error, because POSIX permits a short write without raising:
an unchecked single `os.write()` call can silently leave a file smaller
than what was reported as written. For the registry, the commit point is
`os.replace()` of a fresh temp file (`O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`,
mode 0600, never check-then-create) over `aliases.json`. For the secret,
there is no rename -- it is created directly at its final name with
`O_CREAT|O_EXCL`, so the commit point is simply `create_secret()`
returning without raising. A failure strictly *before* its own commit
point removes only the file this call just created and leaves the other
file, and the registry or secret that already existed, exactly as they
were. A failure strictly *after* the commit point -- the disclosure-only
strict directory fsync (`state.fsync_directory`, which raises on failure,
unlike `state.json`'s own best-effort `_fsync_dir`), or the final
post-commit reverification (a reopen of the final path with `O_NOFOLLOW`
and `O_NONBLOCK` too, for the same FIFO-blocking reason the probing reopen
above already gives, never a later, separate, symlink-following `stat()`
of the path, with every check -- regular file, mode exactly 0600, owner
the current uid, and content -- run from that one descriptor) -- raises
`PostCommitVerificationError` instead of a plain `OSError`: the file on
disk already holds the new content, so
treating this the same as a pre-commit failure would make a caller falsely
believe nothing happened. `agent_postmortem.disclosure` catches that error
and re-probes the exact file once, under the same lock: if it reads back
valid and, for the registry, equal (as parsed dicts) to what was just
written, registration returns the committed result (the aliases it
already computed); otherwise it falls back, exactly as for any other
unavailable outcome. This replaces the earlier, inexact claim that any
failure anywhere leaves the file "untouched" -- that is true only for a
failure before the commit point.

**Initialization (P6).** Under the lock, probing the secret and the
registry each independently yields absent, valid, or invalid, and the
combination decides what happens next:

| registry | secret | action |
|---|---|---|
| absent | absent | write an empty valid registry (temp, fsync, rename, fsync dir), then create the secret (`O_CREAT\|O_EXCL`, fsync, fsync dir) |
| valid and empty | absent | create the secret only (safe: no assignment exists yet to re-key) |
| valid and non-empty | absent | unavailable (a new secret would re-key every existing alias) |
| absent | valid | unavailable (a new registry could reassign an alias the secret already vouches for) |
| valid | valid | normal |
| either invalid | either | unavailable |

A crash between the two steps of the first row leaves exactly "registry
valid and empty, secret absent" -- the second row -- which the next call
completes with no special-cased recovery path: the same state-machine
function handles both rows identically. Every "unavailable" outcome, plus
lock contention past `LOCK_TIMEOUT_SECONDS`, an existing registry already
over `MAX_BYTES` or `MAX_ENTRIES` (both module constants, 50,000 entries /
4 MiB) when it is read, and a write that would newly take the registry
over either bound, is `agent_postmortem.disclosure`'s fallback (4.13.5's
"stable aliases unavailable"): a positional alias for the whole render.
**(N3a fix round 2, new Finding 5)** that fallback does not uniformly mean
the real files are untouched: a pre-commit failure or a pre-existing
unavailable state (every row of the table above) leaves the real secret
and registry exactly as they were, byte-for-byte and mtime untouched, but
a post-commit failure (the paragraph above) is different -- the new file
may already be installed, and this same fallback is only what the caller
reaches after re-probing it, requiring a fresh directory-durability
confirmation (`disclosure._directory_is_durable`), and still not being
able to accept the result; it never claims the state was unchanged in
that case. N3b wires this into every renderer, hook, and `watch`
(4.13.5).

**AliasBook and the slow path.** An identity `register()` was not handed
up front takes one more locked registration through `AliasBook.alias()`
(the "slow path", counted in `slow_path_count`): it applies the identical
canonical-id check `register()` applies to its whole batch first, so a
kind `alias()` does not recognize, or an identity that is not canonical
for its kind, never reaches an HMAC or a write -- it is simply not
eligible for a stable alias and gets a positional name without even
attempting the lock, which also does not count against `slow_path_count`.
A genuine failure of that locked attempt (lock contention, an unavailable
secret or registry, a failed write, every ordinary `Exception` from
probing, assignment, or writing) degrades the whole book atomically rather
than falling back for only the one identity that triggered it: `available`
becomes `False`, every identity the book had already handed a stable alias
to is immediately reassigned a positional one instead, in the order it was
first registered, and the newly failing identity gets the next positional
name in sequence. A book never mixes a stable and a positional alias for
different identities in the same render (4.13.5/P8); `watch`'s own rule is
separate (C-1, 4.12: an already-emitted line stands, only the current and
later events go positional).

## 8. Distribution

### 8.1 Package

`pyproject.toml`: name `agent-postmortem`, version `0.3.5`, console scripts
`agent-postmortem` and `postmortem`, `requires-python >= 3.11`, no runtime
dependencies, `[project.optional-dependencies].dev = ["pytest>=8",
"ruff>=0.6"]`, classifiers, URLs (Homepage, Issues, Changelog). The wheel
ships `agent_postmortem/demo/**` and `agent_postmortem/hooks/*.mjs`.

Not in 0.3.1 (non-goals, also in the CHANGELOG): round-scoped paths, risk and WASTED for resumed
children; a `main` row for the main thread's commits; TaskStop of a background shell; PATHS for
writes inside inline Python or a script.

### 8.2 Demo

`agent-postmortem demo` renders the packaged synthetic run so a first run
needs no data; `--json` exports it. The README's fenced code block on its
first screen (8.4) is this output, byte for byte, on every machine: the demo
renders with a fixed home directory that matches no account, so the
bare-account-name rule (5.1) never rewrites a word of it (N3b S12).
The packaged run is a complete workflow run in the standard layout: the
demo ships the parent session transcript (the user turn, the Workflow
launch and its result naming `wf_00000000-d30`, and the workflow's
completed notification after the last journal event) beside the run, so the
lifecycle is finished and every attempt is placed in the launch; no line of
the output says a launch was not found.
The packaged run is resolved through `importlib.resources.files(...)`
(D4): a plain `Path(__file__).parent` has nothing to `iterdir()`/`open()`
when the package is imported straight out of a wheel `.whl` (itself a zip)
rather than unpacked into site-packages. A package on the filesystem, which
every pip, uv, and pipx install is, is read in place. A package imported
from a zip is extracted to a temporary directory by
`importlib.resources.as_file(...)`, which accepts a directory only from
Python 3.12 on; on Python 3.11 `demo` exits 2 with one line saying to
install the wheel instead (the tool writes nothing outside its state
directory, 5.4, so it does not copy the tree itself; release review of
0.3.0, finding 1; that refusal exits 2). Once the packaged run is loaded, `demo`'s exit code is derived exactly the way `run`'s is (D5): 3
for a critical finding, 4 for status INCOMPLETE, else 0 -- never a
hardcoded 0 regardless of what the packaged run's own findings are.
**(v0.3, N5)** from N5 on, read "3 for verdict FAILED" in place of "3 for a
critical finding" (4.6), the same substitution `run`'s own 4.8 entry makes.

### 8.3 CI and release

`.github/workflows/ci.yml`: on push and pull request; matrix ubuntu-latest
and macos-latest times Python 3.11, 3.12, 3.13; steps: `uv sync --extra dev`,
`uv run ruff check`, `uv run pytest`, `uv build`, `scripts/check-privacy.sh
--tree --artifacts dist/`, and `claude plugin validate --strict ./plugin` and
`claude plugin validate --strict .` when the binary exists. `.github/workflows/release.yml`: on tag `v*`, a build job (tests, build, privacy
check of the artifacts), then a `github-release` job (`permissions: contents: write`,
`gh release create <tag> dist/* --verify-tag` with that version's CHANGELOG section as
notes) and a publish job with `environment: pypi`, `permissions: id-token: write`,
`pypa/gh-action-pypi-publish@release/v1` that runs only when the repository variable
`PUBLISH_PYPI` is `true` (a tag must never turn the release red when publishing is
switched off; a skipped job is not a failure; PyPI publishing is on from 0.3.4). Both workflows declare a
top-level `permissions: contents: read` so every job is read-only by
default regardless of the repository's own token-permission setting; the
publish job's own `id-token: write` is layered on top of that at the job
level, and the `github-release` job's `contents: write` is the only other elevated
permission.
CONTRIBUTING.md documents the one-time trusted-publisher setup on
pypi.org and the TestPyPI dry run.

### 8.4 README

First screen, in order (from 0.3.3): name; the question the tool answers
("Which 'done' in your last Claude Code run can't you trust?"); one paragraph on
what it reads, what it reports and that it is local and read-only; one dated number
from a named version's scan; `uvx agent-postmortem scan` (from PyPI, 0.3.5; 0.3.3 and
0.3.4 used the tagged GitHub release while PyPI was not yet working); the status paragraph; badges (CI, license; PyPI version and Python versions only once a
PyPI release exists, since a badge for a missing release renders broken),
the demo output as a fenced code block (not a screenshot: it must stay text,
so it can be diffed against a live `agent-postmortem demo` run -- 8.2,
tests/test_readme.py), the install command that works for the current
distribution (`uv tool install agent-postmortem` from PyPI from 0.3.5; a
GitHub install with `uv tool install git+...` in 0.3.3 and 0.3.4), a command table
listing exactly which commands take `--json` and which take `--no-redact`.
Then "What it covers", before Quick start: one table of run shapes and failure
kinds, each marked covered, partial or not yet, as checked against the upstream issue
tracker on a stated date, and a line asking users of the partial and not-yet shapes to
open a "my run shape isn't covered" issue with `--bug-report` output. Issue templates
under `.github/ISSUE_TEMPLATE` (wrong verdict, confusing output, run shape not covered,
crash or error, detector request) ask for default-level `--bug-report` output and the
Claude Code version, and never for a raw transcript (tests/test_launch_docs.py).
Then: Why (the retry count from one dated scan, naming the version that
produced it, and links to anthropics/claude-code issues with their status
on that date: #88993, #85206, #98170, #104986, #84346, #65500; the 0.1
figure of 110 runs / 38 h was dropped after 0.3.1, because the hours were
summed attempt time rather than time lost and later versions count retries
differently), plugin install in two commands after the CLI, what it detects
(table from 4.3 with the honesty rule from 4.4), privacy (what is redacted,
what a pseudonym is, the one network action), how it was built, limits
(internal format, observed versions, retries not observable in sessions).

## 9. Rules library (v0.2, unit M6: proposals only)

The library itself, unchanged in substance from v0: R-RETRY-SAFE,
R-CAP-CONCURRENCY, R-NO-LONG-BASH, R-SMALL-QUANTUM, R-ASSERT-ARTIFACT,
R-SCALAR-SCHEMA, R-CODEX-CONTRACT, R-MONITOR-RETRIES, R-GIT-CWD,
R-MODEL-ALLOCATION (hook), and R-USAGE-LIMIT (backed by `attempt_failed`:
when a run reports a usage-limit failure, do not relaunch the same key
before the reset time the failure named). What changed is what the tool
does with them: it never writes one anywhere (R3). R-CODEX-CONTRACT is backed
by two detectors, `codex_write_denied` and `codex_unavailable` (4.3), so
`rules review` and `trend` count a finding of either after adoption as a
recurrence; its text ends with: "Before a workflow relies on a Codex agent,
check from the same environment that the companion starts (for example codex
--version) and fail the run when it does not, so a review that never ran
cannot read as done." `rules.RULES`,
`rules_for()`, `rules --show`, and `rules --run`/`--all` are otherwise
unchanged from v0.1.

**Target kind.** Every `Rule` carries a `target_kind`: `"workflow prompt"`
(the agent prompt preamble inside a workflow script -- where most rules
belong, since the failure they describe is specific to how one workflow
launches and talks to its own agents: R-RETRY-SAFE, R-CAP-CONCURRENCY,
R-NO-LONG-BASH, R-SMALL-QUANTUM, R-ASSERT-ARTIFACT, R-SCALAR-SCHEMA,
R-CODEX-CONTRACT, R-MONITOR-RETRIES, R-USAGE-LIMIT), `"hook"` (a Claude Code
hook that can enforce the rule mechanically before an agent even starts:
R-MODEL-ALLOCATION), `"project instructions"` (a repo-wide file, for the
one rule that is not about any one workflow's own prompt but about how
every agent in every workflow is launched: R-GIT-CWD), or (v0.3, N8, 4.13.8)
`"agent prompt"` (the prompt any agent receives, either a workflow script's
agent preamble or an Agent-tool subagent's prompt: R-NO-LONG-BASH, whose
failure is one agent's own Bash use and was seen firing from a session
subagent, where no workflow prompt exists). `rules --show` and
`rules propose` both print it, so a reader is never left guessing whether a
suggestion belongs in the file they already think of as "the rules file."
An `agent prompt` rule has no file to apply to, so its proposal text and
`rules --show` print "add this directive to the workflow's agent preamble or
to the Agent-tool prompt that launches the subagent" and no `apply` line; every
other kind keeps the `apply ... --target <file>` line. JSON rows are
unchanged in shape (the line is text only). R-NO-LONG-BASH's text also names
the macOS gap: macOS has no `timeout(1)`, so use a `python3 -c 'import
subprocess; subprocess.run([...], timeout=N)'` or `perl -e 'alarm N; exec
@ARGV'` form instead, and in a subagent run long jobs from the main session
and never background and wait.

**`apply` prints, never writes (R3).** `agent-postmortem apply <RULE_ID>
--target FILE` computes the same unified diff it always has -- target's
current content (or `""` if it does not exist) against the same content
with the rule's managed block added -- and prints it. There is no `--write`
flag; `rules.apply_rule()` takes no argument that could turn writing on. It
exits 0 after printing. When the rule is already present in the target it
prints `<RULE_ID> already present in <target>; nothing to add` instead of a
diff, still exit 0. The last line of `apply`'s own output, always, is one
sentence: "Apply it yourself: paste the block into the target file, or save this
output and run git apply." The old drift logic ("already present,
recorded"), the applied-entry write, and the `state.transaction()` call
around all of it are gone with `--write`; `apply` no longer touches the
state store at all.

**`apply` at each disclosure level (N3b P18, C-4, P19).** The paragraph above
is `raw`: today's output exactly, usable with `git apply`, plus the first-line
label. At `share` no byte derived from the target file is printed: `rule
<RULE_ID> would add this directive to path-N:`, the rule's canonical directive
(`rules.directive_line`, the `- <RULE_ID>: <text>` line `apply` would put in
the managed block), one note line saying the actual patch needs `--local` or
`--raw`, the hook note when the rule has one, and the closing sentence "Apply
it yourself: paste the directive into the target file's managed block." The
already-present message names `path-N` (it states one bit about the target, not
a byte of it), and so does every error: an unreadable or unterminated target
prints a fixed sentence (`target path-N: it is not valid UTF-8 text`, `could
not read it (<strerror>)`, `it has an unterminated agent-postmortem rules
block`), never the exception text. An unknown rule id and a run-directory
target are spans of the typed argument (`value-N`, `path-N`) that also name the
kind of argument expected (4.13.5, error rendering). At `local` the output is
the unified diff with 3 context lines, whose two header lines are the P19 form
of the target: `apply` makes no observation, so it has no repository root (D-n)
and every path counts as outside the repository; the target (a relative one is
made absolute against the working directory) prints as `~/.../<basename>`
inside home and `path-N` elsewhere. A local diff is not meant for `git apply`;
use `--raw` for that.

**`rules propose [--root PATH] [--since YYYY-MM-DD | --run RUN_ID|PATH]
[--json] [--no-redact]`.** Scans runs the same way `scan` does (discovery, `--root`,
`--since` filtering on a run's own start date) and prints, per rule backed
by findings in at least 3 distinct runs on at least 2 distinct days (by run
start date -- `rules.PROPOSE_MIN_RUNS`/`PROPOSE_MIN_DAYS`), a block:

```
R-MONITOR-RETRIES   backed by <f> findings in <r> runs, first <YYYY-MM-DD>, last <YYYY-MM-DD>
  belongs in: <target kind>
  <the rule text, indented>
  show the diff for a file: agent-postmortem apply R-MONITOR-RETRIES --target <file>
```

sorted by run count descending then rule id. A rule below the threshold
prints nothing; when no rule clears it: `no rule is backed by 3 runs on 2
days yet`, exit 0. A finding backs a rule the same way `rules_for()` counts
it: once per rule named in the finding's own `rule_ids`. A run with no
`started_at` still adds to a rule's finding/run counts but can never supply
one of the 2 required distinct days. Exit codes: 0 in every case except a
malformed `--since` (1, a usage error) or no runs discovered under `--root`
at all (2). `--json`: `{schema: 2, proposals: [{rule_id, target_kind,
findings, runs, first, last, text}, ...]}` (5.3a).

**Adopted rules (v0.3, N8, 4.13.8).** A rule listed in config `[[adopted]]`
(4.10) is never proposed again. It prints one line instead:

```
R-GIT-CWD   adopted 2026-08-10; still violated 3 times in 2 runs since
```

(`1 time`, `1 run` in the singular). `N` counts findings whose own
`rule_ids` name the rule, in runs started on or after the adoption date; `M`
counts the distinct runs holding at least one such finding, so a clean run
never counts and two findings in one run are `N` 2, `M` 1. A finding of a
backing detector that does not carry the rule id is not counted. A run with
no start date cannot be placed on or after the adoption date and is not
counted. One shared counter in `rules.py` returns (findings, distinct runs,
first date, last date) for a rule over a run set and serves both the
proposal and the adopted row; `trend.before_after` is not reused (it counts
detector firings and applicable runs). Several `[[adopted]]` records for one
rule: the earliest date is used and the line says `adopted <date> (<k>
records)`. An adopted rule is shown whether or not it clears the
3-runs-2-days threshold, and with no finding at all (`0 times in 0 runs`),
since it is a status, not a proposal. The adopted target path never prints at
any level. Adopted lines follow the proposals, by rule id. In JSON every row
carries `adopted` (`str | null`, the earliest adoption date) and `since`
(`{findings: int, runs: int} | null`, null exactly when `adopted` is null);
for an adopted row `findings`, `runs`, `first`, and `last` describe the same
since-adoption population as `since`, and `first` and `last` are `str |
null` (null when no dated run backs the row: zero violations since adoption,
or an undated run).

**`rules propose --run <RUN_ID|PATH>` (v0.3, N8).** Scopes counting to that
one run, resolved by the shared resolver (4.8, 4.13.6). One run cannot meet
the 3-runs-2-days threshold, so every rule backed by at least one finding in
that run is listed with its counts (an adopted rule backed by a finding in it
shows its adopted line), and the text opens with `rules backed by findings in
run <id>`; with none, `no rule is backed by a finding in run <id>`. A rule's
row for an undated run has `first` and `last` null and prints no dates.
`--run` with `--since` is a usage error (exit 1); an ambiguous id is exit 1
and an unmatched one exit 2, as for `run`.

**`rules review [--root PATH] [--weeks N] [--json] [--no-redact]`.** Reads
the rules the owner has adopted -- `[[adopted]]` config tables (4.10) plus
any legacy state-store `applied` entry (7.5), deduplicated on (rule_id,
date, target) so declaring the same adoption in both places is not counted
twice -- and for each prints its backing detector(s)' rate over runs of the
applicable kind (4.7) in the `N` (default 4) weeks before its adoption date
and the `N` weeks after, with denominators, by calling `trend.before_after`
with a new `window_weeks` argument (the same function `trend`'s own
unbounded before/after already uses; this unit extends it rather than
duplicating its counting):

```
R-MONITOR-RETRIES  adopted 2026-09-10  mislabeled_kill: 9 of 30 runs before, 2 of 25 after
```

then a closing line, `consider removing: <ids>` (comma separated, or `none`)
naming every adopted rule whose after-rate is not lower than its
before-rate, with both windows non-empty (an empty window on either side is
never enough evidence to flag a rule either way). No adopted rule at all:
`no adopted rule to review`, exit 0. Removal is a human decision; the
command only lists candidates, it never removes anything itself (there is
nothing for it to remove -- adoption lives in a config file and a
transcript history, not inside this tool). `--json`: `{schema: 2, reviews:
[{rule_id, target_kind, detectors, adopted_at, target, before, after}, ...],
consider_removing: [...]}` (5.3a). `target` is `target-N` alone by default
(F13 hardening) -- the raw target string, its own basename included, only
under `--no-redact`. This is deliberately stricter than `trend`'s own
before/after rendering (4.7/5.1), which keeps `target-N (basename)` together
under the default redaction: a review's own `target` field renders through
`trend._display_review_target`, a separate function from
`trend._display_target`, since a basename is a real filename that can carry
a client or project name and must not survive by default here the way
trend's richer, human-facing text still lets it.

## 10. Build execution (as executed)

Two Workflow runs, every green increment committed on branch `v0.1`. Each
milestone was one test-first unit: a sonnet agent wrote the failing
fixtures and tests, ran only the touched test files while iterating, then
ran the full suite once green, and committed only once that full suite was
clean. The review step ran from the main session, not from inside either
Workflow run: this was a cross-model review -- Codex reviewed the
milestone's diff (written to `.reviews/`), Claude's own code-reviewer
reviewed it again, and the main session read every finding, verified each
one against the code (several times against this project's own real data)
before accepting or rejecting it, and recorded the disposition in
`.reviews/<milestone>-review.md`; a sonnet agent then applied the accepted
fixes, ran the full suite again, and committed each fix. Milestone 3 grew
large enough to split into two commits, 3a and 3b, reviewed separately;
every other milestone kept a one-to-one mapping to the stage list below.

Workflow A (core): (M1) privacy scrub of the tree and markers, synthetic-id
allowlist, privacy test over tracked files; (M2) models, reader session
kind, dedup, envelope, compaction, parent index; (M3a) attribution,
evidence quality, status INCOMPLETE; (M3b) detector kinds, stratified scan
and trend, report and export allowlist, pseudonyms; (M4) state store v2
with locking and migration. Workflow B (product): (M5) hook subcommands and
benchmark; (M6) plugin files, run.sh, skill, marketplace; (M7) packaging,
demo, CI, release workflow, CHANGELOG, CONTRIBUTING, templates; (M8) README
and docs. Then the main session runs acceptance (section 11) and the public
export.

## 11. Acceptance on this machine

1. Full suite green; `tests/test_privacy.py` green over tracked files and
   the built wheel.
2. `agent-postmortem scan` covers 110 workflow runs and 51 sessions in under
   30 s; the owner's reference run (its id lives in the untracked file
   `.reviews/acceptance.local`, never in the repo) still reports 4 agents, 10
   attempts, 6 retries.
3. A session in which the owner pressed Escape reports info, not major; a
   workflow retry reports cause runtime, major.
4. Plugin installed from the local marketplace in a new session: a small
   Workflow run that retries produces a verdict line on the next turn; a
   clean run produces nothing; `hook check` p95 under 500 ms with nothing
   new.
5. `scripts/check-privacy.sh` passes on the exported public repository,
   including every ref and commit author field.
6. `uvx --from dist/agent_postmortem-0.1.0-py3-none-any.whl agent-postmortem
   demo` runs from an empty directory.
7. CI green on the first push; `claude plugin validate ./plugin` passes.
8. The push happens only after the owner confirms.

## 12. Design review dispositions (2026-09-22)

Accepted in full: session-native model with `Run.main` separate from
agents; session-specific summary and wasted_time metrics; retries declared
not observable in sessions; envelope events; dedup before event emission
with the counted fallback; stratified totals and trends; `NotARun`
classifier for direct paths; public session ids that never expose a UUID
prefix; parent TaskStop and parent sentinel as causal evidence; runtime
cause only with a later attempt as affirmative evidence; nullable silence;
compaction stops the silence walk; main-session interrupts recorded as a
limit; per-file fingerprints with a pending set and stable snapshots;
tri-state unit state; StopFailure, SessionEnd and SessionStart catch-up;
systemMessage as the notification channel with additionalContext gated on
`stop_hook_active` and finding ids; notification policy separate from
verdict; benchmark on generated load; skip subagent contexts; INCOMPLETE
status; capability probe before using a PATH CLI; no package resolution in
Stop; `${CLAUDE_PLUGIN_ROOT}` commands with a pinned CLI version and a
protocol number; pseudonyms for every user-authored name in every output;
export allowlist; privacy test over all tracked files and the wheel; wf ids
in an allowlist; sanitized export instead of a sibling private branch;
explicit v1 to v2 migration; newer-version rejection; flock around
read-modify-write; per-session cursors; unique corrupt backups; owner-only
state permissions with DESIGN.md naming the state store as the redaction
exception; DESIGN.md merged before code; test-first milestones; reviews in
an ignored directory; `--extra dev` in CI.

Adjusted: public ids use a 10-hex digest with collision detection rather
than 96 bits (readability); pseudonyms are per-report ordinals rather than
keyed hashes (readable, unlinkable by construction, no key to manage); the
state store uses flock rather than sqlite (one writer at a time is enough
and the schema is tiny).

Deferred to v0.2: interaction mode for `-p` sessions; byte-offset resume of
partially read transcripts (fingerprints plus one-time parsing of finished
units meet the budget); execution epochs for resumed subagents.

### M7 review dispositions (2026-09-23)

From real-data observations after the M6 fix, not from the milestone's own
code review (whose accepted findings are folded directly into 4.4/5.4/8.1/
8.2/8.3 above and carry no separate log entry here):

- D-A: `hook check`'s unknown-finalization fallback (7.2) fired on a
  workflow that was still actively running, because three Stop checks
  landed within the same quiet minute. A unit may now cross into `unknown`
  only once it has ALSO gone quiet: the newest mtime among its own files
  at least `UNKNOWN_QUIET_SECONDS` (600s = 10 minutes) old, not on
  `stable_checks` alone; short of both conditions it stays pending however
  high `stable_checks` climbs.
- D-B: two of run.sh's own tests used to sleep out a real 8s deadline,
  taking the full suite from 8s to 26s. `AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS`
  (7.2) is a test-only override run.sh honors only when set, letting those
  two tests drive the real deadline-kill path against a 1s deadline
  instead; the probe-timeout test's own fake CLI sleep was also trimmed
  from 10s to 3s.
- D-C: `run wf_...` on a workflow whose killed key never received a result
  used to print a bare `RUNNING (no verdict yet)` line and no findings,
  forever -- hiding exactly the runs (stopped or abandoned, with retries)
  a user most wants to inspect. `run` now always computes and prints
  findings and the verdict (4.7); when `Run.finished` is false the header
  adds a trailing `(unfinished)` tag rather than replacing the verdict,
  and exit codes are unaffected by finished-ness. `scan` rows already
  carried a real verdict; their trailing marker changes from `RUNNING` to
  the verdict followed by `unfinished`, for the same wording. Only the
  hook uses finished-ness (plus D-A's quiet rule) to decide WHEN to
  analyze a unit; its own notify suffix (7.2) now distinguishes a genuine
  reader-diagnosed `(inspection incomplete)` from a unit finalized only
  because it went quiet, `(unfinished)`. `SKILL.md`'s `latest` picks the
  newest row with no `unfinished` marker, falling back to the newest
  `unfinished` row only when it is at least 10 minutes old (7.4).
