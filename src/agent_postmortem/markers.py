"""Exact strings the Claude Code runtime writes into transcripts.

This is the ONE place a marker literal may live. Detectors and the reader
import from here. Each constant carries a comment citing the fixture case
under tests/fixtures/runs that exercises the string (or, when no case on
disk carries it, the test that builds the event in memory) and the Claude
Code version range it was observed on. No comment here names a real run id;
DESIGN.md 3 requires that every marker comment cite a fixture case, or, when
none carries the string yet, the in-memory test that does, instead.

docs/DESIGN.md 4.13.2 (v0.3, N1): REGISTRY below is the marker provenance
table -- one MarkerSpec per canonical id, with its producer, the permitted
shape(s) reader.py evaluates it against, the candidacy fragments (the
constants above, or a dedicated candidate function for the one regex-based
id), and the anchored predicate. reader.py builds Attempt.marker_hits from
this; detectors import canonical ids from here, never fragments, and never
read Event.markers (kept only for compatibility, 4.13.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

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
# "<tool_use_error>InputValidationError: Read was called with input that
# could not be parsed as JSON." on Claude Code 2.1.251 to 2.1.280, and the
# same wrapper/prefix for other tools (docs/DESIGN.md 4.13.2's registry
# anchors on the full "<tool_use_error>InputValidationError:" prefix, on
# line1). No fixture on disk carries this marker; it is exercised by the
# in-memory run tests/test_detectors_tooling.py builds for tool_json_error.
TOOL_INPUT_INVALID = ("InputValidationError",)

# A StructuredOutput tool_result reporting the payload did not satisfy the
# schema. Two observed forms on Claude Code 2.1.251 to 2.1.280: a JSON Schema
# validation failure, "Output does not match required schema: root: must have
# required property '...'", carried by tests/fixtures/runs/structured_output_corrupt
# (agent-ac400000000000001.jsonl, line 3); and an unparsable payload,
# "<tool_use_error>InputValidationError: StructuredOutput was called with
# input that could not be parsed as JSON." (docs/DESIGN.md 4.13.2's registry
# anchors this form on the same wrapper/prefix as tool_input_invalid), which
# no fixture on disk carries.
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

# The runtime's own replies when a tool call is refused (G7, v0.3.1; Claude Code 2.1.251 to
# 2.1.280). Each is the whole text of an `is_error` tool result, or its start: the permission-rule
# sentence carries the denied command, so only its fixed head and tail are fixed here. Carried by
# the in-memory runs tests/test_g_denied.py builds.
PERMISSION_DENIED_AUTO_MODE = (
    "Permission for this action was denied by the Claude Code auto mode classifier."
)
PERMISSION_DENIED_USER = "The user doesn't want to proceed with this tool use."
PERMISSION_DENIED_SAFETY_CHECK = (
    "Permission for this command was denied by a built-in Claude Code safety check, "
    "not by the user."
)
PERMISSION_DENIED_RULE_HEAD = "Permission to use "
PERMISSION_DENIED_RULE_TAIL = " has been denied."
# The sentence may span lines (a multi-line command): the first ` has been denied.` that ends a line
# closes it (G-B1, GB-3). The tool name is `[A-Za-z0-9_]+` and never empty.
_PERMISSION_RULE_RE = re.compile(
    r"Permission to use [A-Za-z0-9_]+ with command .*? has been denied\.[ \t\r]*(?:\n|\Z)",
    re.DOTALL,
)

DENIAL_AUTO_MODE = "auto_mode"
DENIAL_USER = "user"
DENIAL_PERMISSION_RULE = "permission_rule"
DENIAL_SAFETY_CHECK = "safety_check"


def denial_kind(text: str) -> str | None:
    """Which denial a tool result's text is (docs/DESIGN.md 4.13.2, G7), or None. `text` has had
    its leading whitespace stripped. The text must START with exactly one of the four sentences;
    the permission-rule sentence may span lines and ends at the first ` has been denied.` that ends
    a line (docs/DESIGN.md 4.13.2). No wrapper is accepted."""
    if text.startswith(PERMISSION_DENIED_AUTO_MODE):
        return DENIAL_AUTO_MODE
    if text.startswith(PERMISSION_DENIED_USER):
        return DENIAL_USER
    if text.startswith(PERMISSION_DENIED_SAFETY_CHECK):
        return DENIAL_SAFETY_CHECK
    if text.startswith(PERMISSION_DENIED_RULE_HEAD):
        if _PERMISSION_RULE_RE.match(text):
            return DENIAL_PERMISSION_RULE
    return None


def denial_prefix_kind(text: str) -> str | None:
    """The kind by the start of a (possibly truncated) text only: for an event text already
    anchored by `denial_kind` on the full block."""
    text = text.lstrip(" \t\r\n")
    for kind, sentence in (
        (DENIAL_AUTO_MODE, PERMISSION_DENIED_AUTO_MODE),
        (DENIAL_USER, PERMISSION_DENIED_USER),
        (DENIAL_SAFETY_CHECK, PERMISSION_DENIED_SAFETY_CHECK),
        (DENIAL_PERMISSION_RULE, PERMISSION_DENIED_RULE_HEAD),
    ):
        if text.startswith(sentence):
            return kind
    return None


def _candidate_permission_denied(text: str) -> bool:
    return (
        "denied by the Claude Code auto mode classifier" in text
        or PERMISSION_DENIED_USER in text
        or "denied by a built-in Claude Code safety check" in text
        or (PERMISSION_DENIED_RULE_HEAD in text and PERMISSION_DENIED_RULE_TAIL in text)
    )


def _predicate_permission_denied(text: str, shape: str) -> bool:
    return denial_kind(text) is not None


# === Marker provenance registry (docs/DESIGN.md 4.13.2, v0.3 N1) ===========
#
# Shape tags: which kind of block reader.py may evaluate a given id against.
# A block whose shape is not in an id's `shapes` is never even a candidate
# for that id (docs/DESIGN.md 4.13.2: "A shape that is not listed for an id
# is never a hit for it").
SHAPE_USER_ENTRY = "user_entry"  # the whole flattened text of a user entry
SHAPE_TOOL_RESULT_BASH = "tool_result_bash"  # a tool_result paired to Bash
SHAPE_TOOL_RESULT_BASH_CODEX = "tool_result_bash_codex"  # ...whose command
# runs codex as a program (docs/DESIGN.md 4.3)
SHAPE_TOOL_RESULT_ANY_EXCEPT_STRUCTURED_OUTPUT = "tool_result_any_except_structured_output"
SHAPE_TOOL_RESULT_STRUCTURED_OUTPUT = "tool_result_structured_output"
SHAPE_ASSISTANT_CODEX_ATTEMPT = "assistant_codex_attempt"  # an assistant
# entry's whole text, only when the attempt also has a Bash call running
# codex as a program somewhere
SHAPE_TOOL_RESULT_ERROR = "tool_result_error"  # a tool_result block with is_error true,
# whatever tool it is paired to (G7)
SHAPE_FINAL_ASSISTANT_ENTRY = "final_assistant_entry"  # the attempt's own
# last transcript entry, only when that entry is an assistant entry


def _lines(text: str) -> tuple[str, str]:
    """line1, line2 of flattened text (docs/DESIGN.md 4.13.2): the text up
    to the first newline, and the line after it (or '' when there is none)."""
    parts = text.split("\n", 2)
    line1 = parts[0] if parts else ""
    line2 = parts[1] if len(parts) > 1 else ""
    return line1, line2


_EXIT_CODE_LINE_RE = re.compile(r"Exit code [0-9]+")


def _predicate_kill_sentinel(text: str, shape: str) -> bool:
    return text.rstrip(" \t\r\n") in KILL_SENTINEL_VARIANTS


def _predicate_bash_backgrounded(text: str, shape: str) -> bool:
    line1, _line2 = _lines(text)
    return line1.startswith("Command did not complete within its ") and (
        "moved to the background" in line1
    )


def _predicate_bash_timeout(text: str, shape: str) -> bool:
    line1, line2 = _lines(text)
    return line1 == "Exit code 143" and line2.startswith("Command timed out after")


def _predicate_tool_input_invalid(text: str, shape: str) -> bool:
    line1, _line2 = _lines(text)
    return line1.startswith("<tool_use_error>InputValidationError:")


def _predicate_structured_output_mismatch(text: str, shape: str) -> bool:
    line1, _line2 = _lines(text)
    if line1.startswith("Output does not match required schema"):
        return True
    return line1.startswith("<tool_use_error>InputValidationError:") and (
        "could not be parsed as JSON" in line1
    )


def _predicate_classifier_unavailable(text: str, shape: str) -> bool:
    line1, _line2 = _lines(text)
    return "is temporarily unavailable" in line1 and "cannot determine the safety of Bash" in line1


def _predicate_git_not_repo(text: str, shape: str) -> bool:
    line1, line2 = _lines(text)
    if line1.startswith("fatal: not a git repository"):
        return True
    return bool(_EXIT_CODE_LINE_RE.fullmatch(line1)) and line2.startswith(
        "fatal: not a git repository"
    )


def _predicate_sandbox_write_denied(text: str, shape: str) -> bool:
    lines = text.split("\n")
    if any(
        line.startswith("[codex]") and ("WRITE_DENIED" in line or "read-only sandbox" in line)
        for line in lines
    ):
        return True
    if shape == SHAPE_TOOL_RESULT_BASH_CODEX:
        line1 = lines[0] if lines else ""
        return line1.startswith("WRITE_DENIED")
    return False


_SHELL_GUARD_COMPILED_RE = re.compile(SHELL_GUARD_BLOCK_REGEX)


def _predicate_shell_guard_block(text: str, shape: str) -> bool:
    return any(_SHELL_GUARD_COMPILED_RE.match(line) for line in text.split("\n"))


def _candidate_shell_guard_block(text: str) -> bool:
    return bool(_SHELL_GUARD_COMPILED_RE.search(text))


def _make_startswith_predicate(fragment: str) -> Callable[[str, str], bool]:
    def _predicate(text: str, shape: str, _fragment: str = fragment) -> bool:
        return text.startswith(_fragment)

    return _predicate


# codex run as a program: the first word of a command segment (start of the
# command, or after ";", "&", "|", "(", or a newline), optionally
# path-qualified, followed by whitespace or end; or the codex-companion
# script named anywhere. Shared by the reader (to decide the
# tool_result_bash_codex / assistant_codex_attempt shapes) and, previously,
# detectors/codex_write_denied.py's own copy (docs/DESIGN.md 4.3).
_CODEX_PROGRAM_RE = re.compile(r"(^|[;&|(\n])\s*(\S*/)?codex(\s|$)|codex-companion\.mjs")


def runs_codex_program(command: str | None) -> bool:
    """Whether a Bash command runs codex as a program (docs/DESIGN.md 4.3):
    the first word of a command segment names codex (optionally
    path-qualified), or the command names codex-companion.mjs anywhere. A
    command that only mentions "codex" inside data (a heredoc body, a file
    name) does not match."""
    if not isinstance(command, str) or not command:
        return False
    return bool(_CODEX_PROGRAM_RE.search(command.lower()))


@dataclass(frozen=True)
class MarkerSpec:
    """One row of the marker provenance registry (docs/DESIGN.md 4.13.2)."""

    id: str
    producer: str
    shapes: tuple[str, ...]
    fragments: tuple[str, ...]  # candidacy fragments; () when is_candidate is set
    predicate: Callable[[str, str], bool]  # (flattened_text, matched_shape) -> anchored?
    is_candidate: Callable[[str], bool] | None = None

    def candidate(self, text: str) -> bool:
        if self.is_candidate is not None:
            return self.is_candidate(text)
        return any(fragment in text for fragment in self.fragments)


REGISTRY: dict[str, MarkerSpec] = {
    spec.id: spec
    for spec in (
        MarkerSpec(
            id="kill_sentinel",
            producer="harness_user",
            shapes=(SHAPE_USER_ENTRY,),
            fragments=(KILL_SENTINEL_PREFIX,),
            predicate=_predicate_kill_sentinel,
        ),
        MarkerSpec(
            id="bash_backgrounded",
            producer="harness_tool_notice",
            shapes=(SHAPE_TOOL_RESULT_BASH,),
            fragments=BASH_BACKGROUNDED,
            predicate=_predicate_bash_backgrounded,
        ),
        MarkerSpec(
            id="bash_timeout",
            producer="harness_tool_notice",
            shapes=(SHAPE_TOOL_RESULT_BASH,),
            fragments=BASH_TIMEOUT,
            predicate=_predicate_bash_timeout,
        ),
        MarkerSpec(
            id="tool_input_invalid",
            producer="harness_tool_notice",
            shapes=(SHAPE_TOOL_RESULT_ANY_EXCEPT_STRUCTURED_OUTPUT,),
            fragments=TOOL_INPUT_INVALID,
            predicate=_predicate_tool_input_invalid,
        ),
        MarkerSpec(
            id="structured_output_mismatch",
            producer="harness_tool_notice",
            shapes=(SHAPE_TOOL_RESULT_STRUCTURED_OUTPUT,),
            fragments=STRUCTURED_OUTPUT_MISMATCH,
            predicate=_predicate_structured_output_mismatch,
        ),
        MarkerSpec(
            id="classifier_unavailable",
            producer="harness_tool_notice",
            shapes=(SHAPE_TOOL_RESULT_BASH,),
            fragments=CLASSIFIER_UNAVAILABLE,
            predicate=_predicate_classifier_unavailable,
        ),
        MarkerSpec(
            id="git_not_repo",
            producer="command_output",
            shapes=(SHAPE_TOOL_RESULT_BASH,),
            fragments=GIT_NOT_REPO,
            predicate=_predicate_git_not_repo,
        ),
        MarkerSpec(
            id="sandbox_write_denied",
            producer="codex_companion",
            shapes=(SHAPE_TOOL_RESULT_BASH_CODEX, SHAPE_ASSISTANT_CODEX_ATTEMPT),
            fragments=SANDBOX_WRITE_DENIED,
            predicate=_predicate_sandbox_write_denied,
        ),
        MarkerSpec(
            id="shell_guard_block",
            producer="codex_companion",
            shapes=(SHAPE_TOOL_RESULT_BASH_CODEX,),
            fragments=(),
            is_candidate=_candidate_shell_guard_block,
            predicate=_predicate_shell_guard_block,
        ),
        MarkerSpec(
            id="permission_denied",
            producer="harness_tool_notice",
            shapes=(SHAPE_TOOL_RESULT_ERROR,),
            fragments=(),
            is_candidate=_candidate_permission_denied,
            predicate=_predicate_permission_denied,
        ),
        MarkerSpec(
            id="usage_limit_session",
            producer="runtime_assistant",
            shapes=(SHAPE_FINAL_ASSISTANT_ENTRY,),
            fragments=USAGE_LIMIT_SESSION,
            predicate=_make_startswith_predicate(USAGE_LIMIT_SESSION[0]),
        ),
        MarkerSpec(
            id="usage_limit_weekly",
            producer="runtime_assistant",
            shapes=(SHAPE_FINAL_ASSISTANT_ENTRY,),
            fragments=USAGE_LIMIT_WEEKLY,
            predicate=_make_startswith_predicate(USAGE_LIMIT_WEEKLY[0]),
        ),
        MarkerSpec(
            id="machine_sleep",
            producer="runtime_assistant",
            shapes=(SHAPE_FINAL_ASSISTANT_ENTRY,),
            fragments=MACHINE_SLEEP,
            predicate=_make_startswith_predicate(MACHINE_SLEEP[0]),
        ),
        MarkerSpec(
            id="api_error",
            producer="runtime_assistant",
            shapes=(SHAPE_FINAL_ASSISTANT_ENTRY,),
            fragments=API_ERROR,
            predicate=_make_startswith_predicate(API_ERROR[0]),
        ),
    )
}
