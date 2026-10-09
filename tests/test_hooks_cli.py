"""Tests for `agent-postmortem hook <...>` (docs/DESIGN.md 7.2).

Every test points XDG_STATE_HOME at its own tmp_path (so state.json never
leaks between tests or touches a real machine's state) and drives
hooks_cli only through `cli.main(["hook", ...])` with stdin patched, the
same path plugin/hooks/run.sh takes in production.

State is namespaced by root identity plus session id (docs/DESIGN.md 7.5's
D3): `_session_key(root, session_id)` mirrors hooks_cli._session_state_key
so a test can look a unit up in state.json without hardcoding the hash.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path

import pytest

from agent_postmortem import hooks_cli
from agent_postmortem.cli import main

FIXTURE_SESSION_DIR = (
    Path(__file__).parent
    / "fixtures"
    / "runs"
    / "hook_session"
    / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000"
)
MAIN_TRANSCRIPT = FIXTURE_SESSION_DIR.parent / "00000000-0000-4000-8000-000000000000.jsonl"
SESSION_ID = "00000000-0000-4000-8000-000000000000"
# FIXTURE_SESSION_DIR is <root>/-home-user-project/<session>; root is two
# levels up, the same arithmetic hooks_cli itself uses (session_dir.parent
# is the slug directory, .parent.parent is root).
ROOT = FIXTURE_SESSION_DIR.parent.parent


def _session_key(root: Path, session_id: str) -> str:
    return hooks_cli._session_state_key(root, session_id)


SESSION_KEY = _session_key(ROOT, SESSION_ID)
_UNKNOWN_AFTER_STABLE_CHECKS = hooks_cli._UNKNOWN_AFTER_STABLE_CHECKS


def _run(monkeypatch, capsys, hook_command: str, payload) -> tuple[int, str]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = main(["hook", hook_command])
    return code, capsys.readouterr().out


def _base_payload(**overrides) -> dict:
    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": SESSION_ID,
        "stop_hook_active": False,
    }
    payload.update(overrides)
    return payload


def _state_data(tmp_env: str | None = None) -> dict:
    state_file = Path(os.environ["XDG_STATE_HOME"]) / "agent-postmortem" / "state.json"
    return json.loads(state_file.read_text())


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# --- generic silence rules, shared by every subcommand ---------------------


def test_malformed_stdin_is_silent(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "check", "not { valid json")
    assert code == 0
    assert out == ""


def test_empty_stdin_is_silent(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "check", "")
    assert code == 0
    assert out == ""


@pytest.mark.parametrize("agent_id_value", ["a1234567890abcdef", "", None, 42, []])
def test_agent_id_payload_is_silent_regardless_of_value_or_type(monkeypatch, capsys, agent_id_value):
    """docs/DESIGN.md 7.2's D9: presence alone, not truthiness or type."""
    payload = _base_payload(agent_id=agent_id_value)
    code, out = _run(monkeypatch, capsys, "check", payload)
    assert code == 0
    assert out == ""


@pytest.mark.parametrize("hook_command", ["session-start", "session-end", "workflow-launched"])
@pytest.mark.parametrize("agent_id_value", ["a1234567890abcdef", "", None, 7])
def test_agent_id_presence_is_silent_on_every_subcommand(monkeypatch, capsys, hook_command, agent_id_value):
    payload = {
        "hook_event_name": {
            "session-start": "SessionStart", "session-end": "SessionEnd",
            "workflow-launched": "PostToolUse",
        }[hook_command],
        "tool_name": "Workflow",
        "source": "startup",
        "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": SESSION_ID,
        "agent_id": agent_id_value,
    }
    code, out = _run(monkeypatch, capsys, hook_command, payload)
    assert code == 0
    assert out == ""
    state_file = Path(os.environ["XDG_STATE_HOME"]) / "agent-postmortem" / "state.json"
    assert not state_file.exists()


def test_mismatched_hook_event_name_is_silent(monkeypatch, capsys):
    payload = _base_payload(hook_event_name="SomeOtherEvent")
    code, out = _run(monkeypatch, capsys, "check", payload)
    assert code == 0
    assert out == ""


def test_check_missing_session_directory_is_silent(monkeypatch, capsys, tmp_path):
    payload = _base_payload(transcript_path=str(tmp_path / "nope" / "session.jsonl"))
    code, out = _run(monkeypatch, capsys, "check", payload)
    assert code == 0
    assert out == ""


# --- capabilities ------------------------------------------------------


def test_capabilities_prints_protocol_and_version(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "capabilities", "")
    assert code == 0
    payload = json.loads(out)
    assert payload["protocol"] == 1
    assert isinstance(payload["version"], str) and payload["version"]


# --- check: notification policy -----------------------------------------


def test_check_notifies_a_new_major_finding_with_verdict_and_postmortem_hint(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "check", _base_payload())
    assert code == 0
    payload = json.loads(out)
    message = payload["systemMessage"]
    # docs/DESIGN.md 7.2: verdict, headline number, run id, '/postmortem <id>'.
    assert "DEGRADED" in message
    assert "wf_00000000-710" in message
    assert "/postmortem wf_00000000-710" in message
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert payload["hookSpecificOutput"]["additionalContext"] == message


