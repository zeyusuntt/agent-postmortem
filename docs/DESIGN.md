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
  status INCOMPLETE (section 4.6) and is never reported CLEAN.
- Test first. Every detector has a fixture that triggers it, a fixture that
  does not, and an evidence line in `path:line` form pointing into the fixture.
- R8 (v0.2, unit M4, section 4.11). The tool never says "verified".
  `rescue status` reports what a transcript shows happened and names what
  it could not see; a passing check is evidence, never a decision. The word
  itself appears nowhere in a command's output or in `rescue.py`'s own
  source, tested directly.

## 3. Data formats (observed on Claude Code 2.1.251 to 2.1.280)

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
(4.3) reads it from exactly one place, the last entry of that attempt's own
transcript, never a wider window and never the attempt's first user prompt.
A killed-and-restarted attempt has no terminal event of its
own; its only trace is the next `started` for the same key. Unknown event
types are preserved verbatim on `Run.journal` and ignored by detectors.

Per key, the reader runs the state machine of 4.2 over these events: at
most one attempt is open per key; `started` opens one (closing an already
open one as superseded); `result` and `failed` close the open attempt of
that key when their `agentId` matches it or is absent; a terminal event
with a different `agentId`, or with no open attempt, closes nothing and is
a diagnostic (4.6). A key is resolved when it has no open attempt.
`Run.finished` is true when the journal started at least one key and every
key is resolved. One function, `reader.workflow_journal_finished`, is the
only definition of this rule; `hooks_cli._workflow_finished` calls it.

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
    command: str | None = None     # Bash's own command, exact; >2048 chars: None
    command_sha256: str | None = None  # set instead when command is None
    cwd: str | None = None         # the tool_use entry's own 'cwd', through
                                    # os.path.normpath (no filesystem access)
    result_line: int | None = None # line of the matching tool_result, or None
    is_error: bool | None = None
    backgrounded: bool = False     # the result carries BASH_BACKGROUNDED
    exit_code: int | None = None   # same parsing as Event.exit_code
    commit_ids: tuple[str, ...] = ()  # Bash 'git commit' calls only; <=20
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

Parent transcript index: for any attempt whose transcript ends in the kill
sentinel, the
reader needs the parent main transcript (`<root>/<slug>/<session>.jsonl`,
derived from the run path). A process-level cache
`ParentIndex.for_path(path)` scans that file once, line by line, and keeps
only: timestamps and target ids of `TaskStop` tool_use blocks, timestamps of
kill sentinels, and compaction timestamps. Nothing else is retained.

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
parsing `Event.exit_code` already does. `commit_ids` is scanned only for a
Bash call whose own command contains the substring `git commit`, matching
`\[[^\]\s]+ ([0-9a-f]{7,40})\]` against the result's FULL text (never the
excerpt), at most 20 matches; a Bash result that happens to contain a
bracketed hex run is never counted when its command is not a commit.

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
call regardless of completion, unique and in order; `background_unfinished`
counts calls with completion `backgrounded`; `last_tool` is the last call's
tool name; `seconds_since_last_tool_at_end` is the last call's own `ts` to
the attempt's `last_ts`, `None` when either is missing or the delta would
be negative. `Attempt.facts` is `None` only when the attempt has no
transcript at all (a missing file); a transcript that exists but could not
even be opened still produces an `AttemptFacts`, every field the literal
string `FACTS_UNKNOWN` rather than a misleadingly empty list or zero count.

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

