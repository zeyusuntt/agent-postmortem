# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1]

0.5.1 lets you check that alerts can reach you. The plugin's hooks print nothing when all is well
and print nothing when they are broken, so a missing plugin looks the same as a healthy one.
`agent-postmortem doctor` says which.

### Added

- `agent-postmortem doctor` says whether alerts can reach a session: the verdict first, then one
  row per check. It checks the CLI, the plugin (installed, turned on, its hooks bound, the idle
  watcher among them), Claude Code's version, readable transcripts and job files, writable state,
  this session's workers and the idle watcher. It also says when the plugin's hooks have no CLI to
  run. It exits 3 when a check fails, 2 for a named session that does not exist and 1 for
  `--test-alert` with no session. `--json` has the same rows. It prints no path below `--raw` and no
  worker's name or id below `--local`.
- `doctor --test-alert` sends one line through the per-call hook. Inside a session, your next tool
  call or prompt reads `ALERT TEST from agent-postmortem doctor: ...`, once. That tests the per-call
  channel end to end. An idle session is not woken for it, since a wake costs a model turn, so
  `doctor` reports the idle channel as eligible or not.
- The `native state` row compares the job files with `claude agents --json --all`, because every
  worker notice rests on job files and they are not a published format. `doctor` runs that command
  once, with stdin closed and a 10 s limit. No hook runs it, and it is the only time this tool
  starts Claude Code. A difference is a WARN, so a Claude Code update that changes the job files
  shows up.
- `scan` says when it read a run only in part. The default view adds `2 of 40 runs could only be
  read in part, so they may hide more: agent-postmortem scan --all`, and with nothing else to
  report it says `nothing needs attention in what could be read`. Until now only `--all` said so.

- `scripts/ci-like.sh` runs CI's steps before a push the way a runner sees them: a one-commit copy of
  the tree, an empty `HOME` and `CLAUDE_CONFIG_DIR`, no `claude` on `PATH`, and uv's standalone build
  of each Python in the matrix, with the version checked. It cannot show a failure that only happens
  on another operating system.

### Changed

- An agent's structured result now counts as reporting failure when its `status` is `failed`,
  `failure`, `error` or `errored` (the whole value, in any case), or when `tests` holds `failed` or
  `failures` above 0. Both are `claim_contradiction` findings, as `tests_failed` and `ok: false`
  already were. A status such as `done_with_concerns` is not a failure.
- A suggested rule is now one that `might have helped`, in place of `would have prevented this`:
  the tool cannot know that a rule would have stopped a failure.
- Wasted time reads `20m of agent time went to retries and attempts that ended without a result`,
  since parallel attempts each count, so the figure is not wall-clock time.

### Fixed

- A placeholder in a prompt's path, such as `reply-N.json`, is no longer reported as a missing file.
  This covers `N`, `X`, `K` or `ID` as a whole word of the file name. `README.md`, `Xray-N1.json` and
  `run-X7.log` are still names.
- `python3 -c 'import time; time.sleep(N)'` counts as a timer, both for the deadline request and for
  the silence rule. Agents write it when a foreground `sleep` is refused. Any other script does not
  count. A timer already running when you upgrade keeps the old reading until it ends, and may be
  told once as silent: changing that would make every session read its transcript again.
- A `run.sh` timing test that failed once on a slow CI runner (2.8 s against 2 s, with a correct
  build) now checks the number of probes.
- `doctor` and the job file reader count JSON nested past 256 levels as unreadable on every Python.
  The interpreter's own limit used to decide, and it runs from about 1,000 levels on 3.11 to past
  100,000 on 3.14.8.
- CI now runs the Python versions it names. `setup-uv@v3` has no `python-version` input, so the
  matrix only changed each job's name. All 18 CI runs up to now, 0.4.0 and 0.5.0 among them, ran
  3.12.3 on Ubuntu and Homebrew's newest on macOS (3.14.7, then 3.15.0 to 3.15.2), and none ran 3.11
  or 3.13. Each job now selects its version and fails on any other. The matrix and the classifiers
  both list 3.11 to 3.15.
- The test suite no longer depends on the machine it runs on. Each test has its own empty home and
  sees no settings from a session running the suite. Two privacy tests had passed only where the
  plugin was installed. The JSON rows of the privacy matrix also missed a value that was only a path.
  They catch it now, and no row had one.

## [0.5.0]

0.5 tells a session about the workers it launched, with nothing to arm. Claude Code records a
background session's state in its job file and tells nobody: a worker sat 8.5 hours on a
permission prompt while the session that launched it heard nothing. Now that session reads one
line at its next tool call or prompt, and an idle session is woken for it. This needs the plugin;
without it, `status` and `scan` print the same reasons when someone runs them.

### Added

- Worker notice. The plugin reads the `claude --bg` in the session's own Bash call and follows
  that worker from then on: `WORKER night-run (a1b2c3d4) blocked 15m: waiting for a permission
  approval (approve Entering worktree). A person must approve it: claude attach a1b2c3d4`. A
  permission prompt, a question, a first prompt never sent, an API error, a failure and a worker
  idle on a stuck command each have their own line, told once per change at the next tool call
  or prompt, ahead of anything else the hook has to say. A worker that delivered gets a one-line
  note with no problem word. The worker's own words are never printed: only the name its launch
  printed, the short id and a permission's tool. A worker that finishes on its own schedule is
  told finishing twice, then not until trouble. A first look after an upgrade tells trouble from
  the last 24 hours and deliveries from the last 10 minutes, and records the rest silently. A
  launch by a subagent, by another session or by a Bash call that ran in the background is not
  linked; `status` and `scan` still list those workers.
