# Fixture run directories

Every case under `runs/<case>/` has the same file layout as a real run
directory, all synthetic.

## File formats

### `journal.jsonl`

One JSON object per line, in chronological order. Observed `type` values:

- `launched`: `type`, `timestamp`. Marks the workflow tool starting.
- `started`: `type`, `timestamp`, `key` (a `v2:`-prefixed stable hash per
  logical `agent()` call), `agentId`, `label`, `phase`. The same `key`
  appearing in more than one `started` event means that logical agent was
  restarted; attempts are ordered by their position in the journal.
- `result`: `type`, `timestamp`, `agentId`, `key`, `result`. Marks the
  attempt of a logical agent call completing. Real journals carry the
  agent's return value in `result` (a dict or a string); a fixture may use
  any small value. A killed-and-restarted attempt has no `result` of its
  own: the next `started` for the same key is its only trace.
- `failed`: `type`, `timestamp`, `agentId`, `key`. Marks an attempt the
  runtime gave up on. It may be followed by another `started` for the same
  key (a retry) or be the key's last event (terminal).

### `agent-<id>.meta.json`

A flat JSON object: `agentType`, `description`, `workflowPhase`,
`spawnDepth`, `requestShape`, `requestNonInteractive`. All values are
strings.

### `agent-<id>.jsonl`

One JSON object per line: a Claude Code session transcript for one attempt.
Common fields: `type` (`user`, `assistant`, `attachment`), `timestamp` (ISO
8601 with a `Z` suffix), `uuid`, `parentUuid`, `agentId`, `cwd`, `sessionId`,
`version`. `message` has `role` and `content`; `content` is either a plain
string or a list of blocks:

- assistant blocks: `text`, `tool_use` (`id`, `name`, `input`)
- user blocks: `tool_result` (`tool_use_id`, `content`, `is_error`)

Assistant messages also carry `message.usage`: `input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`.
`attachment` lines record a hook result: `hookName`, `hookEvent`, `command`,
`stdout`, `stderr`, `exitCode`, `durationMs`.

Every fixture uses the fake session id `00000000-0000-4000-8000-000000000000`
(also used as every `uuid` and `parentUuid`, since the reader does not model
those fields and reusing the fake id keeps every UUID in the fixtures
identical), the fake cwd `/home/user/project`, and prompt text that is
obviously synthetic. Agent ids are 17 hex characters starting with `a` and
unique across every fixture in this directory.

### Session layout

A session case (`session_*`) has no `journal.jsonl` and no
`subagents/workflows/` directory. Instead:

```
<case>/-home-user-project/00000000-0000-4000-8000-000000000000.jsonl
<case>/-home-user-project/00000000-0000-4000-8000-000000000000/subagents/agent-<id>.jsonl
<case>/-home-user-project/00000000-0000-4000-8000-000000000000/subagents/agent-<id>.meta.json
```

The first file is the main transcript, loaded as `Run.main` (`label="main"`,
`key=None`, `ordinal=1`); it is never in `Run.attempts`. Each
`subagents/agent-<id>.jsonl` is one Agent-tool subagent, loaded as exactly
one `Attempt` (retries of an Agent-tool subagent are not observable in
v0.1) with its own `LogicalAgent`, `key=agentId`, label from the meta file's
`description` (empty string when the meta file is missing), `agent_type`
from `agentType`, and `phase` from `workflowPhase`. A session's public run
id is `s_` plus 10 hex characters, derived from its project slug and
session id (`reader._session_public_id`); it does not depend on file
content, only on path.

## Cases

### `clean_run`

2 logical agents (`impl:alpha`, `impl:beta`), 1 attempt each, both complete
normally. No marker strings anywhere. Exercises the case where no detector
should fire. Also the only fixture with an `attachment` line, to cover that
transcript shape.

Expected numbers: attempts 2, keys 2, retries 0, silent kills 0.

### `silent_retry_kill` (packaged as `wf_00000000-d30`)

