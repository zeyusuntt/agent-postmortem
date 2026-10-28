# agent-postmortem

Which "done" in your last Claude Code run can't you trust?

agent-postmortem reads the transcripts Claude Code already wrote under `~/.claude/projects` and, for every Workflow run and every session with subagents, says which agents were killed (in a Workflow, often quietly started over), which never returned, which said "done" while their own result said otherwise, and which files and commits that left at risk. Then it lists at most 3 next steps. No setup, no proxy, no LLM, no network access, and it never changes your files.

On the maintainer's machine (scan 2026-10-27, 0.3.2), 13 of 166 workflow runs launched at least one agent's task a second time, and 10.2 hours went to retries and to attempts that ended without a result.

```
uvx agent-postmortem scan
```

From GitHub instead of PyPI: `uvx --from git+https://github.com/zeyusuntt/agent-postmortem agent-postmortem scan`.

The command-line tool is local and read-only: Python standard library only, no LLM, no network access, and it never changes your files, transcripts, commits, or branches. (The Claude Code plugin's one network action, installing that tool, is described under Privacy; Limits notes two rare git index modes in which git's own `status` still updates its bookkeeping.) What it covers, below, lists the run shapes it handles and the ones it does not yet. Status: alpha, used so far on the maintainer's own machines. Claude Code only.

