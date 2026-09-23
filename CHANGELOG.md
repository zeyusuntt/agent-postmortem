# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
