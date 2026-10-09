# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1]

A second field round on real runs. Session subagents get a proven return state, the report stops
counting things it cannot see, and four checks the first round missed are added.

Five rules changed after review. When any transcript that could hold a resume request
cannot be read, every child's return is unknown. A resume addressed to an agent name, in `to` or
`recipient`, is no longer a contradiction; only another child's id is. A tool-use id that is not a
string makes the children it names unknown. A hook unit that was re-opened is analyzed again at
once. A transcript whose last line was cut inside a multibyte character is a half-written line,
like one cut in plain text: the run gets a diagnostic and the rest of the transcript is still read.

### Changed

- A session subagent's end comes from what its parent recorded (a returned result, a failed or
  killed task notification, a resume), not only from its own transcript. The AGENT table gains a
  `RESUMES` column when some subagent was resumed, WORK AT RISK names a subagent with no
  established return in one words-only line, and `rescue` says `NO RESCUE UNIT` instead of
  `NOTHING TO RESCUE` when a child has no established return. A child that reported failure is an
  `attempt_failed` finding.
- `claim_contradiction` also fires when a completed attempt's returned result carries a top-level
  `<name>_failed` count above 0 (an integer, not a boolean; `tests_failed` and `suite_failed` keep
  their own clauses) or `blocked: true`. Major, with a fixed title (`reports failures in <n> field(s)`, `n` the number
  of such fields) that names neither the key nor any result text. `blockers_fixed`, `blocker_kind`, `blocked_reason`, nested keys and
  `blocked: "yes"` count for nothing.
- The COMMITS cell reads `?` when a completed shell call contained a commit command but reported no
  commit id and no other id is known (it read `0+`), with a legend line under the table that starts
  `COMMITS ?`. With at
  least one known id it still reads `N+`. The STORIES sentence, the scan reason for a runtime kill
  and the `watch` KILL line follow the same rule: `N+ commits`, or `commit count unknown` where
  they said `0 commits`.
- `run --here` and `scan --here`: when `CLAUDE_CODE_SESSION_ID` names the only session with that
  transcript name and that session itself worked in this directory, the workflow runs it started
  are included, whatever directory their agents worked in. Another session's workflows never are.
- At the default share level, `reported.keys` in the JSON lists `key-1`, `key-2` and so on instead of
  the result's own key names (numbered in order of first appearance within one report). `--local`
  and `--no-redact` keep the real names.

### Added

- Detector `permission_denied` (minor): a tool result with `is_error` that starts with one of the
  runtime's four denial sentences. One finding per attempt, the session main thread included, with
  the four counts and up to 5 `file:line` pointers. A rule denial may span lines (a multi-line
  command) and name an `mcp__` tool. The denied command is never printed. It is not
  run by the session-end hook.
- The maintainer's advisory corpus check gains invariants 6 to 13 (an independent return state, the
  exported outcome, the WORK AT RISK guard, denial counts, claim coverage, rescue wording, `--here`).
  It is a maintainer check, not a shipped feature: its logic is test support under `tests/` and the
  runner over real runs is not part of the package.

### Not in this release

- Round-scoped paths, risk and WASTED for resumed children.
- A `main` row for the main thread's commits.
- Stopping a background shell with TaskStop.
- PATHS for writes inside inline Python or a script.

## [0.3.0]

0.3 rebuilds the evidence under every verdict: severity, WORK AT RISK, and the
run's end come from recorded facts, and anything the tool cannot establish
says `unknown`. A field round on five projects and 238 real runs fixed what
the synthetic tests missed (see Fixed).

### Changed

- `--json` is schema 3 on every versioned command: a `disclosure` field,
  stable aliases and `path-N` names at the default level, no transcript
  excerpt key, a `lifecycle` on each run, a `ledger` on each attempt, and an
  `observation` and `ledger` entry per path in `rescue`.
- Severity is computed from recorded inputs (cause, silence, position, whether
  a later attempt completed, the run lifecycle), not from which detector fired.
  No detector is critical. `FAILED` comes only from a failed run lifecycle.
  `missing_artifact` is `info` and WHAT TO DO adds one `[look]` item that
  points to `run <id> --verbose`.
- `WORK AT RISK` is a multi-line block built from a write ledger. It says
  `none:` only when the risk population is complete and no ledger names a
  write, and `unknown:` when the tool cannot establish which attempts were at
  risk. A shell command counts as "may have written more" unless it matches a
  closed read-only grammar.
- `Run.finished` follows the run lifecycle. A session has no lifecycle, so its
  header says `last activity` instead of `ended` and it never carries
  `(unfinished)`.
- The AGENT table's PATHS column counts every ledger path and prints `N+` when
  the count is a lower bound. `KILLED` counts only kills the runtime owns.
- `mislabeled_kill` names an observed backgrounding precursor without claiming
  it caused the kill. `rules propose` shows an adopted rule's status instead of
  proposing it again. `R-NO-LONG-BASH` carries the macOS note.
- `run --observe` and `rescue` work per repository; `rescue status` counts a
  check per declared repository.