- Idle wake. Every Stop starts a watcher (`hook stop-watch`, an `asyncRewake` hook) when the
  session has a worker or a running command to follow, and it wakes an idle session with the
  same line. It checks every 20 s, wakes at most once per 5 minutes, leaves as soon as the session
  acts again, and gives a worker's own report 2 minutes to arrive before it wakes for a delivery.
  A session is never woken for a line it could not record as told, and any error exits without
  waking it. It needs Claude Code 2.1.314 or later, a reboot loses it until the next Stop, and
  `AGENT_POSTMORTEM_WAKE=0` turns it off. A running session starts it after `/reload-plugins` or
  a restart; the per-call line does not need either.
- Reason classes in `status` and `scan`. A BLOCKED row says what the session waits for
  (`waiting for a permission approval`, `waiting for an answer`, `stopped on an API error`, `the
  reason is not recorded`) and `dispatched by this session` when it did, those rows first. A
  worker that delivered and waits on a decision has its own DELIVERED row. `--local` adds the
  tool and the command that helps (`-> claude attach <id>`, `-> claude logs <id>`), `status
  --json` adds `class` and `mine`, and `--local` adds `label` and `short`. Another session's row
  older than 3 days folds into one count line.
- `scan` lists under NEEDS ATTENTION every background session waiting on a person or failed in
  the last day, read at scan time and whatever `--since` says, the caller's own workers first. One
  that delivered is not listed. `scan --json` gains `blocked` only when one is listed.
- Stall checks. A background command whose output has not changed for 30 minutes, or whose output
  ends in codex's "Reading additional input from stdin..." for 2 minutes, is told once through a
  COMMAND line at the next tool call or prompt. `status` prints a STALLED row and `scan` lists the
  session first under NEEDS ATTENTION when its process is alive, its turn ended 30 minutes ago and
  nothing is set to wake it. The BACKGROUND line counts such a session as stalled, not working. A
  command under a real `timeout`, a timer by any path to `sleep`, or one carrying the marker
  `# postmortem: silent-wait` is quiet by design, and only the stdin rule still speaks for it. So
  is `perl -e 'alarm N; exec @ARGV' ...` or `'alarm shift; exec @ARGV' N ...`, in that exact shape
  with only exact, literal steps around it. Any other wrapper is told once, in words that say it
  may be fine: a loose rule would silence the only alarm for a real hang.
- Codex stdin warning. `hook pre-bash` warns before a background `codex exec` with no stdin
  redirect runs: codex reads stdin to its end when it is not a terminal, and one review hung
  26 hours that way.
- A Stop with a background command still running asks the session to arm a deadline, once per
  command, wherever the idle watcher would not catch a hang: an interactive session, an older
  Claude Code or plugin, or a command whose output the watcher cannot read (`> "$LOG"`).
  A Monitor or a wake that can still fire counts as that deadline.
- A background session whose subagents and workflows have all ended, with no `status`, `scan`,
  `run` or `watch` of this tool since, is reminded once at a Stop with nothing else to say to run
  `status` before reporting done. An interactive session gets none, and
  `AGENT_POSTMORTEM_WATCH_HINT=0` turns it off.
- At a session start with nothing else to say, at most once a day, the plugin says so when the
  `agent-postmortem` on PATH is older than the plugin and its newer hooks stay silent. `hook
  capabilities` lists `hooks`, and an older CLI is never started for `stop-watch`.

### Changed

- A tool call with nothing new starts about a third faster: 26 ms down to 15 ms for the CLI on Linux,
  measured in a container. It no longer loads `dataclasses`, `tempfile`, `secrets` or `datetime`,
  nor OpenSSL where Python has its builtin sha256, and that call uses none of them. Every tool call
  of every session pays this.
- `watch` flushes every line. A Monitor reads it through a pipe, where block buffering delivered
  nothing until the run ended. It also stops quietly when its reader closes the pipe.
- `watch --session`, `status` and `scan` read a worker's `tempo` before its `state`, which is
  written only when a turn ends: a worker on a permission prompt reads `state: working, tempo:
  blocked`, and one given a new prompt reads `state: blocked, tempo: active`.
- The top-level help names `watch --session` and a background worker's blocked or needs-input
  state.
- In a session that is itself a background worker, `status` lists only the rows it launched and
  ends the count line with `; this session launched <n>, scan lists the rest`. `--json` keeps every
  row with its owner, and if it launched a worker whose id it never saw, all rows stay.

### Fixed

- `status` no longer keeps a finished subagent as RUNNING. A subagent resumed by a message ends
  with a notice naming only its task, no tool-use id, and the live read dropped it, so the first
  notice stood while the transcript grew past it. It now counts when the task is a subagent this
  session launched.
- A commit's id is read per segment of the shell command. When an assignment or expansion
  elsewhere in the command made the whole command unplaced, an agent's printed summary line was
  dropped and its COMMITS read `?`. Substitutions that spell a commit still read `?`.