| id | severity | kinds | fires when | metrics | rule_id |
|---|---|---|---|---|---|
| silent_retry | major | workflow | a key has more than one attempt | attempts, keys, wasted_seconds, burst | R-RETRY-SAFE, plus R-CAP-CONCURRENCY when burst (one Finding's rule_ids carries both, never two Findings) |
| mislabeled_kill | see 4.4 | both | an attempt's `interrupt` is set | silence_seconds (nullable), cause, events | R-MONITOR-RETRIES when cause is runtime or unattributed-major |
| wasted_time | info | both | always, one per run | workflow: run_seconds, attempt_seconds, wasted_seconds, wasted_tokens. session: run_seconds, child_seconds, child_count | none |
| bash_backgrounded | minor | both | Bash tool_result carries the backgrounded marker | count | R-NO-LONG-BASH |
| bash_timeout | minor | both | Bash tool_result shows exit 143 or the timeout marker | count | R-NO-LONG-BASH |
| tool_json_error | minor | both | tool_result carries the input-validation marker | count, tools | none |
| structured_output_failure | major | both | StructuredOutput tool_result carries a mismatch marker | count | R-SCALAR-SCHEMA |
| codex_write_denied | critical | both | a sandbox denial or shell-guard marker in an assistant event whose text starts with "[codex]" (the companion's output echoed), or in a Bash tool_result whose output starts with "[codex]" or whose command runs `codex` as the first word of a command segment (optionally path-qualified) or names `codex-companion.mjs` (the word inside command data, a heredoc body or a file name such as "review-codex.txt", does not count). Measured on the build machine: 23 of 23 real denials were companion output; assistant prose quoting the marker was never one | count, marker | R-CODEX-CONTRACT, R-ASSERT-ARTIFACT |
| classifier_unavailable | minor | both | tool_result carries both classifier-outage fragments | count | none |
| git_cwd_mismatch | minor | both | Bash tool_result carries the not-a-git-repository marker | count | R-GIT-CWD |
| missing_artifact | critical | both | first user message names an absolute path it must write and the path does not exist at scan time; outside allowed roots: info 'unverified' | paths, verified | R-ASSERT-ARTIFACT |
| attempt_failed | major/minor | workflow | an attempt's `outcome` is `failed` (major when no later attempt of the key completed -- whether or not one was even tried; minor when a later attempt did complete) | cause, retry_now, reset_time | R-USAGE-LIMIT when cause is a usage limit |
| claim_contradiction | major | workflow | a `completed` attempt's own dict `reported` reports a failure: `tests_failed`/`suite_failed` > 0, `ok`/`ready` false, `blockers_count` > 0, or a set `report_path` absent on disk (a relative path is unverified, never counted; the path is checked only when the run ended at most 72 hours ago or its end time is unknown, since temp reports are cleaned up later) | clauses, claims | none |

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
classifier_unavailable, git_cwd_mismatch) and never by mislabeled_kill or
missing_artifact. Whether a main-session interrupt was human is not
knowable from the transcript (a `-p` session has no human either); v0.1
records this as a documented limit rather than guessing.

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
consult it: a timed-out `cat` is still a timed-out command. Markers only
ever count in a `tool_result` event or an `assistant` text event, never a
`user` one (D-B): a review or spec pasted into a USER prompt that happens
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
diagnostic affected an attempt's first or last event, or a `started` event
carried a non-string `key` or `agentId` (M1 review finding 2). Otherwise
COMPLETE.

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

Verdict word: `FAILED` (any critical), `DEGRADED` (major, no critical),
`CLEAN` (neither), always suffixed with ` (INCOMPLETE)` when the status is
INCOMPLETE. Exit codes: 0 ok, 1 usage error, 2 no runs or not a run, 3
verdict FAILED, 4 INCOMPLETE without FAILED.

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

### 4.7 Report contract

`run` text output (rewritten by unit M3a; see `.reviews/proposals/
2026-09-v0.2-guided-rescue.md` section 6.1 for the design this implements)
leads with the verdict and the work at risk, not a findings dump:

```
RUN <id>  <VERDICT>[ (INCOMPLETE)][ (unfinished)]  ended <YYYY-MM-DD HH:MMZ> (<N days|hours|minutes> ago)
<account: 1 to 3+ sentences>

WORK AT RISK  <one line>
  <optional second line>

WHAT TO DO
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
word is `SESSION`, not `RUN`. `ended` uses `run.ended_at`, converted to UTC;
`ended: unknown` when it is `None`. The relative age ("N days/hours/minutes
ago") is computed against `report._now()` (a thin `datetime.now(timezone.
utc)` wrapper a test monkeypatches, the same pattern `detectors/
claim_contradiction.py` already uses), floor-rounded to the largest unit
that is at least 1. The header carries a trailing `(unfinished)` tag when
`Run.finished` is false (12's D-C): `run` always computes and prints real
findings and a real verdict over whatever a workflow has so far, even one
that has not finished, since an unfinished, retried, or abandoned run is
exactly the run a user most wants to inspect; the tag is the only
difference, never a substitute verdict. A session is always finished, so
this never applies to one. The old v0.1 project line (project slug, claude
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
"`<W>` was spent in attempts that returned no result." (`wasted_time`'s own
`wasted_seconds` metric, formatted with `detectors.base.format_duration`).
Session: "session ran `<T>`; `<N>` subagents, `<K>` interrupted (`<U>`
unattributed); retries not observable." (unchanged from v0.1's session
summary line, `report._session_summary_line`) plus the same failed-sentence
when one applies -- `attempt_failed` is workflow-only today, so this never
actually fires on a session, but the two account builders share the one
failed-clause function rather than duplicating it.

WORK AT RISK: computed from `AttemptFacts` of every attempt whose outcome is
`killed`, `failed`, or `superseded` (the same set `observe.py`'s own
candidate-path rule already uses): "`<n>` killed or failed attempts wrote
`<p>` paths; `<c>` commits reported." (`p`/`c` are the union of
`paths_written`/`commits` over those attempts' known facts). A second,
indented line follows when there is at least one path: the observation
summary when `--observe` was given and `checked` ("`<d>` paths dirty now,
`<m>` missing, `<k>` clean"), or "repo state not checked (`<reason>`)" when
`--observe` was given but not `checked`; "repo state: run with --observe"
when the flag was not given and the run ended within 72 hours (or its end
time is unknown); "historical: repo state not checked (run older than
72h)" otherwise. When no attempt wrote a path (including when there are no
qualifying attempts at all): "none: killed or failed attempts wrote no
paths." (no second line). When every qualifying attempt's `facts` is `None`
(no transcript at all, not merely `FACTS_UNKNOWN` fields): "unknown:
transcripts missing." (no second line).

WHAT TO DO: deterministic, at most 3 items, each tagged `[look]`
(read-only) or `[decide]` (needs a human), evaluated in this fixed order
and appended only while the list has fewer than 3 items already:

1. `[look] agent-postmortem run <id> --observe` ("see which paths are
   dirty") when at least one qualifying attempt has a known, nonempty
   `paths_written`, the run ended within 72 hours (or its end time is
   unknown), and `--observe` was not already given.
2. `[decide] wait until <reset_time> before relaunching` when a major
   `attempt_failed` finding (its key was not yet successfully retried) has
   `retry_now: "no"` and a `reset_time`; otherwise `[decide] relaunch is
   safe now: <cause phrase>` when one has `retry_now: "yes"`.
3. `[decide] rules that would have prevented this: agent-postmortem rules
   propose` when at least one major or critical finding carries a rule id.
   `run` never scans another run to report a real "N of M runs" rate (that
   would mean scanning the whole fleet from inside `run`), so this always
   points at the fleet-wide proposal command (section 9, unit M6) rather
   than naming this one run; `rules --run <id>` still exists for a
   run-scoped count, it is simply no longer what `run` itself prints.
4. `[look] agent-postmortem run <id> --verbose` ("see the findings hidden
   here") when at least one info-severity finding exists.

No item ever names an instruction file (CLAUDE.md or otherwise) or prints
`apply`; `rules propose` is the only way `run`'s own output points at
rules. The old NEXT ACTION and SUGGESTED RULES sections are gone from the
text report entirely (`--bug-report`'s own "Suggested fixes" section is a
different renderer, untouched by this unit).

AGENT table: `AGENT`, `PHASE` (kept only when at least one logical agent of
this run actually carries a nonempty phase; a session's subagents never do,
so a session's table never shows it), `ATTEMPTS`, `END` (one of
`completed`, `killed`, `failed`, `stopped by parent`, `interrupted`,
`superseded`, `unknown`, derived from the final attempt's own `outcome` and,
for a `killed` outcome, its `interrupt.cause`: `parent_stop` reads `stopped
by parent`, `human` reads `interrupted`, `runtime`/`unattributed` read
`killed`), `KILLED` (how many of this agent's attempts carry an
`interrupt`), `PATHS` (the union of `paths_written` over every attempt of
this agent; `?` when no attempt's facts could be established at all),
`COMMITS` (the union of `commits` the same way, no `?` marker), `WASTED`
(unchanged: the wall-clock of every attempt but the last). A session's
table lists subagents only, as before.

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
  `<t>`]" per finding, `n` the failing attempt's own ordinal.
- `claim_contradiction`: "attempt `<n>` completed but reported `<clauses>`"
  per finding, clauses rebuilt from `metrics.clauses`/`metrics.claims`
  (the same phrases `detectors/claim_contradiction.py`'s own title uses),
  never by re-parsing the finding's title text.
- facts: "Wrote `<p>` paths, reported `<c>` commits." when `p` or `c` is
  nonzero; "wrote no paths" (no trailing period, a fragment) when both are
  known to be 0 and the agent's final attempt was killed; omitted
  otherwise.
- a tooling detector (`bash_backgrounded`, `bash_timeout`,
  `tool_json_error`, `structured_output_failure`, `codex_write_denied`,
  `classifier_unavailable`, `git_cwd_mismatch`) attributed to the agent:
  "`<n>` `<short phrase>`", every present one joined by `"; "`.

A finding with no `agent_id` (`wasted_time` always; `missing_artifact` when
it names no attempt) backs the one `run` story instead, its text simply
every relevant finding's own (redacted) title joined into one paragraph.
Info-severity findings never make a story unless `--verbose`; `wasted_time`
is therefore counted (in the trailing "N info findings not shown"
line) but never turns into a story of its own outside `--verbose`.

Evidence: the default report prints no evidence excerpt anywhere -- not
even the `path:line` pointer. `--evidence` prints, under each story, at
most 2 `path:line: excerpt` lines (the v0.1 evidence format, redacted the
same way) drawn from that story's own findings. `--verbose` prints, after
STORIES, the full v0.1-style `FINDINGS (...)` list (`report.
_findings_section`, unchanged), including info findings; its own excerpts
still only appear when `--evidence` is also given, otherwise the section
prints titles only, no excerpt lines.

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
LAST 7 DAYS   <w> workflow runs, <r> with retries, <t> wasted; <s> session runs, <i> subagents interrupted
ALL TIME      <same shape>
  full list: agent-postmortem scan --all
```

A run needs attention when any of: it is `possibly live` (`Run.finished`
false and the newest mtime among its own files is under
`hooks_cli._UNKNOWN_QUIET_SECONDS`, 600s, old -- the best a cold scan can
say about liveness without hook state); it ended within 72 hours with
verdict FAILED or DEGRADED; it ended within 72 hours and a killed, failed,
or superseded attempt wrote a path; its verdict is FAILED regardless of
age. Ranked, in order: possibly live first; paths written by such an
attempt, descending; verdict (FAILED, DEGRADED, CLEAN); wasted seconds,
descending; `ended_at`, descending. At most 10 rows (`--limit N`); the
header's `k` is the true candidate count, never truncated by the limit. A
session with 0 subagents and a CLEAN verdict is never a candidate, only
counted in the totals. The one-line reason is deterministic, first that
applies: an `attempt_failed` clause naming the cause(s) and a reset time
when one exists; a runtime-kill clause naming paths written and commits
when a `mislabeled_kill` finding's cause is `runtime`; the top major-or-
critical detector as `<detector> x<n>`; `<n> subagents interrupted` for a
session; else `clean`. The next command adds `--observe` only when a
killed or failed attempt is known to have written a path and the run ended
within 72 hours (or its end time is unknown); it never names an
instruction file or `apply`. LAST 7 DAYS and ALL TIME are the same
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
(D8: `Run.finished` false), then totals stratified by kind: workflow runs,
runs with retries, attempts, wasted hours; session runs, subagents,
interrupted; findings per detector, each over the runs of the kinds the
detector applies to. This is byte-for-byte what `scan`'s own output was
before unit M3b; only the default (no `--all`) changed, plus the silenced-
detectors last line above when applicable.

`--json` rows are unchanged in population (one object per run, every run,
regardless of `--all`) and shape (`finished`, `top_detector` `null` instead
of a detector id when there is no major or critical finding), plus six
keys unit M3b adds to every row: `attention` (bool, the same rule the
triage list uses), `attention_reason` (the same one-line reason, or `null`
when `attention` is false), `age` (`"possibly live"` or the abbreviated
`"<n>m/h/d ago"`, or `"unknown"` when both `attention` and `ended_at` are
absent), `possibly_live` (bool), `paths_at_risk` (int, the same path count
the ranking key uses), and `unfinished_state` (`"possibly live"` |
`"failed"` | `"abandoned"` | `null`): `null` when every logical agent's own
last attempt completed; `"failed"` when every one of the rest is itself
outcome `failed` (only ever true of an attempt the journal actually closed
with a `failed` event, docs/DESIGN.md 4.2 -- so this can be `"failed"` even
when `Run.finished` is true, a run that genuinely finished with an
unretried failure); `"abandoned"` otherwise (some key's last attempt is
still open, killed, superseded, or unknown). `trend` prints per-detector
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
agent-postmortem scan   [--root PATH] [--since YYYY-MM-DD] [--kind workflow|session] [--all] [--limit N] [--json] [--no-redact]
agent-postmortem run    <RUN_ID|PATH> [--root PATH] [--json] [--no-redact] [--bug-report] [--observe] [--verbose] [--evidence]
agent-postmortem rules  [--run <RUN_ID|PATH>] [--root PATH] [--all] [--show RULE_ID] [--json] [--no-redact]
agent-postmortem rules propose [--root PATH] [--since YYYY-MM-DD] [--json] [--no-redact]
agent-postmortem rules review  [--root PATH] [--weeks N] [--json] [--no-redact]
agent-postmortem apply  <RULE_ID> --target FILE [--no-redact]
agent-postmortem trend  [--root PATH] [--weeks N] [--json] [--no-redact]
agent-postmortem demo   [--json]
agent-postmortem rescue plan   <RUN_ID|PATH> [--root PATH] [--json] [--no-redact]
agent-postmortem rescue status <RUN_ID|PATH> [--root PATH] [--json] [--no-redact]
agent-postmortem watch  <RUN_ID|PATH> [--root PATH] [--interval SECONDS] [--max-hours H] [--quiet-after SECONDS] [--done-grace SECONDS] [--json]
agent-postmortem hook   <check|session-start|session-end|workflow-launched|capabilities>
agent-postmortem --version
```

`postmortem` remains as an alias console script. `RUN_ID` may be a prefix of
either kind; ambiguity is an error listing the candidates. Default `--root`
is `~/.claude/projects`, overridable by `CLAUDE_CONFIG_DIR` (first entry when
comma-separated) for parity with ccusage. `scan --all` (unit M3b, 4.7) prints
the full one-line-per-run list instead of the default NEEDS ATTENTION triage
view; `--limit N` (default 10) caps how many candidate rows the triage view
prints and is ignored with `--all`. `run` on a workflow that has not
finished (D8: `Run.finished` false) computes and prints findings and a
verdict exactly as it does for a finished one (12's D-C); only the header
differs, tagged `(unfinished)` (4.7). Exit codes (below) are unaffected by
finished-ness: 3 for a critical finding, 4 for status INCOMPLETE, apply
the same whether or not the run has finished. `--observe` (4.9) is off by
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
starts no process a repository's own configuration could steer.

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
   `git -C <dir> config --get-regexp ^filter\..*\.(clean|process)$`;
   `git -C <dir> --no-optional-locks -c core.fsmonitor=false status
   --porcelain=v2 -z --no-renames --ignore-submodules=all -- <paths...>`.
   `git` itself is resolved to one absolute path with `shutil.which("git")`
   at the very start of `observe_run`, before any of the five calls; that
   same absolute path, never the bare string `"git"`, is argv[0] on every
   one of them, so all five run the identical binary rather than each
   doing its own separate PATH search. Each runs via `subprocess.run` with
   a list (never a shell), `timeout=10`, `stdin=DEVNULL`, and an
   environment built from scratch: `PATH` and `HOME` copied from the
   current process, plus `GIT_OPTIONAL_LOCKS=0`, `GIT_PAGER=cat`,
   `PAGER=cat`, `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, nothing else. A
   timeout or non-zero exit (except `config --get-regexp` exiting 1, which
   means no filter driver is configured) gives `checked=False` with a
   reason naming the step; no exception ever escapes `observe_run`.
5. Guards, in order, first failure wins: git not on `PATH` (`shutil.which`);
   git older than 2.36 or its version unparsable; `rev-parse
   --show-toplevel` failing, meaning the cwd is not inside a git
   repository. A fourth guard, a clean or process filter driver configured
   (`config --get-regexp` exits 0 with output), is checked only once there
   is at least one candidate path contained in the work tree (rule 6) --
   i.e. only when `status` is actually about to run -- and as the call
   immediately before `status`, with no other subprocess call in between:
   this is what keeps the window between the guard and the command it
   guards as small as this module can make it. `status` is never run when
   that guard finds a filter driver configured. Known limit, documented
   rather than fixed: nothing in the standard library stops a process with
   write access to this repository from editing `.git/config` to add a
   filter driver in the interval between this guard's own `config
   --get-regexp` call returning and `status` actually starting -- closing
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
- An id whose `severity_default` is `"critical"` (`codex_write_denied`,
  `missing_artifact` today, determined by asking the detector registry, not
  a hardcoded list): never added to `detectors_off`, recorded in
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
well-formed `[[adopted]]` table is still kept. `rules review` is the only
reader of `Config.adopted`, and combines it with the state store's legacy
`applied` entries (7.5) so a v0.1 user's history still reviews.

### 4.11 Guided rescue

`rescue.py` (v0.2, unit M4), standard library only. Two commands, both
read-only and stateless, neither writing a file, running a check, or
touching git beyond what `observe.py` (4.9) already does:

```
agent-postmortem rescue plan   <RUN_ID|PATH> [--root PATH] [--json] [--no-redact]
agent-postmortem rescue status <RUN_ID|PATH> [--root PATH] [--json] [--no-redact]
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

**`rescue plan`: the decision packet.** Always runs `observe.observe_run(run)`
first, read-only, the same call `run --observe` makes. A unit of work is one
`LogicalAgent` (4.1) whose attempts include at least one `killed`, `failed`,
or `superseded` one -- `rescue.qualifying_units(run)`. Per unit, in order:

- `UNIT <agent> (<phase>) N attempts, last one <end>`: `<agent>`/`<phase>`
  are the same `agent-N`/`phase-N` pseudonyms (5.1) `run`'s own AGENT table
  uses; `<end>` is the last attempt's outcome, `killed` refined to `stopped
  by parent`/`interrupted` the same way the AGENT table's END column already
  reads `Interrupt.cause` (4.7).
- `TASK`: a pointer, `full text: <transcript file name>:<line>`, to the
  first user message of the unit's own first (ordinal 1) attempt --
  transcript file names carry no user text and are never redacted (5.1), so
  this line is identical under both redaction modes. Under `--no-redact` a
  preceding line quotes the first 300 characters of that message, read back
  from the transcript at render time (never stored on `Attempt`, which only
  ever carries a 400-char, harness-noise-stripped excerpt); the default
  instead adds `prompt length: N chars`, the true length of that same text.
- `REPORTED`: the last attempt's own `Attempt.reported` (section 3): for a
  dict result, every allowlisted claim as `key: value` (a `report_path`
  claim gets its own `path-N` pseudonym, 5.1; a `status` claim -- up to 32
  chars of an agent's own free text, section 3 -- prints as the fixed word
  `set` by default, F7 hardening, its real value only under `--no-redact`,
  `--json` likewise: `"status": "set"`); for a text result, `text result, N
  chars, not kept`; `no result event` otherwise.
- `PATHS`: a table, one row per path the unit's own killed/failed/superseded
  attempts wrote or left in flight (the same union `observe.py`'s own rule 2
  computes, scoped to this one unit): the path (pseudonym by default, real
  repo-relative under `--no-redact`), its observed state, the ordinals of
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
- `CHECKS`: `config.checks_for_repo(cfg, observation.repo_root)` when the
  repo was observed and matched, each check its own `check-N` pseudonym by
  default (5.1, F7 hardening) -- the whole command hidden, not only a path
  embedded inside it -- raw under `--no-redact`; otherwise "no checks
  declared for `<repo>`: add `[[repos]]` to the config, see docs" (`<repo>`
  is the real toplevel path only under `--no-redact`, else the fixed word
  "this repository" -- a repo root has no pseudonym of its own, 5.3, so it
  is never printed raw by default).
- `STEPS`: two read-only `[look]` items (`git status --short`/`git diff`
  over the `DIRTY_NOW` paths, or every path written when the observation
  was not checked) always printed; four `[decide]` items (run the declared
  check, keep/fix/set aside, commit, relaunch) printed only when the run is
  not possibly live (below).
- `NEVER`: the fixed line naming `git checkout`/`restore`/`reset`/`stash`/
  `clean` as never to be run on these paths.

After every unit, `BRIEF FOR A RELAUNCH`: which attempts were killed (agent,
attempt, kill time, silence) and which failed (`attempt_failed`'s own cause,
`retry_now`, `reset_time`, 4.2/4.3), then the paths and commit ids that
already exist across the whole run (every attempt's own `completed_ok`
writes and reported commits, not only the killed/failed/superseded ones),
then two fixed sentences: "start from what exists; do not redo it" and the
resume/fan-out caveat (`anthropics/claude-code#95076`).

LIVE rule: when the run is possibly live (`report.is_possibly_live`, the
same unfinished-plus-fresh-mtime signal `scan`'s triage already uses, 4.7),
the very first line of the output is `POSSIBLY LIVE: do not modify files in
this repo. Confirm with the human that the run has stopped.`; every unit
still prints its `TASK` through `NEVER` block (the run may have already
killed/failed something even while other agents keep going), but `STEPS`
shows only its two `[look]` items, and `BRIEF FOR A RELAUNCH` is not printed
at all. A possibly-live run with no qualifying unit yet prints only the LIVE
line, never the `NOTHING TO RESCUE` sentence below, since that sentence's
own claim ("every attempt completed") would be false for a run that may
still be running.

No unit at all and the run is not possibly live (F6 hardening): the
cheerful `NOTHING TO RESCUE: every attempt completed.`, nothing else, exit
0, only when `run.finished` is true AND every attempt's own `outcome` is
`completed`. Otherwise (an unfinished run, or one whose only attempt is
`unknown` -- no transcript at all, so not proven completed either) that
claim would be false: `NOTHING TO RESCUE YET: the run has not finished and
no attempt was killed or failed.`, exit 4.

Exit codes: 0 packet complete; 2 no such run; 4 a possibly-live run, a
"NOTHING TO RESCUE YET" run, or any `UNKNOWN` fact anywhere in the packet
(an unchecked observation, a path state of `UNKNOWN`, an `AttemptFacts`
field the reader could not establish, or a killed/failed/superseded
attempt whose `facts` is `None`, 4.1); 1 a usage error.

`--json`: `{schema: 2, run: {run_id, kind, finished, possibly_live},
nothing_to_rescue, units: [{agent, phase, attempts, end, task: {pointer,
length, text} | null, reported, paths: [{path, state, reason, attempts,
later_write_observed}], commits, in_flight: [{tool, path}], unknown:
[str, ...], checks: [str, ...], steps: [{grade, text, command}]}], brief:
{killed: [...], failed: [...], already_exists: {paths, commits}} | null,
exit_code}` (`task.text` is `null` unless `--no-redact`; `brief` is `null`
for a possibly-live or nothing-to-rescue run), through the same export
allowlist and redaction rules as the text report.

**`rescue status`: what was observed.** Reads the run's parent session
transcript with the existing reader (a workflow run: the session `.jsonl`
the run directory sits under, `reader._parent_transcript_path`; a session
run: itself), via `reader._parse_main_attempt` so its main attempt carries
`tool_calls` (4.2's v0.2 reader changes). Considers only tool calls whose
own timestamp is after the run's `ended_at` (every one, when `ended_at` is
unknown).

A Bash call counts as a run of a declared check only when all hold: its
`command` equals the declared string exactly, trailing whitespace stripped;
its own `cwd`, normalized, equals the observed repo root; it is paired with
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

Per unit (`rescue.qualifying_units(run)`, the same population `rescue plan`
uses): `checks declared: N`; the last counted run's own command -- its own
`check-N` pseudonym by default (5.1, F7 hardening), raw under `--no-redact`
-- its exit state (`completed_ok` reads exit 0, `completed_error` reads
non-zero), and its time, mapped to one word -- `EXIT_0_OBSERVED`,
`NONZERO_OBSERVED`, or `NOT_OBSERVED` when there was no counted run at all.
`WRITE_AFTER_CHECK` (F3 hardening) is tagged whenever, strictly after that
last counted run: a `completed_ok` Edit/Write/NotebookEdit call wrote one of
the unit's own candidate paths, OR any Bash call anywhere in the transcript
whose command is not proven read-only for this purpose
(`rescue._proven_read_only`, deliberately narrower and different from
`reader._is_read_only_bash_command`'s own self-reference rule, 4.3: `git
status` is never a write, and neither is a `sed` invocation without an
in-place flag, both unlike the reader's own heuristic) -- the latter is not
scoped to one unit's own paths (a possible write to the repository is a
possible write, whichever unit's files it may have touched) and is listed
under "not counted" too, reason "a Bash command that may write ran after the
check". `check cycles observed: K of 2` (a cycle is a counted run preceded,
since the previous one, by at least one write to a listed path -- `K` is the
true observed count, not capped at the 2-cycle protocol budget, so a
session that kept going past it is visible, not hidden); every "not
counted" line (no excerpt in text, only a line number and reason -- see
`--json` for the excerpt); every NEVER-list match (`git checkout`/
`restore`/`reset`/`stash`/`clean`) found anywhere in the transcript after
`ended_at`, shown with only the command's first word (`git`, `sed`,
`printf`) by default, F7 hardening -- the full, path-N-substituted 60-char
excerpt only under `--no-redact`. Any NEVER-list match anywhere forces exit
4 regardless of everything else (below), and appends `NEVER_COMMAND_
OBSERVED` to the printed/exported word (both text and `--json`), alongside
the plain word, never replacing it. The fixed sentence "Writes made outside
this session after the check cannot be seen; freshness beyond the
transcript is UNKNOWN." is always printed last. R8: the word "verified" is
never printed by this command, and never appears in `rescue.py`'s own
source; a test greps both, case-insensitively.

Exit codes: 0 every unit `EXIT_0_OBSERVED` with no `WRITE_AFTER_CHECK` and
no NEVER-list match anywhere; 2 no such run; 3 any unit `NONZERO_OBSERVED`
(checked ahead of 4, the same precedence `run` gives a critical finding over
a merely-INCOMPLETE status, 4.8); 4 any unit `NOT_OBSERVED`, any
`WRITE_AFTER_CHECK`, or the parent transcript itself could not be read; then
-- overriding every code above, including 3 -- any NEVER-list match anywhere
forces exit 4; 1 a usage error. `--json` mirrors the text: `{schema: 2, run:
{run_id}, units: [{agent, checks_declared, last_counted_run: {command,
completion, at} | null, word, writes_after, write_after_check, cycles}],
not_counted: [{line, reason, excerpt}], never_matches: [{line, excerpt}],
transcript_unknown, exit_code}` (`command` is the check-N pseudonym by
default, raw under `--no-redact`; every `excerpt`, in both lists, is the
command's own first word by default, the full 60-char excerpt only under
`--no-redact`; `word` carries the same `NEVER_COMMAND_OBSERVED` suffix the
text line does when applicable).

**The `/rescue` skill** (`plugin/skills/rescue/SKILL.md`, registered the
same way `/postmortem` is, under the plugin's `skills/` directory): what the
tool can and cannot do in one paragraph, then the 7-step protocol from
`.reviews/proposals/2026-09-v0.2-guided-rescue.md` section 8.4 verbatim in
substance (get the packet with `--no-redact` and stop on `POSSIBLY LIVE`;
read the diffs and report them; run the declared checks with the session's
own Bash tool, or ask the human when none are declared; repair within the
2-cycle budget then stop; never undo, copy a file aside with `cp` before a
risky edit; ask the human before every commit and relaunch, showing `rescue
status`; never edit an instruction file), then how to read `UNKNOWN` and
`DIRTY_NOW`. The `/postmortem` skill (7.4) gets one line pointing at
`/rescue` for when `WORK AT RISK` names any paths or commits.

### 4.12 Watching a run

`watch.py` (v0.2, unit M5), standard library only. `agent-postmortem watch
<RUN_ID|PATH>` (4.8 has every flag) is a read-only follower for one
workflow run, designed to be the command a Claude Code Monitor runs: it
prints one line per event a person would act on and nothing otherwise, then
a terminal DONE or LIMIT line, and exits. It never writes inside the run
directory, arms nothing on its own, and holds no state across invocations.

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
or FAILED needs its last line or its silence span.

**Events**, in the order a poll considers them (KILL and FAILED are one-shot
per transition; DONE ends the loop; QUIET recurs; RESET and MALFORMED can
each happen at most once per truncation/malformed-line respectively; lines
are at most 78 columns, a second, indented line is allowed for KILL):

```
KILL   <agent> attempt <n> after <s>s of silence; restarted as attempt <n+1>.
       attempt <n> wrote <p> paths, reported <c> commits.  -> agent-postmortem rescue plan <run id>
FAILED <agent> attempt <n>: <cause phrase>.
QUIET  <agent> attempt <n> has produced nothing for <s>s.
RESET  journal was truncated or replaced; state cleared.
DONE   <A> agents, <T> attempts, <VERDICT>.  -> agent-postmortem run <run id>
LIMIT  <hours>h reached; run not finished.
```

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
  attempt's own last transcript entry, never a wider window; `cause not
  recorded` when nothing there is attributable (this does not, on its own,
  delay emission or count as a reason to retry -- only the rate limit does,
  per F10 below). Unlike the `attempt_failed` detector's own title, this
  line names no retry tail -- `watch` is a live feed of what just happened,
  not a verdict over the whole run.
- `QUIET`: fires when an open (not yet closed by `result`/`failed`/a later
  `started`) attempt's transcript mtime is at least `--quiet-after` seconds
  old. Printed once per attempt, then again only once the silence has
  doubled from the threshold it last fired at (300, 600, 1200, ... seconds
  with the default `--quiet-after`) -- a poll where the doubled threshold is
  not yet reached, or where the per-agent rate limit (below) suppresses it,
  leaves the threshold unchanged, so the next poll can still fire at it.
  Within one poll, KILL and FAILED are always emitted before QUIET.
- `RESET` (release finding F8): fires when the journal is found smaller than
  the byte offset `watch` last read from -- truncated in place, or replaced
  by a fresh run reusing the same directory. Every piece of state derived
  from the old content (the parsed journal itself, which KILL/FAILED
  transitions are already handled or still pending, every QUIET doubling
  threshold, every per-key rate-limit timestamp, the DONE-grace clock and
  its own `started`-count baseline, and the malformed-line flag and line
  counter below) is cleared before the replacement content is processed at
  all, so nothing from before the reset can combine with what comes after
  it into a false KILL or a false DONE. RESET is exempt from the rate limit
  (like DONE and LIMIT) and can fire again later if the journal is
  truncated more than once.
- `DONE`: fires once every key the journal has started is resolved
  (`reader.workflow_journal_finished`, reused verbatim, never re-derived)
  AND that has stayed true for `--done-grace` seconds (default 30) with no
  new `started` event. A workflow whose script launches its agents in
  sequential phases shows every key seen so far resolved in the gap between
  two phases; the grace period covers that gap, since the next phase's
  first `started` lands within seconds. A new `started` resets the grace
  clock. `--done-grace 0` prints DONE at once. `<A>`/`<T>` and `<VERDICT>`
  come from one full `reader.load_run` plus `detectors.run_detectors` at
  that moment, the same verdict `run` would print. Honest limit: a gap
  longer than the grace period still ends the watch early; the DONE line
  is then wrong about the run being over, and the launch hint's Monitor
  will need re-arming. Release finding F9: once the journal has carried any
  malformed line (below), DONE can never be printed again for this run --
  LIMIT (with a different message) fires in its place instead, at the exact
  point DONE otherwise would have.
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

**Retry of a suppressed KILL/FAILED event (release finding F10).** A KILL
or FAILED event is marked handled only once it has actually been resolved,
never merely detected: an event this poll cannot resolve is retried on
every later poll rather than being dropped. KILL can be unresolved for two
reasons: the per-key rate limit (below), or the earlier attempt's own
transcript file not existing on disk yet (the journal's `started` line for
the next attempt can land before the previous attempt's transcript write
is visible). Once the transcript does exist, if it does not end in the
kill sentinel, that is conclusive -- not a kill, never printed, never
retried again. FAILED can be unresolved only by the rate limit; a missing
transcript already degrades gracefully to `cause not recorded` rather than
blocking emission, so it is never a reason to retry FAILED. Either kind of
retry is bounded at 12 polls: on the 12th unresolved poll, KILL is
force-printed without its transcript-derived details --
`KILL   <agent> attempt <n> (details unavailable); restarted as attempt
<n+1>.` -- and FAILED is force-printed bypassing the rate limit (using
whatever cause phrase it already has); either way the event is then marked
handled and never retried further. This is what closes release finding
F10: previously, an event suppressed by the rate limit, or seen before its
transcript existed, was marked handled at the moment it was first
detected and never printed at all.

**Rate limit.** At most one line per agent (per journal key) per 60 seconds,
except DONE, LIMIT, and RESET, which are never rate-limited. A QUIET line
suppressed by the limit leaves its doubling threshold unchanged, so it can
still fire on a later poll once the limit clears. A KILL or FAILED line
suppressed by the limit is retried, not dropped -- see **Retry of a
suppressed KILL/FAILED event** above.

**Exit codes.** 0 on DONE with verdict CLEAN or DEGRADED (with or without
the `(INCOMPLETE)` suffix); 3 on DONE with verdict FAILED; 4 on LIMIT (the
ordinary `--max-hours` timeout, or either malformed-journal case above); 2
when no journal appeared within 60 s.

**Redaction.** `watch` always redacts; there is no `--no-redact`. An
`<agent>` token is that key's own label, pseudonymized exactly as `run`'s
AGENT table would (a `Redactor` built once for the whole poll loop, its
`Pseudonyms` fed one `agent()` registration per journal key in the order
each key's first `started` event is read, the same order `report.
render_run_text`'s own `_register_names` would see) -- `--json`'s `agent`
field carries the same pseudonym. The run id itself is printed as is (it
carries no user text, 5.1).

**`--json`** prints one JSON object per line, `{"event": "KILL" |
"FAILED" | "QUIET" | "RESET" | "DONE" | "LIMIT" | "MALFORMED", ...}` with
the fields named above (KILL: `agent`, `attempt`, `silence_seconds`,
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

## 5. Privacy

### 5.1 Redaction and pseudonyms

`redact(text)` keeps the v0 rules: home directory and truncated spellings
to `~`, bare account name (3+ chars, whole word) to `user`, emails to
`email:` plus a 6-char hash, UUIDs to `uuid:` plus last 4 hex, project slugs
`-Users-...`/`-home-...` to `project:` plus a 6-char hash. This is
structural redaction: it runs on every string a renderer prints -- including
an evidence line, a diagnostic, and a metrics value -- and it never
substitutes a pseudonym.

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
(structural `redact()` followed by a word-boundary anchored substitution,
`(?<![A-Za-z0-9_])name(?![A-Za-z0-9_])`, over every name registered so far)
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
`--no-redact` disables both `redact()` and `Pseudonyms`, `path-N` included.
Run ids and agent ids (17 hex starting with `a`) are not redacted: they
carry no user text.

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

`render_bug_report`'s output ends with a footer stating this plainly:
"Redacted: paths, ids, emails, account names, agent names. Not redacted:
evidence lines, which are transcript excerpts. Read them before posting."
The bug report issue template repeats the same distinction and never calls
the output safe to paste unread; a reader must still check evidence lines
themselves before filing.

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
'dict', keys, claims} / {type: 'text', text_length}; `reported` is not a
structured name field, so the whole thing goes through the plain Redactor
via `redact_value`, the same as an evidence line or a metrics value -- a
`report_path` claim is the one exception (M1 review finding 1): the plain
Redactor call itself substitutes it with its `path-N` pseudonym (5.1),
registered by `_register_names` before export runs, so it never appears raw
either; `reported_line`, being reader-internal only, is never
exported), and facts (v0.2, unit M2a: null, or {paths_written, in_flight,
commits, cwds, background_unfinished, last_tool,
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
  transcript at all) or `{paths_written, in_flight, commits, cwds,
  background_unfinished, last_tool, seconds_since_last_tool_at_end}` --
  every one of those seven fields is independently either its normal shape
  or the literal string `"UNKNOWN"` (a transcript that exists but could not
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
  target_kind, findings, runs, first, last, text}` (`findings`/`runs` the
  finding and distinct-run counts backing the proposal; `first`/`last` the
  earliest/latest backing run's own start date, `"YYYY-MM-DD"`). `proposals`
  is `[]`, never an error, when no rule clears the threshold (section 9).
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
  path), raw under `--no-redact`. Each `steps[]` entry:
  `{grade, text, command}` (`command` nullable; only the two `"look"` steps
  are present at all for a possibly-live run). `brief` is `null` for a
  possibly-live or nothing-to-rescue run, else `{killed, failed,
  already_exists}`: `killed[]`: `{agent, ordinal, time, silence_seconds}`
  (`time` a `"HH:MMZ"` string or `null`); `failed[]`: `{agent, cause,
  retry_now, reset_time}`; `already_exists`: `{paths, commits}`.
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
  lists, is by default only the command's own first word (F7 hardening: a
  full 60-char excerpt, even path-N-substituted, still carried private flags
  and arguments); the full transcript quote (the first 60 characters of the
  Bash command, redacted the same structural way an evidence line is,
  4.5/5.2, with any listed path substituted by its `path-N` pseudonym) is
  shown only under `--no-redact`.

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
- The demo run lives once, in `src/agent_postmortem/demo/wf_00000000-d30/`
  (M8 review D8: a conforming `wf_` run id, not the hash-fallback id a
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
(`src/agent_postmortem/demo/wf_00000000-d30/`, section 5.4 and section
8.2's packaged demo): it is both the `agent-postmortem demo` run and the
case the detector, attribution, redaction, and export tests load, in one
place, never copied. silent_retry_kill is also the runtime-cause case for
section 4.4: each killed attempt's key has a later attempt (the restart)
and no parent transcript exists for either, so cause runtime, major, wins
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
.claude-plugin/marketplace.json    {name: "agent-postmortem", owner, plugins: [{name: "agent-postmortem", source: "./plugin"}]}
plugin/.claude-plugin/plugin.json  name, version (== package version), description, author, hooks, skills
plugin/hooks/hooks.json
plugin/hooks/run.sh
plugin/skills/postmortem/SKILL.md
plugin/README.md
```

Install: `/plugin marketplace add zeyusuntt/agent-postmortem`, then
`/plugin install agent-postmortem@agent-postmortem`. `claude plugin validate
./plugin` passes in CI when the `claude` binary is available.

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
  id), it goes back to `running` with `stable_checks` reset to 0, keeping
  its `reported` ids so an already-surfaced finding is never re-notified
  just because the surrounding content moved. Fingerprint comparison
  always happens first: an unchanged `running`/`unknown` unit only advances
  `stable_checks` and is never reparsed, unless this is the one check that
  crosses the unknown threshold below (that check still needs one real
  parse, to give the forced-INCOMPLETE analysis real findings).
- A workflow unit is `finished` when every started key is resolved by a
  `result` or `failed` event (section 3, `reader.workflow_journal_finished`). A subagent unit is `finished` only with authoritative
  evidence: its own last transcript line is the kill sentinel (a trailing
  line that fails to parse as JSON at all means not finished, never
  "finished as of the last valid line"), or the parent main transcript
  already carries this agent's id on the Agent tool's result -- for a real
  transcript, that id lives on the entry itself
  (`entry["toolUseResult"]["agentId"]`), with a `tool_result` block naming
  it a fallback for a differently-shaped transcript (a light index of the
  main transcript -- agent ids seen either way, alongside the kill-sentinel
  timestamps interrupt attribution already needs -- built once per hook
  invocation and shared across every subagent unit checked in it), or the
  payload's `background_tasks` names it with a
  status meaning done. An assistant text entry with no `tool_use` is never
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
  id (a workflow directory's own name, or a short hash of a subagent's
  transcript path) rather than retrying the same failure on every future
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
  are persisted before the output is printed.
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
children at all) and run just the seven tooling detectors from 4.3 over it
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
"agent-postmortem==<pinned in plugin.json>" --python 3.11+` (`UV_TOOL_DIR`
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

## 8. Distribution

### 8.1 Package

`pyproject.toml`: name `agent-postmortem`, version `0.2.0`, console scripts
`agent-postmortem` and `postmortem`, `requires-python >= 3.11`, no runtime
dependencies, `[project.optional-dependencies].dev = ["pytest>=8",
"ruff>=0.6"]`, classifiers, URLs (Homepage, Issues, Changelog). The wheel
ships `agent_postmortem/demo/**` and `agent_postmortem/hooks/*.mjs`.

### 8.2 Demo

`agent-postmortem demo` renders the packaged synthetic run so a first run
needs no data; `--json` exports it. The README's fenced code block on its
first screen (8.4) is this output, byte for byte.
The packaged run is resolved through `importlib.resources.files(...)`,
read back out only inside `importlib.resources.as_file(...)` at the point
of use (D4): a plain `Path(__file__).parent` has nothing to `iterdir()`/
`open()` when the package is imported straight out of a wheel `.whl`
(itself a zip) rather than unpacked into site-packages, and `as_file` is
what makes that case transparently extract to a real temporary directory
instead. `demo`'s exit code is derived exactly the way `run`'s is (D5): 3
for a critical finding, 4 for status INCOMPLETE, else 0 -- never a
hardcoded 0 regardless of what the packaged run's own findings are.

### 8.3 CI and release

`.github/workflows/ci.yml`: on push and pull request; matrix ubuntu-latest
and macos-latest times Python 3.11, 3.12, 3.13; steps: `uv sync --extra dev`,
`uv run ruff check`, `uv run pytest`, `uv build`, `scripts/check-privacy.sh
--tree --artifacts dist/`, and `claude plugin validate ./plugin` when the
binary exists. `.github/workflows/release.yml`: on tag `v*`, build job then
publish job with `environment: pypi`, `permissions: id-token: write`,
`pypa/gh-action-pypi-publish@release/v1`. Both workflows declare a
top-level `permissions: contents: read` so every job is read-only by
default regardless of the repository's own token-permission setting; the
publish job's own `id-token: write` is layered on top of that at the job
level, the only elevated permission anything in either workflow needs.
CONTRIBUTING.md documents the one-time trusted-publisher setup on
pypi.org and the TestPyPI dry run.

### 8.4 README

First screen, in order: name, one-line tagline, badges (PyPI version, Python
versions, CI, license), the demo output as a fenced code block (not a
screenshot: it must stay text, so it can be diffed against a live
`agent-postmortem demo` run -- 8.2, tests/test_readme.py), `uvx
agent-postmortem scan`, a command table listing exactly which commands take
`--json` and which take `--no-redact`. Then: Why (the 110 runs / 38 h
number and links to anthropics/claude-code issues #88993, #84346, #85206,
#65500), plugin install in two commands, what it detects (table from 4.3
with the honesty rule from 4.4), privacy (what is redacted, what a pseudonym
is, the one network action), how it was built, limits (internal format,
observed versions, retries not observable in sessions).

## 9. Rules library (v0.2, unit M6: proposals only)

The library itself, unchanged in substance from v0: R-RETRY-SAFE,
R-CAP-CONCURRENCY, R-NO-LONG-BASH, R-SMALL-QUANTUM, R-ASSERT-ARTIFACT,
R-SCALAR-SCHEMA, R-CODEX-CONTRACT, R-MONITOR-RETRIES, R-GIT-CWD,
R-MODEL-ALLOCATION (hook), and R-USAGE-LIMIT (backed by `attempt_failed`:
when a run reports a usage-limit failure, do not relaunch the same key
before the reset time the failure named). What changed is what the tool
does with them: it never writes one anywhere (R3). `rules.RULES`,
`rules_for()`, `rules --show`, and `rules --run`/`--all` are otherwise
unchanged from v0.1.

**Target kind.** Every `Rule` carries a `target_kind`: `"workflow prompt"`
(the agent prompt preamble inside a workflow script -- where most rules
belong, since the failure they describe is specific to how one workflow
launches and talks to its own agents: R-RETRY-SAFE, R-CAP-CONCURRENCY,
R-NO-LONG-BASH, R-SMALL-QUANTUM, R-ASSERT-ARTIFACT, R-SCALAR-SCHEMA,
R-CODEX-CONTRACT, R-MONITOR-RETRIES, R-USAGE-LIMIT), `"hook"` (a Claude Code
hook that can enforce the rule mechanically before an agent even starts:
R-MODEL-ALLOCATION), or `"project instructions"` (a repo-wide file, for the
one rule that is not about any one workflow's own prompt but about how
every agent in every workflow is launched: R-GIT-CWD). `rules --show` and
`rules propose` both print it, so a reader is never left guessing whether a
suggestion belongs in the file they already think of as "the rules file."

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

**`rules propose [--root PATH] [--since YYYY-MM-DD] [--json]
[--no-redact]`.** Scans runs the same way `scan` does (discovery, `--root`,
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