def test_check_silences_a_major_finding_by_config_and_never_notifies_or_counts_it_toward_verdict(
    monkeypatch, capsys, tmp_path,
):
    """docs/DESIGN.md 4.10/7.2: wf_00000000-710's only findings are two
    major detectors, silent_retry and mislabeled_kill (see the baseline
    test above: DEGRADED, notified). With both silenced by config, nothing
    is left above the notify threshold (wasted_time is info) -- the hook
    must emit nothing, and the unit's own cached verdict must read CLEAN,
    never DEGRADED, and its 'reported' list must stay empty."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[detectors]\noff = ["silent_retry", "mislabeled_kill"]\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    code, out = _run(monkeypatch, capsys, "check", _base_payload())
    assert code == 0
    assert out == ""

    data = _state_data()
    unit = data["sessions"][SESSION_KEY]["units"]["subagents/workflows/wf_00000000-710"]
    assert unit["state"] == "analyzed"
    assert unit["verdict"] == "CLEAN"
    assert unit["reported"] == []


def test_check_never_notifies_the_same_finding_twice(monkeypatch, capsys):
    code1, out1 = _run(monkeypatch, capsys, "check", _base_payload())
    assert code1 == 0
    assert "wf_00000000-710" in out1

    code2, out2 = _run(monkeypatch, capsys, "check", _base_payload())
    assert code2 == 0
    assert out2 == ""


def test_check_suppresses_additional_context_when_stop_hook_active(monkeypatch, capsys):
    payload = _base_payload(stop_hook_active=True)
    code, out = _run(monkeypatch, capsys, "check", payload)
    assert code == 0
    output = json.loads(out)
    assert "systemMessage" in output
    assert "hookSpecificOutput" not in output


def test_check_running_unit_is_never_reported(monkeypatch, capsys):
    # Three checks: the finished-with-retry unit notifies once and is done;
    # the running unit (wf_00000000-720, no journal 'result') must stay
    # unreported throughout, however many times it is checked.
    for _ in range(3):
        _run(monkeypatch, capsys, "check", _base_payload())

    data = _state_data()
    running_unit = data["sessions"][SESSION_KEY]["units"]["subagents/workflows/wf_00000000-720"]
    assert running_unit["state"] in ("running", "unknown")
    if running_unit["state"] == "running":
        assert running_unit["reported"] == []


def _write_never_finishing_subagent(tmp_path, session_key_suffix: str, agent_id: str):
    """A subagent unit with no kill sentinel, no main-transcript evidence,
    and no background_tasks entry -- it can only ever reach 'unknown'
    through the stable-check/quiet fallback, never through real finished
    evidence."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    agent_path = session_dir / "subagents" / f"agent-{agent_id}.jsonl"
    agent_path.write_text(
        f'{{"type":"user","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":"Investigate something."}}\n'
        f'{{"type":"assistant","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:05Z",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"Still working on it."}],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": f"s-quiet-gate-{session_key_suffix}", "stop_hook_active": False,
    }
    return session_dir, agent_path, payload