- A Monitor no longer gets the deadline request (Claude Code lists it as a plain shell task, so a
  Monitor is now known from the session's own Monitor calls), and a deliberately silent wait no
  longer reads as hung and suggests stopping it.
- On CI the hook-speed test compares a nothing-new call with a bare start of the same interpreter
  in interleaved rounds. A shared runner read 152 ms against the 150 ms budget on a correct
  build; the 150 ms test still runs where CI is unset.

## [0.4.0]

0.4 adds live mode: the tool checks a subagent's "done" the moment it returns, and tells the
main agent at its next tool call, with nothing to arm. It speaks only for what Claude Code does
not already say: an interrupt nothing in the session explains, a failure that left files
changed, and a return its own transcript contradicts. A second `scan` is fast.

### Added

- Per-call live checks. The plugin runs `hook post-tool` after every tool call and
  `hook prompt-submit` on every prompt. When one of the session's own subagents was interrupted
  with nothing in the session to explain it, failed after changing files (with the count), or
  returned while its own last entry is an error or the file its prompt asked for is missing,
  the agent hears it at its next tool call, or at its next prompt if it sat idle. A call with
  nothing new prints nothing: 58 ms median through the plugin with its bundled CLI, about
  130 ms with a CLI on PATH, which the plugin checks at every call.
  Each ending is told once across every channel: these hooks, `watch --session`, Stop and the
  resume sentence.
- `agent-postmortem watch --session [SESSION_ID|PATH]` follows one session, for a Claude Code
  Monitor on a long background run. It prints a line only for the endings above, passes
  background-session state changes through, says ENDED when the session exits, and a re-armed
  watch never repeats a line.
- `agent-postmortem status [SESSION]` prints one snapshot of a session: its subagents running,
  returned, interrupted or failed, what an interrupted one changed, returns their own
  transcripts contradict, how each of its workflows ended, and the background sessions' states
  as Claude Code records them. With no argument it reads the calling session. A second call
  reads only what was appended (0.13 to 0.37 s warm on the maintainer's machine). A session
  that ran shell commands in the background (for example a `codex exec` worker) gets one
  COMMANDS line counting them by how Claude Code says they ended, with the oldest running
  one's age, and saying that status does not check their output.
- `agent-postmortem statusline subagents` is a `subagentStatusLine` command for Claude Code's
  subagent panel. It rewrites a row only when it has something to add: an interrupt nothing in
  the session explains, how many paths an interrupted or failed subagent changed, a return its
  own last entry contradicts, or quiet time over two minutes. `AGENT_POSTMORTEM_STATUSLINE=0`
  turns it off.
- `hook agent-launched` runs on every Agent and Task launch. A foreground subagent that returns
  on an error or without its prompted output file is reported in the same hook. The first
  background launch of a session says that its subagents are checked when they return and
  that a Monitor running `watch --session` is an option for a long run; a foreground launch
  gets no hint, and `AGENT_POSTMORTEM_WATCH_HINT=0` turns it off.
- `hook session-start` on a resumed session says how many of its own subagents never returned
  or were interrupted before it stopped, and how many paths they changed, unless a live
  channel already told it.
- `watch <RUN_ID>` checks each result the moment it lands. A workflow agent that returns
  `ok: false`, failed tests, failure counts or a report path that does not exist gets a CLAIM
  line in place of its OK line, so the main agent hears about it before using the result.
- `agent-postmortem --version` names the build when it was installed from a git checkout, as
  `agent-postmortem <version> (<branch> <commit>, <date>)`, with `+dirty` when tracked files
  were modified. A release prints the plain version, and `hook capabilities` always does.
- The plugin keeps its bundled CLI at the plugin's version. Before, it installed one only when
  none was found, so a plugin upgrade left the old CLI in place. Now SessionStart compares the
  bundled CLI's version with the plugin's and, when they differ, reinstalls the plugin's
  version in the background, at most once a day per version. A CLI you installed yourself on
  PATH is never touched, and neither is a bundled CLI that is a link to a CLI outside the
  plugin's data directory.

### Changed

- A second `scan` is fast. Each run's analysis is cached by the exact size, time and inode of
  every file it reads, and is read again the moment any of them changes, a path it checked
  appears or disappears, the config or version changes, or its 72-hour report window closes.
  A transcript that grew is parsed again on its own, so a live session with hundreds of
  subagents no longer makes every scan slow: on the maintainer's machine a warm scan takes
  2.7 s after the largest session grew, where it took 17 to 23 s. A warm scan prints exactly
  what a cold one prints. `--no-cache` reads everything.
- `scan --since D` no longer reads runs it is about to drop: a run whose files were all last
  written more than a day before D is skipped before it is parsed. The result set is the same,
  except for a run with no readable event at all, which has no start date and can be left out.
  The totals line says `SINCE D` instead of `ALL TIME`.
- `scan` says it is working. On a terminal one stderr line counts the runs read and is erased
  at the end; elsewhere, a scan of more than 50 runs prints one line when it starts.
- The `missing_artifact` path rule, shared with the live check: a path must start a token,
  templates (`<case>`, `{id}`, `*`, `$VAR`) are skipped, and a period that ends the sentence
  right after a path no longer hides it. On the maintainer's 355 runs this removes 12 info
  findings (placeholders and slashes inside relative paths) and adds 16, all in sessions
  built to leave that file missing.
- README: a table maps the live words KILL, FAILED and CLAIM to what `scan` and `run` print.

### Fixed

- A subagent that another subagent launched in the foreground no longer stays RUNNING
  ("quiet 7d") in `status`, the resume sentence and the per-call check after it returned. Its
  result lands in its launcher's transcript as a "[Subagent hand-back]" with no notice in the
  main one; live mode now reads it there, as `run` already did.
- The share-label fix of 0.3.6 also covers an attempt whose journal entry has no key and an
  agent id that carries two labels. Such an attempt's interrupt finding is told as the run's
  story, and its label is now replaced there too.
- A forked session no longer owns its parent's history when the copied entries carry the fork's
  own `sessionId` (Claude Code 2.1.305 and later). The hook attachments in the copied block still
  name the session that wrote them, and that now marks where the fork's own entries begin. On a
  background session forked from its dispatcher, `status` had counted the dispatcher's subagent
  as running and `run` had said WORK AT RISK unknown; both now read 0 subagents.
- `rescue plan` on a session names, under BRIEF FOR A RELAUNCH, only what the rescued subagents
  wrote. It listed every subagent's files, so a relaunched agent was told to build on other
  agents' work. A workflow's brief still covers the whole run.
- A commit run behind `NAME=VALUE`, `env NAME=VALUE` or `mise exec ... --` counts as a commit
  command, so an agent that committed that way and printed no summary line reads `COMMITS ?`
  instead of `0`.
- `scan --since <today>` could print more runs under SINCE than under LAST 7 DAYS. A run whose
  files carry no timestamp is kept by `--since`; the 7-day line now places it by its newest
  file write too.
- `status --local` and `--raw` print real agent ids (the `agent-<id>.jsonl` names), and a blocked
  or failed background session's own name and working directory. Before, `--no-redact` printed
  the same aliases as the default, which ends with one line saying what `--local` adds.
- `scan` marks the caller's own runs: a triage row ends in `(this session)` or
  `(started by this session)` when `CLAUDE_CODE_SESSION_ID` names that session.
- A story's count agrees with its noun: `1 Bash command backgrounded`, not `1 Bash commands`.
- An unattributed interrupt's title says what the transcript logged: `..., cause unattributed,
  logged as a user interrupt, consistent with a runtime kill`.
- `status` says how each of the session's workflows ended. Open ones keep their `watch` command,
  a failed, killed or stopped one points at `run`, and completed ones are one count line, where
  a long session printed one watch line per workflow.
- A worktree removed after its run (a merged branch, cleaned up) no longer reads as false
  alarms: its report files are named as in a directory that no longer exists, WHAT TO DO stops
  asking to check them, WORK AT RISK names the removed directory instead of a `git status`
  command for it, and `status` makes no CLAIM of an output there.
- `rules propose` reads through scan's cache: 101 s on 382 runs before, 1.2 s warm.
- A no-match error repeats a typed `wf_` or `s_` id and says `agent-postmortem scan --all` lists
  every run id.
- A FAILED workflow whose agents all completed says that Claude Code reported the failure and to
  check what the workflow itself returned.

## [0.3.6]

### Fixed

- The default (share) output could print an agent's own label. A story sentence starts with
  a capital, and when an agent's whole story was its interrupt finding, whose title starts
  with the label, the label was capitalized before the name substitution ran. That
  substitution matched names case-sensitively, so a workflow label such as `impl:t2` or a
  subagent description starting with a lowercase letter printed as `Impl:t2` in the
  STORIES section of `run` and in `stories[].text` of `run --json`. A story clause is now
  capitalized after its names are replaced, so it reads `Agent-1 interrupted after ...`.
  `scan`, `run --bug-report`, `rescue` and the hook messages never carried it, and none of
  the 382 runs on the maintainer's machine hit it. `--local` and `--raw` are unchanged.

## [0.3.5]

### Changed

- The PyPI summary is the positioning line the plugin already uses: it reads the Claude Code
  logs already on your machine and tells you which agent's "done" you can't trust. The package
  is classified as Alpha, matching the README's status line, and the README's license badge
  links to the LICENSE file by full URL, so it also works on the PyPI page. PyPI version and
  Python version badges join the CI and license badges.
- Installs come from PyPI now. The README's first command is `uvx agent-postmortem scan`, and
  Quick start, the plugin README and the `/postmortem` skill say `uv tool install
  agent-postmortem`. No git is needed.
- The plugin installs its CLI from PyPI at the plugin's own version. It falls back to the
  matching GitHub tag only if that install fails, for example in the minutes between a release
  reaching GitHub and reaching PyPI. Both attempts share the one 60-second deadline the single
  install had. The missing-CLI hint says `uv tool install agent-postmortem`.

## [0.3.4]

### Fixed

- One bad transcript line could crash a whole scan. A line nested deeper than Python's JSON
  parser can follow (for example 200,000 opening brackets) raised RecursionError, which no
  reader caught, so `scan`, `run`, `rules` and `trend` stopped with "unexpected
  RecursionError" (exit 1), whether the line sat in a transcript, a journal.jsonl or an
  agent's meta.json. Every reader of run data now treats it like any other line that is not
  JSON (diagnostic `nested too deeply to parse`), and `scan` carries on over the other runs
  and prints one `SKIPPED` line: how many unreadable lines in which runs, how many transcript
  files had no readable line, how many meta files and runs could not be read, and that
  `agent-postmortem run <run-id> --json` lists each line under diagnostics. A run that still
  cannot be read makes scan exit 4 (with `--json`, the `SKIPPED` line goes to stderr and the
  JSON is unchanged); `rules`, `rules propose`, `rules review`, `trend` and `run --here` name
  it on stderr and go on; `run`, `rescue` or `watch` on that one run say it could not be read
  and exit 4, also when it was named by `agent-<id>` or `a:<alias>`. A parent transcript line
  that is not a JSON object is now a diagnostic, like one in any other transcript. A last line
  with no newline yet (a live write) is not counted as skipped.

### Changed

- First-run empty state: on a machine with no Claude Code runs, `scan` (and `trend`, `rules`,
  and `run`, `rescue` when the root holds no run at all) no longer prints only
  `no runs found under path-1`. It names the standard location `~/.claude/projects`
  home-relative (it is the same on every machine; a root typed with `--root` still prints as
  `path-N`), says when it does not exist yet, what the tool reads, `next: agent-postmortem
  demo`, and that `--root` or `CLAUDE_CONFIG_DIR` points elsewhere and transcripts are
  deleted after 30 days by default. Exit code 2 is unchanged. `run --here` with runs elsewhere
  points at `scan`; a scan that found no subagent transcript and no Workflow run says so
  (not with `--kind`); `watch`'s 60 s timeout says what it waits for; the
  `/postmortem` skill relays the message and stops.

## [0.3.3]

### Added

- Detector `codex_unavailable`. On one machine, 7 workflow agents whose job was a Codex
  review returned "completed" with a short text saying the review never ran: the Bash call to
  `codex-companion.mjs` failed with "Codex CLI is not installed or is missing required runtime
  support", and 2 of those runs read CLEAN. The detector gives one finding per attempt, major
  when the attempt completed and no later companion call returned a result without that line,
  minor otherwise. Assistant text quoting the sentence (4 entries) and other commands printing it
  never count. It backs R-CODEX-CONTRACT, which now also says to check that the companion
  starts before a workflow relies on a Codex agent, and `rules review` counts it as a
  recurrence of that rule.

### Changed

- Install from the tagged GitHub release while PyPI publishing is not available: the README,
  the plugin README, the `/postmortem` skill and the plugin's missing-CLI hint say
  `uv tool install git+https://github.com/zeyusuntt/agent-postmortem@v0.3.3` (or `uvx --from`
  the same source), and the plugin's one-time install uses the tag that matches its own
  version. A tag now makes the GitHub release with the built files and this CHANGELOG section;
  the PyPI publish job runs only when the repository variable `PUBLISH_PYPI` is `true`, so a
  tag no longer fails while the trusted publisher is not set up (CONTRIBUTING.md has the steps).
- README first screen: it opens with the question the tool answers ("Which 'done' in your
  last Claude Code run can't you trust?"), one paragraph, the dated number (13 of 166
  workflow runs, scan 2026-10-27), and `uvx agent-postmortem scan` with the GitHub form as a
  fallback; Quick start installs from PyPI with the same fallback. A new "What it covers"
  section marks each run shape covered, partial (`claude -p`, background subagents) or not
  yet (`claude --bg` sessions, agent teams, `SendMessage` delivery) and asks users of those
  shapes to open an issue. Issue templates: wrong verdict, confusing output and "my run shape
  isn't covered" are new; the bug report template is now for crashes and errors; every
  template asks for default-level `--bug-report` output and the Claude Code version, and the
  detector request no longer asks for transcript lines.
- The plugin manifest and its marketplace entry now carry the same description: "Reads the
  Claude Code logs already on your machine and tells you which agent's 'done' you can't trust,
  and what to run next." The marketplace has a description of its own, and `plugin.json` names
  its homepage and repository. CI validates both the plugin and the marketplace file with
  `claude plugin validate --strict`; before this the marketplace check failed under `--strict`
  (no description).
- README and DESIGN: the transcript formats are now stated as observed on Claude Code 2.1.251 to
  2.1.312 (was 2.1.280). The Limits section names the five entry types first seen in 2.1.285 to
  2.1.310 that are not interpreted yet, and says Claude Code deletes transcripts older than 30
  days by default (`cleanupPeriodDays`). Why gives the 0.3.2 scan of 2026-10-27 (13 of 166
  workflow runs retried) and the upstream status as of 2026-10-28.

### Fixed

- Reported commit ids printed as written at the default share level, and a short hash of a
  public repository's commit can identify the repository and its author. `rescue plan` (text
  and `--json`) and `run --json` now print each reported commit id as `commit-N`, numbered per
  output, the same id the same N. A result's own `commits` claim is covered too: `run --json`
  (`reported.claims.commits` and, with `ready: false`, `findings[].metrics.claims.commits`),
  `rescue plan --json`, and the REPORTED line of `rescue plan` print `commit-N` instead of the
  ids. `rescue plan --json` now exports the units before the brief, so the first `commit-N` it
  prints is `commit-1`. `--local` prints the real ids, scanned for paths as before; `--raw` prints them unchanged; counts are unchanged. A file named
  `commit-1` no longer turns a commit into `path-1` in JSON.
