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
  1. Run `agent-postmortem scan --all` (`--all` here: the default `scan`
     output is a NEEDS ATTENTION triage list, not the one-row-per-run list
     this recipe parses; no `--since`, because the latest finished run may
     be from an earlier day).
  2. Take the last row whose verdict carries neither `unfinished` nor
     `possibly running`. `unfinished` means a workflow run has no lifecycle
     terminal yet: neither the session's own record nor the journal shows it
     ended (`run` still reports a verdict for it, just tagged unfinished).
     A session row never carries the `unfinished` marker, because a session
     has no lifecycle (its `run` header says `last activity`, not `ended`);
     `possibly running` on a session row means recent activity, so it may
     still be going. Also skip a row whose date is `unknown`: nothing says
     when it ran. Rows are sorted by the time each run started, oldest
     first (undated rows first), so this is the last such row, not the last
     row overall.
  3. If every dated row is marked `unfinished` or `possibly running`, fall
     back to the newest one, but
     only when it is at least 10 minutes old (its row's date, or the run
     directory's own mtime if you need finer precision) -- a workflow
     younger than that may simply still be running. Say plainly that the
     run is unfinished when you use this fallback.
  4. Run `agent-postmortem run <that run's id>` on it.
  If the scan is empty, every row is undated, or every dated row is
  unfinished or possibly running and younger than 10 minutes, say so and
  stop; do not fall back to a run that is probably still in progress, or
  one whose time is unknown, just to have something to show.

- **A run id.** Run `agent-postmortem run <run-id>` (a prefix is fine; the
  CLI reports ambiguity itself if more than one run matches).

## After running it

1. Paste the verdict block back into the chat: the `RUN ...` (or
   `SESSION ...`) header line, the account sentences right after it, and the
   whole `WORK AT RISK` block: its first line and every indented line under
   it (the per-attempt lines and any `run yourself` command). That is the one
   number and the one verdict word (CLEAN, DEGRADED, or FAILED, with
   `(INCOMPLETE)` added when the tool could not read everything) the user
   needs first, plus whether any killed or failed attempt left work that could still
   be lost; do not bury it under the full findings list unless they ask for
   more detail. A header carrying `(unfinished)`, or one whose time clause
   reads `last activity` and ends `possibly running`, means nothing shows
   the run ended when you ran this -- say so, since every finding below it
   is provisional. When the `WORK AT RISK` block names any paths or commits (its
   first line is not the `none:` or `unknown:` form), tell the user the
   `/rescue` skill can walk through recovering it. An `unknown:` first line,
   or a count line ending `the tool cannot see all of what was written`,
   means the tool could not establish everything an attempt wrote. Partial
   can come from an attempt whose transcript is missing, an unrecognized tool,
   a delegated call, a subagent that never returned, a shell command that
   cannot be proven read-only, or an attempt population that may be
   incomplete. So repeat each attempt line's own stated reason, and mention a
   shell command only when an attempt line says one ran. Say that, never say
   nothing was lost, and offer `/rescue` anyway.
2. Show the `WHAT TO DO` list too: at most 3 items, each tagged `[look]`
   (read-only, safe to run yourself) or `[decide]` (needs the user's
   judgment). If it names `agent-postmortem rules propose`, that is how to
   see which rules across the whole fleet would have prevented failures
   like this one -- run it and show the result; it prints each proposal's
   backing counts, the rule text, and the `apply` command for it, and never
   writes anything.
3. `STORIES` gives one short paragraph per agent that had a finding. Paste
   those too when the user wants to know what happened to a specific agent.
   Info-severity findings and evidence excerpts are hidden by default. Add
   `--verbose` for the full finding list. For the transcript lines behind a
   finding, add `--local --evidence` (excerpts with recognized paths
   replaced; at the
   default level `--evidence` prints pointers only). Use `--raw --evidence`
   only when the user explicitly asks for raw lines and accepts that they
   show real paths and names. Say when you add any of these.
4. If a run's header carries `(unfinished)`, or the session just launched a
   workflow and got the "arm a Monitor" hint, offer `agent-postmortem watch
   <run-id>` instead of re-running `scan`/`run` on a loop -- it notices a
   killed or failed agent within seconds instead of hours.
5. If the user wants to adopt a rule `rules propose` showed, run
   `agent-postmortem apply <rule-id> --target <file>` to print the rule
   text for that file (add `--local` for a readable diff; only `--raw`
   prints a diff that `git apply` accepts, and it shows real paths); it
   only ever prints, it never writes the file for you. Ask the user before
   you paste the block into their file or run `git apply` on their behalf, since editing a prompt
   or the instructions they maintain is their call, not yours.

## Notes

- `agent-postmortem` prints nothing beyond what `redact()` allows; pass
  `--no-redact` only if the user asks to see raw paths or session ids, and
  say so when you do.
- If the CLI is missing entirely, tell the user to run
  `uv tool install git+https://github.com/zeyusuntt/agent-postmortem@v0.3.3` and stop; do not try to install it
  yourself.
