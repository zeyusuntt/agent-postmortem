---
name: postmortem
description: Diagnoses a Claude Code Workflow run with the agent-postmortem CLI, proving whether a subagent was silently killed and retried, a kill got logged as a user interrupt, a shell command ran past its timeout, structured output arrived corrupted, or a review finding names a file that never appeared. Use when the user types "/postmortem", asks what went wrong in a run or in the current session, asks about wasted time or a stuck/killed subagent or workflow, or wants a post-mortem verdict before trusting a run's result.
---

# postmortem

This skill is both user-invocable (`/postmortem [latest | <run-id>]`) and
model-invocable: reach for it whenever a run's outcome is in doubt.

The CLI is `agent-postmortem`. It is read-only: it never writes inside a run
directory and it redacts paths and session ids by default. Run it with the
Bash tool.

## Which command to run

- **No argument.** Sweep the last week for anything worth a look:
  ```
  agent-postmortem scan --since <today minus 7 days, as YYYY-MM-DD>
  ```
  Compute that date first (for example `date -u -v-7d +%F` on macOS, or
  `date -u -d '7 days ago' +%F` on Linux); do not guess it.

- **`latest`.** The user means the most recently finished run when one
  exists. Find it yourself rather than guessing an id:
  1. Run `agent-postmortem scan --since <today, as YYYY-MM-DD>`.
  2. Take the newest row with no trailing `unfinished` marker (that marker
     means the run's journal has a started key with no result yet; `run`
     will still report a verdict for it, just tagged unfinished). Scan
     output is already sorted oldest first, so this is the last such row,
     not the last row overall.
  3. If every row is marked `unfinished`, fall back to the newest one, but
     only when it is at least 10 minutes old (its row's date, or the run
     directory's own mtime if you need finer precision) -- a workflow
     younger than that may simply still be running. Say plainly that the
     run is unfinished when you use this fallback.
  4. Run `agent-postmortem run <that run's id>` on it.
  If the scan is empty, or every row is unfinished and younger than 10
  minutes, say so and stop; do not fall back to a run that is probably
  still in progress just to have something to show.

- **A run id.** Run `agent-postmortem run <run-id>` (a prefix is fine; the
  CLI reports ambiguity itself if more than one run matches).

## After running it

1. Paste the verdict block back into the chat: the `RUN ...` header line
   and the summary line right after it. That is the one number and the one
   word (CLEAN / INCOMPLETE / a severity) the user needs first; do not bury
   it under the full findings list unless they ask for more detail. A
   header ending in `(unfinished)` means the workflow had not finished when
   you ran this -- say so, since every finding below it is provisional.
2. If the verdict is not CLEAN, the report's own `NEXT ACTION` line already
   names the top rule and the exact `apply` command for it, for example
   `agent-postmortem apply R-RETRY-SAFE --target ~/.claude/CLAUDE.md`. Show
   that line to the user.
3. **Never run that command with `--write` yourself.** Without `--write`,
   `apply` only prints a diff and changes nothing, which is safe to run to
   show what would happen. Ask the user explicitly before re-running it
   with `--write`, since that edits a file they maintain.

## Notes

- `agent-postmortem` prints nothing beyond what `redact()` allows; pass
  `--no-redact` only if the user asks to see raw paths or session ids, and
  say so when you do.
- If the CLI is missing entirely, tell the user to run
  `uv tool install agent-postmortem` and stop; do not try to install it
  yourself.