- A Codex companion call with a command over 2,048 characters (the forwarder passes long prompts
  inline) was not recognized as codex. A failing call followed by a successful long retry stayed
  `major` instead of `minor`, and an attempt whose only codex call was long lost the
  `codex_write_denied` assistant-text match. Both now judge the full command.
- `structured_output_failure` no longer tells a multi-agent run as the first hit's agent's story:
  with failures in 2 or more agents its `agent_id` is now empty and the finding backs the run
  story, like the other 5 run-wide tooling detectors. Title, count, severity and rule are
  unchanged; the `finding_id` of a multi-agent finding changes.
- The session-end hook dropped a `codex_unavailable` finding from a session's main thread. It
  now runs all 8 tooling detectors, so a main-thread Codex companion failure is reported.
- Scan and the run report disagreed on one run: a killed attempt in the last 72 hours whose only
  write was a relative-path Edit with no working directory read "1+ paths named" in scan and
  "0 paths named" under WORK AT RISK. The scan reason for a runtime kill and for an attempt at
  risk now uses the same helper and the same numbers as the WORK AT RISK count line.
- A nested foreground subagent whose report came back as a plain "[Subagent hand-back]" result
  read "unknown" and WORK AT RISK said "unknown: N subagents have no established return" (4
  children in one real session). The launch is now also found by the `toolUseId` in the child's
  meta file, which matched the result-linked launch in 651 of 651 cases where both exist. A
  `toolUseId` that is not a string reads malformed.
