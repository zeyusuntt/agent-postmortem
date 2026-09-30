"""Exact strings the Claude Code runtime writes into transcripts.

This is the ONE place a marker literal may live. Detectors and the reader
import from here. Each constant carries a comment citing the fixture case
under tests/fixtures/runs that exercises the string (or, when no case on
disk carries it, the test that builds the event in memory) and the Claude
Code version range it was observed on. No comment here names a real run id;
DESIGN.md 3 requires that every marker comment cite a fixture case, or, when
none carries the string yet, the in-memory test that does, instead.
"""

# A Workflow subagent transcript has no human in it. When the runtime kills an
# attempt it appends a user-role message starting with this prefix (observed
# as "[Request interrupted by user]" and "[Request interrupted by user for
# tool use]" on Claude Code 2.1.251 to 2.1.280). The bare form is what
# agent_postmortem/demo/wf_00000000-d30 carries.
KILL_SENTINEL_PREFIX = "[Request interrupted by user"

# Both observed full forms of the kill sentinel, on Claude Code 2.1.251 to
# 2.1.280. "for tool use" appears when the kill lands while a tool_use block
# is in flight; the bare form appears otherwise and is the one
# agent_postmortem/demo/wf_00000000-d30 carries (agent-ac200000000000001.jsonl
# and agent-ac200000000000003.jsonl). No fixture on disk carries the "for
# tool use" form yet; it is kept because the runtime emits it, not because a
# test currently exercises it.
KILL_SENTINEL_VARIANTS = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)

# A Bash tool_result reporting that the command outlived its timeout and was
# detached to run in the background instead of being killed. Observed as
# "Command did not complete within its 30s timeout and was moved to the
# background (ID: ...)." on Claude Code 2.1.251 to 2.1.280; carried by
# tests/fixtures/runs/bash_backgrounded_timeout (agent-ac300000000000001.jsonl,
# line 3). The timeout value and background id vary per call, so match on the
# surrounding substrings only.
BASH_BACKGROUNDED = (
    "did not complete within its",
    "moved to the background",
)

# A Bash tool_result reporting a hard timeout kill. Observed as "Exit code
# 143\nCommand timed out after 10m 0s" on Claude Code 2.1.251 to 2.1.280;
# carried by tests/fixtures/runs/bash_backgrounded_timeout
# (agent-ac300000000000001.jsonl, line 5).
BASH_TIMEOUT = (
    "Exit code 143",
    "Command timed out after",
)

# Exit code the runtime uses for a Bash hard timeout kill (SIGTERM, 128+15),
# on Claude Code 2.1.251 to 2.1.280; carried as text by
# tests/fixtures/runs/reader_minimal
# (-home-user-project/00000000-0000-4000-8000-000000000000/subagents/workflows/
# wf_b0000000-002/agent-a1111111111111111.jsonl, line 3), which
# reader._parse_exit_code turns into the numeric Event.exit_code
# test_reader.py::test_tool_result_exit_code_and_is_error_parsed checks.
BASH_TIMEOUT_EXIT_CODE = 143

# A tool_result wrapping a tool-input validation failure. Observed as
# "InputValidationError: Read was called with input that could not be parsed
# as JSON." on Claude Code 2.1.251 to 2.1.280, and the same prefix for other
# tools. No fixture on disk carries this marker; it is exercised by the
# in-memory run tests/test_detectors_tooling.py builds for tool_json_error.
TOOL_INPUT_INVALID = ("InputValidationError",)

# A StructuredOutput tool_result reporting the payload did not satisfy the
# schema. Two observed forms on Claude Code 2.1.251 to 2.1.280: a JSON Schema
# validation failure, "Output does not match required schema: root: must have
# required property '...'", carried by tests/fixtures/runs/structured_output_corrupt
# (agent-ac400000000000001.jsonl, line 3); and an unparsable payload,
# "InputValidationError: StructuredOutput was called with input that could
# not be parsed as JSON.", which no fixture on disk carries.
STRUCTURED_OUTPUT_MISMATCH = (
    "does not match required schema",
    "could not be parsed as JSON",
)

