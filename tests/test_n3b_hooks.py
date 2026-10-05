"""N3b Part 3: hook messages at share with stable aliases (P17, P8, 7.2).

Every hook renders share; there is no level flag. The one hook message that
can name an identity is `check` (the agents behind the findings that
triggered it); the other subcommands name only run ids and register nothing.
One registration per hook invocation. When the alias book is unavailable the
names are positional and the diagnostic sits inside the hook's one JSON
object: in systemMessage, and in additionalContext whenever that field is
emitted. stdout is one valid JSON object and nothing else.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
from pathlib import Path

import pytest

from agent_postmortem import disclosure, state
from agent_postmortem.cli import main
from tests import n3b_helpers as h

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "hook_session" / "-home-user-project"
SESSION_ID = "00000000-0000-4000-8000-000000000000"
MAIN_TRANSCRIPT = FIXTURE / f"{SESSION_ID}.jsonl"
# the two agents of wf_00000000-710: one mislabeled kill, one silent retry
AGENT_KILLED = "ae100000000000001"
AGENT_RETRY = "ae100000000000002"
DIAGNOSTIC = disclosure.UNAVAILABLE_DIAGNOSTIC


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _notify_at_minor(monkeypatch):
    """N5b (4.13.4): the runtime kill of wf_00000000-710's first attempt has a
    short silence and is not final, so it is an info finding (N5 fix 1: its
    silent_retry carries the loss), below the default notify threshold. These
    tests are about naming both agents behind the findings, so the threshold is
    lowered to info and the two-agent message they were written for still
    arises. The default-threshold message is pinned in tests/test_n5b_verdict.py."""
    monkeypatch.setenv("AGENT_POSTMORTEM_NOTIFY", "info")


def _payload(**overrides) -> dict:
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(MAIN_TRANSCRIPT),
        "session_id": SESSION_ID, "stop_hook_active": False,
    }
    payload.update(overrides)
    return payload


def _hook(monkeypatch, capsys, command: str, payload: dict):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    code = main(["hook", command])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _one_object(out: str) -> dict:
    """stdout is exactly one JSON object and a newline."""
    assert out.endswith("\n") and out.count("\n") == 1, repr(out)
    value = json.loads(out)
    assert isinstance(value, dict)
    return value


def _count_registrations(monkeypatch) -> list:
    calls: list = []
    real = disclosure.register

    def counting(identities, **kwargs):
        calls.append(list(identities))
        return real(identities, **kwargs)

    monkeypatch.setattr(disclosure, "register", counting)
    return calls


def _alias(kind: str, real: str) -> str:
    return h.alias_of(kind, real)


# --- check, stable aliases --------------------------------------------------


def test_check_names_the_agents_by_stable_alias_in_every_output_form(monkeypatch, capsys):
    code, out, err = _hook(monkeypatch, capsys, "check", _payload())
    assert code == 0 and err == ""
    output = _one_object(out)
    message = output["systemMessage"]
    killed, retry = (f"a:{_alias('agent', a)}" for a in (AGENT_KILLED, AGENT_RETRY))
    assert f"agents {killed}, {retry}" in message or f"agents {retry}, {killed}" in message
    assert message.endswith("/postmortem wf_00000000-710") or "/postmortem wf_00000000-710" in message
    assert output["hookSpecificOutput"]["additionalContext"] == message
    assert DIAGNOSTIC not in message
    for real in (AGENT_KILLED, AGENT_RETRY, SESSION_ID, "-home-user-project", "impl:alpha"):
        assert real not in out


def test_check_stop_hook_active_form_carries_the_alias_in_system_message_only(monkeypatch, capsys):
    _code, out, _err = _hook(monkeypatch, capsys, "check", _payload(stop_hook_active=True))
    output = _one_object(out)
    assert "hookSpecificOutput" not in output
    assert f"a:{_alias('agent', AGENT_KILLED)}" in output["systemMessage"]


def test_check_aliases_are_stable_across_invocations(monkeypatch, capsys, tmp_path):
    _code, first, _err = _hook(monkeypatch, capsys, "check", _payload())
    (tmp_path / "state" / "agent-postmortem" / "state.json").unlink()
    _code, second, _err = _hook(monkeypatch, capsys, "check", _payload())
    assert first == second and "a:" in first


def test_check_registers_once_with_both_agents(monkeypatch, capsys):
    calls = _count_registrations(monkeypatch)
    _hook(monkeypatch, capsys, "check", _payload())
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted([("agent", AGENT_KILLED), ("agent", AGENT_RETRY)])


def test_check_with_nothing_to_notify_registers_nothing_and_creates_no_registry(
    monkeypatch, capsys, tmp_path,
):
    calls = _count_registrations(monkeypatch)
    _hook(monkeypatch, capsys, "check", _payload())  # notifies
    calls.clear()
    _code, out, _err = _hook(monkeypatch, capsys, "check", _payload())  # same finding: silent
    assert out == "" and calls == []


# --- check, fallback ---------------------------------------------------------


@pytest.mark.parametrize("active", [False, True])
def test_check_fallback_is_positional_with_the_diagnostic_inside_the_json(
    active, monkeypatch, capsys, tmp_path,
):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    _code, out, err = _hook(monkeypatch, capsys, "check", _payload(stop_hook_active=active))
    assert err == ""
    output = _one_object(out)
    message = output["systemMessage"]
    assert DIAGNOSTIC in message
    assert re.search(r"agents agent-1, agent-2\b", message)
    assert "a:" not in message
    if active:
        assert "hookSpecificOutput" not in output
    else:
        assert output["hookSpecificOutput"]["additionalContext"] == message
        assert DIAGNOSTIC in output["hookSpecificOutput"]["additionalContext"]
    for real in (AGENT_KILLED, AGENT_RETRY, SESSION_ID, "-home-user-project"):
        assert real not in out


def test_check_falls_back_instead_of_waiting_when_the_lock_is_held(monkeypatch, capsys, tmp_path):
    """The registration waits no longer than N3a's lock timeout; here made
    short so the test is fast. The hook's own state transaction ran before
    and released the lock; a second holder then blocks only the registration."""
    monkeypatch.setattr(state, "LOCK_TIMEOUT_SECONDS", 0.2)
    real = disclosure.register
    handles: list[int] = []

    def hold_then_register(identities, **kwargs):
        directory = state.state_dir()
        fd = os.open(directory / "state.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        handles.append(fd)
        return real(identities, **kwargs)

    monkeypatch.setattr(disclosure, "register", hold_then_register)
    try:
        _code, out, _err = _hook(monkeypatch, capsys, "check", _payload())
    finally:
        for fd in handles:
            os.close(fd)
    message = _one_object(out)["systemMessage"]
    assert DIAGNOSTIC in message and "agents agent-1, agent-2" in message


# --- the other subcommands ---------------------------------------------------


def test_session_start_session_end_and_workflow_launched_register_nothing(
    monkeypatch, capsys,
):
    calls = _count_registrations(monkeypatch)
    _hook(monkeypatch, capsys, "check", _payload())
    calls.clear()
    code, out, err = _hook(
        monkeypatch, capsys, "workflow-launched",
        _payload(hook_event_name="PostToolUse", tool_name="Workflow",
                 tool_response={"runId": "wf_00000000-710"}),
    )
    assert code == 0 and err == ""
    message = _one_object(out)["hookSpecificOutput"]["additionalContext"]
    assert "wf_00000000-710" in message and DIAGNOSTIC not in message
    _hook(monkeypatch, capsys, "session-end",
          _payload(hook_event_name="SessionEnd"))
    code, out, err = _hook(monkeypatch, capsys, "session-start",
                           _payload(hook_event_name="SessionStart", source="startup"))
    assert code == 0 and err == ""
    if out:
        message = _one_object(out)["systemMessage"]
        assert DIAGNOSTIC not in message
        for real in (SESSION_ID, "-home-user-project"):
            assert real not in message
    assert calls == []


@pytest.mark.parametrize("command", ["check", "session-start", "session-end", "workflow-launched"])
@pytest.mark.parametrize("flag", ["--local", "--raw", "--no-redact"])
def test_no_hook_subcommand_has_a_path_to_local_or_raw(command, flag, capsys):
    with pytest.raises(SystemExit):
        main(["hook", command, flag])
    capsys.readouterr()
