# agent-postmortem (Claude Code plugin)

Catches what a Claude Code Workflow run left silently broken: a subagent
killed and retried from scratch, a kill logged as a user interrupt, a shell
command that outlived its timeout, structured output that arrived
corrupted, a review finding that names a file that never appeared.

## Install

```
/plugin marketplace add zeyusuntt/agent-postmortem
/plugin install agent-postmortem@agent-postmortem
```

Uninstall with `/plugin uninstall agent-postmortem@agent-postmortem`, or
`claude plugin uninstall agent-postmortem@agent-postmortem` outside a
session.

## What the hooks do

Every hook shells out to `agent-postmortem`, never touches the run
directories it reads, and never fails a turn: if the CLI is missing or
raises, the hook prints nothing and exits 0.

- **SessionStart** performs the one-time install (see below), then
  summarizes any of this project's runs that finished since you last looked.
- **Stop** / **StopFailure** check every subagent and workflow unit spawned
  so far and surface a new finding the moment it is detected, not only at
  session end.
- **SessionEnd** runs the read-only tooling checks over the main thread
  itself.
- **PostToolUse** (on `Workflow`) registers a workflow run the moment it is
  launched, so it is tracked even if its own directory does not exist yet,
  and hands the session the exact `agent-postmortem watch <run id>` command
  to arm a Monitor with -- the hook arms nothing itself. Opt out of the hint
  with `AGENT_POSTMORTEM_WATCH_HINT=0`; registration still happens either way.

## The one network action

On `SessionStart`, if the plugin has not installed a CLI into its own data
directory and no `agent-postmortem` on your `PATH` passes its protocol check
(`agent-postmortem hook capabilities` answers protocol 1), and
[`uv`](https://docs.astral.sh/uv/) is on your `PATH`, the plugin installs
the version pinned in the plugin manifest, retrying at most once every 24
hours on failure, and writes what happened to a log file instead of your
terminal. This is the plugin's only network access.

Opt out by setting `AGENT_POSTMORTEM_NO_AUTO_INSTALL=1` (in your shell
profile, or wherever you set environment variables for Claude Code), or by
installing the CLI yourself ahead of time with
`uv tool install agent-postmortem`.

## The `/postmortem` skill

`/postmortem`, `/postmortem latest`, or `/postmortem <run-id>` diagnoses a
run and pastes back its verdict. See `skills/postmortem/SKILL.md`.
