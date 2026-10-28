"""N4b-2 coverage group 7 (ledger existence, completeness, and every reason with
every producer, in the fixed order): docs/DESIGN.md 4.13.1, C-14's 4.1 rule,
C-15; .reviews/v0.3/N4-plan.md L12.

Each producer has a test that asserts the whole reasons list, so a missing,
extra, or reordered reason fails it. Runs are real: a synthetic workflow run (one
generation, so the attempt is assignable) or session, read by reader.load_run.
"""

from __future__ import annotations

import os

import pytest

from tests.n4b2_helpers import (
    ledger_of,
    result,
    session_run,
    stamp,
    text,
    use,
    workflow_run,
    write_lines,
)
from tests.test_field_v03 import _aid, _wf_dir

CLEAN = [use("t1", "Write", {"file_path": "/tmp/ws/a.py"}), result("t1"), text()]


def reasons(tmp_path, entries, **kwargs):
    return ledger_of(tmp_path, entries, **kwargs).reasons


# --- existence: the ledger is never None (C-14) ----------------------------------------


def test_a_clean_file_tool_attempt_has_a_complete_ledger(tmp_path):
    built = ledger_of(tmp_path, CLEAN)
    assert built.completeness == "complete"
    assert built.reasons == []
    assert [e.path for e in built.entries] == ["/tmp/ws/a.py"]


def test_an_attempt_with_no_tool_calls_has_an_empty_complete_ledger(tmp_path):
    built = ledger_of(tmp_path, [text()])
    assert (built.entries, built.completeness, built.reasons) == ([], "complete", [])


def test_a_missing_expected_transcript_gives_the_partial_ledger_transcript_missing(tmp_path):
    run = workflow_run(tmp_path, None)
    built = run.attempts[0].ledger
    assert run.attempts[0].transcript_path is None
    assert built is not None
    assert built.entries == []
    assert built.completeness == "partial"
    assert built.reasons == ["transcript_missing", "unassignable_attempt"]


def test_an_attempt_with_no_canonical_agent_id_gives_transcript_missing(tmp_path):
    from tests.n4b2_helpers import WF
    run_dir = _wf_dir(tmp_path / "root", WF)
    write_lines(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": "not-an-id", "label": "l", "phase": "p"},
    ])
    from agent_postmortem import reader
    run = reader.load_run(run_dir)
    built = run.attempts[0].ledger
    assert built.reasons == ["transcript_missing", "unassignable_attempt", "discovery_incomplete"]
    assert built.entries == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_transcript_gives_transcript_unreadable(tmp_path):
    run = workflow_run(tmp_path, CLEAN)
    path = run.attempts[0].transcript_path
    os.chmod(path, 0)
    try:
        from agent_postmortem import reader
        run = reader.load_run(run.path)
    finally:
        os.chmod(path, 0o644)
    built = run.attempts[0].ledger
    assert built.entries == []
    assert built.completeness == "partial"
    assert built.reasons == ["transcript_unreadable", "unassignable_attempt", "discovery_incomplete"]


def test_a_session_main_thread_and_a_subagent_each_get_a_ledger(tmp_path):
    child = _aid(9)
    run = session_run(
        tmp_path,
        [text(who="user", body="go"), use("m1", "Write", {"file_path": "/tmp/ws/m.py"}), result("m1")],
        {child: [use("c1", "Write", {"file_path": "/tmp/ws/c.py"}), result("c1")]},
    )
    assert [e.path for e in run.main.ledger.entries] == ["/tmp/ws/m.py"]
    assert [e.path for e in run.attempts[0].ledger.entries] == ["/tmp/ws/c.py"]
    assert run.main.ledger.completeness == "complete"
    assert run.attempts[0].ledger.completeness == "complete"


# --- undecodable_line -----------------------------------------------------------------


def test_an_undecodable_line_gives_undecodable_line(tmp_path):
    lines = stamp(CLEAN) + ["{not json"]
    assert reasons(tmp_path, lines, stamped=False) == ["undecodable_line", "discovery_incomplete"]


# --- unexpected_entry_shape (each shape rule alone) -------------------------------------