def test_check_unknown_fallback_stays_pending_when_not_yet_quiet(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 12's D-A: real data showed the plain 3-stable-check
    rule finalizing a workflow that was still actively running, because
    three Stop checks happened to land within the same quiet minute. A unit
    touched a minute ago must stay pending past the stable-check threshold,
    not finalize, however many times it is checked."""
    session_dir, agent_path, payload = _write_never_finishing_subagent(tmp_path, "recent", "afa00000000000001")
    one_minute_ago = time.time() - 60
    os.utime(agent_path, (one_minute_ago, one_minute_ago))

    for _ in range(5):
        _run(monkeypatch, capsys, "check", payload)

    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"][f"subagents/{agent_path.name}"]
    assert unit["state"] == "running"
    assert unit["stable_checks"] >= _UNKNOWN_AFTER_STABLE_CHECKS
    assert unit["reported"] == []


def test_check_unknown_fallback_finalizes_once_quiet_for_ten_minutes(monkeypatch, capsys, tmp_path):
    """The same never-finishing unit, but its file's mtime is 11 minutes
    old throughout: once stable_checks also crosses the threshold, it
    finalizes as analyzed/INCOMPLETE exactly as the plain 3-stable-check
    rule used to on its own."""
    session_dir, agent_path, payload = _write_never_finishing_subagent(tmp_path, "old", "afa00000000000002")
    eleven_minutes_ago = time.time() - 11 * 60
    os.utime(agent_path, (eleven_minutes_ago, eleven_minutes_ago))

    for _ in range(4):
        _run(monkeypatch, capsys, "check", payload)

    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"][f"subagents/{agent_path.name}"]
    assert unit["state"] == "analyzed"
    assert "(INCOMPLETE)" in unit["verdict"]


def test_check_forced_unknown_with_no_diagnostics_or_findings_is_analyzed_but_not_notified(monkeypatch, capsys):
    # docs/DESIGN.md 7.2's D-C: wf_00000000-720 never gets a journal
    # 'result' and its files never change, so its fingerprint is stable on
    # every check after the first; the fourth check crosses the
    # 3-stable-check threshold and it is force-analyzed INCOMPLETE. But
    # that forced analysis raised no reader diagnostics of its own and
    # found nothing at or above the notify threshold -- a unit that is
    # simply still running, not a real problem -- so it must be recorded
    # (state analyzed, verdict carries "(INCOMPLETE)") without ever being
    # notified: noise with nothing a reader could act on.
    #
    # docs/DESIGN.md 12's D-A also gates finalization on the unit having
    # gone quiet (fixture files untouched for 10 minutes); this test is
    # about the stable-check counting alone, so the quiet gate is disabled
    # here (its own real behavior is covered by the D-A tests below) rather
    # than backdating a shared fixture's real mtimes.
    monkeypatch.setattr(hooks_cli, "_UNKNOWN_QUIET_SECONDS", 0)
    outputs = []
    for _ in range(5):
        _, out = _run(monkeypatch, capsys, "check", _base_payload())
        outputs.append(out)

    unknown_messages = [out for out in outputs if out and "wf_00000000-720" in out]
    assert unknown_messages == []

    data = _state_data()
    unit = data["sessions"][SESSION_KEY]["units"]["subagents/workflows/wf_00000000-720"]
    assert unit["state"] == "analyzed"
    assert "(INCOMPLETE)" in unit["verdict"]


def test_check_finished_subagent_finishes_on_the_first_check_via_main_transcript_evidence(monkeypatch, capsys):
    # docs/DESIGN.md 7.2's D2: the main transcript's own tool_result already
    # names agent-ae300000000000001's agent id, which is authoritative
    # evidence on its own -- no fingerprint-stability wait required, unlike
    # the old (and now-removed) "assistant text, no tool_use" rule.
    _run(monkeypatch, capsys, "check", _base_payload())
    data = _state_data()
    unit_id = "subagents/agent-ae300000000000001.jsonl"
    unit = data["sessions"][SESSION_KEY]["units"][unit_id]
    assert unit["state"] == "analyzed"
    # Clean content: no findings at or above the default 'major' threshold.
    assert unit["reported"] == []


def test_check_finished_subagent_finishes_via_entry_level_tool_use_result(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D-A: a real main transcript carries the Agent
    tool's result at the entry level (entry["toolUseResult"]["agentId"]),
    not inside the tool_result block -- this must be just as authoritative
    as the block-level shape the fixture above covers."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"go"},'
        '"timestamp":"2026-01-01T00:00:00Z"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"toolu_1","name":"Agent","input":{}}]},'
        '"timestamp":"2026-01-01T00:00:01Z"}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
        '"tool_use_id":"toolu_1","content":"done"}]},'
        '"toolUseResult":{"isAsync":false,"status":"completed",'
        '"agentId":"af900000000000001","description":"impl","resolvedModel":"x",'
        '"prompt":"do it","outputFile":null},'
        '"timestamp":"2026-01-01T00:00:02Z"}\n',
        encoding="utf-8",
    )
    agent_path = session_dir / "subagents" / "agent-af900000000000001.jsonl"
    agent_path.write_text(
        '{"type":"user","agentId":"af900000000000001","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":"Investigate something."}}\n'
        '{"type":"assistant","agentId":"af900000000000001","timestamp":"2026-01-01T00:00:05Z",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"Still working on it."}],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-entry-level-evidence", "stop_hook_active": False,
    }
    _run(monkeypatch, capsys, "check", payload)
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["subagents/agent-af900000000000001.jsonl"]
    assert unit["state"] == "analyzed"


def test_check_assistant_text_alone_is_not_evidence_of_finished(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D2: an assistant entry with no tool_use is
    never evidence by itself -- it is also the ordinary shape of an agent
    still waiting for its next turn. With no main-transcript evidence, no
    kill sentinel, and no background_tasks signal, the unit must stay
    pending indefinitely, not finish."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    agent_path = session_dir / "subagents" / "agent-af500000000000001.jsonl"
    agent_path.write_text(
        '{"type":"user","agentId":"af500000000000001","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":"Investigate something."}}\n'
        '{"type":"assistant","agentId":"af500000000000001","timestamp":"2026-01-01T00:00:05Z",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"Still working on it."}],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-no-evidence", "stop_hook_active": False,
    }
    for _ in range(3):
        _run(monkeypatch, capsys, "check", payload)
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["subagents/agent-af500000000000001.jsonl"]
    assert unit["state"] in ("running", "unknown")


def test_check_background_tasks_finished_status_is_evidence(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D2: a background_tasks entry naming this agent
    id with a status meaning done is authoritative on its own, even with no
    kill sentinel and no main-transcript evidence."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    agent_path = session_dir / "subagents" / "agent-af600000000000001.jsonl"
    agent_path.write_text(
        '{"type":"user","agentId":"af600000000000001","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":"Investigate something."}}\n'
        '{"type":"assistant","agentId":"af600000000000001","timestamp":"2026-01-01T00:00:05Z",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"Still working on it."}],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-bg-finished", "stop_hook_active": False,
        "background_tasks": [{"agent_id": "af600000000000001", "status": "completed"}],
    }
    _run(monkeypatch, capsys, "check", payload)
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["subagents/agent-af600000000000001.jsonl"]
    assert unit["state"] == "analyzed"


def test_check_background_tasks_running_overrides_a_kill_sentinel(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D2: a background_tasks entry naming this agent
    id as still active is checked first and overrides every other signal,
    including the agent's own kill sentinel."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    agent_path = session_dir / "subagents" / "agent-af700000000000001.jsonl"
    agent_path.write_text(
        '{"type":"user","agentId":"af700000000000001","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":"Investigate something."}}\n'
        '{"type":"user","agentId":"af700000000000001","timestamp":"2026-01-01T00:00:05Z",'
        '"message":{"role":"user","content":"[Request interrupted by user]"}}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-bg-running", "stop_hook_active": False,
        "background_tasks": ["af700000000000001"],
    }
    _run(monkeypatch, capsys, "check", payload)
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["subagents/agent-af700000000000001.jsonl"]
    assert unit["state"] not in ("analyzed",)


def test_check_trailing_partial_json_line_means_not_finished(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D2: a trailing line that fails to parse as JSON
    at all means not finished, even though an earlier, valid line is a
    kill sentinel."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    agent_path = session_dir / "subagents" / "agent-af800000000000001.jsonl"
    agent_path.write_text(
        '{"type":"user","agentId":"af800000000000001","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":"Investigate something."}}\n'
        '{"type":"user","agentId":"af800000000000001","timestamp":"2026-01-01T00:00:05Z",'
        '"message":{"role":"user","content":"[Request interrupted by user]"}}\n'
        '{"type":"assistant","agentId":"af800000000000001","timestamp":"2026-01-01T00:00:06Z",'
        '"message":{"role":"assistant","content":[{"type":"text","tex',  # deliberately truncated
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-partial-line", "stop_hook_active": False,
    }
    _run(monkeypatch, capsys, "check", payload)
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["subagents/agent-af800000000000001.jsonl"]
    assert unit["state"] != "analyzed"


def test_check_analysis_exception_counts_as_a_stable_check_and_eventually_finalizes(monkeypatch, capsys):
    """docs/DESIGN.md 7.2's D6: an analysis exception must not reset
    stable_checks to 0 (the old bug: the unit gets stuck forever); it must
    count toward the unknown threshold, and once the forced-INCOMPLETE
    analysis attempt itself also raises, the unit still finalizes as
    analyzed/INCOMPLETE and notifies exactly once, rather than looping.

    docs/DESIGN.md 12's D-A quiet gate is disabled here for the same
    reason as the test above: this test is about stable-check counting
    across repeated exceptions, not about real quiet time."""
    from agent_postmortem import reader as reader_module

    monkeypatch.setattr(hooks_cli, "_UNKNOWN_QUIET_SECONDS", 0)

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(reader_module, "load_session_view", _boom)

    unit_id = "subagents/agent-ae300000000000001.jsonl"
    outputs = []
    for _ in range(3):
        _, out = _run(monkeypatch, capsys, "check", _base_payload())
        outputs.append(out)

    data = _state_data()
    unit = data["sessions"][SESSION_KEY]["units"][unit_id]
    assert unit["state"] == "analyzed"
    assert unit["stable_checks"] == 0
    assert "(INCOMPLETE)" in unit["verdict"]

    # The third check is the one that crosses the unknown threshold and
    # finalizes it; the first two only advance stable_checks, silently.
    incomplete_messages = [out for out in outputs if out and "(inspection incomplete)" in out]
    assert len(incomplete_messages) == 1
    assert outputs[-1] == incomplete_messages[0]


def test_check_groups_multiple_finished_subagents_of_one_session_into_one_line(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D-C: several subagent units of one session share
    that session's own run id (its public s_ id), so when more than one
    finishes on the same check, their notify lines collapse into a single
    '<n> subagents finished, <k> findings' line rather than one line per
    unit (the old behavior that printed identical-looking lines back to
    back with nothing to distinguish them)."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    agent_ids = ["afb00000000000001", "afb00000000000002"]
    tool_result_blocks = ",".join(
        f'{{"type":"tool_result","tool_use_id":"toolu_{i}","agentId":"{agent_id}","content":"done"}}'
        for i, agent_id in enumerate(agent_ids)
    )
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n'
        # G-A (H1): a return needs the launch itself, so the two Agent launches are recorded
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"tool_use","id":"toolu_0","name":"Agent","input":{}},'
        '{"type":"tool_use","id":"toolu_1","name":"Agent","input":{}}]},'
        '"timestamp":"2026-01-01T00:00:00Z"}\n'
        '{"type":"user","message":{"role":"user","content":[' + tool_result_blocks + ']},'
        '"timestamp":"2026-01-01T00:00:01Z"}\n',
        encoding="utf-8",
    )
    for agent_id in agent_ids:
        agent_path = session_dir / "subagents" / f"agent-{agent_id}.jsonl"
        agent_path.write_text(
            f'{{"type":"user","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:01Z",'
            '"message":{"role":"user","content":"Emit the structured output."}}\n'
            f'{{"type":"assistant","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:02Z",'
            '"message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_so",'
            '"name":"StructuredOutput","input":{}}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
            f'{{"type":"user","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:03Z",'
            '"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_so",'
            '"content":"Output does not match required schema: root: must have required '
            'property \'ok\'","is_error":true}]}}\n',
            encoding="utf-8",
        )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-group-subagents", "stop_hook_active": False,
    }
    code, out = _run(monkeypatch, capsys, "check", payload)
    assert code == 0
    message = json.loads(out)["systemMessage"]
    assert message.count("\n") == 0  # one grouped line, not one per unit
    assert "2 subagents finished, 2 findings" in message
    assert "DEGRADED" in message
    # Exactly one run id in the whole line: the session's own s_ id, shared
    # by both units, appearing once as the run id and once after
    # '/postmortem'.
    run_ids = set(re.findall(r"s_[0-9a-f]+", message))
    assert len(run_ids) == 1
    assert message.count(next(iter(run_ids))) == 2


def test_check_quiet_unknown_fallback_with_real_findings_says_unfinished(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 12's D-C: a unit finalized purely because it went
    quiet (D-A's stable-check + mtime fallback) -- not because the reader
    itself found real diagnostics -- says '(unfinished)' in the notify
    line, never '(inspection incomplete)', even when the forced analysis
    did turn up a real threshold finding worth notifying about."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    agent_id = "afc00000000000001"
    agent_path = session_dir / "subagents" / f"agent-{agent_id}.jsonl"
    agent_path.write_text(
        f'{{"type":"user","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":"Emit the structured output."}}\n'
        f'{{"type":"assistant","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:02Z",'
        '"message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_so",'
        '"name":"StructuredOutput","input":{}}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        f'{{"type":"user","agentId":"{agent_id}","timestamp":"2026-01-01T00:00:03Z",'
        '"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_so",'
        '"content":"Output does not match required schema: root: must have required '
        'property \'ok\'","is_error":true}]}}\n',
        encoding="utf-8",
    )
    eleven_minutes_ago = time.time() - 11 * 60
    os.utime(agent_path, (eleven_minutes_ago, eleven_minutes_ago))

    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-quiet-with-findings", "stop_hook_active": False,
    }
    outputs = []
    for _ in range(4):
        _, out = _run(monkeypatch, capsys, "check", payload)
        outputs.append(out)

    messages = [out for out in outputs if out]
    assert len(messages) == 1
    message = json.loads(messages[0])["systemMessage"]
    assert "(unfinished)" in message
    assert "(inspection incomplete)" not in message


def test_check_debug_env_writes_a_redacted_traceback_to_stderr(monkeypatch, capsys):
    """docs/DESIGN.md 7.2's D10: AGENT_POSTMORTEM_DEBUG writes a redacted
    traceback to stderr on an analysis exception; exit code and pending
    state are unaffected either way."""
    from agent_postmortem import reader as reader_module

    real_uuid = "11111111-2222-4000-8000-" + "333333333333"  # assembled: never a literal in this file

    def _boom(*args, **kwargs):
        raise RuntimeError(f"synthetic parse failure, session {real_uuid}")

    monkeypatch.setattr(reader_module, "load_session_view", _boom)
    monkeypatch.setenv("AGENT_POSTMORTEM_DEBUG", "1")

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_base_payload())))
    code = main(["hook", "check"])
    captured = capsys.readouterr()
    assert code == 0
    assert "RuntimeError" in captured.err
    assert "synthetic parse failure" in captured.err
    assert real_uuid not in captured.err  # redact() masks a raw UUID to "uuid:<last 4>"
    assert "uuid:3333" in captured.err


def test_check_without_debug_env_writes_nothing_to_stderr(monkeypatch, capsys):
    from agent_postmortem import reader as reader_module

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(reader_module, "load_session_view", _boom)
    monkeypatch.delenv("AGENT_POSTMORTEM_DEBUG", raising=False)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_base_payload())))
    code = main(["hook", "check"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""


def test_check_analyzed_unit_re_stats_every_check_and_resets_when_content_changes(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D1: an analyzed unit whose journal gets another
    entry appended must go back to running, not stay stuck analyzed."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    workflows_dir = session_dir / "subagents" / "workflows" / "wf_00000000-751"
    workflows_dir.mkdir(parents=True)
    (session_dir / "subagents").mkdir(exist_ok=True)
    (workflows_dir / "journal.jsonl").write_text(
        '{"type":"launched","timestamp":"2026-01-01T00:00:00Z"}\n'
        '{"type":"started","timestamp":"2026-01-01T00:00:01Z","key":"v2:a","agentId":"af100000000000001",'
        '"label":"impl:a","phase":"Implement"}\n'
        '{"type":"result","timestamp":"2026-01-01T00:00:02Z","agentId":"af100000000000001","key":"v2:a",'
        '"outcome":"completed"}\n',
        encoding="utf-8",
    )
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-restat", "stop_hook_active": False,
    }
    _run(monkeypatch, capsys, "check", payload)
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit_id = "subagents/workflows/wf_00000000-751"
    assert _state_data()["sessions"][key]["units"][unit_id]["state"] == "analyzed"

    # Content changes after being analyzed: another key started.
    with (workflows_dir / "journal.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            '{"type":"started","timestamp":"2026-01-01T00:05:00Z","key":"v2:b","agentId":"af100000000000002",'
            '"label":"impl:b","phase":"Implement"}\n'
        )
    _run(monkeypatch, capsys, "check", payload)
    unit = _state_data()["sessions"][key]["units"][unit_id]
    assert unit["state"] == "running"
    assert unit["stable_checks"] == 0


def test_check_deleted_and_recreated_unit_is_re_evaluated(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D1: a unit id that vanishes and reappears
    (deleted, then recreated) must not stay stuck at its old analyzed
    state -- its fingerprint necessarily differs."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    unit_dir = session_dir / "subagents" / "workflows" / "wf_00000000-752"
    unit_dir.mkdir(parents=True)

    def _write_journal(agent_id: str):
        unit_dir.joinpath("journal.jsonl").write_text(
            '{"type":"launched","timestamp":"2026-01-01T00:00:00Z"}\n'
            f'{{"type":"started","timestamp":"2026-01-01T00:00:01Z","key":"v2:a","agentId":"{agent_id}",'
            '"label":"impl:a","phase":"Implement"}\n'
            f'{{"type":"result","timestamp":"2026-01-01T00:00:02Z","agentId":"{agent_id}","key":"v2:a",'
            '"outcome":"completed"}\n',
            encoding="utf-8",
        )

    _write_journal("af200000000000001")
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-recreate", "stop_hook_active": False,
    }
    _run(monkeypatch, capsys, "check", payload)
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit_id = "subagents/workflows/wf_00000000-752"
    first = _state_data()["sessions"][key]["units"][unit_id]
    assert first["state"] == "analyzed"

    (unit_dir / "journal.jsonl").unlink()
    (unit_dir / "agent-af200000000000001.jsonl").unlink(missing_ok=True)
    _write_journal("af200000000000002")
    _run(monkeypatch, capsys, "check", payload)
    unit = _state_data()["sessions"][key]["units"][unit_id]
    # G-A1 (GA-8): the re-opened unit re-evaluates its finished evidence in the same check, and
    # the recreated journal is still finished, so it is analyzed again at once under a new
    # fingerprint (was: parked as `running`)
    assert unit["fingerprint"] != first["fingerprint"]
    assert unit["state"] == "analyzed"
    assert unit["stable_checks"] == 0


# --- workflow-launched ---------------------------------------------------


def test_workflow_launched_registers_a_running_priority_unit_with_its_locator(monkeypatch, capsys, tmp_path):
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    # docs/DESIGN.md 7.2's release-hardening note (F12): transcriptDir is
    # only ever honored when it names a location inside this session's own
    # subagents/workflows/ directory, so this fixture uses that shape
    # (release finding F12) rather than an arbitrary path.
    valid_transcript_dir = str(session_dir / "subagents" / "workflows" / "wf_00000000-798")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Workflow",
        "transcript_path": str(transcript),
        "session_id": "s-workflow-launched",
        "tool_response": {"runId": "wf_00000000-798", "transcriptDir": valid_transcript_dir},
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    # .reviews/proposals/2026-09-v0.2-guided-rescue.md section 7: this used
    # to assert out == "" (docs/DESIGN.md 7.2 pre-M5 said "emit nothing").
    # workflow-launched now hands the session the watch command whenever it
    # could read a run id, so a successful registration also emits the hint.
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "Workflow wf_00000000-798 launched. To notice a killed or failed agent "
                "within seconds instead of hours, arm a Monitor now with this command: "
                "agent-postmortem watch wf_00000000-798"
            ),
        }
    }

    key = _session_key(session_dir.parent.parent, session_dir.name)
    data = _state_data()
    unit = data["sessions"][key]["units"]["subagents/workflows/wf_00000000-798"]
    assert unit["state"] == "running"
    assert unit["priority"] is True
    assert unit["transcript_dir"] == valid_transcript_dir


def test_workflow_launched_falls_back_to_the_run_id_regex(monkeypatch, capsys, tmp_path):
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Workflow",
        "transcript_path": str(transcript),
        "session_id": "s-workflow-regex",
        "tool_response": "Launched workflow. Run ID: wf_00000000-799",
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    # See the locator test above: was out == "" before unit M5.
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == (
        "Workflow wf_00000000-799 launched. To notice a killed or failed agent "
        "within seconds instead of hours, arm a Monitor now with this command: "
        "agent-postmortem watch wf_00000000-799"
    )

    key = _session_key(session_dir.parent.parent, session_dir.name)
    data = _state_data()
    assert "subagents/workflows/wf_00000000-799" in data["sessions"][key]["units"]


def test_workflow_launched_wrong_tool_name_is_silent(monkeypatch, capsys):
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": SESSION_ID,
        "tool_response": {"runId": "wf_00000000-800"},
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    assert out == ""
    state_file = Path(os.environ["XDG_STATE_HOME"]) / "agent-postmortem" / "state.json"
    assert not state_file.exists()


def test_workflow_launched_hint_opted_out_by_env_var(monkeypatch, capsys, tmp_path):
    """.reviews/proposals/2026-09-v0.2-guided-rescue.md section 7: 'Opt out:
    AGENT_POSTMORTEM_WATCH_HINT=0'. Registration still happens -- only the
    hint is suppressed, back to the pre-M5 'emit nothing'."""
    monkeypatch.setenv("AGENT_POSTMORTEM_WATCH_HINT", "0")
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Workflow",
        "transcript_path": str(transcript),
        "session_id": "s-workflow-hint-opt-out",
        "tool_response": {"runId": "wf_00000000-801"},
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    assert out == ""

    key = _session_key(session_dir.parent.parent, session_dir.name)
    data = _state_data()
    assert "subagents/workflows/wf_00000000-801" in data["sessions"][key]["units"]


@pytest.mark.parametrize(
    "bad_run_id",
    [
        "wf_00000000-798\nmalicious",
        "wf_00000000-798 extra",
        "wf_00000000-79",  # too short: fails the 8-hex+dash+3-hex grammar
        "wf_00000000-7980",  # too long
        "not-even-close",
    ],
)
def test_workflow_launched_structured_run_id_must_full_match_the_canonical_grammar(
    monkeypatch, capsys, tmp_path, bad_run_id,
):
    """release finding F12: a structured runId is accepted only when it
    full-matches docs/DESIGN.md 5.1's canonical grammar (reader._WORKFLOW_
    RUN_ID_RE: 'wf_' + 8 hex + '-' + 3 hex). Anything else -- a newline or
    trailing text that could inject content into additionalContext, or a
    value simply the wrong shape -- means no run id was read at all: the
    hook emits nothing and registers nothing, the same as
    test_workflow_launched_no_run_id_emits_nothing."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Workflow",
        "transcript_path": str(transcript),
        "session_id": "s-workflow-bad-run-id",
        "tool_response": {"runId": bad_run_id, "transcriptDir": str(session_dir / "subagents" / "workflows" / "x")},
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    assert out == ""
    state_file = Path(os.environ["XDG_STATE_HOME"]) / "agent-postmortem" / "state.json"
    assert not state_file.exists()


