# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.3]

### Added

- Detector `codex_unavailable`. On one machine, 7 workflow agents whose job was a Codex
  review returned "completed" with a short text saying the review never ran: the Bash call to
  `codex-companion.mjs` failed with "Codex CLI is not installed or is missing required runtime
  support", and 2 of those runs read CLEAN. The detector gives one finding per attempt, major
  when the attempt completed and no later companion call returned a result without that line,
  minor otherwise. Assistant text quoting the sentence (4 entries) and other commands printing it
  never count. It backs R-CODEX-CONTRACT, which now also says to check that the companion
  starts before a workflow relies on a Codex agent, and `rules review` counts it as a
  recurrence of that rule.

### Changed

- README and DESIGN: the transcript formats are now stated as observed on Claude Code 2.1.251 to
  2.1.312 (was 2.1.280). The Limits section names the five entry types first seen in 2.1.285 to
  2.1.310 that are not interpreted yet, and says Claude Code deletes transcripts older than 30
  days by default (`cleanupPeriodDays`). Why gives the 0.3.2 scan of 2026-10-27 (13 of 166
  workflow runs retried) and the upstream status as of 2026-10-28.

### Fixed

- Reported commit ids printed as written at the default share level, and a short hash of a
  public repository's commit can identify the repository and its author. `rescue plan` (text
  and `--json`) and `run --json` now print each reported commit id as `commit-N`, numbered per
  output, the same id the same N. A result's own `commits` claim is covered too: `run --json`
  (`reported.claims.commits` and, with `ready: false`, `findings[].metrics.claims.commits`),
  `rescue plan --json`, and the REPORTED line of `rescue plan` print `commit-N` instead of the
  ids. `rescue plan --json` now exports the units before the brief, so the first `commit-N` it
  prints is `commit-1`. `--local` prints the real ids, scanned for paths as before; `--raw` prints them unchanged; counts are unchanged. A file named
  `commit-1` no longer turns a commit into `path-1` in JSON.
- A Codex companion call with a command over 2,048 characters (the forwarder passes long prompts
  inline) was not recognized as codex. A failing call followed by a successful long retry stayed
  `major` instead of `minor`, and an attempt whose only codex call was long lost the
  `codex_write_denied` assistant-text match. Both now judge the full command.
- `structured_output_failure` no longer tells a multi-agent run as the first hit's agent's story:
  with failures in 2 or more agents its `agent_id` is now empty and the finding backs the run
  story, like the other 5 run-wide tooling detectors. Title, count, severity and rule are
  unchanged; the `finding_id` of a multi-agent finding changes.
- The session-end hook dropped a `codex_unavailable` finding from a session's main thread. It
  now runs all 8 tooling detectors, so a main-thread Codex companion failure is reported.
- Scan and the run report disagreed on one run: a killed attempt in the last 72 hours whose only
  write was a relative-path Edit with no working directory read "1+ paths named" in scan and
  "0 paths named" under WORK AT RISK. The scan reason for a runtime kill and for an attempt at
  risk now uses the same helper and the same numbers as the WORK AT RISK count line.
- A nested foreground subagent whose report came back as a plain "[Subagent hand-back]" result
  read "unknown" and WORK AT RISK said "unknown: N subagents have no established return" (4
  children in one real session). The launch is now also found by the `toolUseId` in the child's
  meta file, which matched the result-linked launch in 651 of 651 cases where both exist. A
  `toolUseId` that is not a string reads malformed.
- A usage-limit, machine-sleep or API-error failure would have read "not recorded" on Claude
  Code 2.1.305 or later when a SubagentStop hook is set. Those transcripts end with an
  `attachment` entry (type `hook_success`) after the last assistant entry, which 163 of 205
  transcripts measured on 2.1.305 to 2.1.312 had. Trailing `attachment` entries no longer
  count as the attempt's final entry, so the cause and reset time are read, in the `watch` FAILED line too. A `user` or
  `system` entry after the assistant entry still makes it not final. No real case was hidden.