SHAPES = {
    "not an object": ["[1, 2]"],
    "no type": [{"message": {"content": "x"}, "timestamp": "2026-09-01T12:00:30Z"}],
    "non-string type": [{"type": 5, "timestamp": "2026-09-01T12:00:30Z"}],
    "user message not an object": [
        {"type": "user", "message": "x", "timestamp": "2026-09-01T12:00:30Z"}],
    "assistant message not an object": [
        {"type": "assistant", "message": [1], "timestamp": "2026-09-01T12:00:30Z"}],
    "user content neither": [
        {"type": "user", "message": {"content": 5}, "timestamp": "2026-09-01T12:00:30Z"}],
    "assistant content neither": [
        {"type": "assistant", "message": {"content": {"a": 1}},
         "timestamp": "2026-09-01T12:00:30Z"}],
    "user content missing": [
        {"type": "user", "message": {}, "timestamp": "2026-09-01T12:00:30Z"}],
    "tool_use without id": [
        {"type": "assistant", "timestamp": "2026-09-01T12:00:30Z", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {}}]}}],
    "tool_use with a non-string id": [
        {"type": "assistant", "timestamp": "2026-09-01T12:00:30Z", "message": {"content": [
            {"type": "tool_use", "id": 7, "name": "Read", "input": {}}]}}],
    "tool_result without tool_use_id": [
        {"type": "user", "timestamp": "2026-09-01T12:00:30Z", "message": {"content": [
            {"type": "tool_result", "content": "x"}]}}],
    "tool_result with a non-string tool_use_id": [
        {"type": "user", "timestamp": "2026-09-01T12:00:30Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": 3, "content": "x"}]}}],
}
# These shapes also make the reader itself report a diagnostic.
SHAPES_WITH_DIAGNOSTIC = {"not an object", "user message not an object",
                          "assistant message not an object"}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_each_unexpected_shape_gives_unexpected_entry_shape(tmp_path, name):
    entries = stamp(CLEAN[:2]) + SHAPES[name]
    expected = ["unexpected_entry_shape"]
    if name in SHAPES_WITH_DIAGNOSTIC:
        expected.append("discovery_incomplete")
    assert reasons(tmp_path, entries, stamped=False) == expected


def test_a_tool_use_block_with_no_name_is_an_unknown_tool_and_a_shape_reason(tmp_path):
    entries = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "input": {}}]}}, result("t1")]
    built = ledger_of(tmp_path, entries)
    assert built.reasons == ["unexpected_entry_shape", "possible_unknown_tool"]
    assert [(e.effect, e.basis) for e in built.entries] == [("possible", "unknown_tool")]


# --- unknown_entry_type ---------------------------------------------------------------

KNOWN_TYPES = [
    "attachment", "system", "summary", "queue-operation", "last-prompt", "ai-title", "atis-latch",
    "mode", "permission-mode", "pr-link", "agent-name", "bridge-session", "custom-title",
    "file-history-snapshot", "file-history-delta", "cost-state", "frame-link", "artifact-anything",
    "artifact-",
]


@pytest.mark.parametrize("kind", KNOWN_TYPES)
def test_a_known_entry_type_is_ignored_for_the_ledger(tmp_path, kind):
    built = ledger_of(tmp_path, CLEAN + [{"type": kind}])
    assert built.reasons == []
    assert built.completeness == "complete"


# Claude Code 2.1.300 records each API request in the transcript (its changelog: "record each
# API request's system prompt, tools and parameters"). The shapes below carry the key sets
# observed on 2.1.300 to 2.1.312; a blob echoes earlier messages, so a tool_use inside one is
# not a call the attempt made.
REQUEST_LOG_ENTRIES = [
    {"type": "api-request", "sessionId": "s", "id": "r1", "timestamp": "2026-09-01T12:00:00Z",
     "version": "2.1.300", "querySource": "agent", "shapeHash": "h1", "params": {"model": "m"},
     "keep": 0, "tail": ["b1"], "cacheControl": [], "requestDigest": "d1"},
    {"type": "api-request-shape", "sessionId": "s", "shapeHash": "h1",
     "timestamp": "2026-09-01T12:00:00Z", "version": "2.1.300",
     "shape": {"system": [{"type": "text", "text": "x"}], "tools": []}},
    {"type": "api-request-blob", "sessionId": "s", "hash": "b1",
     "message": {"role": "assistant", "content": [
         {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "rm -rf /tmp/ws"}},
     ]}},
]


@pytest.mark.parametrize("entry", REQUEST_LOG_ENTRIES, ids=lambda e: e["type"])
def test_a_request_log_entry_is_ignored_for_the_ledger(tmp_path, entry):
    built = ledger_of(tmp_path, CLEAN + [entry])
    assert built.reasons == []
    assert built.completeness == "complete"
    assert [e.path for e in built.entries] == ["/tmp/ws/a.py"]


@pytest.mark.parametrize("kind", ["weird", "", "Attachment", "artifact", "users", "tool_use",
                                  "api-request-other", "api-requests"])
def test_any_other_entry_type_gives_unknown_entry_type(tmp_path, kind):
    assert reasons(tmp_path, CLEAN + [{"type": kind}]) == ["unknown_entry_type"]