# A sandbox write denial from the Codex plugin. Two observed forms on Claude
# Code 2.1.251 to 2.1.280: the verdict token an agent is instructed to emit
# when a write is rejected, "WRITE_DENIED: patch rejected: writing outside of
# the project; rejected by user approval settings", carried by
# tests/fixtures/runs/codex_write_denied (agent-ac500000000000001.jsonl, line
# 3); and prose narrating that a write was refused by the sandbox, for
# example "was rejected by the read-only sandbox" and "was blocked by the
# read-only sandbox", which no fixture on disk carries.
SANDBOX_WRITE_DENIED = (
    "WRITE_DENIED",
    "read-only sandbox",
)

# The rote shell guard's block message: "Security: <command> blocked in
# workspace" (observed as "Security: node blocked in workspace" on Claude
# Code 2.1.251 to 2.1.280); carried by tests/fixtures/runs/codex_write_denied
# (agent-ac500000000000002.jsonl, line 3).
SHELL_GUARD_BLOCK_REGEX = r"Security: \S+ blocked in workspace"

# The model classifier auto mode relies on to judge Bash safety went down.
# Observed as "claude-sonnet-5[1m] is temporarily unavailable (timed out), so
# auto mode cannot determine the safety of Bash right now." on Claude Code
# 2.1.251 to 2.1.280. No fixture on disk carries this marker; it is exercised
# by the in-memory run tests/test_detectors_tooling.py builds for
# classifier_unavailable. Both fragments are required: "temporarily
# unavailable" alone is generic enough that an unrelated tool or service
# outage would match it too, mislabeling it as a Bash-safety classifier
# outage.
CLASSIFIER_UNAVAILABLE = ("temporarily unavailable", "cannot determine the safety of Bash")

# A Bash tool_result showing the command ran outside any git repository.
# Observed as "fatal: not a git repository (or any of the parent
# directories): .git" on Claude Code 2.1.251 to 2.1.280. No fixture on disk
# carries this marker; it is exercised by the in-memory run
# tests/test_detectors_tooling.py builds for git_cwd_mismatch.
GIT_NOT_REPO = ("fatal: not a git repository",)

# A workflow attempt the journal closed with a `failed` event has, as the
# last entry of its own transcript, an assistant message the runtime itself
# appends -- never something the agent wrote. Observed on Claude Code 2.1.251
# to 2.1.280, 13 of 21 real `failed` events on this machine: "You've hit your
# session limit · resets 2pm" ("·" is U+00B7 MIDDLE DOT; the time
# after "resets " varies per case). Carried by
# tests/fixtures/runs/attempt_failed_causes (agent-af100000000000001.jsonl,
# last line). detectors/attempt_failed.py treats everything up to and
# including "resets " as this fixed prefix and the remainder of the line as
# the reset time.
USAGE_LIMIT_SESSION = ("You've hit your session limit · resets ",)

# Same shape as USAGE_LIMIT_SESSION for the weekly limit instead of the
# session one. Observed on Claude Code 2.1.251 to 2.1.280, 2 of 21 real
# `failed` events. Carried by tests/fixtures/runs/attempt_failed_causes
# (agent-af100000000000002.jsonl, last line).
USAGE_LIMIT_WEEKLY = ("You've hit your weekly limit · resets ",)

# A workflow attempt's last transcript entry stating the machine slept
# mid-request, appended by the runtime the same way the two usage-limit
# markers above are. Observed as "API Error: Your computer went to sleep" on
# Claude Code 2.1.251 to 2.1.280, 4 of 21 real `failed` events. Carried by
# tests/fixtures/runs/attempt_failed_causes (agent-af100000000000003.jsonl,
# last line). Unlike the usage limits, no reset time follows.
MACHINE_SLEEP = ("API Error: Your computer went to sleep",)

# A workflow attempt's last transcript entry stating the runtime's own API
# call failed mid-response. Observed as "API Error: Server error
# mid-response" on Claude Code 2.1.251 to 2.1.280, 1 of 21 real `failed`
# events. Carried by tests/fixtures/runs/attempt_failed_causes
# (agent-af100000000000004.jsonl, last line).
API_ERROR = ("API Error: Server error mid-response",)