- Detector discovery runs once per process.
- Paths in free text (titles, diagnostics, error messages, and at `--local`
  the excerpts) are found by one shell-word scanner. A path start hides the
  rest of its quoted fragment, or of its line when it is not quoted, and a
  hidden span that held spaces is marked ` [text hidden]`; `--raw` shows
  everything. Relative names stay visible: the default level prints no
  transcript text at all, so they appear only at `--local` and `--raw`.
- `run --here` diagnoses the session it is run from (Claude Code's
  `CLAUDE_CODE_SESSION_ID`), else the most recently active run that worked
  in this directory or a parent of it, and says on stderr when several runs
  matched. `scan` lists
  possibly live runs with no other problem under `LIVE NOW` instead of
  `NEEDS ATTENTION`. `scan --all` orders rows by the instant each run started,
  oldest first, with undated runs first, and a session row that may still be
  running says `possibly running`.
- WASTED counts every attempt before an agent's last one, whether or not it
  returned a result, plus the last one when it was killed, failed, or
  superseded; a killed or failed last attempt with no retry now counts too.
- The AGENT and PHASE columns size to the longest shown label (long labels are
  cut with `...`), a legend explains `N+`, and WORK AT RISK, REPO STATE, the
  scan detector counts, and the `bash_timeout` title name what they measure.
  `run`, `rescue plan`, `rescue status`, and `watch` document their exit codes
  in `--help`.
- The packaged demo is a complete run with its parent session transcript, so
  its output shows what a typical run shows.

### Added

- Three disclosure levels: share (the default), `--local`, and `--raw`, with
  aliases from a persistent registry (`a:`, `s:`, `project:`).
- A write evidence ledger per attempt and a risk population per run.
- Marker provenance: a marker counts only in the position the registry
  anchors; marker-like text anywhere else is ignored. A Bash result whose
  first line matches the runtime notice still counts, by design, even when a
  command printed it (a `cat` of a log that starts with that notice).
- The `workflow_failed` detector, and a run lifecycle with generations read
  from the parent transcript.
- `run --here`, `scan --here`, and `RUN_ID` forms for a session UUID, an
  `agent-<id>` stem, and `a:<alias>`.
- `watch` prints `START` first and `OK` per returned result, and `DONE` fires
  at once from lifecycle evidence.
- Exit codes for `rescue plan`, `rescue status`, and `watch`.
- `rules propose --run <id>` and the `agent prompt` rule target.

### Fixed

- A workflow the harness reported failed no longer reads `CLEAN`.
- `rescue plan` and `rescue status` no longer print `NOTHING TO RESCUE` for a
  run whose risk population is not empty and complete; they print
  `NO KILLED OR FAILED ATTEMPT` and exit 4.
- A missing transcript no longer shows an exact PATHS count, and the `watch`
  KILL line writes `P+` for a partial ledger.
- An asynchronous Agent launch counts as returned only through an accepted
  completed notification.
- `rescue plan` prints no `<none>` step and the fan-out caveat only for
  overlapping attempts of different keys.