def test_user_and_assistant_entries_are_never_unknown(tmp_path):
    assert reasons(tmp_path, [text(who="user", body="hi"), text()]) == []


# --- truncated_field ------------------------------------------------------------------


def test_a_file_tool_path_over_1024_characters_gives_truncated_field(tmp_path):
    entries = [use("t1", "Write", {"file_path": "/" + "a" * 1024}), result("t1")]
    assert reasons(tmp_path, entries) == ["truncated_field"]


def test_a_write_grammar_target_over_1024_characters_gives_truncated_field_and_shell(tmp_path):
    entries = [use("t1", "Bash", {"command": "echo x > /tmp/" + "a" * 1100}), result("t1", "")]
    assert reasons(tmp_path, entries) == ["truncated_field", "shell_context_unverified"]


# --- ambiguous_tool_id ----------------------------------------------------------------


def test_a_duplicate_tool_use_id_gives_ambiguous_tool_id_even_for_a_read_tool(tmp_path):
    entries = [use("t1", "Read", {"file_path": "/tmp/a"}), use("t1", "Read", {"file_path": "/tmp/b"}),
               result("t1")]
    assert reasons(tmp_path, entries) == ["ambiguous_tool_id", "discovery_incomplete"]


# --- unpaired_write_call --------------------------------------------------------------


def test_an_entry_producing_call_with_no_result_gives_unpaired_write_call(tmp_path):
    entries = [use("t1", "Write", {"file_path": "/tmp/ws/a.py"})]
    assert reasons(tmp_path, entries) == ["unpaired_write_call"]


def test_a_read_tool_with_no_result_is_not_an_unpaired_write(tmp_path):
    assert reasons(tmp_path, [use("t1", "Read", {"file_path": "/tmp/a"})]) == []


def test_a_read_only_shell_call_with_no_result_is_not_an_unpaired_write(tmp_path):
    entries = [use("t1", "Bash", {"command": "cat f"})]
    assert reasons(tmp_path, entries) == ["shell_context_unverified"]


def test_a_backgrounded_call_has_a_result_so_it_is_not_unpaired(tmp_path):
    entries = [use("t1", "Bash", {"command": "make"}),
               result("t1", "ok\nCommand did not complete within its 120s timeout and was moved to the background")]
    assert "unpaired_write_call" not in reasons(tmp_path, entries)


# --- shell_context_unverified (C-15) ----------------------------------------------------


@pytest.mark.parametrize("tool", ["Bash", "Monitor"])
def test_a_read_only_shell_or_monitor_call_alone_gives_shell_context_unverified(tmp_path, tool):
    built = ledger_of(tmp_path, [use("t1", tool, {"command": "cat f"}), result("t1")])
    assert built.reasons == ["shell_context_unverified"]
    assert built.completeness == "partial"
    assert built.entries == []


def test_only_file_tools_never_give_shell_context_unverified(tmp_path):
    assert reasons(tmp_path, CLEAN) == []


def test_the_lowercase_bash_tool_is_not_a_shell_call(tmp_path):
    built = ledger_of(tmp_path, [use("t1", "bash", {"command": "cat f"}), result("t1")])
    assert built.reasons == ["possible_unknown_tool"]


def test_a_delegation_tool_is_not_a_shell_call(tmp_path):
    built = ledger_of(tmp_path, [use("t1", "Agent", {"prompt": "x"}), result("t1")])
    assert built.reasons == ["possible_delegation"]


# --- possible_* (one per basis) ---------------------------------------------------------


def test_possible_opaque_command(tmp_path):
    entries = [use("t1", "Bash", {"command": "make all"}), result("t1")]
    assert reasons(tmp_path, entries) == ["shell_context_unverified", "possible_opaque_command"]


def test_possible_delegation(tmp_path):
    entries = [use("t1", "Task", {"prompt": "x"}), result("t1")]
    assert reasons(tmp_path, entries) == ["possible_delegation"]


def test_possible_background_process(tmp_path):
    entries = [use("t1", "Bash", {"command": "make &"}), result("t1")]
    assert reasons(tmp_path, entries) == ["shell_context_unverified", "possible_background_process"]


def test_possible_unknown_tool(tmp_path):
    entries = [use("t1", "mcp__fs__write", {}), result("t1")]
    assert reasons(tmp_path, entries) == ["possible_unknown_tool"]


def test_a_claimed_target_alone_adds_no_possible_reason(tmp_path):
    entries = [use("t1", "Bash", {"command": "echo x > /tmp/ws/f"}), result("t1", "")]
    assert reasons(tmp_path, entries) == ["shell_context_unverified"]


