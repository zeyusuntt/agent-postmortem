# agent-postmortem

Turn the run directories a Claude Code Workflow leaves behind into a verdict, the work that is at risk, and the next step. Then guide the rescue without ever touching your files.

[![PyPI version](https://img.shields.io/pypi/v/agent-postmortem.svg)](https://pypi.org/project/agent-postmortem/)
[![Python versions](https://img.shields.io/pypi/pyversions/agent-postmortem.svg)](https://pypi.org/project/agent-postmortem/)
[![CI](https://github.com/zeyusuntt/agent-postmortem/actions/workflows/ci.yml/badge.svg)](https://github.com/zeyusuntt/agent-postmortem/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

```
RUN wf_00000000-d30  DEGRADED  ended 2026-01-06 19:55Z
2 agents took 4 attempts.
2 attempts were killed by the runtime and restarted.
20m was spent in attempts that returned no result.

WORK AT RISK  none: killed or failed attempts wrote no paths.

WHAT TO DO
  1. [decide] rules that would have prevented this: agent-postmortem rules propose
  2. [look]   agent-postmortem run wf_00000000-d30 --verbose   see the findings hidden here

AGENT     PHASE      ATTEMPTS  END                KILLED  PATHS  COMMITS  WASTED
agent-1   phase-1    2         completed          1       0      0        10m
agent-2   phase-1    2         completed          1       0      0        10m

STORIES (2)   details: --verbose   evidence: --evidence
  agent-1  Killed 1 time by the runtime (416s of silence).
  agent-2  Killed 1 time by the runtime (416s of silence).

1 info finding not shown (--verbose)
```

That is `agent-postmortem demo`, run against a packaged synthetic run. No setup, no real data, no network access. The header leads with the verdict; the account underneath it says what happened in plain sentences; WORK AT RISK says whether a killed or failed attempt left work that could still be lost; WHAT TO DO is at most 3 next steps, each tagged `[look]` (read-only) or `[decide]` (needs you); STORIES tells one short story per agent that has a finding, worst first. Evidence excerpts and info-severity findings (`wasted_time` here) are hidden by default; add `--evidence` or `--verbose` to see them.

## Quick start

```
uvx agent-postmortem scan
uvx agent-postmortem run <run-id>
```

`scan` looks under `~/.claude/projects` by default and prints the runs that need attention: ranked, with a one-line reason and a next command for each, plus 7-day and all-time totals. Add `--all` for the full one-line-per-run list instead. `run` takes a run id (or a unique prefix of one, or a path) and prints the verdict for that one run, what work is at risk, and at most 3 next steps; add `--evidence` for excerpt pointers or `--verbose` for the full finding list.

## Commands

| Command | What it does |
|---|---|
| `agent-postmortem scan` | Scan every run under a root directory and print the ones that need attention, ranked, plus 7-day and all-time totals. `--all` prints the full one-line-per-run list instead. |
| `agent-postmortem run <run-id>` | Diagnose one run: verdict, work at risk, and at most 3 next steps, one story per agent. |
| `agent-postmortem run <run-id> --bug-report` | Render the same diagnosis as a markdown bug report instead of the default text. |
| `agent-postmortem rules` | List suggested rules and how many findings back each one. |
| `agent-postmortem rules propose` | Print the rules backed by findings in at least 3 runs on 2 distinct days, and where each belongs. |
| `agent-postmortem rules review` | Print each adopted rule's detector rate before and after adoption, and list rules to consider removing. |
| `agent-postmortem apply <rule-id> --target <file>` | Print the rule's managed block as a diff against the target. The tool never writes it for you; paste the block in yourself, or save the output and run `git apply`. |
| `agent-postmortem trend` | Show weekly detector counts and before/after rates for rules you have adopted. |
| `agent-postmortem demo` | Render the packaged synthetic demo run. No root and no real data needed. |
| `agent-postmortem rescue plan <run-id>` | Print a read-only decision packet: what a killed or failed attempt wrote, its repo state, declared checks, and the steps to take next. Never edits anything or runs a check itself. |
| `agent-postmortem rescue status <run-id>` | Print what the main session's own transcript shows was run and observed after the run ended. Reports only what it saw, never a confirmation that a check passed. |
| `agent-postmortem watch <run-id>` | Follow one running workflow: print KILL, FAILED, and QUIET as they happen, then DONE or LIMIT. Read-only, designed to be a Claude Code Monitor command. |
| `agent-postmortem hook <event>` | Claude Code plugin hook entry point; reads hook JSON from stdin. |

`scan`, `run`, `rules`, and `trend` take `--json` and `--no-redact`; `apply` takes `--no-redact` only; `demo` and `watch` take `--json` only; `rescue` and `hook` take neither.

## Why

On one machine, 110 Claude Code Workflow runs over five weeks had 39 with a silent retry: an agent killed mid-task and restarted from scratch with no journal event and no notification, its transcript recording the kill as `[Request interrupted by user]` although no human interrupted. Rebuilding the lost work across those 39 runs cost about 38 hours on that one machine alone. The same failure is reported upstream:

- [anthropics/claude-code#88993](https://github.com/anthropics/claude-code/issues/88993): workflow subagents are interrupted around 180 seconds while a model request is still in flight.
- [anthropics/claude-code#84346](https://github.com/anthropics/claude-code/issues/84346): a subagent model-stall watchdog kills a subagent after about 600 seconds of silence.
- [anthropics/claude-code#85206](https://github.com/anthropics/claude-code/issues/85206): the stall watchdog kills a subagent that is actively working every 8 to 9 minutes.
- [anthropics/claude-code#65500](https://github.com/anthropics/claude-code/issues/65500): a deep-research workflow aborts its entire run the moment any one schema-bound subagent fails to emit structured output.

agent-postmortem turns the run directories the harness already leaves behind into a verdict, evidence, a suggested rule when one exists, and a trend line that shows whether the rule worked. Since 0.2 it also names the cause of a failed attempt (a usage limit with its reset time, the machine sleeping, a server error), reads what each agent reported back and flags an agent that said "done" while its own result reported failed tests, and shows which files a killed attempt wrote and whether they are still uncommitted.

## Rescue: who does what

When a run leaves work behind, the question is what to keep and how to continue without making it worse. agent-postmortem does not repair anything. It splits the job three ways, and the tool's part is read-only by construction:

| Actor | Does | Never does |
|---|---|---|
| The tool | reads transcripts, observes the repository with 5 fixed read-only git commands, prints a decision packet (`rescue plan`) and later reports what the session's own transcript shows was run (`rescue status`) | edits a file, writes to a repository, runs a check, launches an agent, reverts anything, says "verified" |
| The Claude session | follows the `/rescue` protocol: reads the packet, inspects the diffs, runs the checks you declared, repairs within 2 observed check cycles, and asks you before every commit and relaunch | undoes with `git checkout`, `restore`, `reset`, `stash`, or `clean`; edits an instruction file; continues past the budget |
| You | approve commits and relaunches, decide whether a possibly live run has stopped, and adopt rules | |

Declare the check that counts for a repository in `~/.config/agent-postmortem/config.toml`:

```toml
[[repos]]
path = "~/code/my-project"
checks = ["uv run pytest -q"]
```

`rescue status` counts a check only when the session ran exactly that command from the repository root and its result came back; a compound command, another directory, or a backgrounded call is listed as "not counted" with the reason. The output says `EXIT_0_OBSERVED`, never "verified": a passing check is evidence, not a decision.

To notice a killed or failed agent in seconds instead of hours, arm `agent-postmortem watch <run-id>` as a Claude Code Monitor; the plugin hands the session that exact command the moment a Workflow launches.

## Inside Claude Code

Install the plugin with two commands:

```
/plugin marketplace add zeyusuntt/agent-postmortem
/plugin install agent-postmortem@agent-postmortem
```

Its hooks shell out to the same CLI, never touch a run directory, and never fail a turn. On `SessionStart` they summarize any run that finished since you last looked. On `Stop` and `StopFailure` they check every subagent and workflow unit spawned so far and print a finding the moment it is detected, not only at session end. On `SessionEnd` they run the read-only tooling checks over the main thread itself. On a `Workflow` launch they register the run immediately, so it is tracked even before its own directory exists, and hand the session the `watch` command to arm as a Monitor. Two skills come with it: `/postmortem` for the verdict and `/rescue` for the bounded rescue protocol.

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
| `codex_write_denied` | critical | The codex-companion's output, or the result of a `codex` command, carries a sandbox denial. Prose that merely quotes the marker never counts. |
| `attempt_failed` | major or minor | The journal closed an attempt with a `failed` event; the cause is read from the last transcript line only: session or weekly usage limit (with the reset time), the machine went to sleep, a server error, an interrupt, or not recorded. Minor when a later attempt completed. |
| `claim_contradiction` | major | An attempt completed but its own returned result says `tests_failed > 0`, `ok: false`, `ready: false`, has blockers, or names a report file that does not exist (checked only for runs that ended within 72 hours). |
| `classifier_unavailable` | minor | A tool result carries both classifier-outage fragments. |
| `git_cwd_mismatch` | minor | A Bash result carries the not-a-git-repository marker. |
| `missing_artifact` | critical or info | The first user message names an absolute `.md`/`.json`/`.txt`/`.jsonl` path it must write, and the path does not exist at scan time; critical inside the home or temp roots, info outside them. |

The honesty rule: `mislabeled_kill` blames the runtime, major severity, only when the same key was retried afterward -- the restart itself is the evidence, never a guess. Long silence before a kill with nothing else to explain it is still major severity, but reported as "cause unattributed, consistent with a runtime kill," never asserted as the runtime's fault outright. It reports info when a parent session stopped the attempt on purpose, a human interrupt happened alongside it, or the silence was short.

### Quieting a detector

A detector you have already acted on, or one that does not apply to how you work, can be quieted without losing its data. List its id in `~/.config/agent-postmortem/config.toml` (or `$XDG_CONFIG_HOME/agent-postmortem/config.toml`):

```toml
[detectors]
off = ["bash_backgrounded"]
```

A quieted detector still runs. Its findings still show up in `--json`, marked `"silenced": true`, and `trend` still counts them: nothing is lost, and turning a detector back on picks up right where the data already is. `run`, `scan`, and `demo` just stop showing that detector's findings in text: not in a story, not in `--verbose`, not in the verdict. Both `run` and `scan` print a last line naming what was quieted, for example `1 detector silenced by config: bash_backgrounded`.

A critical detector (`codex_write_denied`, `missing_artifact`) can never be quieted this way: a silenced critical would change the exit code, which defeats the point of a critical finding. Naming one in `off` makes `run`/`scan`/`demo` exit 1 with a message on stderr rather than quietly ignore it; the plugin's hooks just skip that one id and keep going, since a hook must never fail a turn. An id that is not a real detector only prints a warning.

## Privacy

`redact()` replaces your home directory with `~`, a bare account name with `user`, an email with `email:` plus a 6-character hash, a session id with `uuid:` plus its last 4 hex digits, and a project slug with `project:` plus a 6-character hash, on every string any command prints. A pseudonym is different: a stable per-render label like `agent-1` or `phase-2` standing in for a name you or a teammate chose, so numbering restarts every time a report is rendered and two reports can never be linked by a repeated label. The only transcript text that ever leaves the tool is an evidence excerpt, a short quoted line pointing at exactly where a finding fires; read those lines yourself before pasting a report anywhere.

State (which findings a session already reported, and the rules the 0.1 `apply` command wrote, now read-only history) lives in `$XDG_STATE_HOME/agent-postmortem/state.json`, or `~/.local/state/agent-postmortem/state.json` if that variable is unset; nothing in it is ever printed. The plugin's one network action happens on `SessionStart` only: when no protocol-compatible CLI resolves from the plugin's own data directory or `PATH`, it installs the version pinned in the plugin manifest, retrying at most once every 24 hours on failure; opt out by setting `AGENT_POSTMORTEM_NO_AUTO_INSTALL=1`, or by installing the CLI yourself first.

Pass `--no-redact` to any command to see raw paths and session ids instead.

## How it was built

It was built across two Claude Code Workflow runs, and those same runs are among the ones agent-postmortem itself audits as an acceptance check: the tool that finds silent retries had real ones to find in the runs that built it. Version 0.2 was designed first (8 code-level studies of how aider, OpenHands, Cline, SWE-agent, Codex, goose, LangGraph, and others bound their own repair loops), reviewed twice by Codex before a line was written, and the review removed every write surface the first draft had: the tool observes and reports, and a human decides. Every milestone was test-first: a failing fixture and test before the code that made it pass, its own touched tests run while iterating, and the full suite run clean before its commit. Every milestone's diff then got a cross-model review from the main session -- Codex, then Claude's own code-reviewer -- before the next milestone started.

## Limits

The formats this tool parses are internal to Claude Code, not a published API, and were observed on versions 2.1.251 to 2.1.280; a future version can change them without notice. A session's retries are not observable the way a workflow's are: a session has no cache key to compare attempts against, so `silent_retry` never fires on one. Whether a main-session interrupt was a human pressing Escape is not knowable from the transcript, so it is never attributed, only recorded as a limit. Tested on macOS and Linux only.

## Contributing

See `CONTRIBUTING.md` for dev setup, the test-first workflow, and the privacy check every push must pass.

## License

MIT, see `LICENSE`.
