---
name: rescue
description: Guides a bounded, read-only-on-the-tool's-side rescue of work a killed or failed agent left behind. Use when the user types "/rescue", asks to rescue, recover, or salvage work from a killed or failed run, or after `/postmortem` shows WORK AT RISK is non-empty and the user wants to act on it.
---

# rescue

`agent-postmortem` reads transcripts, observes repo state with read-only git
commands, and reports facts. It never edits a file, never runs a check
itself, never launches or resumes an agent, and never reverts anything. A
passing check the main session runs is evidence that the work is probably
fine, not a decision that it is: the human still decides what to keep, what
to fix, and when to commit or relaunch.

Everything below is the bounded protocol this skill follows. Do not skip a
step or improvise past it.

## The protocol

**0. If the run might still be going.** Before anything else, if you are not
sure the run has actually stopped: for a workflow run (a `wf_...` id), arm
`agent-postmortem watch <run-id>` in a Monitor -- it notices a kill or a
failure within seconds and tells you when the run is really done, instead of
you polling `rescue plan` by hand. `watch` follows workflow runs only; for a
session, ask the human to confirm the session has stopped (unless the packet
says `YOUR CURRENT SESSION`, below).

**1. Get the packet.** Run:
```
agent-postmortem rescue plan <run-id> --local --evidence
```
This puts real names, repo-relative paths, and each unit's task prompt into
this session's transcript on purpose: carrying out the rescue needs them to
act on, and the default output would give you `path-1` placeholders and no
prompt. Keep that output inside this session; its first line,
`disclosure: local (real names and paths; do not paste)`, says the same.
Do not switch to the raw level instead: `--local` never prints an absolute
path it recognizes, and the raw level is not needed for anything this
protocol does.

The first line after the disclosure line says who may still be writing, and
decides what you do next:

- If it starts with `POSSIBLY LIVE`, stop right there. Tell the human the run
  may still be running and another agent may be writing the files the packet
  lists, and do not touch any of them until they confirm the run has actually
  stopped.
- If it starts with `YOUR CURRENT SESSION`, the run is the session you are
  working in, so do not stop. Check the files the packet lists yourself
  (steps 2 and 3, read-only) before relaunching the killed work, and ask the
  human before every commit and relaunch as step 6 says.
- If it starts with `NOTHING TO RESCUE YET: the run may still be running`,
  nothing has been killed or failed so far. Say so, follow step 0 (`watch`
  for a workflow run, the human's confirmation for a session), and stop.

**1b. If there is no unit.** A packet with no `UNIT` block says one of three
things. `NOTHING TO RESCUE: every attempt completed.` means the run finished,
every attempt completed, and the tool could establish what was at risk; tell
the human so and stop. `NOTHING TO RESCUE YET` means the run has not finished
(or an attempt has no terminal outcome yet), so nothing can be judged; say so
and stop. `NO KILLED OR FAILED ATTEMPT: ...
may still hold unchecked work` (or `the tool could not establish which
attempts were at risk`) means no attempt was recorded as killed, failed, or
superseded; this does not make the workflow verdict CLEAN (the run itself can
still have failed). The tool also cannot rule out changes it did not see; it
exits with exit code 4. Do not call that a clean run: show the human the `WORK
AT RISK` block from `agent-postmortem run <run-id>` and let them decide.

**2. Read the diffs.** For every dirty path the packet lists, read its diff
(`git diff -- <path>`) yourself and report to the human what is actually in
it, in plain terms: what changed, whether it looks like the described task,
anything that looks unfinished or wrong.

**3. Run the declared checks.** If the packet names declared checks, run
each one, from that repository's root (a packet can name more than one
repository, each with its own checks), exactly as declared, with your own
Bash tool.
If none are declared, say so plainly and ask the human how they want this
verified; do not invent a check yourself and do not guess one from the
repository's own files.

**4. Repair if needed, within budget.** If a check fails, fix the salvaged
work and run the checks again, from that repository's root, as the exact
declared command. After 2 cycles of repair-then-check with no exit 0, stop. Report
what you tried and what still fails, and change nothing more until the
human tells you what to do next.

**5. Never undo.** Never run `git checkout`, `git restore`, `git reset`,
`git stash`, or `git clean` on any of these paths, for any reason, at any
point in this protocol. Before an edit that could lose something, copy the
file aside first with `cp` to a new name outside the repository, and tell
the human exactly where you put it.

**6. Ask before every commit and every relaunch.** Never commit and never
relaunch on your own judgment. Before either one, run:
```
agent-postmortem rescue status <run-id> --local
```
and show the human its output alongside your specific request ("commit
these N files with this message" or "relaunch agent X"). Wait for them to
say yes.

**7. Never touch an instruction file.** Never edit CLAUDE.md, or any other
instruction file, as part of a rescue. That is a change for the human to
make deliberately, later, never something this protocol does for them.

## How to read the packet

- `UNKNOWN` means genuinely unknown: the tool could not establish that fact,
  not that the answer is bad news.
- `DIRTY_NOW` means the path's content differs from HEAD right now; it says
  nothing about whose edit is in it or whether that edit is good. A path
  written by more than one attempt can be `DIRTY_NOW` from either one.
- `rescue status` never says a check was "verified": it reports what a
  transcript shows was run and how it ended, and says plainly when it could
  not observe something (a write made outside this session, for instance).
  Treat a passing check as one more piece of evidence, never as proof.