def test_a_basis_that_fires_twice_is_listed_once(tmp_path):
    entries = [use("t1", "Bash", {"command": "make a"}), result("t1"),
               use("t2", "Bash", {"command": "make b"}), result("t2")]
    assert reasons(tmp_path, entries) == ["shell_context_unverified", "possible_opaque_command"]


# --- the fixed order --------------------------------------------------------------------


def test_every_reason_that_applies_is_listed_in_the_fixed_order(tmp_path):
    entries = stamp([
        use("t1", "Write", {"file_path": "/" + "a" * 1024}),
        result("t1"),
        use("t2", "Write", {"file_path": "/tmp/ws/b"}),              # unpaired
        use("t3", "Read", {"file_path": "/tmp/x"}), use("t3", "Read", {"file_path": "/tmp/y"}),
        use("t4", "mcp__x__y", {}), result("t4"),
        use("t5", "Bash", {"command": "make"}), result("t5"),
        use("t6", "Agent", {"prompt": "p"}), result("t6"),
        use("t7", "Bash", {"command": "make &"}), result("t7"),
        {"type": "weird"},
        {"type": "user", "message": {"content": 5}},
    ]) + ["{not json"]
    assert reasons(tmp_path, entries, stamped=False) == [
        "undecodable_line", "unexpected_entry_shape", "unknown_entry_type", "truncated_field",
        "ambiguous_tool_id", "unpaired_write_call", "shell_context_unverified",
        "possible_opaque_command", "possible_delegation", "possible_background_process",
        "possible_unknown_tool", "discovery_incomplete",
    ]


# --- the reasons and the completeness agree ---------------------------------------------


def test_completeness_is_partial_exactly_when_there_is_a_reason(tmp_path):
    for entries in (CLEAN, [use("t1", "Bash", {"command": "make"}), result("t1")]):
        built = ledger_of(tmp_path, entries)
        assert (built.completeness == "partial") == bool(built.reasons)


def test_the_main_thread_of_a_session_with_a_shell_call_is_partial(tmp_path):
    run = session_run(
        tmp_path,
        [text(who="user", body="go"), use("m1", "Bash", {"command": "cat f"}), result("m1")],
    )
    assert run.main.ledger.reasons == ["shell_context_unverified"]
    assert run.main.ledger.completeness == "partial"


# --- a transcript that is missing or unreadable carries no calls ----------------------------


def test_a_missing_or_unreadable_state_ignores_whatever_calls_it_is_handed(tmp_path):
    from tests.n4b2_helpers import NAME, read_calls
    from tests.n4b2_helpers import ledger as ledger_module
    calls = read_calls(tmp_path, [use("t1", "Write", {"file_path": "/tmp/ws/a"}), result("t1")])
    missing = ledger_module.build_ledger(calls, NAME, ledger_module.TranscriptState(missing=True))
    assert (missing.entries, missing.reasons) == ([], ["transcript_missing"])
    unreadable = ledger_module.build_ledger(
        calls, NAME, ledger_module.TranscriptState(unreadable=True))
    assert (unreadable.entries, unreadable.reasons) == ([], ["transcript_unreadable"])


# --- sessions: the main thread and a subagent report what reading their own transcript saw ---


def test_a_session_main_transcript_reports_its_own_undecodable_and_unknown_lines(tmp_path):
    main = stamp([text(who="user", body="go"), {"type": "weird"}]) + ["{bad"]
    from agent_postmortem import reader
    from tests.n4b2_helpers import SESSION_UUID
    root = tmp_path / "root"
    path = root / "-tmp-proj" / f"{SESSION_UUID}.jsonl"
    first = {**main[0], "sessionId": SESSION_UUID}
    write_lines(path, [first, *main[1:]])
    run = reader.load_run(path)
    assert run.main.ledger.reasons == [
        "undecodable_line", "unknown_entry_type", "discovery_incomplete"]


def test_a_session_subagent_reports_its_own_shape_problems(tmp_path):
    child = _aid(8)
    run = session_run(
        tmp_path, [text(who="user", body="go")],
        {child: [text(who="user", body="x"), {"type": "user", "message": {"content": 5}},
                 {"type": "weird"}]},
    )
    assert run.attempts[0].ledger.reasons == ["unexpected_entry_shape", "unknown_entry_type"]
    assert run.main.ledger.reasons == []


def test_a_session_subagent_with_a_shell_call_is_partial_and_its_main_is_not(tmp_path):
    child = _aid(8)
    run = session_run(
        tmp_path, [text(who="user", body="go")],
        {child: [text(who="user", body="x"), use("c1", "Bash", {"command": "make"}), result("c1")]},
    )
    assert run.attempts[0].ledger.reasons == ["shell_context_unverified", "possible_opaque_command"]
    assert run.main.ledger.completeness == "complete"
