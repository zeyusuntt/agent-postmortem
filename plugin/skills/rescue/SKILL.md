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
sure the run has actually stopped, arm `agent-postmortem watch <run-id>` in a
Monitor -- it notices a kill or a failure within seconds and tells you when
the run is really done, instead of you polling `rescue plan` by hand.

**1. Get the packet.** Run:
```
agent-postmortem rescue plan <run-id> --no-redact
```
Use `--no-redact` here: the packet's output stays inside this local session
and you need the real repo-relative paths to run the commands it shows, not
`path-1` placeholders. If the first line reads `POSSIBLY LIVE`, stop right
there. Tell the human the run may still be running and do not touch any
file in that repo until they confirm it has actually stopped.

**2. Read the diffs.** For every dirty path the packet lists, read its diff
(`git diff -- <path>`) yourself and report to the human what is actually in
it, in plain terms: what changed, whether it looks like the described task,
anything that looks unfinished or wrong.

**3. Run the declared checks.** If the packet names declared checks, run
each one, from the repo root, exactly as declared, with your own Bash tool.
If none are declared, say so plainly and ask the human how they want this
verified; do not invent a check yourself and do not guess one from the
repository's own files.

**4. Repair if needed, within budget.** If a check fails, fix the salvaged
work and run the checks again, from the repo root, as the exact declared
command. After 2 cycles of repair-then-check with no exit 0, stop. Report
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
agent-postmortem rescue status <run-id>
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
