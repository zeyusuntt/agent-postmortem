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
- Read-only over inspected data. The only writes: the rules target the user
  names in `apply --write`, and the tool's own state directory. Never inside
  a run directory or a transcript.
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

## 3. Data formats (observed on Claude Code 2.1.251 to 2.1.280)

`journal.jsonl` (workflow runs): one JSON object per line. Observed `type`
values: `launched`, `started`, `result`. A `started` event carries `key` (a
stable hash per logical agent() call, prefixed `v2:`), `agentId`, `label`,
`phase`. The same `key` appearing in more than one `started` event means the
runtime restarted that logical agent; attempts are ordered by journal order.
A `result` event carries `key` and `outcome`. Unknown event types are
preserved verbatim on `Run.journal` and ignored by detectors.

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
class Attempt:
    agent_id: str; key: str | None; label: str; phase: str
    agent_type: str | None; meta: dict; transcript_path: Path | None
    events: list[Event]; first_ts: datetime | None; last_ts: datetime | None
    ordinal: int; outcome: str   # 'completed' | 'killed' | 'unknown'
    usage: dict
    interrupt: Interrupt | None  # set by the reader when outcome == 'killed'

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

Parent transcript index: for any attempt with `outcome == 'killed'`, the
reader needs the parent main transcript (`<root>/<slug>/<session>.jsonl`,
derived from the run path). A process-level cache
`ParentIndex.for_path(path)` scans that file once, line by line, and keeps
only: timestamps and target ids of `TaskStop` tool_use blocks, timestamps of
kill sentinels, and compaction timestamps. Nothing else is retained.

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
| silent_retry | major | workflow | a key has more than one attempt | attempts, keys, wasted_seconds, burst | R-RETRY-SAFE, plus R-CAP-CONCURRENCY when burst |
| mislabeled_kill | see 4.4 | both | an attempt's `interrupt` is set | silence_seconds (nullable), cause, events | R-MONITOR-RETRIES when cause is runtime or unattributed-major |
| wasted_time | info | both | always, one per run | workflow: run_seconds, attempt_seconds, wasted_seconds, wasted_tokens. session: run_seconds, child_seconds, child_count | none |
| bash_backgrounded | minor | both | Bash tool_result carries the backgrounded marker | count | R-NO-LONG-BASH |
| bash_timeout | minor | both | Bash tool_result shows exit 143 or the timeout marker | count | R-NO-LONG-BASH |
| tool_json_error | minor | both | tool_result carries the input-validation marker | count, tools | none |
| structured_output_failure | major | both | StructuredOutput tool_result carries a mismatch marker | count | R-SCALAR-SCHEMA |
| codex_write_denied | critical | both | an assistant event, or a Bash tool_result whose own command mentions "codex", carries a sandbox denial or shell-guard marker | count, marker | R-CODEX-CONTRACT, R-ASSERT-ARTIFACT |
| classifier_unavailable | minor | both | tool_result carries both classifier-outage fragments | count | none |
| git_cwd_mismatch | minor | both | Bash tool_result carries the not-a-git-repository marker | count | R-GIT-CWD |
| missing_artifact | critical | both | first user message names an absolute path it must write and the path does not exist at scan time; outside allowed roots: info 'unverified' | paths, verified | R-ASSERT-ARTIFACT |

The main attempt of a session run is scanned by the tooling detectors
(bash_*, tool_json_error, structured_output_failure, codex_write_denied,
classifier_unavailable, git_cwd_mismatch) and never by mislabeled_kill or
missing_artifact. Whether a main-session interrupt was human is not
knowable from the transcript (a `-p` session has no human either); v0.1
records this as a documented limit rather than guessing.

Silence and durations come from transcript timestamps only. A detector never
reads a file outside the run directory except `missing_artifact`
(`os.path.exists` on the named path) and the parent index (4.2).

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
than 5 percent of a transcript's lines were undecodable, or a timestamp
diagnostic affected an attempt's first or last event. Otherwise COMPLETE.

Verdict word: `FAILED` (any critical), `DEGRADED` (major, no critical),
`CLEAN` (neither), always suffixed with ` (INCOMPLETE)` when the status is
INCOMPLETE. Exit codes: 0 ok, 1 usage error, 2 no runs or not a run, 3
verdict FAILED, 4 INCOMPLETE without FAILED.

### 4.7 Report contract

