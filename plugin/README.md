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

- **SessionStart** installs the CLI when needed (see below), then
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
- **PostToolUse** (every tool) and **UserPromptSubmit** check the session's
  own subagents: one interrupted with nothing in the session to explain it,
  one that failed after changing files, or one whose return its own
  transcript contradicts is reported at the next tool call, or at the next
  prompt if the session sat idle. Each ending is told once, and a call with
  nothing new prints nothing.
- **PostToolUse** (on `Agent` and `Task`) reports a foreground subagent that
  returned on an error or without the file its prompt asked for, in the
  same hook. The first background launch of a session also says that a
  Monitor running `agent-postmortem watch --session` is an option for a long
  run. The same opt-out applies.
- On **SessionStart** after a resume, the session hears which of its own
  subagents never returned.

On SessionStart the plugin also keeps its bundled CLI at the plugin's
version: after a plugin upgrade it reinstalls the matching CLI in the
background, at most once a day per version.

## The one network action

On `SessionStart`, when [`uv`](https://docs.astral.sh/uv/) is on your
`PATH`, the plugin installs the version pinned in the plugin manifest into
its own data directory, from PyPI, or from the matching GitHub tag if that
install fails (that fallback needs `git`). It does so in two cases:

- it has not installed a CLI there and no `agent-postmortem` on your
  `PATH` passes its protocol check (`agent-postmortem hook capabilities`
  answers protocol 1); a failed attempt is retried at most once every 24
  hours;
- the CLI it installed there reports another version than the plugin's,
  for example after a plugin upgrade; this runs in the background after the
  hook's own output, at most once a day per version, and the old CLI keeps
  serving the hooks until the new one is in place. A CLI on your `PATH`, or
  a bundled CLI that is a link to one outside the data directory, is never
  replaced.

Both write what happened to a log file instead of your terminal. This is the
plugin's only network access.

Opt out of both by setting `AGENT_POSTMORTEM_NO_AUTO_INSTALL=1` (in your shell
profile, or wherever you set environment variables for Claude Code), or by
installing the CLI yourself ahead of time with
`uv tool install agent-postmortem`.

## The `/postmortem` skill

`/postmortem`, `/postmortem latest`, or `/postmortem <run-id>` diagnoses a
run and pastes back its verdict. See `skills/postmortem/SKILL.md`.