Moved into the package (`src/agent_postmortem/demo/-home-user-project/
00000000-0000-4000-8000-000000000000/subagents/workflows/wf_00000000-d30/`,
with its parent session transcript at
`src/agent_postmortem/demo/-home-user-project/00000000-0000-4000-8000-000000000000.jsonl`:
a user turn, the Workflow launch, its result and a completed notification;
docs/DESIGN.md 5.4/6/8.2) once the packaged demo landed; it is no longer under
`tests/fixtures/runs/`. Its directory is named `wf_00000000-d30` (a
conforming workflow run id, M8 review D8) rather than a hash-fallback id, so
`agent-postmortem demo`'s own RUN header never depends on
`_valid_workflow_run_id`'s replacement path. It is both the packaged
`agent-postmortem demo` run and the fixture the detector/redaction/export
tests load, kept in one place, never copied. This entry stays here only as
the field-level description of its shape.

2 logical agents (`impl:silent-alpha`, `impl:silent-beta`), 2 attempts each,
4 attempts total. In each pair the first attempt is killed (its transcript
ends with the kill sentinel `[Request interrupted by user]`) and the other,
later attempt is the final, completed one.

Both killed attempts are silent for exactly 416s before the kill sentinel:
the last preceding event lands 416s before the sentinel timestamp (for
example `19:03:04.000Z` to `19:10:00.000Z` for the first pair). Each killed
attempt's own wall-clock span (its first event to the kill sentinel) is
exactly 600s, so the wasted-time detector's total for this run (the sum of
the two non-final, killed attempts) is exactly **1200s**.

Expected numbers: attempts 4, keys 2, retries 2 (both keys), 416s of silence
per killed attempt (2 findings), 1200s wasted in total.

silent_retry itself (unit M1d) still fires once per retried key (2
findings total here, one per agent, each carrying `rule_ids =
("R-RETRY-SAFE",)`: the two agents start 30 minutes apart, well outside the
5s burst window). Its evidence no longer quotes either attempt's prompt:
for the first pair it is `agent-ac200000000000001.jsonl:5: [Request
interrupted by user]` (the kill sentinel line) followed by
`agent-ac200000000000002.jsonl:1: attempt 2 started` (a plain pointer, no
excerpt), and the second pair follows the same shape on
`agent-ac200000000000003.jsonl`/`agent-ac200000000000004.jsonl`.

### `bash_backgrounded_timeout`