- A workflow's first agent, which starts a fraction of a second before the
  launch result is written, is no longer reported as unplaced and at risk.
  Attempts are placed in a launch by the time of the launch call itself
  (unplaced attempts on the maintainer's 238 runs: 90 before, 0 after).
- A registered path inside a longer path no longer leaves the longer path's
  tail visible at the default level.
- A forked session no longer counts the block it copied from its parent as
  its own findings and time.
- A commit made with `git commit -q`, which prints no summary line, is shown
  as a lower bound (`N+`) instead of an exact 0.
- Paths glued to Chinese, Japanese, Korean, Thai, Lao, Tibetan, Myanmar, or
  Khmer text, Windows and `file://` paths, `$HOME/`, `$PWD/`, and
  `$CLAUDE_PROJECT_DIR/` spellings, colour-coded paths, and quoted names with
  apostrophes, smart quotes, or a cut-off closing quote are hidden at the
  default and `--local` levels. After a word of those scripts a path counts
  when it has two segments, a file extension, or a slash after it, so a slash
  between two words (`成功/失败`) stays prose. A path glued straight to a word
  of a language written with spaces (`read/srv/x`) is not read as a path, by
  design; docs/DESIGN.md 5.1 lists every recorded limit.
- A run directory the tool cannot read is reported as a diagnostic instead of
  ending the command with a traceback.

## [0.2.0]

Design first: 8 code-level studies of how other agent tools bound their
repair loops, two Codex reviews of the design, and the reviews removed every
write surface the first draft had. In 0.2 the tool observes and reports; a
human decides. The tool never edits a file, never writes to a repository,
never runs a check, never launches an agent, and never says "verified".

### Added

- `agent-postmortem rescue plan <run-id>`: a read-only decision packet per
  killed or failed agent: the task pointer, what it reported back, every path
  it wrote with its state in the repository right now (`DIRTY_NOW`,
  `CLEAN_NOW`, `MISSING_NOW`, `UNKNOWN`), the commits it reported, the tool
  call that was in flight at the kill, what is unknown, the checks declared
  for that repository, and the steps to take, each tagged `[look]` or
  `[decide]`. A possibly live run gets a stop line instead of steps.
- `agent-postmortem rescue status <run-id>`: what the session's own
  transcript shows was run after the run ended: `EXIT_0_OBSERVED`,
  `NONZERO_OBSERVED`, `NOT_OBSERVED`, `WRITE_AFTER_CHECK`, the check cycles
  observed against a budget of 2, calls that were not counted and why, and
  any command matching the never-undo list.
- `agent-postmortem watch <run-id>`: a read-only follower for one running
  workflow that prints `KILL`, `FAILED`, `QUIET`, then `DONE` or `LIMIT`,
  designed to be a Claude Code Monitor command. The plugin's Workflow launch
  hook hands the session that command.
- `/rescue` skill: the bounded protocol the session follows (read the
  packet, inspect, run the declared checks, at most 2 repair cycles, never
  undo with git, ask before every commit and relaunch, never edit an
  instruction file).
- Repository observation behind `run --observe` and inside `rescue plan`:
  5 fixed read-only git argument lists, an environment built from scratch,
  skipped entirely when git is older than 2.36 or a filter driver is
  configured, and skipped for runs older than 72 hours.
- Two detectors: `attempt_failed` (the cause of a journal `failed` event,
  read from the last transcript line: usage limit with its reset time,
  machine sleep, server error, interrupt) and `claim_contradiction` (an
  attempt that completed while its own result reported failed tests,
  `ok: false`, `ready: false`, blockers, or a missing report file).
- Per-attempt facts from the full transcript entry: paths written, tool
  calls in flight at the kill, commits reported, backgrounded commands,
  seconds since the last tool call.
- `rules propose`: the rules backed by findings in at least 3 runs on 2
  distinct days, with where each belongs (a workflow prompt, a hook, or
  project instructions). `rules review`: each adopted rule's detector rate
  before and after adoption, and the rules to consider removing.
- User configuration at `~/.config/agent-postmortem/config.toml`: quiet a
  detector (`[detectors] off`), declare a repository's checks (`[[repos]]`),
  record adopted rules (`[[adopted]]`).
- `--json` carries `schema: 2`, `facts`, `stories`, `next`, `observation`,
  `reported`, and `silenced` on every finding.

### Changed

- `run` text report: verdict, a plain account of what happened, WORK AT
  RISK, at most 3 next steps, the agent table with killed/paths/commits
  columns, and one story per agent. Evidence excerpts and info findings are
  behind `--evidence` and `--verbose`. The report never names an instruction
  file.
- `scan` defaults to a ranked NEEDS ATTENTION view with a reason and a next
  command per run, plus 7-day and all-time totals; `--all` prints the old
  full list. Unfinished runs are split into possibly live, failed, and
  abandoned.
- Journal `failed` events are read: a run whose every key ended is never
  shown as unfinished, and the hook and the reader share one definition of
  finished.
- `silent_retry` is one finding per agent; its evidence points at the kill
  line and the restart line instead of quoting the prompt; its title counts
  restarts, not attempts.
- `codex_write_denied` counts only the codex-companion's output or a `codex`
  command's result, never prose that quotes the marker.
- `report_path` and every observed path print as `path-N` pseudonyms by
  default.

### Removed

- `apply --write`. `apply` prints the diff and never writes; a human pastes
  the block in. The tool has no code path that writes an instruction file.

## [0.1.0]

### Added

- `agent-postmortem scan`: scan every run under a root directory and print
  one line per run plus fleet totals, stratified by workflow and session.
- `agent-postmortem run`: diagnose one run by id, prefix, or path: a
  verdict, evidence in `path:line` form, and the rule that would have
  prevented the failure. `--json` for machine-readable output, `--bug-report`
  for a markdown bug report, `--no-redact` to see raw paths and session ids.
- `agent-postmortem rules`: list suggested rules and how many findings back
  each one, or show the full text of one rule.
- `agent-postmortem apply`: write a suggested rule into a target markdown
  file as a managed, idempotent block.
- `agent-postmortem trend`: weekly detector counts and before/after rates
  for rules that have been applied.
- `agent-postmortem demo`: render the packaged synthetic demo run, so a
  first run needs no real data on disk.
- `agent-postmortem --version`.
- Eleven detectors: `silent_retry`, `mislabeled_kill`, `wasted_time`,
  `bash_backgrounded`, `bash_timeout`, `classifier_unavailable`,
  `codex_write_denied`, `git_cwd_mismatch`, `structured_output_failure`,
  `tool_json_error`, `missing_artifact`.
- Interrupt attribution: distinguishes a runtime kill from a parent stop, a
  human interrupt, and an unattributed silence.
- Redaction on by default: paths, session ids, and user-authored labels are
  pseudonymized in every default output; `--no-redact` is the only way to
  see them raw.
- A Claude Code plugin (`plugin/`) that runs the same detectors as hooks
  during a live session and notifies on a new finding.
- Zero runtime dependencies beyond the Python 3.11 standard library.