- A usage-limit, machine-sleep or API-error failure would have read "not recorded" on Claude
  Code 2.1.305 or later when a SubagentStop hook is set. Those transcripts end with an
  `attachment` entry (type `hook_success`) after the last assistant entry, which 163 of 205
  transcripts measured on 2.1.305 to 2.1.312 had. Trailing `attachment` entries no longer
  count as the attempt's final entry, so the cause and reset time are read, in the `watch` FAILED line too. A `user` or
  `system` entry after the assistant entry still makes it not final. No real case was hidden.
- A tooling finding that counted hits from several agents was told as the first hit's agent's
  story, for example `agent-1  6 Bash commands backgrounded.` when the 6 hits were in 5 agents.
  The five run-wide detectors (`bash_backgrounded`, `bash_timeout`, `tool_json_error`,
  `classifier_unavailable`, `git_cwd_mismatch`) now set the agent only when all hits are in one
  agent; with hits in 2 or more agents there is no agent and the finding backs the run story.
  Titles, counts, severity and rules are unchanged. The `finding_id` of a multi-agent finding
  changes, because the id includes the agent.
- `scan` no longer prints the reason `clean` next to a run in NEEDS ATTENTION. A run listed
  only because an attempt at risk wrote a path (ended within 72 hours, no finding, no
  interrupt) now reads like `2 attempts at risk: 1+ paths named, 0 commits`, with the same
  counts as the WORK AT RISK line, in the text and in the JSON `attention_reason`.
