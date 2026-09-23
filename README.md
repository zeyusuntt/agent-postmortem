# agent-postmortem

Turn the run directories a Claude Code Workflow leaves behind into a verdict, evidence, and a suggested rule when one exists.

[![PyPI version](https://img.shields.io/pypi/v/agent-postmortem.svg)](https://pypi.org/project/agent-postmortem/)
[![Python versions](https://img.shields.io/pypi/pyversions/agent-postmortem.svg)](https://pypi.org/project/agent-postmortem/)
[![CI](https://github.com/zeyusuntt/agent-postmortem/actions/workflows/ci.yml/badge.svg)](https://github.com/zeyusuntt/agent-postmortem/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

```
RUN wf_00000000-d30  DEGRADED
2 agents became 4 attempts; 2 silent retries burned 20m; 2 kills recorded as user interrupts
project demo   claude 2.1.267   2026-01-06 19:00Z to 19:55Z

AGENT                 PHASE      ATTEMPTS  OUTCOME    WALL-CLOCK
agent-1               phase-1    2         completed  25m (1 killed, 10m wasted)
agent-2               phase-1    2         completed  25m (1 killed, 10m wasted)

FINDINGS (critical 0, major 4, minor 0)
[major] mislabeled_kill  agent-1 attempt 1 was killed by the runtime after 416s of silence, logged as a user interrupt
  agent-ac200000000000001.jsonl:4: Tests pass. Writing the implementation now.
  agent-ac200000000000001.jsonl:5: [Request interrupted by user]
[major] mislabeled_kill  agent-2 attempt 1 was killed by the runtime after 416s of silence, logged as a user interrupt
  agent-ac200000000000003.jsonl:4: Tests pass. Writing the implementation now.
  agent-ac200000000000003.jsonl:5: [Request interrupted by user]
[major] silent_retry  agent-1 restarted 2 times, 10m wasted
  agent-ac200000000000001.jsonl:1: Implement the silent-alpha module and add a test.
  agent-ac200000000000002.jsonl:1: You may be a retry after a killed attempt. Implement the silent-alpha module and add a test.
[major] silent_retry  agent-2 restarted 2 times, 10m wasted
  agent-ac200000000000003.jsonl:1: Implement the silent-beta module and add a test.
  agent-ac200000000000004.jsonl:1: You may be a retry after a killed attempt. Implement the silent-beta module and add a test.
[info] wasted_time  run spent 55m wall-clock; 20m wasted across 2 retried attempts

SUGGESTED RULES
R-MONITOR-RETRIES  (backed by 2 findings in this run)  agent-postmortem rules --show R-MONITOR-RETRIES
R-RETRY-SAFE  (backed by 2 findings in this run)  agent-postmortem rules --show R-RETRY-SAFE

NEXT ACTION
agent-postmortem apply R-MONITOR-RETRIES --target ~/.claude/CLAUDE.md
```

That is `agent-postmortem demo`, run against a packaged synthetic run. No setup, no real data, no network access.

## Quick start

```
uvx agent-postmortem scan
uvx agent-postmortem run <run-id>
```

`scan` looks under `~/.claude/projects` by default and prints one line per run plus fleet totals. `run` takes a run id (or a unique prefix of one, or a path) and prints the verdict for that one run, with evidence in `path:line` form.

## Commands

| Command | What it does |
|---|---|
| `agent-postmortem scan` | Scan every run under a root directory and print one line per run plus fleet totals. |
| `agent-postmortem run <run-id>` | Diagnose one run: verdict, findings with evidence, and the next action. |
| `agent-postmortem run <run-id> --bug-report` | Render the same diagnosis as a markdown bug report instead of the default text. |
| `agent-postmortem rules` | List suggested rules and how many findings back each one. |
| `agent-postmortem apply <rule-id> --target <file>` | Print a diff of the rule's managed block against the target. Writes nothing. |
| `agent-postmortem apply <rule-id> --target <file> --write` | Write that diff into the target, once you have read it. |
| `agent-postmortem trend` | Show weekly detector counts and before/after rates for rules you have applied. |
| `agent-postmortem demo` | Render the packaged synthetic demo run. No root and no real data needed. |
| `agent-postmortem hook <event>` | Claude Code plugin hook entry point; reads hook JSON from stdin. |

`scan`, `run`, `rules`, and `trend` take `--json` and `--no-redact`; `apply` takes `--no-redact` only; `demo` takes `--json` only; `hook` takes neither.

## Why

On one machine, 110 Claude Code Workflow runs over five weeks had 39 with a silent retry: an agent killed mid-task and restarted from scratch with no journal event and no notification, its transcript recording the kill as `[Request interrupted by user]` although no human interrupted. Rebuilding the lost work across those 39 runs cost about 38 hours on that one machine alone. The same failure is reported upstream:

- [anthropics/claude-code#88993](https://github.com/anthropics/claude-code/issues/88993): workflow subagents are interrupted around 180 seconds while a model request is still in flight.
- [anthropics/claude-code#84346](https://github.com/anthropics/claude-code/issues/84346): a subagent model-stall watchdog kills a subagent after about 600 seconds of silence.
- [anthropics/claude-code#85206](https://github.com/anthropics/claude-code/issues/85206): the stall watchdog kills a subagent that is actively working every 8 to 9 minutes.
- [anthropics/claude-code#65500](https://github.com/anthropics/claude-code/issues/65500): a deep-research workflow aborts its entire run the moment any one schema-bound subagent fails to emit structured output.

agent-postmortem turns the run directories the harness already leaves behind into a verdict, evidence, a suggested rule when one exists, and a trend line that shows whether the rule worked.

## Inside Claude Code

Install the plugin with two commands:

```
/plugin marketplace add zeyusuntt/agent-postmortem
/plugin install agent-postmortem@agent-postmortem
```

Its hooks shell out to the same CLI, never touch a run directory, and never fail a turn. On `SessionStart` they summarize any run that finished since you last looked. On `Stop` and `StopFailure` they check every subagent and workflow unit spawned so far and print a finding the moment it is detected, not only at session end. On `SessionEnd` they run the read-only tooling checks over the main thread itself. On a `Workflow` launch they register the run immediately, so it is tracked even before its own directory exists.

## What it detects

| Detector | Severity | Fires when |
|---|---|---|
| `silent_retry` | major | A key started more than one attempt. |
| `mislabeled_kill` | info or major | An attempt was interrupted; see the honesty rule below. |
| `wasted_time` | info | Always, once per run: sums attempt durations from transcript timestamps; a workflow run also reports the time spent in attempts that were later retried. |
| `bash_backgrounded` | minor | A Bash result carries the backgrounded marker. |
| `bash_timeout` | minor | A Bash result shows exit 143 or the timeout marker. |
| `tool_json_error` | minor | A tool result carries the input-validation marker. |
| `structured_output_failure` | major | A StructuredOutput result carries a mismatch marker. |
| `codex_write_denied` | critical | A Codex Bash command or an assistant message carries a sandbox denial. |
| `classifier_unavailable` | minor | A tool result carries both classifier-outage fragments. |
| `git_cwd_mismatch` | minor | A Bash result carries the not-a-git-repository marker. |
| `missing_artifact` | critical or info | The first user message names an absolute `.md`/`.json`/`.txt`/`.jsonl` path it must write, and the path does not exist at scan time; critical inside the home or temp roots, info outside them. |

The honesty rule: `mislabeled_kill` blames the runtime, major severity, only when the same key was retried afterward -- the restart itself is the evidence, never a guess. Long silence before a kill with nothing else to explain it is still major severity, but reported as "cause unattributed, consistent with a runtime kill," never asserted as the runtime's fault outright. It reports info when a parent session stopped the attempt on purpose, a human interrupt happened alongside it, or the silence was short.

## Privacy

`redact()` replaces your home directory with `~`, a bare account name with `user`, an email with `email:` plus a 6-character hash, a session id with `uuid:` plus its last 4 hex digits, and a project slug with `project:` plus a 6-character hash, on every string any command prints. A pseudonym is different: a stable per-render label like `agent-1` or `phase-2` standing in for a name you or a teammate chose, so numbering restarts every time a report is rendered and two reports can never be linked by a repeated label. The only transcript text that ever leaves the tool is an evidence excerpt, a short quoted line pointing at exactly where a finding fires; read those lines yourself before pasting a report anywhere.

State (which rules you have applied, which findings a session already reported) lives in `$XDG_STATE_HOME/agent-postmortem/state.json`, or `~/.local/state/agent-postmortem/state.json` if that variable is unset; nothing in it is ever printed. The plugin's one network action happens on `SessionStart` only: when no protocol-compatible CLI resolves from the plugin's own data directory or `PATH`, it installs the version pinned in the plugin manifest, retrying at most once every 24 hours on failure; opt out by setting `AGENT_POSTMORTEM_NO_AUTO_INSTALL=1`, or by installing the CLI yourself first.

Pass `--no-redact` to any command to see raw paths and session ids instead.

## How it was built

It was built across two Claude Code Workflow runs, and those same runs are among the ones agent-postmortem itself audits as an acceptance check: the tool that finds silent retries had real ones to find in the runs that built it. Every milestone was test-first: a failing fixture and test before the code that made it pass, its own touched tests run while iterating, and the full suite run clean before its commit. Every milestone's diff then got a cross-model review from the main session -- Codex, then Claude's own code-reviewer -- before the next milestone started.

## Limits

The formats this tool parses are internal to Claude Code, not a published API, and were observed on versions 2.1.251 to 2.1.280; a future version can change them without notice. A session's retries are not observable the way a workflow's are: a session has no cache key to compare attempts against, so `silent_retry` never fires on one. Whether a main-session interrupt was a human pressing Escape is not knowable from the transcript, so it is never attributed, only recorded as a limit. Tested on macOS and Linux only.

## Contributing

See `CONTRIBUTING.md` for dev setup, the test-first workflow, and the privacy check every push must pass.

## License

MIT, see `LICENSE`.