def test_workflow_launched_transcript_dir_outside_the_session_is_dropped(monkeypatch, capsys, tmp_path):
    """release finding F12: transcriptDir, after normalization, must be
    inside this session's own subagents/workflows/ directory (the same
    session_dir `check` derives from transcript_path). A value outside it
    is dropped -- treated exactly as if transcriptDir had never been given
    -- but the run id is still valid, so registration and the watch hint
    proceed normally; only the untrusted locator itself is refused."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    outside = tmp_path / "elsewhere-outside-the-session"
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Workflow",
        "transcript_path": str(transcript),
        "session_id": "s-workflow-bad-transcript-dir",
        "tool_response": {"runId": "wf_00000000-799", "transcriptDir": str(outside)},
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == (
        "Workflow wf_00000000-799 launched. To notice a killed or failed agent "
        "within seconds instead of hours, arm a Monitor now with this command: "
        "agent-postmortem watch wf_00000000-799"
    )
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["subagents/workflows/wf_00000000-799"]
    assert unit["state"] == "running"
    assert "transcript_dir" not in unit


def test_workflow_launched_no_run_id_emits_nothing(monkeypatch, capsys):
    """docs/DESIGN.md 7.2: the hint is emitted only 'when it could read a run
    id'; with neither a structured runId nor a regex-matchable one, nothing
    is registered and nothing is emitted."""
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Workflow",
        "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": "s-workflow-no-run-id",
        "tool_response": {"note": "no run id anywhere in here"},
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    assert out == ""


def test_workflow_launched_as_the_first_call_of_a_session_needs_no_subagents_directory(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D4/D8: a workflow can be the very first thing a
    session ever does; the session's own subagents/ directory (and the
    unit's own conventional directory) need not exist yet."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    session_dir.parent.mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    assert not session_dir.exists()

    # release finding F12: transcriptDir must be inside this session's own
    # subagents/workflows/ directory (it need not exist on disk yet -- that
    # is the whole point of this test, D8).
    valid_transcript_dir = str(session_dir / "subagents" / "workflows" / "wf_00000000-753")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Workflow",
        "transcript_path": str(transcript),
        "session_id": "s-workflow-first-call",
        "tool_response": {"runId": "wf_00000000-753", "transcriptDir": valid_transcript_dir},
    }
    code, out = _run(monkeypatch, capsys, "workflow-launched", payload)
    assert code == 0
    # See the locator test above: was out == "" before unit M5.
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == (
        "Workflow wf_00000000-753 launched. To notice a killed or failed agent "
        "within seconds instead of hours, arm a Monitor now with this command: "
        "agent-postmortem watch wf_00000000-753"
    )
    key = _session_key(session_dir.parent.parent, session_dir.name)
    data = _state_data()
    assert data["sessions"][key]["units"]["subagents/workflows/wf_00000000-753"]["state"] == "running"


def test_check_uses_the_stored_transcript_dir_when_the_conventional_path_is_absent(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D8: `check` must be able to inspect a unit
    workflow-launched registered by its transcriptDir, before the
    conventional subagents/workflows/<run id> directory exists.

    release finding F12: transcriptDir is only ever honored when it is
    inside this session's own subagents/workflows/ directory, so "elsewhere"
    is nested there too -- named so it does not start with "wf_", the one
    prefix _discover_units's own glob looks for, so this still exercises the
    stored-locator fallback rather than plain directory discovery."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    elsewhere = session_dir / "subagents" / "workflows" / "not-discovered-754"
    elsewhere.mkdir(parents=True)
    elsewhere.joinpath("journal.jsonl").write_text(
        '{"type":"launched","timestamp":"2026-01-01T00:00:00Z"}\n'
        '{"type":"started","timestamp":"2026-01-01T00:00:01Z","key":"v2:a","agentId":"af300000000000001",'
        '"label":"impl:a","phase":"Implement"}\n'
        '{"type":"result","timestamp":"2026-01-01T00:00:02Z","agentId":"af300000000000001","key":"v2:a",'
        '"outcome":"completed"}\n',
        encoding="utf-8",
    )

    launched_payload = {
        "hook_event_name": "PostToolUse", "tool_name": "Workflow",
        "transcript_path": str(transcript), "session_id": "s-stored-locator",
        "tool_response": {"runId": "wf_00000000-754", "transcriptDir": str(elsewhere)},
    }
    _run(monkeypatch, capsys, "workflow-launched", launched_payload)

    check_payload = {
        "hook_event_name": "Stop", "transcript_path": str(transcript),
        "session_id": "s-stored-locator", "stop_hook_active": False,
    }
    _run(monkeypatch, capsys, "check", check_payload)

    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["subagents/workflows/wf_00000000-754"]
    assert unit["state"] == "analyzed"


# --- session-end / session-start -----------------------------------------


def test_session_end_is_silent_and_records_an_analyzed_main_unit(monkeypatch, capsys):
    payload = {
        "hook_event_name": "SessionEnd",
        "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": SESSION_ID,
    }
    code, out = _run(monkeypatch, capsys, "session-end", payload)
    assert code == 0
    assert out == ""

    data = _state_data()
    main_unit = data["sessions"][SESSION_KEY]["units"]["main"]
    assert main_unit["state"] == "analyzed"
    assert f"{SESSION_KEY}::main" in data["digest"]["pending"]


def test_session_end_runs_only_tooling_detectors_never_mislabeled_kill(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D5: session-end's own main-only view has no
    children at all, so mislabeled_kill/missing_artifact (which need a
    child attempt, or a first user message naming a path) can never surface
    through it regardless."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    session_dir.parent.mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"done"}],'
        '"usage":{"input_tokens":1,"output_tokens":1}},"timestamp":"2026-01-01T00:00:05Z"}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "SessionEnd", "transcript_path": str(transcript),
        "session_id": "s-session-end-tooling-only",
    }
    code, out = _run(monkeypatch, capsys, "session-end", payload)
    assert code == 0
    assert out == ""
    key = _session_key(session_dir.parent.parent, session_dir.name)
    unit = _state_data()["sessions"][key]["units"]["main"]
    assert unit["state"] == "analyzed"
    assert unit["reported"] == []


def test_session_end_needs_no_subagents_directory(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D4: a session that never spawned a subagent
    (the common case) must still get a session-end analysis."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    session_dir.parent.mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    assert not session_dir.exists()
    payload = {
        "hook_event_name": "SessionEnd", "transcript_path": str(transcript),
        "session_id": "s-session-end-no-subagents",
    }
    code, out = _run(monkeypatch, capsys, "session-end", payload)
    assert code == 0
    assert out == ""
    key = _session_key(session_dir.parent.parent, session_dir.name)
    data = _state_data()
    assert data["sessions"][key]["units"]["main"]["state"] == "analyzed"


def test_session_start_skips_sources_other_than_startup_or_resume(monkeypatch, capsys):
    payload = {
        "hook_event_name": "SessionStart",
        "source": "compact",
        "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": SESSION_ID,
    }
    code, out = _run(monkeypatch, capsys, "session-start", payload)
    assert code == 0
    assert out == ""


def test_session_start_digests_units_analyzed_by_an_earlier_check(monkeypatch, capsys):
    _run(monkeypatch, capsys, "check", _base_payload())  # analyzes wf_00000000-710

    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": SESSION_ID,
    }
    code, out = _run(monkeypatch, capsys, "session-start", payload)
    assert code == 0
    assert out != ""
    message = json.loads(out)["systemMessage"]
    assert "DEGRADED" in message
    assert "/postmortem" in message

    data = _state_data()
    assert data["digest"]["pending"] == []


def test_session_start_needs_no_subagents_directory_for_its_own_session(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D4: session-start must still compute `root` (to
    run its cross-session sweep) for a session that never spawned a
    subagent of its own."""
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    session_dir.parent.mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    assert not session_dir.exists()
    payload = {
        "hook_event_name": "SessionStart", "source": "startup",
        "transcript_path": str(transcript), "session_id": "s-start-no-subagents",
    }
    code, out = _run(monkeypatch, capsys, "session-start", payload)
    assert code == 0
    assert out == ""  # nothing pending anywhere under this root