- Transcripts written by Claude Code 2.1.300 or later no longer make every attempt's write
  ledger partial. Those versions record each API request in the transcript (`api-request`,
  `api-request-shape`, `api-request-blob` entries), which the ledger did not know, so every
  attempt line in WORK AT RISK ended with `ledger incomplete (unknown_entry_type)`. A scan of
  296 real runs on 2026-10-28 found the entries in every transcript from 2.1.300 to 2.1.312;
  with the fix no verdict changed and one run's at-risk list became complete.

## [0.3.2]

Repository checks now work on machines with Git LFS, and they never run a filter driver or a
fetch the repository could steer. Before this, every `--observe` and `rescue status` check that
reached `git status` on such a machine stopped at `guard refused`, including on GitHub's CI
runners, which is why CI failed there after 0.3.1 was pushed.

### Changed

- The filter-driver guard reads where each clean or process filter driver is configured
  (`git config --show-scope`). A driver from your own system or global git config, which is
  where `git lfs install` puts Git LFS's, no longer stops `git status`; it is switched off for
  that one call instead, so it never runs (a repository can steer even a driver it did not
  configure: its attributes pick the driver, and Git LFS reads extension commands from the
  repository's own config). A file routed through such a driver is compared without that
  external filter (built-in conversions still apply), so a Git LFS file whose real content is
  checked out reads dirty once its timestamp changes, even when its content did not. A driver the
  repository configures (its `.git/config`, its worktree config, or a file either of those
  includes), a line the guard cannot read, an unusual driver name, and a failed check all still
  stop `status`.
- README: the opening says what problem the tool answers; the quick start installs from GitHub,
  since no PyPI release exists yet; the PyPI badges are gone until one does; the plugin section
  says to install the command-line tool first; Why gives the retry count from a dated 0.3.1 scan
  (12 of 130 workflow runs) instead of the 0.1 figures, and lists six upstream issues with their
  status on 2026-10-26.

### Fixed

- In a partial clone, `git status` fetches a missing object on demand through the promisor
  remote the repository configured, and an `ext::<command>` URL runs that command. Repository
  checks now run git with `GIT_ALLOW_PROTOCOL=` (which refuses the transport on every supported
  git version) and `GIT_NO_LAZY_FETCH=1` (on versions that know it), so the fetch is refused and
  the check reports unchecked. This was possible since repository checks were added in 0.2.
- `test_trend_still_counts_a_detector_silenced_by_config` ran on the real clock and failed from
  2026-10-26 on, when its 2026-09-01 fixture left the default 8-week window. The clock is now
  pinned inside the test.

### Known limits

- In a repository with a sparse index or a split index, git's own `status` can still store a
  cached tree object or refresh the shared index file's timestamps during a repository check.
  Tracked content and refs are not changed. Refusing these modes is left for a later change.

## [0.3.1]

A second field round on real runs. Session subagents get a proven return state, the report stops
counting things it cannot see, and four checks the first round missed are added.

Five rules changed after review. When any transcript that could hold a resume request
cannot be read, every child's return is unknown. A resume addressed to an agent name, in `to` or
`recipient`, is no longer a contradiction; only another child's id is. A tool-use id that is not a
string makes the children it names unknown. A hook unit that was re-opened is analyzed again at
once. A transcript whose last line was cut inside a multibyte character is a half-written line,
like one cut in plain text: the run gets a diagnostic and the rest of the transcript is still read.

### Changed

- A session subagent's end comes from what its parent recorded (a returned result, a failed or
  killed task notification, a resume), not only from its own transcript. The AGENT table gains a
  `RESUMES` column when some subagent was resumed, WORK AT RISK names a subagent with no
  established return in one words-only line, and `rescue` says `NO RESCUE UNIT` instead of
  `NOTHING TO RESCUE` when a child has no established return. A child that reported failure is an
  `attempt_failed` finding.
- `claim_contradiction` also fires when a completed attempt's returned result carries a top-level
  `<name>_failed` count above 0 (an integer, not a boolean; `tests_failed` and `suite_failed` keep
  their own clauses) or `blocked: true`. Major, with a fixed title (`reports failures in <n> field(s)`, `n` the number
  of such fields) that names neither the key nor any result text. `blockers_fixed`, `blocker_kind`, `blocked_reason`, nested keys and
  `blocked: "yes"` count for nothing.
- The COMMITS cell reads `?` when a completed shell call contained a commit command but reported no
  commit id and no other id is known (it read `0+`), with a legend line under the table that starts
  `COMMITS ?`. With at
  least one known id it still reads `N+`. The STORIES sentence, the scan reason for a runtime kill
  and the `watch` KILL line follow the same rule: `N+ commits`, or `commit count unknown` where
  they said `0 commits`.
- `run --here` and `scan --here`: when `CLAUDE_CODE_SESSION_ID` names the only session with that
  transcript name and that session itself worked in this directory, the workflow runs it started
  are included, whatever directory their agents worked in. Another session's workflows never are.
