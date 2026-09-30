# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