def test_session_start_silent_when_nothing_to_digest(monkeypatch, capsys, tmp_path):
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    (session_dir / "subagents").mkdir(parents=True)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},'
        '"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "transcript_path": str(transcript),
        "session_id": "s-nothing-to-digest",
    }
    code, out = _run(monkeypatch, capsys, "session-start", payload)
    assert code == 0
    assert out == ""


def test_session_start_digest_cap_leaves_the_rest_for_next_time(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D11: 21 finished units under one root, 20
    analyzed and digested in one SessionStart, one left pending."""
    root = tmp_path / "root"
    slug_dir = root / "-home-user-project"
    session_id = "00000000-0000-4000-8000-000000000000"
    session_dir = slug_dir / session_id
    workflows_dir = session_dir / "subagents" / "workflows"
    (session_dir / "subagents").mkdir(parents=True)
    main_transcript = slug_dir / f"{session_id}.jsonl"
    main_transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    for i in range(21):
        run_dir = workflows_dir / f"wf_00000000-{i:03x}"
        run_dir.mkdir(parents=True)
        agent_id = "af4" + format(i, "014x")
        run_dir.joinpath("journal.jsonl").write_text(
            '{"type":"launched","timestamp":"2026-01-01T00:00:00Z"}\n'
            f'{{"type":"started","timestamp":"2026-01-01T00:00:01Z","key":"v2:k{i}","agentId":"{agent_id}",'
            '"label":"impl:a","phase":"Implement"}\n'
            f'{{"type":"result","timestamp":"2026-01-01T00:00:02Z","agentId":"{agent_id}","key":"v2:k{i}",'
            '"outcome":"completed"}\n',
            encoding="utf-8",
        )
    payload = {
        "hook_event_name": "SessionStart", "source": "startup",
        "transcript_path": str(main_transcript), "session_id": session_id,
    }
    code, out = _run(monkeypatch, capsys, "session-start", payload)
    assert code == 0
    assert out != ""
    message = json.loads(out)["systemMessage"]
    assert "20 runs analyzed" in message

    key = _session_key(root, session_id)
    data = _state_data()
    units = data["sessions"][key]["units"]
    analyzed = [u for u in units.values() if u.get("state") == "analyzed"]
    assert len(analyzed) == 20
    # The 21st unit was never reached this call (the cap breaks the loop
    # before it), so it is not even in unit_states yet -- there is nothing
    # of this root left in digest.pending; the 21st is left for the next
    # check/session-start entirely, not sitting pre-digested.
    assert len(units) == 20
    assert data["digest"]["pending"] == []


def test_session_start_only_digests_its_own_root(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.5's D3: a pending digest entry qualified under one
    root must survive a SessionStart in a different root untouched. Reusing
    SESSION_ID for the other root too is deliberate: the same session id
    under two different roots is exactly the collision D3 makes safe."""
    other_root = tmp_path / "other-root"
    other_slug = other_root / "-home-other-project"
    other_session = SESSION_ID
    other_session_dir = other_slug / other_session
    (other_session_dir / "subagents").mkdir(parents=True)
    other_transcript = other_slug / f"{other_session}.jsonl"
    other_transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    other_key = _session_key(other_root, other_session)
    other_qualified = f"{other_key}::main"
    from agent_postmortem import state as state_module

    with state_module.transaction() as data:
        sessions = data.setdefault("sessions", {})
        sessions[other_key] = {
            "units": {
                "main": {
                    "fingerprint": [], "state": "analyzed", "stable_checks": 0, "reported": [],
                    "verdict": "CLEAN", "wasted_seconds": 0.0, "run_id": "s_other0000",
                }
            },
            "last_check": None,
        }
        digest = data.setdefault("digest", {"pending": []})
        digest["pending"].append(other_qualified)

    payload = {
        "hook_event_name": "SessionStart", "source": "startup",
        "transcript_path": str(MAIN_TRANSCRIPT), "session_id": SESSION_ID,
    }
    _run(monkeypatch, capsys, "session-start", payload)

    data = _state_data()
    assert other_qualified in data["digest"]["pending"]


# --- F2 (M1 review finding 2): a malformed 'started' entry must not --------
# --- crash the check hook path ----------------------------------------------

MALFORMED_FIELDS_ROOT = (
    Path(__file__).parent / "fixtures" / "runs" / "journal_malformed_fields"
)
MALFORMED_FIELDS_TRANSCRIPT = (
    MALFORMED_FIELDS_ROOT / "-home-user-project" / "00000000-0000-4000-8000-000000000000.jsonl"
)


def test_check_does_not_crash_on_a_workflow_with_malformed_started_fields(monkeypatch, capsys):
    """tests/fixtures/runs/journal_malformed_fields carries a 'started' entry
    with a non-string agentId, one with a non-string key, one with a
    non-string label, and one with a non-string phase (docs/DESIGN.md 4.6).
    Before the fix, a non-string agentId crashed reader._valid_agent_id with
    a TypeError; check must complete with exit 0 and, if it prints anything
    at all, well-formed JSON."""
    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(MALFORMED_FIELDS_TRANSCRIPT),
        "session_id": "00000000-0000-4000-8000-000000000000",
        "stop_hook_active": False,
    }
    code, out = _run(monkeypatch, capsys, "check", payload)  # must not raise
    assert code == 0
    if out:
        json.loads(out)