- At the default share level, `reported.keys` in the JSON lists `key-1`, `key-2` and so on instead of
  the result's own key names (numbered in order of first appearance within one report). `--local`
  and `--no-redact` keep the real names.

### Added

- Detector `permission_denied` (minor): a tool result with `is_error` that starts with one of the
  runtime's four denial sentences. One finding per attempt, the session main thread included, with
  the four counts and up to 5 `file:line` pointers. A rule denial may span lines (a multi-line
  command) and name an `mcp__` tool. The denied command is never printed. It is not
  run by the session-end hook.
- The maintainer's advisory corpus check gains invariants 6 to 13 (an independent return state, the
  exported outcome, the WORK AT RISK guard, denial counts, claim coverage, rescue wording, `--here`).
  It is a maintainer check, not a shipped feature: its logic is test support under `tests/` and the
  runner over real runs is not part of the package.

### Not in this release

- Round-scoped paths, risk and WASTED for resumed children.
- A `main` row for the main thread's commits.
- Stopping a background shell with TaskStop.
- PATHS for writes inside inline Python or a script.

## [0.3.0]

0.3 rebuilds the evidence under every verdict: severity, WORK AT RISK, and the
run's end come from recorded facts, and anything the tool cannot establish
says `unknown`. A field round on five projects and 238 real runs fixed what
the synthetic tests missed (see Fixed).

### Changed

- `--json` is schema 3 on every versioned command: a `disclosure` field,
  stable aliases and `path-N` names at the default level, no transcript
  excerpt key, a `lifecycle` on each run, a `ledger` on each attempt, and an
  `observation` and `ledger` entry per path in `rescue`.
- Severity is computed from recorded inputs (cause, silence, position, whether
  a later attempt completed, the run lifecycle), not from which detector fired.
  No detector is critical. `FAILED` comes only from a failed run lifecycle.
  `missing_artifact` is `info` and WHAT TO DO adds one `[look]` item that
  points to `run <id> --verbose`.
- `WORK AT RISK` is a multi-line block built from a write ledger. It says
  `none:` only when the risk population is complete and no ledger names a
  write, and `unknown:` when the tool cannot establish which attempts were at
  risk. A shell command counts as "may have written more" unless it matches a
  closed read-only grammar.
- `Run.finished` follows the run lifecycle. A session has no lifecycle, so its
  header says `last activity` instead of `ended` and it never carries
  `(unfinished)`.
- The AGENT table's PATHS column counts every ledger path and prints `N+` when
  the count is a lower bound. `KILLED` counts only kills the runtime owns.
- `mislabeled_kill` names an observed backgrounding precursor without claiming
  it caused the kill. `rules propose` shows an adopted rule's status instead of
  proposing it again. `R-NO-LONG-BASH` carries the macOS note.
- `run --observe` and `rescue` work per repository; `rescue status` counts a
  check per declared repository.
- Detector discovery runs once per process.
- Paths in free text (titles, diagnostics, error messages, and at `--local`
  the excerpts) are found by one shell-word scanner. A path start hides the
  rest of its quoted fragment, or of its line when it is not quoted, and a
  hidden span that held spaces is marked ` [text hidden]`; `--raw` shows
  everything. Relative names stay visible: the default level prints no
  transcript text at all, so they appear only at `--local` and `--raw`.
- `run --here` diagnoses the session it is run from (Claude Code's
  `CLAUDE_CODE_SESSION_ID`), else the most recently active run that worked
  in this directory or a parent of it, and says on stderr when several runs
  matched. `scan` lists
  possibly live runs with no other problem under `LIVE NOW` instead of
  `NEEDS ATTENTION`. `scan --all` orders rows by the instant each run started,
  oldest first, with undated runs first, and a session row that may still be
  running says `possibly running`.
- WASTED counts every attempt before an agent's last one, whether or not it
  returned a result, plus the last one when it was killed, failed, or
  superseded; a killed or failed last attempt with no retry now counts too.
- The AGENT and PHASE columns size to the longest shown label (long labels are
  cut with `...`), a legend explains `N+`, and WORK AT RISK, REPO STATE, the
  scan detector counts, and the `bash_timeout` title name what they measure.
  `run`, `rescue plan`, `rescue status`, and `watch` document their exit codes
  in `--help`.
- The packaged demo is a complete run with its parent session transcript, so
  its output shows what a typical run shows.

### Added

- Three disclosure levels: share (the default), `--local`, and `--raw`, with
  aliases from a persistent registry (`a:`, `s:`, `project:`).
- A write evidence ledger per attempt and a risk population per run.
- Marker provenance: a marker counts only in the position the registry
  anchors; marker-like text anywhere else is ignored. A Bash result whose
  first line matches the runtime notice still counts, by design, even when a
  command printed it (a `cat` of a log that starts with that notice).
- The `workflow_failed` detector, and a run lifecycle with generations read
  from the parent transcript.
- `run --here`, `scan --here`, and `RUN_ID` forms for a session UUID, an
  `agent-<id>` stem, and `a:<alias>`.
- `watch` prints `START` first and `OK` per returned result, and `DONE` fires
  at once from lifecycle evidence.
- Exit codes for `rescue plan`, `rescue status`, and `watch`.
- `rules propose --run <id>` and the `agent prompt` rule target.

### Fixed

- A workflow the harness reported failed no longer reads `CLEAN`.
- `rescue plan` and `rescue status` no longer print `NOTHING TO RESCUE` for a
  run whose risk population is not empty and complete; they print
  `NO KILLED OR FAILED ATTEMPT` and exit 4.