- A tooling finding that counted hits from several agents was told as the first hit's agent's
  story, for example `agent-1  6 Bash commands backgrounded.` when the 6 hits were in 5 agents.
  The five run-wide detectors (`bash_backgrounded`, `bash_timeout`, `tool_json_error`,
  `classifier_unavailable`, `git_cwd_mismatch`) now set the agent only when all hits are in one
  agent; with hits in 2 or more agents there is no agent and the finding backs the run story.
  Titles, counts, severity and rules are unchanged. The `finding_id` of a multi-agent finding
  changes, because the id includes the agent.
- `scan` no longer prints the reason `clean` next to a run in NEEDS ATTENTION. A run listed
  only because an attempt at risk wrote a path (ended within 72 hours, no finding, no
  interrupt) now reads like `2 attempts at risk: 1+ paths named, 0 commits`, with the same
  counts as the WORK AT RISK line, in the text and in the JSON `attention_reason`.
- Transcripts written by Claude Code 2.1.300 or later no longer make every attempt's write
  ledger partial. Those versions record each API request in the transcript (`api-request`,
  `api-request-shape`, `api-request-blob` entries), which the ledger did not know, so every
  attempt line in WORK AT RISK ended with `ledger incomplete (unknown_entry_type)`. A scan of
  296 real runs on 2026-10-28 found the entries in every transcript from 2.1.300 to 2.1.312;
  with the fix no verdict changed and one run's at-risk list became complete.

## [0.3.2]

Repository checks now work on machines with Git LFS, and they never run a filter driver or a
fetch the repository could steer. Before this, every `--observe` and `rescue status` check that
reached `git status` on such a machine stopped at `guard refused`, including on GitHub's CI
runners, which is why CI failed there after 0.3.1 was pushed.

### Changed

- The filter-driver guard reads where each clean or process filter driver is configured
  (`git config --show-scope`). A driver from your own system or global git config, which is
  where `git lfs install` puts Git LFS's, no longer stops `git status`; it is switched off for
  that one call instead, so it never runs (a repository can steer even a driver it did not
  configure: its attributes pick the driver, and Git LFS reads extension commands from the
  repository's own config). A file routed through such a driver is compared without that
  external filter (built-in conversions still apply), so a Git LFS file whose real content is
  checked out reads dirty once its timestamp changes, even when its content did not. A driver the
  repository configures (its `.git/config`, its worktree config, or a file either of those
  includes), a line the guard cannot read, an unusual driver name, and a failed check all still
  stop `status`.
- README: the opening says what problem the tool answers; the quick start installs from GitHub,
  since no PyPI release exists yet; the PyPI badges are gone until one does; the plugin section
  says to install the command-line tool first; Why gives the retry count from a dated 0.3.1 scan
  (12 of 130 workflow runs) instead of the 0.1 figures, and lists six upstream issues with their
  status on 2026-10-26.

### Fixed

- In a partial clone, `git status` fetches a missing object on demand through the promisor
  remote the repository configured, and an `ext::<command>` URL runs that command. Repository
  checks now run git with `GIT_ALLOW_PROTOCOL=` (which refuses the transport on every supported
  git version) and `GIT_NO_LAZY_FETCH=1` (on versions that know it), so the fetch is refused and
  the check reports unchecked. This was possible since repository checks were added in 0.2.
- `test_trend_still_counts_a_detector_silenced_by_config` ran on the real clock and failed from
  2026-10-26 on, when its 2026-09-01 fixture left the default 8-week window. The clock is now
  pinned inside the test.

### Known limits

- In a repository with a sparse index or a split index, git's own `status` can still store a
  cached tree object or refresh the shared index file's timestamps during a repository check.
  Tracked content and refs are not changed. Refusing these modes is left for a later change.

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