[![CI](https://github.com/zeyusuntt/agent-postmortem/actions/workflows/ci.yml/badge.svg)](https://github.com/zeyusuntt/agent-postmortem/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

```
RUN wf_00000000-d30  DEGRADED  ended 2026-01-06 19:55Z
2 agents took 4 attempts.
2 attempts were killed by the runtime and restarted.
20m was spent on retries and attempts that ended without a result.

WORK AT RISK  2 attempts at risk: 0 paths named, 0 commits reported; the tool cannot see all of what was written.
  a:76c7463d911b (agent-1)  ran 1 shell command that may have written more
  a:46ef967114a1 (agent-2)  ran 1 shell command that may have written more
  run yourself: git status --short in path-1; --raw prints the exact command
  run yourself: git status --short in the repository the agent worked in

WHAT TO DO
  1. [decide] rules that would have prevented this: agent-postmortem rules propose

AGENT     PHASE      ATTEMPTS  END                KILLED  PATHS  COMMITS  WASTED
agent-1   phase-1    2         completed          1       0+     0        10m
agent-2   phase-1    2         completed          1       0+     0        10m
+ is a lower bound: incomplete evidence may hide more paths or commits.

STORIES (2)   details: --verbose   evidence: --evidence
  agent-1  Killed 1 time by the runtime (416s of silence).
  agent-2  Killed 1 time by the runtime (416s of silence).

1 info finding not shown (--verbose)
```

That is `agent-postmortem demo`, run against a packaged synthetic run: no setup, no real data, no network access. How to read it:

- The header leads with the verdict, and the sentences under it say what happened.
- WORK AT RISK says what a killed or failed attempt left that could still be lost, and what the tool cannot see. A shell command may have written anything, so an attempt that ran one is never reported as having written nothing.
- WHAT TO DO is at most 3 next steps, each tagged `[look]` (read-only) or `[decide]` (needs you), and is left out when there are none.
- In the AGENT table, PATHS counts every path an attempt's write ledger names. A `+` (`0+` here) means the count is a lower bound because a shell command ran that the tool cannot see into; a legend line under the table says so whenever a `+` is shown.
- STORIES tells one short story per agent that has a finding, worst first.

Info-severity findings (`wasted_time` here) and evidence are hidden by default: `--verbose` shows every finding with its evidence pointers, `--evidence` adds up to 2 pointers per story, and the transcript lines themselves need `--local --evidence` (recognized paths replaced) or `--raw --evidence`.

## What it covers

Coverage today, checked against the failure reports in Claude Code's issue tracker on 2026-10-28:

| Run shape or failure | Coverage | What you get |
|---|---|---|
| Workflow runs: runtime kills and retries | covered | kills logged as `[Request interrupted by user]` with no one at the keyboard, cold same-key retries and the time they cost, failed attempts with their cause (usage limit and reset time, machine sleep, server error), and a workflow the harness reported failed |
| A "done" you can't trust | covered | an agent that completed while its own result reports failed tests, `ok: false` or blockers (`claim_contradiction`), or while its Codex companion call never started (`codex_unavailable`) |
| Subagents that never returned | covered | WORK AT RISK says `unknown` instead of `none`, and names them |
| `claude -p` (headless) | partial | killed and unreturned subagents show; the process's own exit status (for example exit 0 with `result: success` after a kill) is not in the transcript, so the tool cannot see it |
| Background subagents (`run_in_background`) | partial | killed and unreturned ones show; a "completed" that arrived while the agent's own background work was still running, or with a tool call that never got a result, is not flagged yet |
| `claude --bg` sessions | not yet | each job's session is read like any session, but the job's own state (its "done" line, work still in flight) is not |
| Agent teams | not yet | teammates and their idle or blocked states are not modeled |
| Cross-session messages (`SendMessage`) | not yet | whether a message was delivered is not checked |

If you run one of the partial or not-yet shapes, please open a ["my run shape isn't covered"](https://github.com/zeyusuntt/agent-postmortem/issues/new?template=run_shape_not_covered.yml) issue with the output of `agent-postmortem run <run-id> --bug-report`. Logs from those runs are what would make support possible, and that output is redacted by default (never paste a raw transcript).

## Quick start

Install the command-line tool with [uv](https://docs.astral.sh/uv/) (it fetches Python 3.11 or later if you don't have it), then try it:

```
uv tool install agent-postmortem
agent-postmortem demo
agent-postmortem scan
agent-postmortem run <run-id>
```

To install from GitHub instead of PyPI: `uv tool install git+https://github.com/zeyusuntt/agent-postmortem`.

`scan` looks under `~/.claude/projects` by default and prints the runs that need attention: ranked, with a one-line reason and a next command for each, plus 7-day and all-time totals. Add `--all` for the full one-line-per-run list instead. `run` takes a run id (or a unique prefix of one, a session UUID, an agent transcript stem such as `agent-<id>`, an agent alias such as `a:<alias>` from a share report, or a path) and prints the verdict for that one run, what work is at risk, and at most 3 next steps; an id that matches more than one run is an error listing the candidates. Add `--evidence` for excerpt pointers or `--verbose` for the full finding list.

## What changed in 0.3

Verdict: 0.3 stops guessing. Severity, the WORK AT RISK block, and the run's end all come from recorded evidence, and a fact the tool cannot establish prints as `unknown` instead of an optimistic answer.

- **JSON is schema 3 and follows the share rules.** Every versioned `--json` output carries `schema: 3` and a `disclosure` field. At the default level it applies the same transformations as the text, with the same recorded limits, and never carries a transcript excerpt.
- **Severity comes from recorded inputs** (the cause of an interruption, the silence before it, its position among the key's attempts, whether a later attempt completed), not from which detector fired. FAILED comes only from a failed lifecycle: the harness itself reported the workflow failed. A missing expected artifact is `info`, and WHAT TO DO adds one `[look]` item that says how many expected files are absent now and points to `run <id> --verbose`.
- **WORK AT RISK is a block.** It opens with a count line, then one line per attempt. In the demo above: `2 attempts at risk: 0 paths named`. A line says `none:` only when the tool can prove it: `none: no attempt was killed, failed, or left unfinished` when nothing was at risk, `none: the attempts at risk wrote no paths` when attempts were at risk and none of them wrote anything. It says `unknown:` when it cannot establish which attempts were at risk (the line says why), or when a session has an unreturned subagent. A count line ending `the tool cannot see all of what was written` means the ledger is partial: a shell command that cannot be proven read-only (only a short list of read-only forms is trusted) counts as "may have written more"; a missing transcript, an unrecognized tool, or a delegated call also makes the ledger partial. The AGENT table then shows `N+` (for example `0+`), and each attempt line gives its own reason. After the attempt lines, a known repository gets `repo state: run with --observe` until you run `--observe` (for a run that ended more than 72 hours ago it says `historical: repo state not checked` instead). A `run yourself` command appears only for a repository that was refused, unchecked, or unknown, and `the list of attempts at risk may be incomplete` says the population itself is partial.
- **Observation and rescue are per repository.** `run --observe` checks each repository the written paths belong to on its own and prints `REPO STATE` with `checked: yes` or `checked: no (<reason>)` for each; a repository the tool refused to check says its ledger is partial and prints the `git status --short` command to run yourself, and a run with no repository at risk prints `REPO STATE  (no repository holds a path at risk)`. `rescue status` counts a check per declared repository.
- **`run --here` diagnoses the session you are working in** (Claude Code sets `CLAUDE_CODE_SESSION_ID`), else the most recently active run that worked in this directory or a parent of it, and says on stderr when several runs matched. When that variable names a session that worked here, the workflow runs it started join the runs `--here` matches, whatever directory their agents worked in. `scan --here` filters the scan to the runs that worked here, and `scan` lists possibly live runs with nothing else wrong under `LIVE NOW` instead of NEEDS ATTENTION. `RUN_ID` also takes a session UUID, an `agent-<id>` transcript stem, or an `a:<alias>` from a share report.
- **`watch` prints START, OK, and DONE.** START comes first and once (`START <run id>: 2 agents started so far, 4 attempts.`). OK means an attempt returned a result, at most one per agent every 60 seconds. DONE fires at once when the session's own record says the run ended; the `--done-grace` wait applies only when nothing but the journal says every key is resolved.
- **A field round on real runs.** Five projects and the maintainer's own runs found what the synthetic tests missed: a workflow's first agent, which starts a fraction of a second before the launch result is written, could be reported as unplaced and at risk (90 such attempts before the fix and 0 after, on a 238-run recheck), `run --here` could pick another live session, a forked session repeated its parent's findings, and a path in free text could survive in spellings the old patterns missed. CHANGELOG lists each fix.
- **`rescue` says "nothing to rescue" only when it is proven.** With no killed or failed attempt, `rescue plan` and `rescue status` print `NOTHING TO RESCUE` only for a finished run whose risk population is empty and complete. Otherwise they print `NO KILLED OR FAILED ATTEMPT: <n> attempt(s) may still hold unchecked work; see WORK AT RISK`, and exit 4.

## Commands

| Command | What it does |
|---|---|
| `agent-postmortem scan` | Scan every run under a root directory and print the ones that need attention, ranked, plus 7-day and all-time totals. `--all` prints the full one-line-per-run list instead. |
| `agent-postmortem run <run-id>` | Diagnose one run: verdict, work at risk, and at most 3 next steps, one story per agent. |
| `agent-postmortem run --here` | Diagnose the session you are working in, else the most recently active run that worked in this directory or a parent of it, instead of naming one. `scan --here` filters the scan to those runs. |
| `agent-postmortem run <run-id> --bug-report` | Render the same diagnosis as a markdown bug report instead of the default text. |
| `agent-postmortem rules` | List suggested rules and how many findings back each one. |
| `agent-postmortem rules propose` | Print the rules backed by findings in at least 3 runs on 2 distinct days, and where each belongs. A rule you listed under `[[adopted]]` in the config is not proposed again; it shows when you adopted it and how many times it was still violated since. `--run <run-id>` counts one run only. |
| `agent-postmortem rules review` | Print each adopted rule's detector rate before and after adoption, and list rules to consider removing. |
| `agent-postmortem apply <rule-id> --target <file>` | Print the rule's managed block for the target. At the default level it prints the block and a note, never a byte of the target; `--local` adds a readable unified diff, and `--raw` prints the diff you can save and pass to `git apply`. The tool never writes the file for you. |
| `agent-postmortem trend` | Show weekly detector counts and before/after rates for rules you have adopted. |
| `agent-postmortem demo` | Render the packaged synthetic demo run. No root and no real data needed. |
| `agent-postmortem rescue plan <run-id>` | Print a read-only decision packet: what a killed or failed attempt wrote, its repo state, declared checks, and the steps to take next. Never edits anything or runs a check itself. |
| `agent-postmortem rescue status <run-id>` | Print what the main session's own transcript shows was run and observed after the run ended. Reports only what it saw, never a confirmation that a check passed. |
| `agent-postmortem watch <run-id>` | Follow one running workflow: print START, then KILL, FAILED, OK (a result came back), and QUIET as they happen, then DONE or LIMIT. DONE comes the moment the session's own record says the run ended, or after a grace period once every journal key is resolved. Read-only, designed to be a Claude Code Monitor command. |
| `agent-postmortem hook <event>` | Claude Code plugin hook entry point; reads hook JSON from stdin. |

`scan`, `run`, `rules`, `trend`, and `rescue` take `--json` and the level flags; `apply` takes the level flags only; `demo` and `watch` take `--json` only; `hook` takes neither. The level flags are `--local` and `--raw` (`--no-redact` is the same as `--raw`); see Privacy.

## Why

On the maintainer's machine, a scan with 0.3.2 on 2026-10-27 found 166 Claude Code Workflow runs, 13 of which launched at least one agent's task a second time. A retry is easy to miss. A kill can reach the transcript as `[Request interrupted by user]` although no one pressed a key, and a run with retries can still end as completed, so the logs are where it shows. The same failures are reported upstream (status as of 2026-10-28; of about 45 related issues read that day, one has a reply from Anthropic staff, and `run <run-id> --bug-report` renders a share-safe report to attach to one):

- [anthropics/claude-code#88993](https://github.com/anthropics/claude-code/issues/88993) (open): workflow subagents are interrupted around 180 seconds while a model request is still in flight.
- [anthropics/claude-code#85206](https://github.com/anthropics/claude-code/issues/85206) (open): the stall watchdog kills a subagent that is actively working every 8 to 9 minutes.
- [anthropics/claude-code#98170](https://github.com/anthropics/claude-code/issues/98170) (open): `claude -p` kills working background subagents, records it as "user rejected", and exits 0 with `result: success`.
- [anthropics/claude-code#104986](https://github.com/anthropics/claude-code/issues/104986) (open): a background subagent is terminated at its first Bash call and the result is reported as success.
- [anthropics/claude-code#84346](https://github.com/anthropics/claude-code/issues/84346) (closed, not planned): a subagent model-stall watchdog kills a subagent after about 600 seconds of silence, recorded as `[Request interrupted by user for tool use]`.
- [anthropics/claude-code#65500](https://github.com/anthropics/claude-code/issues/65500) (closed, not planned): a deep-research workflow aborts its entire run the moment any one schema-bound subagent fails to emit structured output.

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

`rescue status` counts a check, per repository you declared in the config, only when the session ran exactly that command from that repository's own directory and its result came back; a compound command, another directory, or a backgrounded call is listed as "not counted" with the reason. The output says `EXIT_0_OBSERVED`, never "verified": a passing check is evidence, not a decision.

To notice a killed or failed agent in seconds instead of hours, arm `agent-postmortem watch <run-id>` as a Claude Code Monitor; the plugin hands the session that exact command the moment a Workflow launches.

## Inside Claude Code

Install the command-line tool first (Quick start above), so the plugin finds it on your `PATH`. Then install the plugin with two commands:

```
/plugin marketplace add zeyusuntt/agent-postmortem
/plugin install agent-postmortem@agent-postmortem
```

Its hooks shell out to the same CLI, never touch a run directory, and never fail a turn. On `SessionStart` they summarize any run that finished since you last looked. On `Stop` and `StopFailure` they check every subagent and workflow unit spawned so far and print a finding the moment it is detected, not only at session end. On `SessionEnd` they run the read-only tooling checks over the main thread itself. On a `Workflow` launch they register the run immediately, so it is tracked even before its own directory exists, and hand the session the `watch` command to arm as a Monitor. Two skills come with it: `/postmortem` for the verdict and `/rescue` for the bounded rescue protocol.

## What it detects

| Detector | Severity | Fires when |
|---|---|---|
| `silent_retry` | major or minor | A key started more than one attempt. Minor only when a later attempt completed with a result and no attempt that could have lost work is known to have written anything; major in every other case, and whenever that cannot be established. |
| `mislabeled_kill` | info, minor, or major | An attempt was interrupted; see the honesty rule below. |
| `wasted_time` | info | Always, once per run: sums attempt durations from transcript timestamps and reports the time spent in every attempt before an agent's last one, whether or not it returned a result, plus the last one when it was killed, failed, or superseded (a last attempt that completed or may still be running does not count). |
| `workflow_failed` | major | The run's lifecycle terminal is `failed`: the parent session recorded an accepted notification that the workflow failed, or a synchronous launch result whose status is `failed`. Evidence is that parent transcript line. |
| `bash_backgrounded` | minor | A Bash result carries the backgrounded marker. |
| `bash_timeout` | minor | A Bash result shows exit 143 or the timeout marker. |
| `tool_json_error` | minor | A tool result carries the input-validation marker. |
| `structured_output_failure` | major or minor | A StructuredOutput result carries a mismatch marker. Minor when the attempt, or a later attempt of the same key, completed with a result; major otherwise. |
| `codex_write_denied` | minor | The codex-companion's output, or the result of a `codex` command, carries a sandbox denial. One finding per attempt. A denial alone does not show that a required write was blocked; when a required file is missing, `missing_artifact` or `claim_contradiction` says so. Prose that merely quotes the marker never counts. |
| `attempt_failed` | major or minor | The journal closed an attempt with a `failed` event; the cause is read from the last transcript entry, not counting trailing attachment entries, only: session or weekly usage limit (with the reset time), the machine went to sleep, a server error, an interrupt, or not recorded. Minor when a later attempt completed. |
| `claim_contradiction` | major or info | An attempt completed but its own returned result says `tests_failed > 0`, `suite_failed > 0`, `ok: false`, `ready: false`, has blockers, or returns a `<name>_failed` count above 0 or `blocked: true` (major, in a fixed sentence that does not name the key), or names a report file that does not exist when the tool looks (info; checked only for runs that ended within 72 hours or whose end time is unknown). |
| `codex_unavailable` | major or minor | A `codex-companion.mjs` or `codex` Bash call failed and a line of its result starts with "Codex CLI is not installed or is missing required runtime support". One finding per attempt. Major when the attempt still completed and no later companion call in it returned a result without that line; minor otherwise. Assistant text and the output of other commands never count. |
| `classifier_unavailable` | minor | A tool result carries both classifier-outage fragments. |
| `git_cwd_mismatch` | minor | A Bash result carries the not-a-git-repository marker. |
| `permission_denied` | minor | A tool result with `is_error` starts with one of the runtime's four denial sentences (auto mode classifier, the user declined, a permission rule, a built-in safety check). One finding per attempt, with the four counts and up to 5 `file:line` pointers; the denied command is never printed. |
| `missing_artifact` | info | The first user message names an absolute `.md`/`.json`/`.txt`/`.jsonl` path it must write, and the path is absent when the tool looks. The check happens later than the run, so it cannot say whether the file existed when the run ended. |

The honesty rule: `mislabeled_kill` blames the runtime only when the same key was retried afterward -- the restart itself is the evidence, never a guess -- and reports it as minor, because `silent_retry` carries the loss. Long silence (300 seconds or more) before a kill of a key's last attempt, with nothing else to explain it, is major severity, but reported as "cause unattributed, consistent with a runtime kill," never asserted as the runtime's fault outright. It reports info when a parent session stopped the attempt on purpose, a human interrupt happened alongside it, or the silence was short or unknown.

The verdict follows from those severities: `FAILED` when the harness itself reported the workflow failed (or a finding is critical, which no detector produces today), `DEGRADED` when any finding is major, `CLEAN` otherwise, with `(INCOMPLETE)` added when the tool could not read everything. `run`, `demo`, and `scan` exit 3 for `FAILED` and 4 for `INCOMPLETE` without `FAILED`; 3 wins when both apply.

### Quieting a detector

A detector you have already acted on, or one that does not apply to how you work, can be quieted without losing its data. List its id in `~/.config/agent-postmortem/config.toml` (or `$XDG_CONFIG_HOME/agent-postmortem/config.toml`):

```toml
[detectors]
off = ["bash_backgrounded"]
```

A quieted detector still runs. Its findings still show up in `--json`, marked `"silenced": true`, and `trend` still counts them: nothing is lost, and turning a detector back on picks up right where the data already is. `run`, `scan`, and `demo` just stop showing that detector's findings in text: not in a story, not in `--verbose`, not in the verdict. Both `run` and `scan` print a last line naming what was quieted, for example `1 detector silenced by config: bash_backgrounded`.

No detector is critical in this version, so any detector can be quieted. A `FAILED` verdict comes from the harness reporting the whole workflow failed, and no setting can quiet that. If a detector ever becomes critical it can never be quieted: naming it in `off` makes `run`/`scan`/`demo` exit 1 with a message on stderr rather than quietly ignore it, and the plugin's hooks just skip that one id and keep going, since a hook must never fail a turn. An id that is not a real detector only prints a warning.

## Privacy

The default output is meant for sharing: names become aliases, every path the tool recognizes is replaced or shortened, and no transcript text is printed. The path rule below has two recorded gaps, so read the output once before you post it in public. Every command prints at one of three disclosure levels, and only the default one is meant to leave your terminal:

| Level | Flag | Names | Paths | Transcript excerpts |
|---|---|---|---|---|
| share | none (the default) | stable aliases such as `a:3f2c9a1b7e04` for an agent, `s:` for a session, `project:` for a project; labels and phases as `agent-1` and `phase-2` | a recognized path is `path-N`, or shortened to `~/.../<name>` in your home directory and `/tmp/<dir>/.../<name>` under `/tmp`; a reported commit id is `commit-N` | never: evidence is a pointer such as `agent-3f2c9a1b7e04.jsonl:42 [kill_sentinel]` |
| local | `--local` | real labels, phase titles, agent ids, and project names | a recognized path is relative to the repository, `~/.../<name>` inside your home directory, otherwise `path-N`; never a recognized absolute path | only with `--evidence` |
| raw | `--raw` | real | absolute | only with `--evidence` |

At `local` and `raw` the first line of a command's output says so and says not to paste it (an error printed alone on stderr carries no such line; at the default and `local` levels an error never echoes a path or id, and at `raw` it prints as 0.2 did). Hooks and `watch` always print at the default level. An error message never echoes a path or id you typed back at the default level: it says `path-1` or `value-1` and names what it expected.

An alias is a prefix, 12 or more hex characters, of an HMAC of the real id under a secret created once on your machine. Once assigned, an alias never changes: the same agent has the same alias in `run`, `rescue`, `watch`, and hook messages, the same project in `scan --all` and `run`, and nobody without the secret can turn an alias back into the id or match it to a transcript file. On top of that, the default and `local` levels replace your home directory with `~`, a bare account name with `user`, an email with `email:` plus a 6-character hash, a session id inside text with `uuid:` plus its last 4 hex digits, and any other project slug with `project:` plus a 6-character hash.

Paths inside free text (finding titles, diagnostics, error messages, and at `--local` the excerpts) follow one rule: from the start of an absolute, home (`~/`, `$HOME/`), working-directory (`$PWD/`), Windows, or `file://` path, everything to the end of its quoted fragment, or of its line when it is not quoted, is hidden, and ` [text hidden]` says so when the hidden part held spaces; a word glued in front of a recognized path start is hidden with it. Two spellings are not read as a path start, by design: a path glued straight to a word of a language written with spaces (`read/srv/x`, the same shape as `and/or` and the relative name `src/a.py`), and, after a word of a language written without spaces (Chinese, Japanese, Korean, Thai, Lao, Tibetan, Myanmar, Khmer), a path with one segment, no file extension, and no slash after it (`读取/data`), so that `成功/失败` stays prose. Relative names such as `src/a.py` stay visible; they can only appear at `--local` and `--raw`, because the default level prints no transcript text. docs/DESIGN.md 5.1 lists every recorded limit of the path rule.

`--json` follows the same levels. At the default level the JSON carries the same aliases and `path-N` names as the text, never a transcript excerpt, and its top-level `disclosure` field says `share`, so it is as safe to paste as the text. `--local --json` and `--raw --json` work too: they carry the level in that `disclosure` field instead of a first line (a first line would break the JSON), and they are no more shareable than their text. `watch --json` and the hook messages are always at the default level.

State lives in `$XDG_STATE_HOME/agent-postmortem/`, or `~/.local/state/agent-postmortem/` if that variable is unset: `state.json` (which findings a session already reported, and the rules the 0.1 `apply` command wrote, now read-only history), plus `secret` and `aliases.json` for the aliases. All three are mode 0600. The secret and the registry are never printed; `trend` shows the applied-rule history from `state.json` at the active level. If a command prints `stable aliases unavailable` on stderr, that output named everything `agent-1`, `agent-2`, and so on instead of by alias; nothing is repaired automatically, because a new secret would change every alias. Remove both `secret` and `aliases.json` to start over with new aliases.

The plugin's one network action happens on `SessionStart` only: when it has not installed a CLI into its own data directory and no `agent-postmortem` on `PATH` passes its protocol check, it installs the version pinned in the plugin manifest into that data directory, retrying at most once every 24 hours on failure; opt out by setting `AGENT_POSTMORTEM_NO_AUTO_INSTALL=1`, or by installing the CLI yourself first.

## How it was built

It was built across two Claude Code Workflow runs, and those same runs are among the ones agent-postmortem itself audits as an acceptance check: the tool that finds silent retries had real ones to find in the runs that built it. Version 0.2 was designed first (8 code-level studies of how aider, OpenHands, Cline, SWE-agent, Codex, goose, LangGraph, and others bound their own repair loops), reviewed twice by Codex before a line was written, and the review removed every write surface the first draft had: the tool observes and reports, and a human decides. Every milestone was test-first: a failing fixture and test before the code that made it pass, its own touched tests run while iterating, and the full suite run clean before its commit. Every milestone's diff then got a cross-model review from the main session -- Codex, then Claude's own code-reviewer -- before the next milestone started.

## Limits

The formats this tool parses are internal to Claude Code, not a published API, and were observed on versions 2.1.251 to 2.1.312; a future version can change them without notice. A few transcript entry types first seen in 2.1.285 to 2.1.310 (`tools-used`, `relocated`, `agent-setting`, `history-suppression`, `continued-in`) are not interpreted yet, so an attempt whose transcript holds one reads as "the tool cannot see all of what was written". Claude Code deletes transcripts older than 30 days by default (the `cleanupPeriodDays` setting), and with them the history `scan`, `trend` and `rules propose` read; raise that setting to keep more. A session's retries are not observable the way a workflow's are: a session has no cache key to compare attempts against, so `silent_retry` never fires on one. Whether a main-session interrupt was a human pressing Escape is not knowable from the transcript, so it is never attributed, only recorded as a limit. Tested on macOS and Linux only. The repository check (`run --observe`, `rescue status`) uses `git status` to inspect paths, with every filter driver switched off and no network transport allowed; in a repository with a sparse index or a split index, git's own `status` can still store a cached tree object or refresh the shared index file's timestamps, without changing tracked content, commits, or branches.

## Contributing

See `CONTRIBUTING.md` for dev setup, the test-first workflow, and the privacy check every push must pass.

## License

MIT, see `LICENSE`.