- A missing transcript no longer shows an exact PATHS count, and the `watch`
  KILL line writes `P+` for a partial ledger.
- An asynchronous Agent launch counts as returned only through an accepted
  completed notification.
- `rescue plan` prints no `<none>` step and the fan-out caveat only for
  overlapping attempts of different keys.
- A workflow's first agent, which starts a fraction of a second before the
  launch result is written, is no longer reported as unplaced and at risk.
  Attempts are placed in a launch by the time of the launch call itself
  (unplaced attempts on the maintainer's 238 runs: 90 before, 0 after).
- A registered path inside a longer path no longer leaves the longer path's
  tail visible at the default level.
- A forked session no longer counts the block it copied from its parent as
  its own findings and time.
- A commit made with `git commit -q`, which prints no summary line, is shown
  as a lower bound (`N+`) instead of an exact 0.
- Paths glued to Chinese, Japanese, Korean, Thai, Lao, Tibetan, Myanmar, or
  Khmer text, Windows and `file://` paths, `$HOME/`, `$PWD/`, and
  `$CLAUDE_PROJECT_DIR/` spellings, colour-coded paths, and quoted names with
  apostrophes, smart quotes, or a cut-off closing quote are hidden at the
  default and `--local` levels. After a word of those scripts a path counts
  when it has two segments, a file extension, or a slash after it, so a slash
  between two words (`成功/失败`) stays prose. A path glued straight to a word
  of a language written with spaces (`read/srv/x`) is not read as a path, by
  design; docs/DESIGN.md 5.1 lists every recorded limit.
- A run directory the tool cannot read is reported as a diagnostic instead of
  ending the command with a traceback.

## [0.2.0]

Design first: 8 code-level studies of how other agent tools bound their
repair loops, two Codex reviews of the design, and the reviews removed every
write surface the first draft had. In 0.2 the tool observes and reports; a
human decides. The tool never edits a file, never writes to a repository,
never runs a check, never launches an agent, and never says "verified".

### Added

- `agent-postmortem rescue plan <run-id>`: a read-only decision packet per
  killed or failed agent: the task pointer, what it reported back, every path
  it wrote with its state in the repository right now (`DIRTY_NOW`,
  `CLEAN_NOW`, `MISSING_NOW`, `UNKNOWN`), the commits it reported, the tool
  call that was in flight at the kill, what is unknown, the checks declared
  for that repository, and the steps to take, each tagged `[look]` or
  `[decide]`. A possibly live run gets a stop line instead of steps.
- `agent-postmortem rescue status <run-id>`: what the session's own
  transcript shows was run after the run ended: `EXIT_0_OBSERVED`,
  `NONZERO_OBSERVED`, `NOT_OBSERVED`, `WRITE_AFTER_CHECK`, the check cycles
  observed against a budget of 2, calls that were not counted and why, and
  any command matching the never-undo list.
- `agent-postmortem watch <run-id>`: a read-only follower for one running
  workflow that prints `KILL`, `FAILED`, `QUIET`, then `DONE` or `LIMIT`,
  designed to be a Claude Code Monitor command. The plugin's Workflow launch
  hook hands the session that command.
- `/rescue` skill: the bounded protocol the session follows (read the
  packet, inspect, run the declared checks, at most 2 repair cycles, never
  undo with git, ask before every commit and relaunch, never edit an
  instruction file).
- Repository observation behind `run --observe` and inside `rescue plan`:
  5 fixed read-only git argument lists, an environment built from scratch,
  skipped entirely when git is older than 2.36 or a filter driver is
  configured, and skipped for runs older than 72 hours.
- Two detectors: `attempt_failed` (the cause of a journal `failed` event,
  read from the last transcript line: usage limit with its reset time,
  machine sleep, server error, interrupt) and `claim_contradiction` (an
  attempt that completed while its own result reported failed tests,
  `ok: false`, `ready: false`, blockers, or a missing report file).
- Per-attempt facts from the full transcript entry: paths written, tool
  calls in flight at the kill, commits reported, backgrounded commands,
  seconds since the last tool call.
- `rules propose`: the rules backed by findings in at least 3 runs on 2
  distinct days, with where each belongs (a workflow prompt, a hook, or
  project instructions). `rules review`: each adopted rule's detector rate
  before and after adoption, and the rules to consider removing.
- User configuration at `~/.config/agent-postmortem/config.toml`: quiet a
  detector (`[detectors] off`), declare a repository's checks (`[[repos]]`),
  record adopted rules (`[[adopted]]`).
- `--json` carries `schema: 2`, `facts`, `stories`, `next`, `observation`,
  `reported`, and `silenced` on every finding.

### Changed

- `run` text report: verdict, a plain account of what happened, WORK AT
  RISK, at most 3 next steps, the agent table with killed/paths/commits
  columns, and one story per agent. Evidence excerpts and info findings are
  behind `--evidence` and `--verbose`. The report never names an instruction
  file.
- `scan` defaults to a ranked NEEDS ATTENTION view with a reason and a next
  command per run, plus 7-day and all-time totals; `--all` prints the old
  full list. Unfinished runs are split into possibly live, failed, and
  abandoned.
- Journal `failed` events are read: a run whose every key ended is never
  shown as unfinished, and the hook and the reader share one definition of
  finished.
- `silent_retry` is one finding per agent; its evidence points at the kill
  line and the restart line instead of quoting the prompt; its title counts
  restarts, not attempts.
- `codex_write_denied` counts only the codex-companion's output or a `codex`
  command's result, never prose that quotes the marker.
- `report_path` and every observed path print as `path-N` pseudonyms by
  default.

### Removed

- `apply --write`. `apply` prints the diff and never writes; a human pastes
  the block in. The tool has no code path that writes an instruction file.

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
