"""Tests for ToolCall and AttemptFacts (docs/DESIGN.md 4.1/4.2/5.3's v0.2
reader changes, unit M2a).

tests/fixtures/runs/attempt_facts/ carries one workflow run with one attempt
per fact being exercised, so each test reads a single, clearly-named
attempt rather than re-deriving state from a shared complex fixture. The
Bash-command-over-2048-chars case is built with tmp_path instead of a
checked-in fixture file: DESIGN.md 5.4 caps every fixture line at 2000
chars, and a command that long would blow straight through that limit.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from agent_postmortem import reader, report
from agent_postmortem.models import AttemptFacts, ToolCall
from agent_postmortem.redact import Redactor

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs" / "attempt_facts"
WF_DIR = (
    FIXTURES_ROOT
    / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000"
    / "subagents"
    / "workflows"
    / "wf_00000000-af0"
)


@pytest.fixture(scope="module")
def run():
    return reader.load_run(WF_DIR)


def _attempt(run, agent_id_suffix: str):
    agent_id = f"af2{'0' * (14 - len(agent_id_suffix))}{agent_id_suffix}"
    for attempt in run.attempts:
        if attempt.agent_id == agent_id:
            return attempt
    raise AssertionError(f"no attempt with agent id ending {agent_id_suffix!r}")


# --- paths_written -----------------------------------------------------


def test_edit_with_successful_result_is_in_paths_written(run):
    attempt = _attempt(run, "1")
    assert attempt.outcome == "completed"
    assert attempt.facts.paths_written == [
        "/tmp/agent-postmortem-fixture/attempt_facts/edit-ok/notes.md"
    ]
    assert attempt.facts.in_flight == []


def test_write_with_is_error_true_is_not_written(run):
    attempt = _attempt(run, "2")
    assert attempt.facts.paths_written == []
    call = attempt.tool_calls[0]
    assert call.tool_name == "Write"
    assert call.is_error is True
    assert call.completion == "completed_error"


# --- in_flight -----------------------------------------------------------


def test_edit_with_no_result_on_a_killed_attempt_is_in_flight_with_path(run):
    attempt = _attempt(run, "3")
    assert attempt.outcome == "killed"
    call = attempt.tool_calls[0]
    assert call.completion == "no_result"
    assert attempt.facts.in_flight == [
        {
            "tool": "Edit",
            "path": "/tmp/agent-postmortem-fixture/attempt_facts/killed/pending.md",
            "line_no": call.line_no,
        }
    ]
    assert attempt.facts.paths_written == []


def test_same_shape_on_a_completed_attempt_has_empty_in_flight(run):
    attempt = _attempt(run, "4")
    assert attempt.outcome == "completed"
    call = attempt.tool_calls[0]
    assert call.completion == "no_result"  # the Edit's own result was never seen
    assert attempt.facts.in_flight == []
    assert attempt.facts.paths_written == []


# --- commit ids ------------------------------------------------------------


def test_git_commit_result_captures_the_commit_id_but_a_plain_bash_call_does_not(run):
    attempt = _attempt(run, "5")
    commit_call, echo_call = attempt.tool_calls
    assert commit_call.command == "git commit -m 'synthetic change'"
    assert commit_call.commit_ids == ("abc1234",)
    assert echo_call.command == "echo done"
    assert echo_call.commit_ids == ()
    assert attempt.facts.commits == ["abc1234"]


# --- backgrounded ----------------------------------------------------------


def test_bash_result_with_backgrounded_marker_is_backgrounded(run):
    attempt = _attempt(run, "6")
    call = attempt.tool_calls[0]
    assert call.completion == "backgrounded"
    assert attempt.facts.background_unfinished == 1


# --- command length bound (tmp_path: see module docstring) -----------------


def _write_minimal_workflow_with_bash_command(tmp_path: Path, command: str) -> Path:
    run_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000" \
        / "subagents" / "workflows" / "wf_00000000-999"
    run_dir.mkdir(parents=True)
    agent_id = "af900000000000001"
    (run_dir / "journal.jsonl").write_text(
        '{"type":"started","key":"v2:long-cmd","agentId":"' + agent_id + '",'
        '"label":"impl:long-cmd","phase":"Implement"}\n'
        '{"type":"result","key":"v2:long-cmd","agentId":"' + agent_id + '","result":"completed"}\n',
        encoding="utf-8",
    )
    import json

    lines = [
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Run a long command."},
            "agentId": agent_id,
            "cwd": "/home/user/project",
            "timestamp": "2026-09-01T10:00:00Z",
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_long", "name": "Bash", "input": {"command": command}}
                ],
                "usage": {},
            },
            "agentId": agent_id,
            "cwd": "/home/user/project",
            "timestamp": "2026-09-01T10:00:05Z",
        }),
        json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_long", "content": "ok", "is_error": False}
                ],
            },
            "agentId": agent_id,
            "cwd": "/home/user/project",
            "timestamp": "2026-09-01T10:00:06Z",
        }),
    ]
    (run_dir / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


def test_bash_command_longer_than_2048_chars_is_none_with_sha256_set(tmp_path):
    long_command = "echo " + ("a" * 2050)
    assert len(long_command) > 2048
    run_dir = _write_minimal_workflow_with_bash_command(tmp_path, long_command)
    loaded = reader.load_run(run_dir)
    (attempt,) = loaded.attempts
    call = attempt.tool_calls[0]
    assert call.command is None
    assert call.command_sha256 == hashlib.sha256(long_command.encode("utf-8")).hexdigest()


def test_bash_command_at_the_2048_char_bound_is_kept(tmp_path):
    command = "echo " + ("a" * 2043)  # exactly 2048 chars total
    assert len(command) == 2048
    run_dir = _write_minimal_workflow_with_bash_command(tmp_path, command)
    loaded = reader.load_run(run_dir)
    (attempt,) = loaded.attempts
    call = attempt.tool_calls[0]
    assert call.command == command
    assert call.command_sha256 is None


# --- path length bound -------------------------------------------------


def test_long_file_path_past_the_excerpt_limit_is_captured_whole(run):
    attempt = _attempt(run, "8")
    ok_call, too_long_call = attempt.tool_calls
    assert ok_call.path is not None
    assert 400 < len(ok_call.path) <= 1024
    assert ok_call.path.startswith("/tmp/agent-postmortem-fixture/attempt_facts/long-path/")
    assert attempt.facts.paths_written == [ok_call.path]

    # The tool_use JSON is long enough that Event.text (the 400-char
    # excerpt) would have truncated it; ToolCall.path must not be.
    tool_use_event = next(e for e in attempt.events if e.kind == "tool_use" and e.line_no == 2)
    assert len(tool_use_event.text) <= 400
    assert len(ok_call.path) > len(tool_use_event.text)


def test_file_path_over_1024_chars_is_none_and_excluded_from_paths_written(run):
    attempt = _attempt(run, "8")
    ok_call, too_long_call = attempt.tool_calls
    assert len(too_long_call.tool_use_id) > 0  # sanity: it is the second call
    assert too_long_call.path is None
    assert too_long_call.completion == "completed_ok"  # the call itself still resolved
    assert ok_call.path is not None
    assert attempt.facts.paths_written == [ok_call.path]  # the over-long path never appears


# --- cwd normalization -------------------------------------------------


def test_cwd_is_normalized_with_os_path_normpath(run):
    attempt = _attempt(run, "9")
    first_call, second_call = attempt.tool_calls
    assert first_call.cwd == os.path.normpath("/home/user/project/a/../b")
    assert first_call.cwd == "/home/user/project/b"
    assert second_call.cwd == "/home/user/project/c"
    assert attempt.facts.cwds == ["/home/user/project/b", "/home/user/project/c"]


# --- seconds_since_last_tool_at_end -------------------------------------


def test_seconds_since_last_tool_at_end_is_computed(run):
    attempt = _attempt(run, "1")
    last_call = attempt.tool_calls[-1]
    assert last_call.ts is not None
    assert attempt.last_ts is not None
    expected = (attempt.last_ts - last_call.ts).total_seconds()
    assert expected == 25.0
    assert attempt.facts.seconds_since_last_tool_at_end == expected


def test_seconds_since_last_tool_at_end_is_none_when_the_last_tool_call_has_no_timestamp(run):
    attempt = _attempt(run, "10")
    last_call = attempt.tool_calls[-1]
    assert last_call.tool_name == "Edit"
    assert last_call.ts is None
    assert attempt.last_ts is not None  # the attempt's own last_ts is known
    assert attempt.facts.seconds_since_last_tool_at_end is None


def test_seconds_since_last_tool_at_end_is_none_when_negative():
    """A direct AttemptFacts computation (not a fixture): a last tool call
    whose own timestamp is somehow after the attempt's last_ts must never
    report a negative duration (docs/DESIGN.md 4.1's v0.2 reader changes)."""
    from datetime import datetime, timedelta, timezone

    from agent_postmortem.reader import _build_attempt_facts

    later = datetime(2026, 1, 1, tzinfo=timezone.utc)
    earlier = later - timedelta(seconds=10)
    call = ToolCall(tool_use_id="t1", tool_name="Bash", line_no=1, ts=later)
    facts = _build_attempt_facts(
        [call], outcome="completed", last_ts=earlier, transcript_path=Path("/tmp/x"),
        unreadable=False,
    )
    assert facts.seconds_since_last_tool_at_end is None


# --- parallel tool calls, paired by id -----------------------------------


def test_parallel_tool_calls_are_paired_by_id_not_position(run):
    attempt = _attempt(run, "11")
    first_call, second_call = attempt.tool_calls
    assert first_call.tool_use_id == "toolu_p1"
    assert second_call.tool_use_id == "toolu_p2"
    # toolu_p2's result (line 3) arrives before toolu_p1's (line 4) in the
    # transcript; pairing by id must still land each result on its own call.
    assert first_call.result_line == 4
    assert second_call.result_line == 3
    assert first_call.command == "echo one"
    assert second_call.command == "echo two"


# --- missing transcript --------------------------------------------------


def test_attempt_with_no_transcript_file_has_facts_none(run):
    attempt = _attempt(run, "12")
    assert attempt.transcript_path is None
    assert attempt.tool_calls == []
    assert attempt.facts is None


# --- unreadable transcript: every field becomes FACTS_UNKNOWN --------------


def _skip_if_root():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")


def test_unreadable_transcript_has_every_fact_unknown(tmp_path):
    _skip_if_root()
    run_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000" \
        / "subagents" / "workflows" / "wf_00000000-998"
    run_dir.mkdir(parents=True)
    agent_id = "af800000000000001"
    (run_dir / "journal.jsonl").write_text(
        '{"type":"started","key":"v2:unreadable","agentId":"' + agent_id + '",'
        '"label":"impl:unreadable","phase":"Implement"}\n',
        encoding="utf-8",
    )
    transcript = run_dir / f"agent-{agent_id}.jsonl"
    transcript.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8")
    transcript.chmod(0o000)
    try:
        loaded = reader.load_run(run_dir)
    finally:
        transcript.chmod(0o644)
    (attempt,) = loaded.attempts
    assert attempt.transcript_path is not None
    assert attempt.facts is not None
    facts_dict = attempt.facts.to_dict()
    for key in (
        "paths_written", "in_flight", "commits", "cwds",
        "background_unfinished", "last_tool", "seconds_since_last_tool_at_end",
    ):
        assert facts_dict[key] == "UNKNOWN"


# --- private complete form: Attempt.to_dict()/ToolCall.to_dict() -----------


def test_attempt_to_dict_includes_tool_calls_and_facts(run):
    attempt = _attempt(run, "1")
    as_dict = attempt.to_dict()
    assert "tool_calls" in as_dict
    assert "facts" in as_dict
    assert as_dict["tool_calls"][0]["tool_name"] == "Edit"
    assert as_dict["tool_calls"][0]["command"] is None
    assert as_dict["facts"]["paths_written"] == attempt.facts.paths_written


def test_tool_call_completion_property_covers_every_state():
    ok = ToolCall(tool_use_id="a", tool_name="Bash", line_no=1, ts=None, result_line=2, is_error=False)
    err = ToolCall(tool_use_id="b", tool_name="Bash", line_no=1, ts=None, result_line=2, is_error=True)
    bg = ToolCall(
        tool_use_id="c", tool_name="Bash", line_no=1, ts=None, result_line=2, is_error=False,
        backgrounded=True,
    )
    pending = ToolCall(tool_use_id="d", tool_name="Bash", line_no=1, ts=None)
    assert ok.completion == "completed_ok"
    assert err.completion == "completed_error"
    assert bg.completion == "backgrounded"
    assert pending.completion == "no_result"


# --- F2: completion must honor a parsed exit code, not just is_error -------


def test_completion_nonzero_exit_code_with_is_error_false_is_completed_error():
    """docs/DESIGN.md 4.1 (F2 hardening): a Bash result whose own text parses
    to a nonzero exit code is completed_error even when the runtime's
    is_error flag is (incorrectly, or simply differently) false -- rescue
    status must never read this as EXIT_0_OBSERVED."""
    call = ToolCall(
        tool_use_id="a", tool_name="Bash", line_no=1, ts=None, result_line=2,
        is_error=False, exit_code=1,
    )
    assert call.completion == "completed_error"


def test_completion_nonzero_exit_code_with_is_error_true_is_completed_error():
    call = ToolCall(
        tool_use_id="b", tool_name="Bash", line_no=1, ts=None, result_line=2,
        is_error=True, exit_code=1,
    )
    assert call.completion == "completed_error"


def test_completion_exit_code_zero_explicit_is_completed_ok():
    call = ToolCall(
        tool_use_id="c", tool_name="Bash", line_no=1, ts=None, result_line=2,
        is_error=False, exit_code=0,
    )
    assert call.completion == "completed_ok"


def test_completion_ambiguous_outranks_every_other_state():
    """F5 hardening: a duplicate tool_use_id makes every other signal on the
    call untrustworthy, so `ambiguous` is checked first, before is_error,
    exit_code, or backgrounded."""
    call = ToolCall(
        tool_use_id="d", tool_name="Bash", line_no=1, ts=None, result_line=2,
        is_error=False, exit_code=0, ambiguous=True,
    )
    assert call.completion == "ambiguous"
    pending_ambiguous = ToolCall(tool_use_id="e", tool_name="Bash", line_no=1, ts=None, ambiguous=True)
    assert pending_ambiguous.completion == "ambiguous"


def test_attempt_facts_dataclass_to_dict_round_trips():
    facts = AttemptFacts(
        paths_written=["/tmp/x/y.md"], in_flight=[], commits=["abc1234"], cwds=["/tmp/x"],
        background_unfinished=0, last_tool="Edit", seconds_since_last_tool_at_end=1.5,
    )
    as_dict = facts.to_dict()
    assert as_dict["paths_written"] == ["/tmp/x/y.md"]
    assert as_dict["commits"] == ["abc1234"]
    assert as_dict["last_tool"] == "Edit"
    assert as_dict["seconds_since_last_tool_at_end"] == 1.5


# --- --json export ----------------------------------------------------


ATTEMPT_KEYS_WITH_FACTS = {
    "agent_id", "key", "label", "phase", "agent_type", "ordinal", "outcome",
    "first_ts", "last_ts", "usage", "event_count", "interrupt", "reported",
    "facts",
}


def _export(run, redactor):
    from agent_postmortem import detectors

    findings = detectors.run_detectors(run)
    return report.export_run(run, findings, redactor)


def test_json_export_passes_the_allowlist_and_carries_facts_but_not_tool_calls(run):
    exported = _export(run, Redactor(enabled=False))
    attempts = exported["run"]["attempts"]
    assert attempts
    for attempt in attempts:
        assert set(attempt.keys()) == ATTEMPT_KEYS_WITH_FACTS
        assert "tool_calls" not in attempt
    edit_ok = next(a for a in attempts if a["agent_id"] == "af200000000000001")
    assert edit_ok["facts"] is not None
    assert edit_ok["facts"]["paths_written"] == [
        "/tmp/agent-postmortem-fixture/attempt_facts/edit-ok/notes.md"
    ]


def test_json_export_never_carries_a_raw_bash_command():
    exported_text = str(_export(reader.load_run(WF_DIR), Redactor(enabled=False)))
    assert "git commit -m" not in exported_text
    assert "echo done" not in exported_text
    assert "sleep 999" not in exported_text


def test_json_export_paths_are_redacted_by_default_and_raw_with_no_redact(run):
    # F4: changed from the old ".../basename" structural-collapse expectation
    # -- a facts path now registers through the same path-N pseudonym
    # namespace report_path/observation paths use (docs/DESIGN.md 5.1/5.3),
    # so the whole path is replaced, not just its middle directories.
    redacted = _export(run, Redactor(enabled=True))
    raw = _export(run, Redactor(enabled=False))

    redacted_edit = next(
        a for a in redacted["run"]["attempts"] if a["agent_id"] == "af200000000000001"
    )
    raw_edit = next(a for a in raw["run"]["attempts"] if a["agent_id"] == "af200000000000001")

    raw_path = raw_edit["facts"]["paths_written"][0]
    redacted_path = redacted_edit["facts"]["paths_written"][0]
    assert raw_path == "/tmp/agent-postmortem-fixture/attempt_facts/edit-ok/notes.md"
    assert redacted_path.startswith("path-")
    assert raw_path != redacted_path

    killed_attempt = next(
        a for a in raw["run"]["attempts"] if a["agent_id"] == "af200000000000003"
    )
    redacted_killed = next(
        a for a in redacted["run"]["attempts"] if a["agent_id"] == "af200000000000003"
    )
    assert killed_attempt["facts"]["in_flight"][0]["path"] == (
        "/tmp/agent-postmortem-fixture/attempt_facts/killed/pending.md"
    )
    assert redacted_killed["facts"]["in_flight"][0]["path"].startswith("path-")


def test_json_export_cwds_are_redacted_by_default(run):
    """F4: facts.cwds is also registered through the path-N pseudonym."""
    redacted = _export(run, Redactor(enabled=True))
    redacted_attempt = next(
        a for a in redacted["run"]["attempts"] if a["agent_id"] == "af200000000000009"
    )
    for cwd in redacted_attempt["facts"]["cwds"]:
        assert cwd.startswith("path-")
    assert "/home/user/project" not in str(redacted_attempt["facts"]["cwds"])


def test_json_export_facts_path_and_observation_path_share_one_pseudonym(run):
    """F4: the same raw path appearing in both an attempt's own facts and in
    the --observe payload must map to the identical path-N pseudonym in one
    render (docs/DESIGN.md 5.1)."""
    from agent_postmortem import observe

    raw_path = "/tmp/agent-postmortem-fixture/attempt_facts/edit-ok/notes.md"
    fake_observation = observe.RepoObservation(
        checked=True, reason=None, repo_root="/tmp/agent-postmortem-fixture/attempt_facts",
        head=None, git_version=None,
        paths={
            raw_path: observe.PathObservation(
                state="DIRTY_NOW", reason=None, later_write_observed=False,
                attempts=[{"agent_id": "af200000000000001", "ordinal": 1}],
            )
        },
    )
    from agent_postmortem import detectors

    findings = detectors.run_detectors(run)
    redactor = Redactor(enabled=True)
    exported = report.export_run(run, findings, redactor, observation=fake_observation)
    edit_attempt = next(
        a for a in exported["run"]["attempts"] if a["agent_id"] == "af200000000000001"
    )
    facts_pseudo = edit_attempt["facts"]["paths_written"][0]
    observation_pseudo = next(iter(exported["observation"]["paths"]))
    assert facts_pseudo.startswith("path-")
    assert facts_pseudo == observation_pseudo


def test_missing_transcript_attempt_exports_facts_as_null(run):
    exported = _export(run, Redactor(enabled=False))
    missing = next(
        a for a in exported["run"]["attempts"] if a["agent_id"] == "af200000000000012"
    )
    assert missing["facts"] is None