`run` text output for a workflow run keeps the v0 shape: header line, the
summary line "N agents became M attempts; R silent retries burned T; K
kills recorded as user interrupts", project line, AGENT table, FINDINGS,
SUGGESTED RULES, NEXT ACTION. Session runs use a session summary: "session
ran T; N subagents, K interrupted (U unattributed); retries not observable".
The AGENT table for a session lists subagents only. The header carries a
trailing `(unfinished)` tag when `Run.finished` is false (12's D-C): `run`
always computes and prints real findings and a real verdict over whatever
a workflow has so far, even one that has not finished, since an unfinished,
retried, or abandoned run is exactly the run a user most wants to inspect;
the tag is the only difference, never a substitute verdict. A session is
always finished, so this never applies to one.

`scan` prints one line per run (id, date, project, kind, agents, attempts,
verdict, top detector), with the verdict followed by `unfinished` (not
replacing it, 12's D-C) when that run has not finished (D8: `Run.finished`
false), then totals stratified by kind: workflow runs, runs with retries,
attempts, wasted hours; session runs, subagents, interrupted; findings per
detector, each over the runs of the kinds the detector applies to.
`--json` rows also carry `finished`. `trend` prints per-detector weekly
counts and before/after rates with the applicable-kind denominator beside
every rate. There is no cross-kind total.

`--json` emits `{run, findings, rules, verdict, status}` through the export
allowlist (5.3). `--bug-report` renders the v0 section order with
pseudonyms for every user-authored name.

### 4.8 CLI

```
agent-postmortem scan   [--root PATH] [--since YYYY-MM-DD] [--kind workflow|session] [--json] [--no-redact]
agent-postmortem run    <RUN_ID|PATH> [--root PATH] [--json] [--no-redact] [--bug-report]
agent-postmortem rules  [--run <RUN_ID|PATH>] [--root PATH] [--all] [--show RULE_ID] [--json] [--no-redact]
agent-postmortem apply  <RULE_ID> --target FILE [--write] [--no-redact]
agent-postmortem trend  [--root PATH] [--weeks N] [--json] [--no-redact]
agent-postmortem demo   [--json]
agent-postmortem hook   <check|session-start|session-end|workflow-launched|capabilities>
agent-postmortem --version
```

`postmortem` remains as an alias console script. `RUN_ID` may be a prefix of
either kind; ambiguity is an error listing the candidates. Default `--root`
is `~/.claude/projects`, overridable by `CLAUDE_CONFIG_DIR` (first entry when
comma-separated) for parity with ccusage. `run` on a workflow that has not
finished (D8: `Run.finished` false) computes and prints findings and a
verdict exactly as it does for a finished one (12's D-C); only the header
differs, tagged `(unfinished)` (4.7). Exit codes (below) are unaffected by
finished-ness: 3 for a critical finding, 4 for status INCOMPLETE, apply
the same whether or not the run has finished.

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
target as `target-N`.

Pseudonym substitution applies to exactly two places: a structured name
field a renderer reads directly off `Pseudonyms` (the AGENT table, a bug
report's reproduction line, an exported attempt's label/phase/agent_type, a
trend row's target), and `Finding.title`, rendered through `Redactor.title()`
(structural `redact()` followed by a word-boundary anchored substitution,
`(?<![A-Za-z0-9_])name(?![A-Za-z0-9_])`, over every name registered so far).
Evidence, a diagnostic, and a metrics value are never fed to `Pseudonyms`:
they pass through structural `redact()` alone (plain `Redactor.__call__`),
because they are a faithful quote of transcript text (4.5), not a rendered
sentence a detector built from a label -- a phase named "Implement" must
never turn "Implement the fix" inside an evidence line into "phase-1 the
fix". `--no-redact` disables both `redact()` and `Pseudonyms`. Run ids and
agent ids (17 hex starting with `a`) are not redacted: they carry no user
text.

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
event_count, interrupt {cause, silence_seconds}. Finding: all fields, title
through `Redactor.title()` (a title routinely embeds a label verbatim),
evidence and metrics through the plain Redactor, structural redaction only,
never a pseudonym substitution (5.1) -- a metrics value can in principle
carry raw text of its own, which is not a rendered sentence built from a
label the way a title is, so it is never fed to Pseudonyms even when
present. In practice no v0.1 detector's metrics carry transcript-derived
free text at all (D3: bash_backgrounded reports only a count, never the
commands themselves -- the evidence lines already show the marker line each
one fired on): a finding's evidence is the one place transcript text
leaves the tool. No meta, no cwd, no branch, no event list, no transcript
text beyond evidence.
`to_json(run, findings, redactor)` wraps export_run with rules, verdict, and
a top-level status, matching 4.7's `{run, findings, rules, verdict,
status}`. Neither ever reaches for a dataclass's own private, complete dict
form (that form exists for tests only).

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
- A workflow unit is `finished` when the journal has a `result` for every
  started key. A subagent unit is `finished` only with authoritative
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
whenever the conventional path is not found on disk. Emit nothing.

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
result yet, and a scan row for one carries that trailing marker, 12's
D-C), falling back to the newest `unfinished` row only when it is at least
10 minutes old -- `latest` should not hand back a unit that is probably
still actively running just to have something to show, but an unfinished
row old enough to likely be stalled or abandoned is still a real answer;
with a run id runs `agent-postmortem run <id>`. The skill pastes the
verdict block (noting a `(unfinished)` header tag when present), and when
the verdict is not CLEAN, shows the `apply` command for the top rule and
asks before writing.

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
 "applied": [{"rule_id", "target", "applied_at"}],          # unchanged from v1
 "sessions": {"<session-key>": {"units": {...}, "last_check": iso}},
 "digest": {"pending": ["<session-key>::<unit-id>", ...]}}
```

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

`pyproject.toml`: name `agent-postmortem`, version `0.1.0`, console scripts
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

## 9. Rules library

Unchanged from v0: R-RETRY-SAFE, R-CAP-CONCURRENCY, R-NO-LONG-BASH,
R-SMALL-QUANTUM, R-ASSERT-ARTIFACT, R-SCALAR-SCHEMA, R-CODEX-CONTRACT,
R-MONITOR-RETRIES, R-GIT-CWD, R-MODEL-ALLOCATION (hook). `apply` keeps the
managed-block contract and atomic write; with `--write` it runs the whole
operation -- read target, compute diff, write target, record the applied
entry -- inside one `state.transaction()` (section 7.5), so a failure on
the state side never leaves a target already changed with no matching
record, and two concurrent `--write` calls against the same target are
fully serialized. Apply is idempotent against drift: when the rule is
already present in the target but no applied entry names that (rule_id,
target) pair, `--write` records one anyway, and both `--write` and a dry
run report "already present, recorded" rather than the plain "nothing to
do" of a true no-op.

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
