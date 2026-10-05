"""N1 acceptance tests for marker provenance (docs/DESIGN.md 4.13.2).

For every canonical id in markers.REGISTRY: one positive case (an anchored,
not-in-payload hit) and one payload-negative case (a candidate -- the
fragment occurs -- that the anchored predicate correctly rejects), each
asserting the complete MarkerHit (all seven fields). Plus one entry with two
tool_result blocks, one positive and one negative, yielding two hits with
distinct tool_use_id (docs/DESIGN.md 4.13.2's own acceptance list).

Every wf_ id here is registered in tests/synthetic-ids.txt. Builders are
imported from tests.test_field_v03 rather than copied (CLAUDE.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import reader
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _assistant_tool,
    _kill,
    _no_config,
    _simple_completed_journal,
    _tool_result,
    _user,
    _wf_dir,
    _write_jsonl,
)

WF_ID = "wf_00000000-ba0"
WF_ID_TWO_BLOCKS = "wf_00000000-ba1"


def _one_attempt_run(tmp_path, monkeypatch, wf_id: str, agent_n: int, events: list[dict]):
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(agent_n)
    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)
    run = reader.load_run(run_dir)
    return run.attempts[0]


def _hit(attempt, marker: str):
    """The sole MarkerHit on attempt for marker (fails loudly if there is
    not exactly one, so a test never silently inspects the wrong object)."""
    hits = [h for h in attempt.marker_hits if h.marker == marker]
    assert len(hits) == 1, (marker, attempt.marker_hits)
    return hits[0]


BASE = datetime.now(timezone.utc) - timedelta(minutes=10)


# --- kill_sentinel -----------------------------------------------------


def test_kill_sentinel_positive(tmp_path, monkeypatch):
    agent_id = _aid(900)
    events = [
        _user("Do the thing.", BASE, agent_id),
        _assistant_text("Working.", BASE + timedelta(seconds=1), agent_id),
        _kill(BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 900, events)
    hit = _hit(attempt, "kill_sentinel")
    assert hit.producer == "harness_user"
    assert hit.event_line == 3
    assert hit.tool_use_id is None
    assert hit.result_line is None
    assert hit.anchored is True
    assert hit.in_payload is False


def test_kill_sentinel_payload_negative(tmp_path, monkeypatch):
    """A user message that merely quotes the sentinel inside prose is a
    candidate (the prefix fragment occurs) but never anchors: the whole
    flattened text must equal one of KILL_SENTINEL_VARIANTS exactly."""
    agent_id = _aid(901)
    events = [
        _user(
            "Earlier I saw [Request interrupted by user] in the logs here.",
            BASE, agent_id,
        ),
        _assistant_text("Noted.", BASE + timedelta(seconds=1), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 901, events)
    hit = _hit(attempt, "kill_sentinel")
    assert hit.producer == "harness_user"
    assert hit.event_line == 1
    assert hit.tool_use_id is None
    assert hit.result_line is None
    assert hit.anchored is False
    assert hit.in_payload is True


# --- bash_backgrounded ---------------------------------------------------


def test_bash_backgrounded_positive(tmp_path, monkeypatch):
    agent_id = _aid(902)
    events = [
        _user("Run it.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "sleep 999"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result(
            "t1",
            "Command did not complete within its 30s timeout and was moved to the background (ID: 1).",
            BASE + timedelta(seconds=2), agent_id,
        ),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 902, events)
    hit = _hit(attempt, "bash_backgrounded")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_bash_backgrounded_payload_negative(tmp_path, monkeypatch):
    """A cat result whose printed file content happens to carry both
    BASH_BACKGROUNDED fragments, well after the result's own start."""
    agent_id = _aid(903)
    content = (
        "build step 1: compiling...\n"
        "note: an unrelated earlier job did not complete within its own "
        "budget and was later moved to the background queue by someone else\n"
    )
    events = [
        _user("Show me the log.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "cat build.log"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 903, events)
    hit = _hit(attempt, "bash_backgrounded")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- bash_timeout ---------------------------------------------------------


def test_bash_timeout_positive(tmp_path, monkeypatch):
    agent_id = _aid(904)
    events = [
        _user("Run it.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "sleep 999"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", "Exit code 143\nCommand timed out after 10m 0s", BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 904, events)
    hit = _hit(attempt, "bash_timeout")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_bash_timeout_payload_negative(tmp_path, monkeypatch):
    """A grep result quoting 'Exit code 143' from an old log line, with no
    'Command timed out after' line following it -- a candidate (the first
    fragment occurs) that never anchors."""
    agent_id = _aid(905)
    content = "ci.log:142:job finished with Exit code 143 last Tuesday\n"
    events = [
        _user("Check the log.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "grep 'Exit code' ci.log"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 905, events)
    hit = _hit(attempt, "bash_timeout")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- tool_input_invalid ----------------------------------------------------


def test_tool_input_invalid_positive(tmp_path, monkeypatch):
    agent_id = _aid(906)
    events = [
        _user("Read the file.", BASE, agent_id),
        _assistant_tool("t1", "Read", {"file_path": "/tmp/x"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result(
            "t1",
            "<tool_use_error>InputValidationError: Read was called with input "
            "that could not be parsed as JSON.",
            BASE + timedelta(seconds=2), agent_id,
        ),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 906, events)
    hit = _hit(attempt, "tool_input_invalid")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_tool_input_invalid_payload_negative(tmp_path, monkeypatch):
    """A cat result printing a source file that documents the marker
    string, the self-reference shape (docs/DESIGN.md 4.3): a candidate that
    never anchors because it never carries the runtime's own wrapper tag at
    the start of a line."""
    agent_id = _aid(907)
    content = (
        "# comment: a tool_result can carry InputValidationError when the\n"
        "# runtime rejects malformed input.\n"
    )
    events = [
        _user("Show me markers.py.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "cat markers.py"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 907, events)
    hit = _hit(attempt, "tool_input_invalid")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- structured_output_mismatch -------------------------------------------


def test_structured_output_mismatch_positive(tmp_path, monkeypatch):
    agent_id = _aid(908)
    events = [
        _user("Report.", BASE, agent_id),
        _assistant_tool("t1", "StructuredOutput", {}, BASE + timedelta(seconds=1), agent_id),
        _tool_result(
            "t1",
            "Output does not match required schema: root: must have required property 'ok'",
            BASE + timedelta(seconds=2), agent_id,
        ),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 908, events)
    hit = _hit(attempt, "structured_output_mismatch")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_structured_output_mismatch_payload_negative(tmp_path, monkeypatch):
    """A StructuredOutput result whose own line1 is unrelated prose that
    merely goes on to quote the schema-mismatch phrase on a later line."""
    agent_id = _aid(909)
    content = "note: earlier today 'does not match required schema' was logged\nok"
    events = [
        _user("Report.", BASE, agent_id),
        _assistant_tool("t1", "StructuredOutput", {}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 909, events)
    hit = _hit(attempt, "structured_output_mismatch")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- classifier_unavailable -------------------------------------------------


def test_classifier_unavailable_positive(tmp_path, monkeypatch):
    agent_id = _aid(910)
    events = [
        _user("Run it.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "rm -rf /"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result(
            "t1",
            "claude-sonnet-5[1m] is temporarily unavailable (timed out), so auto "
            "mode cannot determine the safety of Bash right now.",
            BASE + timedelta(seconds=2), agent_id,
        ),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 910, events)
    hit = _hit(attempt, "classifier_unavailable")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_classifier_unavailable_payload_negative(tmp_path, monkeypatch):
    """Both fragments occur somewhere in the result (a candidate), but never
    together on line1, so the predicate never anchors."""
    agent_id = _aid(911)
    content = (
        "note: a service is temporarily unavailable\n"
        "we cannot determine the safety of Bash from this log alone\n"
    )
    events = [
        _user("Run it.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "cat status.log"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 911, events)
    hit = _hit(attempt, "classifier_unavailable")
    assert hit.producer == "harness_tool_notice"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- git_not_repo -----------------------------------------------------------


def test_git_not_repo_positive(tmp_path, monkeypatch):
    agent_id = _aid(912)
    events = [
        _user("Check status.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "git status"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result(
            "t1", "fatal: not a git repository (or any of the parent directories): .git",
            BASE + timedelta(seconds=2), agent_id,
        ),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 912, events)
    hit = _hit(attempt, "git_not_repo")
    assert hit.producer == "command_output"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_git_not_repo_payload_negative(tmp_path, monkeypatch):
    """A cat result printing a log that quotes the fatal line, not as its
    own line1."""
    agent_id = _aid(913)
    content = "build.log:\nfatal: not a git repository (or any of the parent directories): .git\n"
    events = [
        _user("Show the build log.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "cat build.log"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 913, events)
    hit = _hit(attempt, "git_not_repo")
    assert hit.producer == "command_output"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- sandbox_write_denied (tool_result shape) -------------------------------


def test_sandbox_write_denied_positive(tmp_path, monkeypatch):
    agent_id = _aid(914)
    events = [
        _user("Write the report.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "codex write report.md"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result(
            "t1",
            "WRITE_DENIED: patch rejected: writing outside of the project; rejected by user approval settings",
            BASE + timedelta(seconds=2), agent_id,
        ),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 914, events)
    hit = _hit(attempt, "sandbox_write_denied")
    assert hit.producer == "codex_companion"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_sandbox_write_denied_payload_negative(tmp_path, monkeypatch):
    """A codex-program Bash call whose own result happens to print an
    earlier log line mentioning WRITE_DENIED, never as line1 and never on a
    line starting with '[codex]'."""
    agent_id = _aid(915)
    content = "Patch log:\nWRITE_DENIED appears in this historical log entry from a previous run\n"
    events = [
        _user("Check the patch log.", BASE, agent_id),
        _assistant_tool(
            "t1", "Bash", {"command": "codex exec --full-auto cat patch.log"},
            BASE + timedelta(seconds=1), agent_id,
        ),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 915, events)
    hit = _hit(attempt, "sandbox_write_denied")
    assert hit.producer == "codex_companion"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- shell_guard_block -------------------------------------------------------


def test_shell_guard_block_positive(tmp_path, monkeypatch):
    agent_id = _aid(916)
    events = [
        _user("Run node.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "codex exec node app.js"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", "Security: node blocked in workspace", BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 916, events)
    hit = _hit(attempt, "shell_guard_block")
    assert hit.producer == "codex_companion"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is True
    assert hit.in_payload is False


def test_shell_guard_block_payload_negative(tmp_path, monkeypatch):
    """The guard sentence occurs mid-line (never at a line's own start), so
    it is a candidate (the regex matches somewhere) that never anchors
    (SHELL_GUARD_BLOCK_REGEX must match from a line's first character)."""
    agent_id = _aid(917)
    content = "Note: Security: node blocked in workspace was logged previously"
    events = [
        _user("Run node.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "codex exec node app.js"}, BASE + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, BASE + timedelta(seconds=2), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 917, events)
    hit = _hit(attempt, "shell_guard_block")
    assert hit.producer == "codex_companion"
    assert hit.event_line == 3
    assert hit.tool_use_id == "t1"
    assert hit.result_line == 3
    assert hit.anchored is False
    assert hit.in_payload is True


# --- the four final-assistant-entry markers --------------------------------

_FINAL_ENTRY_CASES = [
    ("usage_limit_session", "You've hit your session limit · resets 2pm", "runtime_assistant"),
    ("usage_limit_weekly", "You've hit your weekly limit · resets Monday", "runtime_assistant"),
    ("machine_sleep", "API Error: Your computer went to sleep", "runtime_assistant"),
    ("api_error", "API Error: Server error mid-response", "runtime_assistant"),
]


@pytest.mark.parametrize("marker, text, producer", _FINAL_ENTRY_CASES)
def test_final_entry_marker_positive(tmp_path, monkeypatch, marker, text, producer):
    agent_id = _aid(920)
    events = [
        _user("Do the thing.", BASE, agent_id),
        _assistant_text(text, BASE + timedelta(seconds=1), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 920, events)
    hit = _hit(attempt, marker)
    assert hit.producer == producer
    assert hit.event_line == 2
    assert hit.tool_use_id is None
    assert hit.result_line is None
    assert hit.anchored is True
    assert hit.in_payload is False


@pytest.mark.parametrize("marker, text, producer", _FINAL_ENTRY_CASES)
def test_final_entry_marker_payload_negative(tmp_path, monkeypatch, marker, text, producer):
    """The fragment occurs in the final assistant entry (a candidate) but
    not at its very start (a decoy sentence quoting it), so it never
    anchors -- the same shape tests/fixtures/runs/attempt_failed_causes's
    af-decoy key pins at the detector level."""
    agent_id = _aid(921)
    events = [
        _user("Do the thing.", BASE, agent_id),
        _assistant_text(f"For context, last week {text!r} happened too.", BASE + timedelta(seconds=1), agent_id),
    ]
    attempt = _one_attempt_run(tmp_path, monkeypatch, WF_ID, 921, events)
    hit = _hit(attempt, marker)
    assert hit.producer == producer
    assert hit.event_line == 2
    assert hit.tool_use_id is None
    assert hit.result_line is None
    assert hit.anchored is False
    assert hit.in_payload is True


# --- two tool_result blocks in one entry, distinct tool_use_id -------------


def test_two_tool_result_blocks_one_entry_yield_two_hits(tmp_path, monkeypatch):
    """docs/DESIGN.md 4.13.2's own N1 acceptance test: one user entry
    carrying two tool_result blocks, one positive and one payload-negative,
    yields two MarkerHits with the same event_line/result_line but distinct
    tool_use_id."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, WF_ID_TWO_BLOCKS)
    agent_id = _aid(922)
    _simple_completed_journal(run_dir, agent_id)

    good = "Command did not complete within its 30s timeout and was moved to the background (ID: 9)."
    payload = (
        "note: an unrelated earlier job did not complete within its own "
        "budget and was later moved to the background queue by someone else\n"
    )
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": good, "is_error": True},
                {"type": "tool_result", "tool_use_id": "t2", "content": payload},
            ],
        },
        "agentId": agent_id,
        "timestamp": (BASE + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    events = [
        _user("Run two commands.", BASE, agent_id),
        _assistant_tool("t1", "Bash", {"command": "sleep 999"}, BASE + timedelta(seconds=1), agent_id),
        _assistant_tool("t2", "Bash", {"command": "cat build.log"}, BASE + timedelta(seconds=1, milliseconds=500), agent_id),
        entry,
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)
    run = reader.load_run(run_dir)
    attempt = run.attempts[0]

    hits = [h for h in attempt.marker_hits if h.marker == "bash_backgrounded"]
    assert len(hits) == 2
    by_id = {h.tool_use_id: h for h in hits}
    assert set(by_id) == {"t1", "t2"}
    assert by_id["t1"].anchored is True and by_id["t1"].in_payload is False
    assert by_id["t2"].anchored is False and by_id["t2"].in_payload is True
    for hit in hits:
        assert hit.event_line == 4
        assert hit.result_line == 4
        assert hit.producer == "harness_tool_notice"