1 logical agent, 1 attempt, completed. Its transcript has two Bash
`tool_result` blocks: the first contains the backgrounded-command marker
("did not complete within its" / "moved to the background"), the other
contains the hard-timeout marker ("Exit code 143" / "Command timed out
after").

Expected numbers: attempts 1, `bash_backgrounded` count 1, `bash_timeout`
count 1.

### `structured_output_corrupt`

1 logical agent, 1 attempt, completed. The first `StructuredOutput` call
gets a `tool_result` with the schema-mismatch marker ("does not match
required schema") and a leaked XML-like tag inside the excerpt
(`<notes>partial write leaked here</notes>`). The agent retries and the
retry call succeeds.

Expected numbers: attempts 1, `structured_output_failure` count 1.

### `codex_write_denied`

2 logical agents, 1 attempt each. The first attempt's Bash `tool_result`
carries the sandbox write-denial marker (`WRITE_DENIED: patch rejected:
writing outside of the project; rejected by user approval settings`). The
other attempt's Bash `tool_result` carries the shell-guard block marker
(`Security: node blocked in workspace`).

Expected numbers: attempts 2, `codex_write_denied` count 2 (one finding per
marker).

### `missing_artifact`

2 logical agents, 1 attempt each, both complete normally with no error
markers. The first attempt's first user message names
`/tmp/agent-postmortem-fixture/missing-report.md`, a path that is never
created; a detector test asserts it is reported missing. The other
attempt's first user message names
`/tmp/agent-postmortem-fixture/present-report.md`; a detector test creates
that file before scanning so this attempt is reported verified.

Expected numbers: attempts 2, `missing_artifact` findings 1 (the missing
path only, once the present path has been created by the test).

### `self_reference`

Workflow kind (flat, like `clean_run`). 3 logical agents, 1 attempt each,
all complete normally. Exercises DESIGN.md 4.3's self-reference rule
(`Event.read_only_output`): agent `af000000000000001` does a `Read` of
`markers.py` whose content quotes both `SANDBOX_WRITE_DENIED` fragments;
agent `af000000000000002` runs `sed -n '1,40p' src/x.py` whose result
quotes both `CLASSIFIER_UNAVAILABLE` fragments; agent
`af000000000000003` runs `node codex-companion.mjs task run` whose result
carries a real `WRITE_DENIED` denial.

Expected numbers: attempts 3, `codex_write_denied` findings 2 (the fixed
denial+artifact pair, both attributed to `af000000000000003` only, `count`
1), `classifier_unavailable` findings 0.

### `session_clean`

Session kind. A main transcript that dispatches two Agent-tool subagents
(`impl:alpha`, `impl:beta`) and reports both finished, plus their two
subagent transcripts. No marker strings anywhere.

Expected numbers: `Run.attempts` 2 (child attempts only; main is not in
it), `Run.agents` 2, `Run.main.label` `"main"`.

### `session_dedup`

Session kind, main transcript only (no subagents). The main transcript
carries two assistant entries sharing `(message.id, requestId)`
(`msg_dup0001` / `req_dup0001`), each reporting usage 10 input / 15 output
tokens, followed by a third, distinct entry reporting 3/4.

Expected numbers: `Run.main.usage` `{"input_tokens": 13, "output_tokens":
19}` (10+3, 15+4; the duplicate's 10/15 is not counted twice). The
duplicate assistant `Event.usage` is `{}`.

### `session_envelope`

Session kind. The main transcript's `Agent` `tool_result` quotes the child
subagent's own `WRITE_DENIED` denial text verbatim (the parent narrating
what its subagent reported); the child subagent's own Bash `tool_result`
carries the same marker directly.

Expected numbers: the main transcript's `Agent` `tool_result` event has
`envelope=True` and `markers=frozenset()` even though its excerpt still
contains the text; the child's Bash `tool_result` event has `envelope=False`
and carries the marker. `codex_write_denied` fires with `count` 1 (from the
child only) and `agent_id` set to the child's.

### `session_compaction`

Session kind. The subagent transcript (`impl:investigator`) has one real
assistant message, then a `compact_boundary` system entry, then the kill
sentinel about 7 minutes later.

Expected numbers: the subagent's `outcome` is `"killed"`; `interrupt.cause`
is `"unattributed"` (the main transcript has no TaskStop and no kill
sentinel of its own, so clauses 1 and 2 of docs/DESIGN.md 4.4 never match);
`interrupt.preceding` is `None` (`meaningful_event_before` stops the walk at
the `compact_boundary` event rather than reaching the assistant message
before it); `interrupt.silence_seconds` is `None` for the same reason.

### `session_parent_stop`

Session kind. The main transcript dispatches `impl:fixer` and then calls
`TaskStop` naming the child's agent id (`acb00000000000001`) 5s before the
child's own kill sentinel.

Expected numbers: `interrupt.cause` is `"parent_stop"`, severity info
(docs/DESIGN.md 4.4 clause 1); `interrupt.cause_evidence` points at the
main transcript's `TaskStop` line.

### `session_human_interrupt`

Session kind. The main transcript's own last event is the kill sentinel
(the owner pressed Escape on the whole session); the child's kill sentinel
lands 5s later, well inside the 30s window and with no `TaskStop` anywhere.

Expected numbers: `interrupt.cause` is `"human"`, severity info (docs/
DESIGN.md 4.4 clause 2); `interrupt.cause_evidence` points at the main
transcript's own kill-sentinel line. The main transcript's own `outcome` is
also `"killed"`, but its interrupt is never attributed (docs/DESIGN.md
4.3's documented limit): `_attribute_interrupts` only touches
`run.attempts`, never `run.main`.

### `session_unattributed_long`

Session kind. The main transcript dispatches `impl:investigator` and never
does anything else (no `TaskStop`, no kill sentinel); the child goes silent
for exactly 400s before its own kill sentinel.

Expected numbers: `interrupt.cause` is `"unattributed"`, `silence_seconds`
400.0, severity major (docs/DESIGN.md 4.4 clause 4, silence at or above
300s), title mentions "consistent with a runtime kill", rule_id
`R-MONITOR-RETRIES`.

### `session_unattributed_short`

Session kind. Same shape as `session_unattributed_long`, but the child goes
silent for only 20s before its own kill sentinel.

Expected numbers: `interrupt.cause` is `"unattributed"`, `silence_seconds`
20.0, severity info (below the 300s major threshold), rule_id `None`.

### `evidence_noise`

Workflow kind (flat, like `clean_run`). One logical agent, one killed
attempt (no retry, so docs/DESIGN.md 4.4 clause 3 never applies and cause
falls through to unattributed): a real assistant text line, then a
`thinking` block, then a user line whose entire text is a
`<system-reminder>...</system-reminder>` span (empty once reader.py strips
it), then the kill sentinel.

Expected numbers: `interrupt.preceding` is the real assistant text line
(`meaningful_event_before` skips the `thinking` block and the now-empty
system-reminder-only line), not either of the two noise events directly
above the sentinel; `silence_seconds` 42.0.

### `incomplete_journal`

Workflow kind (flat). `journal.jsonl` has one line that is not valid JSON
at all, between an otherwise normal `launched`/`started`/`result` sequence
for one completed attempt.

Expected numbers: `run.status` is `"INCOMPLETE"` (docs/DESIGN.md 4.6: the
journal could not be parsed cleanly); the text and JSON reports carry the
` (INCOMPLETE)` verdict suffix; `postmortem run` on this fixture exits 4
(INCOMPLETE without FAILED).

### `hook_session`

A full `<root>/<slug>/<session>[.jsonl]` tree (docs/DESIGN.md 6, 7.2), used
by `tests/test_hooks_cli.py` and `tests/test_hooks_benchmark.py` instead of
a flat run directory: `-home-user-project/00000000-0000-4000-8000-
000000000000.jsonl` is the main session transcript, and
`-home-user-project/00000000-0000-4000-8000-000000000000/subagents/` holds
three units.

- `subagents/workflows/wf_00000000-710`: a finished workflow unit. Key
  `v2:impl-alpha` starts twice (the first attempt is killed, the second
  completes) and the journal carries a `result` for it, so `_workflow_finished`
  is true on the very first check. The retry makes `silent_retry` fire at
  major, so this unit notifies (docs/DESIGN.md 7.2's default `major`
  threshold) the first time it is checked.
- `subagents/workflows/wf_00000000-720`: a running workflow unit. Key
  `v2:impl-beta` starts once; the journal carries no matching `result`, so
  `_workflow_finished` is always false -- this unit stays `running` (and,
  after three checks with an unchanged fingerprint, `unknown`) and its
  `reported` list stays empty however many times it is checked.
- `subagents/agent-ae300000000000001.jsonl`: a finished subagent unit
  (an `Agent`-tool child of the main session). Its own last line is an
  assistant entry with a text block only, no `tool_use` -- docs/DESIGN.md
  7.2's D2 says that alone is never evidence of anything, since it is also
  the normal shape of an agent still waiting for its next turn. What makes
  this unit `finished` is the main transcript's own `Agent` `tool_result`
  (line 5), which carries `"agentId":"ae300000000000001"` -- the parent's
  own record that this child already returned control to it -- so the unit
  finishes on the very first check that sees it, no fingerprint stability
  required. It carries no marker, so its own findings are empty and it
  never notifies.

The main transcript's `Workflow` `tool_result` (line 3) says "Run ID:
wf_00000000-710" so a `workflow-launched` payload built from it exercises
the regex fallback in `hooks_cli._extract_run_id`, alongside a synthetic
`tool_response` dict for the structured-field path.

### `journal_state_resolved`

Workflow kind (flat). Seven keys exercising the journal state machine of
docs/DESIGN.md section 3: `started, result`; `started, failed`; `started,
failed, started, result`; `started, started, result` where the first
transcript ends in the kill sentinel; `started, started, result` where it
does not; `started, failed, failed`; `started, failed` where the transcript
also ends in the kill sentinel.

Expected numbers: outcomes `completed`, `failed`, `failed` then `completed`,
`killed` then `completed`, `superseded` then `completed`, `failed`, `failed`
(with `interrupt` set); `run.finished` is true; `run.status` is
`"INCOMPLETE"` because of the superseded attempt and the second `failed`
for an attempt already closed.

Its four `failed` attempts double as `attempt_failed` detector fixtures
(none of their transcripts carries a cause marker, so all four come back
`unattributed`): `v2:jsm-failed` and `v2:jsm-double-fail` are terminal
(severity major), `v2:jsm-retry-after-fail`'s first attempt is retried and
the key later completes (severity minor), and `v2:jsm-failed-with-sentinel`
gives cause `interrupted` instead, since its last transcript entry is the
kill sentinel rather than plain text. See `attempt_failed_causes` below for
the four marker-based causes and the decoy and non-triggering cases.

### `attempt_failed_causes`

Workflow kind (flat). Six keys exercising the `attempt_failed` detector's
cause attribution from a `failed` attempt's own last transcript entry:

- `v2:af-usage-session`: last line `You've hit your session limit ·
  resets 2pm`. Expected cause `usage_limit_session`, `retry_now` `no`,
  `reset_time` `2pm`, severity major (no later attempt), rule_id
  `R-USAGE-LIMIT`.
- `v2:af-usage-weekly`: last line `You've hit your weekly limit ·
  resets Monday 9am`. Expected cause `usage_limit_weekly`, `retry_now` `no`,
  `reset_time` `Monday 9am`.
- `v2:af-machine-sleep`: last line `API Error: Your computer went to sleep`.
  Expected cause `machine_sleep`, `retry_now` `yes`, `reset_time` `None`, no
  rule_id.
- `v2:af-api-error`: last line `API Error: Server error mid-response`.
  Expected cause `api_error`, `retry_now` `yes`, `reset_time` `None`, no
  rule_id.
- `v2:af-decoy`: the first user message (the prompt) mentions "API Error:
  Server error mid-response" but the attempt's own last line is plain text
  with no marker. Expected cause `unattributed` (the detector never reads
  further back than the last entry), title says "cause not recorded in the
  transcript".
- `v2:af-marker-not-last`: `started, result` (completed, not failed). The
  first assistant line carries the session-limit marker but a second,
  ordinary assistant line follows it as the actual last entry. Expected:
  `attempt_failed` never fires for this key at all (the outcome gate alone
  already excludes it; the marker's mid-transcript position is not what
  keeps it out).

Every fixture that does trigger has exactly one attempt for its key, so
severity is always major here; the two severities of the unattributed cause
(major for a terminal failure, minor once a later attempt of the same key
completed) are exercised by `journal_state_resolved`'s own `v2:jsm-failed`
and `v2:jsm-retry-after-fail` keys instead (see below), which already have
that shape from the journal-state-machine fixture and need no marker text of
their own. `journal_state_resolved`'s `v2:jsm-failed-with-sentinel` key is
also the `interrupted` cause fixture: its last transcript entry is the kill
sentinel itself, so `attempt_failed` reports cause `interrupted`, `retry_now`
`unknown`, independently of the pre-existing `Attempt.interrupt` evidence the
journal state machine tests already check on that same key.

### `journal_state_unresolved`

Workflow kind (flat). One key: `started`, then a `result` whose `agentId`
names a different agent. The result closes nothing.

Expected numbers: one attempt, `run.finished` false, `run.status`
`"INCOMPLETE"`, one diagnostic naming the agent id mismatch.

### `journal_state_orphan_failed`

Workflow kind (flat). A `failed` event with no `started` before it.

Expected numbers: zero attempts, `run.finished` false, `run.status`
`"INCOMPLETE"`, one diagnostic naming the orphan event.

### `claim_contradiction`

Workflow kind (flat). Eleven keys exercising `Attempt.reported` (docs/
DESIGN.md section 3/4.1) and the `claim_contradiction` detector. Unlike
every other case here, none of its eleven attempts has an `agent-<id>.jsonl`
transcript at all: this detector's evidence is the `journal.jsonl` line of
the `result` event itself, never a transcript excerpt, so no transcript is
needed to exercise it (only `journal.jsonl` exists on disk).

- `v2:cc-tests-failed`: `result` `{"ok": true, "tests_passed": 8,
  "tests_failed": 2}` (line 3). One clause, `tests_failed`.
- `v2:cc-suite-failed`: `{"suite_passed": 4, "suite_failed": 1}` (line 5).
  One clause, `suite_failed`.
- `v2:cc-ok-false`: `{"ok": false, "status": "blocked"}` (line 7). One
  clause, `ok_false`.
- `v2:cc-ready-false`: `{"ready": false}` (line 9). One clause,
  `ready_false`.
- `v2:cc-blockers`: `{"blockers": [3 strings]}` (line 11). One clause,
  `blockers` (`blockers_count` 3; the blocker text itself is never stored,
  only the count).
- `v2:cc-report-missing`: `{"report_path":
  "/tmp/agent-postmortem-fixture/claim/missing-report.md"}` (line 13), a
  path never created. One clause, `report_path_missing`.
- `v2:cc-two-clauses`: `{"ok": false, "tests_passed": 3, "tests_failed": 1}`
  (line 15). Two clauses in one finding, `tests_failed` then `ok_false`.
- `v2:cc-clean-dict`: `{"ok": true, "tests_passed": 10, "tests_failed": 0,
  "report_path": "/tmp/agent-postmortem-fixture/claim/present-report.md",
  "note": "looks fine", "summary": "all good"}` (line 17). `present-
  report.md` is created by a fixture in `tests/test_claim_contradiction.py`
  before each test and removed after (mirroring `missing_artifact`'s own
  convention), so this key never fires: `tests_failed` is 0, `ok` is true,
  and the path exists. `note` and `summary` are not on the claim allowlist
  and appear in `reported.keys` but never in `reported.claims`.
- `v2:cc-text-result`: `result` is the string `"All tests pass and the
  report has been written."` (line 19). `reported` is `{"type": "text",
  "text_length": 47}`; the string itself is never stored.
- `v2:cc-commits-mixed`: `{"commits": ["abc1234", "not-a-hash!",
  "deadbeefcafe0123"]}` (line 21). `commits_count` is 3 (the claimed list's
  own length); `commits` keeps only the two entries that match a commit-id
  shape (`^[0-9a-f]{7,40}$`), dropping `not-a-hash!`. Commits alone are
  never a contradiction clause (the "commit id not in the repo" check is
  deferred to M2), so this key never fires either.
- `v2:cc-failed-attempt`: `started` then `failed`, no `result` at all.
  `reported` is `None`; `claim_contradiction` never fires on a `failed`
  attempt regardless of what its transcript might say.

Expected numbers: attempts 11, `claim_contradiction` findings 7 (every key
above except `clean-dict`, `text-result`, `commits-mixed`, and
`failed-attempt`).

### `attempt_facts`

Workflow kind (one run, `wf_00000000-af0`). Twelve keys, one fact each
(docs/DESIGN.md 4.1/4.2's v0.2 reader changes, unit M2a: `ToolCall` and
`AttemptFacts`). File targets use `/tmp/agent-postmortem-fixture/
attempt_facts/...` rather than the usual `/home/user/project` cwd, since a
path under `/tmp` collapses to `.../<basename>` under redaction regardless
of the machine's own home directory, while `/home/user/...` only collapses
when the redactor's `home` is literally set to it (see `claim_contradiction`
for the same convention).

- `v2:af-edit-ok` (`af2...001`): one Edit, result not an error.
  `facts.paths_written` is the edited path; `facts.
  seconds_since_last_tool_at_end` is 25.0 (a final assistant text line
  lands 25s after the Edit's own `tool_use` line).
- `v2:af-write-error` (`af2...002`): one Write, `tool_result.is_error`
  true. `facts.paths_written` is `[]`.
- `v2:af-killed` (`af2...003`): one Edit with no `tool_result` ever seen;
  the journal never closes this key (only `started`), so the outcome falls
  back to the transcript alone, which ends in the kill sentinel: `killed`.
  `facts.in_flight` names the Edit's path and its `tool_use` line number.
- `v2:af-completed-inflight` (`af2...004`): the same shape (an Edit with no
  result), but the journal closes this attempt with a `result` event:
  `completed`. `facts.in_flight` is `[]` regardless -- a completed
  attempt's verdict always wins over a stray unresolved call.
- `v2:af-commit` (`af2...005`): two Bash calls. The first's command
  contains `git commit`; its result `"[main abc1234] synthetic change"`
  yields `commit_ids ("abc1234",)`. The second's command is `echo done`;
  its result contains `"[x 1234567]"`, which is never scanned for a commit
  id because the command itself is not a commit. `facts.commits` is
  `["abc1234"]`.
- `v2:af-backgrounded` (`af2...006`): one Bash call whose result carries
  the `BASH_BACKGROUNDED` marker. `completion` is `backgrounded`;
  `facts.background_unfinished` is 1.
- `v2:af-longpath` (`af2...008`): two Edit calls. The first's `file_path`
  is 600 chars (past the 400-char excerpt limit, under the 1024-char
  `ToolCall.path` bound): captured whole. The second's is 1100 chars (over
  the bound): `path` is `None`. `facts.paths_written` holds only the first.
- `v2:af-cwd` (`af2...009`): two Bash calls with `cwd`
  `/home/user/project/a/../b` and `/home/user/project/./c`. `ToolCall.cwd`
  and `facts.cwds` hold the `os.path.normpath`-normalized forms,
  `/home/user/project/b` and `/home/user/project/c`.
- `v2:af-no-ts` (`af2...010`): a Bash call, then an Edit call whose own
  transcript entry carries no `timestamp` field at all. The attempt's
  `last_ts` still comes from a later `tool_result` event, so
  `facts.seconds_since_last_tool_at_end` is `None` specifically because the
  *last tool call's own* timestamp is missing, not because the attempt has
  no `last_ts`.
- `v2:af-parallel` (`af2...011`): one assistant entry with two `tool_use`
  blocks (`toolu_p1` then `toolu_p2`), followed by their `tool_result`s in
  the opposite order (`toolu_p2`'s result on the earlier line). Proves
  pairing by id: `tool_calls[0]` (`toolu_p1`) gets the later result line,
  `tool_calls[1]` (`toolu_p2`) the earlier one.
- `v2:af-missing` (`af2...012`): `started` then `failed`; deliberately no
  `agent-af2...012.jsonl` file on disk at all. `Attempt.facts` is `None`
  (never `FACTS_UNKNOWN` -- that value is for a transcript that exists but
  could not be read, not one that never existed).

A Bash command over the 2048-char `ToolCall.command` bound, and a
transcript that exists but cannot be opened (every fact `FACTS_UNKNOWN`),
are not fixtures here: both are built with `tmp_path` directly in
`tests/test_attempt_facts.py` instead, since a 2048+-char line would blow
past this directory's 2000-char fixture line limit (docs/DESIGN.md 5.4) and
a chmod-based unreadable file cannot be represented as tracked fixture
content at all.
