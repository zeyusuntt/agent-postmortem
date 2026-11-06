"""`agent-postmortem doctor`: can alerts reach this session? ("no way to
tell quiet because healthy from quiet because broken"; docs/DESIGN.md 4.14.12). The plugin's normal
success is silence, and its failures are silent too, so one command says which it is.

Synthetic data only: the fake session id, paths under tmp_path.
"""

from __future__ import annotations

import io
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from live_helpers import FAKE_UUID, assistant_text
from test_worker_notice import PERMISSION, SHORT, Manager, _ago, _hook

from agent_postmortem import doctor, hook_gate, stalls, state, workers
from agent_postmortem.cli import main

_WRAPPER = Path(__file__).resolve().parent.parent / "plugin" / "bin" / "agent-postmortem"


def _versioned(m: Manager, version: str = "2.1.318") -> None:
    entries = [*m.entries, assistant_text(None, _ago(30), "done for now")]
    for entry in entries:
        entry["version"] = version
    m.entries = []
    m.append(*entries)


def _plugin(tmp_path, *, stop_watch: bool = True, enabled: bool = True, version: str = "0.5.0"):
    """A plugin install laid out as Claude Code keeps it, in a directory marketplace."""
    home = tmp_path / "plugins"
    folder = tmp_path / "market" / "plugin"
    (folder / "hooks").mkdir(parents=True)
    (folder / ".claude-plugin").mkdir()
    (tmp_path / "market" / ".claude-plugin").mkdir()
    (tmp_path / "market" / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"plugins": [{"name": "agent-postmortem", "source": "./plugin"}]}), encoding="utf-8")
    (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    hooks = {"Stop": [{"hooks": [{"command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" check'}]}],
             "PostToolUse": [{"hooks": [{"command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" post-tool'}]}],
             "UserPromptSubmit": [{"hooks": [{"command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" prompt-submit'}]}]}
    if stop_watch:
        hooks["Stop"].append({"hooks": [{"command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" stop-watch', "asyncRewake": True}]})
    (folder / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    (folder / "hooks" / "run.sh").write_text("#!/usr/bin/env bash\n")
    (folder / "hooks" / "run.sh").chmod(0o755)
    stamp = time.time() - 86400
    os.utime(folder / "hooks" / "hooks.json", (stamp, stamp))
    home.mkdir()
    (home / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "agent-postmortem@agent-postmortem": [
            {"installPath": str(tmp_path / "cache" / "0.4.0"), "version": "0.4.0"}]}}))
    (home / "known_marketplaces.json").write_text(json.dumps({"agent-postmortem": {
        "source": {"source": "directory", "path": str(tmp_path / "market")}}}))
    (tmp_path / "settings.json").write_text(json.dumps(
        {"enabledPlugins": {"agent-postmortem@agent-postmortem": enabled}}))
    return home


def _run(m: Manager, tmp_path, capsys, monkeypatch, *flags, session: str | None = FAKE_UUID,
         native: bool = False, hook_cli_checked: bool = False):
    if session:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    else:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(doctor, "_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(doctor, "_plugins_root", lambda: tmp_path / "plugins")
    if not native:
        monkeypatch.setattr(doctor, "_claude_path", lambda: None)  # never the real one in a test
    if not hook_cli_checked:  # this machine's PATH is no fixture
        monkeypatch.setattr(doctor, "_hook_cli_found", lambda: True)
    code = main(["doctor", "--root", str(m.main.parent.parent), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _row(out: str, name: str) -> str:
    return next(line for line in out.splitlines() if line.split()[1:2] == [name] or name in line[:24])


def test_a_healthy_setup_says_nothing_would_stop_an_alert(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=10)
    _versioned(m)
    _plugin(tmp_path)
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {FAKE_UUID: "bg"})
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 0, out
    first = out.splitlines()[0]
    assert first == "DOCTOR  nothing here would stop an alert reaching this session", out
    assert "2.1.318" in out and "1 launched by this session" in out
    assert "agent-postmortem doctor --test-alert" in out
    assert " FAIL " not in out and " WARN " not in out, out


@pytest.mark.parametrize(
    "setup,word,code",
    [
        pytest.param("no plugin", "FAIL", 3),
        pytest.param("disabled", "FAIL", 3),
        pytest.param("old plugin", "WARN", 0),
        pytest.param("old claude", "WARN", 0),
        pytest.param("wake off", "WARN", 0),
    ],
)
def test_each_way_alerts_can_be_lost_is_named(tmp_path, capsys, monkeypatch, setup, word, code):
    m = Manager(tmp_path)
    _versioned(m, "2.1.300" if setup == "old claude" else "2.1.318")
    if setup == "no plugin":
        (tmp_path / "plugins").mkdir()
    else:
        _plugin(tmp_path, enabled=setup != "disabled", stop_watch=setup != "old plugin")
    if setup == "wake off":
        monkeypatch.setenv("AGENT_POSTMORTEM_WAKE", "0")
    got, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert got == code, out
    assert f" {word} " in out, out
    first = out.splitlines()[0]
    assert first.startswith("DOCTOR  alerts will not reach you:" if word == "FAIL"
                            else "DOCTOR  alerts can reach you, with a limit:"), first


def test_a_state_directory_it_cannot_write_is_a_failure(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)

    def broken(path):
        raise state.StateError("cache unavailable")

    monkeypatch.setattr(state, "cache_transaction", broken)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    # a per-call line still goes out, though it may repeat: a limit, not "will not reach you"
    assert code == 0 and "not writable" in out and " WARN " in out, out
    assert out.splitlines()[0].startswith("DOCTOR  alerts can reach you, with a limit:"), out


def test_unreadable_transcripts_are_a_failure(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _plugin(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", FAKE_UUID)
    monkeypatch.setattr(doctor, "_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(doctor, "_plugins_root", lambda: tmp_path / "plugins")
    monkeypatch.setattr(doctor, "_claude_path", lambda: None)
    assert main(["doctor", "--root", str(tmp_path / "nowhere")]) == 3
    assert "transcripts" in capsys.readouterr().out
    assert m.main.exists()


def test_a_session_older_than_the_plugins_hooks_is_told_to_reload(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    hooks = tmp_path / "market" / "plugin" / "hooks" / "hooks.json"
    os.utime(hooks, (time.time(), time.time()))
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert "/reload-plugins" in out and " WARN " in out, out


def test_a_different_bundled_cli_is_named(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    bundled = tmp_path / "plugins" / "data" / "agent-postmortem-agent-postmortem" / "bin"
    bundled.mkdir(parents=True)
    (bundled / "agent-postmortem").write_text("#!/bin/sh\n")
    (bundled / "agent-postmortem").chmod(0o755)  # run.sh uses only an executable one
    this_one = tmp_path / "elsewhere" / "agent-postmortem"
    this_one.parent.mkdir()
    this_one.write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.argv", [str(this_one)])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert "the plugin's hooks run their own copy" in out, out


def test_outside_a_session_it_says_what_it_could_not_check(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, session=None)
    assert code == 0 and "no session named" in out and "--session" in out, out
    assert "1 launched" not in out


def test_json_carries_the_same_checks(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--json")
    payload = json.loads(out)
    assert payload["document"] == "doctor" and payload["schema"] == 3
    assert payload["disclosure"] == "share"
    names = {check["name"] for check in payload["checks"]}
    assert {"cli", "plugin", "claude code", "transcripts", "state", "idle watcher"} <= names
    assert all(check["status"] in ("OK", "WARN", "FAIL", "INFO") for check in payload["checks"])


def test_it_names_workers_from_local_only(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    _versioned(m)
    _plugin(tmp_path)
    _code, share, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert "night-run" not in share and SHORT not in share
    _code, local, _err = _run(m, tmp_path, capsys, monkeypatch, "--local")
    assert "night-run" in local and SHORT in local


def test_it_prints_no_path_below_raw(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    for flags in ((), ("--local",)):
        _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, *flags)
        assert str(tmp_path) not in out, out


# --- the test alert ------------------------------------------------------------------------------


def test_a_test_alert_arrives_at_the_next_tool_call_once(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    assert _hook(monkeypatch, capsys, m) == ""  # a session with nothing to check
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    assert code == 0 and "at your next tool call or prompt" in out and "ALERT TEST" in out, out
    text = _hook(monkeypatch, capsys, m)
    assert text == doctor.TEST_ALERT, text  # seen live: it once came with "... and -1 more"
    assert "Nothing is wrong" in text and len(text) <= 400
    assert _hook(monkeypatch, capsys, m) == ""


def test_the_test_alert_comes_with_what_else_is_due(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=10)
    _versioned(m)
    _plugin(tmp_path)
    _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    text = _hook(monkeypatch, capsys, m)
    assert "ALERT TEST" in text and PERMISSION in text and len(text) <= 400, text
    assert "more" not in text, text


def test_a_test_alert_needs_a_session(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    code, _out, err = _run(m, tmp_path, capsys, monkeypatch, "--test-alert", session=None)
    assert code == 1 and "--session" in err, err


def test_a_test_alert_with_no_usable_store_says_so(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)

    def broken(path):
        raise state.StateError("cache unavailable")

    monkeypatch.setattr(state, "cache_transaction", broken)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    assert code == 3 and "could not arm" in out, out


def test_the_gate_opens_for_a_test_alert_in_a_session_with_no_gate_state(
        tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    assert not hook_gate._gate_path(str(m.main)).exists()
    _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(m.payload())))
    assert "ALERT TEST" in _hook(monkeypatch, capsys, m)


# --- further cases ----------------------------------------------------------------


def test_a_version_string_that_is_no_version_is_not_printed(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path, version="/tmp/zz-private-build")
    for flags in ((), ("--local",), ("--json",)):
        _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, *flags)
        assert "zz-private" not in out, out


def test_commands_named_in_the_advice_are_printed_whole(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    os.utime(tmp_path / "market" / "plugin" / "hooks" / "hooks.json", (time.time(), time.time()))
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert "/reload-plugins" in out, out


def test_all_hooks_turned_off_is_a_failure(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    settings = tmp_path / "settings.json"
    data = json.loads(settings.read_text())
    settings.write_text(json.dumps({**data, "disableAllHooks": True}))
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 3 and "disableAllHooks" in out, out


@pytest.mark.parametrize(
    "event,sub,word",
    [("PostToolUse", "post-tool", "FAIL"), ("UserPromptSubmit", "prompt-submit", "WARN")],
)
def test_a_hooks_file_missing_a_channel_is_named(tmp_path, capsys, monkeypatch, event, sub, word):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    path = tmp_path / "market" / "plugin" / "hooks" / "hooks.json"
    hooks = json.loads(path.read_text())
    del hooks["hooks"][event]
    path.write_text(json.dumps(hooks))
    stamp = time.time() - 86400
    os.utime(path, (stamp, stamp))
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert sub in out and f" {word} " in out, out
    assert code == (3 if word == "FAIL" else 0)


def test_a_watcher_with_no_async_rewake_is_not_the_idle_channel(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    path = tmp_path / "market" / "plugin" / "hooks" / "hooks.json"
    hooks = json.loads(path.read_text())
    hooks["hooks"]["Stop"][1]["hooks"][0].pop("asyncRewake")
    path.write_text(json.dumps(hooks))
    stamp = time.time() - 86400
    os.utime(path, (stamp, stamp))
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert " WARN " in out and "too old to wake" in out, out


def test_the_plugin_is_found_under_the_config_dir_transcripts_use(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alt"))
    assert doctor._plugins_root() == tmp_path / "alt" / "plugins"
    assert doctor._settings_path() == tmp_path / "alt" / "settings.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert doctor._settings_path().name == "settings.json"


def test_a_worker_whose_job_cannot_be_read_is_named(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    _versioned(m)
    _plugin(tmp_path)
    (m.jobs / SHORT / "state.json").write_text("{not json", encoding="utf-8")
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert "could not be read" in out and " WARN " in out, out
    assert "none launched" not in out, out


def test_a_test_alert_survives_a_store_that_fails_once(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    real = state.cache_transaction
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] > 1:  # the follower's save goes through; the claim's transaction does not
            raise state.StateLocked("busy")
        return real(path)

    with monkeypatch.context() as mp:
        mp.setattr(state, "cache_transaction", flaky)
        assert "ALERT TEST" not in _hook(monkeypatch, capsys, m)
    assert _hook(monkeypatch, capsys, m) == doctor.TEST_ALERT
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_commit_that_fails_does_not_tell_it_twice(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    real = state._write_cache
    broken = {"on": False}

    def write(path, data):
        if any("test_alert" not in record and record.get("seen") and broken["on"]
               for record in data.get("sessions", {}).values()):
            raise OSError("disk full")
        return real(path, data)

    with monkeypatch.context() as mp:
        mp.setattr(state, "_write_cache", write)
        broken["on"] = True
        first = _hook(monkeypatch, capsys, m)
    broken["on"] = False
    second = _hook(monkeypatch, capsys, m)
    assert (first + second).count("ALERT TEST") == 1, (first, second)


def test_arming_says_so_when_the_gate_cannot_be_opened(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)

    def broken(path, data):
        raise state.StateError("no room")

    monkeypatch.setattr(state, "write_private_bytes", broken)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    assert code == 3 and "could not arm" in out, out


def test_a_test_alert_beside_a_delivered_worker_tells_both(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    _versioned(m)
    _plugin(tmp_path)
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="done", output={"result": "r"}, mtime_ago=30)
    _run(m, tmp_path, capsys, monkeypatch, "--test-alert")
    text = _hook(monkeypatch, capsys, m)
    assert "ALERT TEST" in text and "finished" in text and "more" not in text, text


@pytest.mark.parametrize("broken", [False, True])
def test_test_alert_json_is_json(tmp_path, capsys, monkeypatch, broken):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    if broken:
        def fail(path):
            raise state.StateError("cache unavailable")

        monkeypatch.setattr(state, "cache_transaction", fail)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--test-alert", "--json")
    payload = json.loads(out)
    assert payload["document"] == "doctor" and payload["armed"] is (not broken)
    assert code == (3 if broken else 0)


# --- further cases ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_project_settings_can_turn_off_every_hook(tmp_path, capsys, monkeypatch, name):
    m = Manager(tmp_path)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / name).write_text(json.dumps({"disableAllHooks": True}))
    _versioned(m)
    for entry in m.entries:
        entry["cwd"] = str(project)
    entries, m.entries = m.entries, []
    m.append(*entries)
    _plugin(tmp_path)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 3 and "disableAllHooks" in out, out


@pytest.mark.parametrize("with_a_change", [False, True])
def test_a_test_alert_armed_during_a_hook_call_is_not_lost(
        tmp_path, capsys, monkeypatch, with_a_change):
    """The hook read the gate and the store before doctor armed, and writes the gate after. With
    nothing else to say it claims nothing, so only the gate can bring the alert to the next call."""
    from agent_postmortem import hooks_cli

    m = Manager(tmp_path)
    m.launched()
    _versioned(m)
    _plugin(tmp_path)
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    if with_a_change:
        m.job(state="done", output={"result": "r"}, mtime_ago=1)
    path = hook_gate._gate_path(str(m.main))
    gate = json.loads(path.read_bytes())
    state.write_private_bytes(path, json.dumps({**gate, "pending": True}).encode())  # gate open
    real = hooks_cli.post_tool_check

    def in_flight(main, payload):
        checked = real(main, payload)
        doctor.arm_test_alert(m.main)  # after its reads, before its gate write
        return checked

    with monkeypatch.context() as mp:
        mp.setattr(hooks_cli, "post_tool_check", in_flight)
        first = _hook(monkeypatch, capsys, m)
    second = _hook(monkeypatch, capsys, m)
    assert (first + second).count("ALERT TEST") == 1, (first, second)
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    assert "alert" not in json.loads(path.read_bytes())


@pytest.mark.parametrize(
    "command,bound",
    [
        pytest.param('"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" post-tool', True, id="the real form"),
        pytest.param('"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "post-tool"', True, id="quoted"),
        pytest.param('"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" no-post-tool', False, id="another word"),
        pytest.param("echo post-tool", False, id="not the entry point"),
        pytest.param('"unbalanced', False, id="unparsable"),
    ],
)
def test_a_binding_is_the_entry_point_and_the_exact_word(command, bound):
    hooks = {"hooks": {"PostToolUse": [{"hooks": [{"command": command}]}]}}
    assert doctor._binds(hooks, "PostToolUse", "post-tool") is bound


def test_a_directory_it_cannot_list_is_a_failure_not_empty(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _plugin(tmp_path)
    real = os.scandir

    def denied(path="."):
        if str(path) == str(m.main.parent.parent):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", denied)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, session=None)
    assert code == 3 and "could not be read" in out, out


def test_a_transcript_symlink_arms_the_real_session(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(m.main)
    code, _out, _err = _run(m, tmp_path, capsys, monkeypatch, "--test-alert", "--session", str(alias),
                            session=None)
    assert code == 0
    assert _hook(monkeypatch, capsys, m) == doctor.TEST_ALERT


@pytest.mark.parametrize("body", ["{}", '{"state": "a-word-it-does-not-know"}'])
def test_a_job_it_cannot_interpret_is_named(tmp_path, capsys, monkeypatch, body):
    m = Manager(tmp_path)
    m.launched()
    _versioned(m)
    _plugin(tmp_path)
    (m.jobs / SHORT / "state.json").write_text(body, encoding="utf-8")
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert "could not be read" in out and "none launched" not in out, out


# --- further cases ----------------------------------------------------------------


def test_project_settings_can_turn_the_plugin_off(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(json.dumps(
        {"enabledPlugins": {"agent-postmortem@agent-postmortem": False}}))
    _versioned(m)
    for entry in m.entries:
        entry["cwd"] = str(project)
    entries, m.entries = m.entries, []
    m.append(*entries)
    _plugin(tmp_path)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 3 and "turned off" in out, out


def test_a_missing_launcher_is_a_failure(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    (tmp_path / "market" / "plugin" / "hooks" / "run.sh").unlink()
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 3 and "run.sh" in out, out


def test_a_launcher_it_cannot_run_is_a_failure(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    (tmp_path / "market" / "plugin" / "hooks" / "run.sh").chmod(0o600)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 3 and "run.sh" in out, out


def _deny_listing(monkeypatch, denied: str) -> None:
    real = os.scandir

    def scandir(path="."):
        if str(path) == denied:
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_one_project_folder_it_cannot_list_is_a_limit(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    other = m.main.parent.parent / "another-project"
    other.mkdir()
    _deny_listing(monkeypatch, str(other))
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 0 and "1 project folder could not be read" in out and " WARN " in out, out


def test_every_project_folder_unreadable_is_a_failure(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _deny_listing(monkeypatch, str(m.main.parent))
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, session=None)
    assert code == 3 and "could not be read" in out, out


# --- the native list against the job files (assessment 2026-11-05, option A) ---------------------


def _native(tmp_path, monkeypatch, rows, *, code: int = 0, delay: str = "0") -> None:
    """A fake `claude` that prints this list when asked for `agents --json --all`. It is run for
    real, as a subprocess, and answers from a file the test can change between calls."""
    answer = tmp_path / "native.json"
    answer.write_bytes(rows if isinstance(rows, bytes) else (
        rows if isinstance(rows, str) else json.dumps(rows)).encode("utf-8"))
    log = tmp_path / "native.log"
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nsleep {delay}\ncat "{answer}"\nexit {code}\n')
    script.chmod(0o755)
    monkeypatch.setattr(doctor, "_claude_path", lambda: str(script))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))


def _row_of(short: str, state_word: str, **extra) -> dict:
    return {"id": short, "kind": "background", "state": state_word, "status": "idle",
            "waitingFor": None, "name": "zz-native-name", **extra}


def _with_worker(tmp_path, monkeypatch, capsys, **job):
    m = Manager(tmp_path)
    m.launched()
    m.job(mtime_ago=600, **job)
    _versioned(m)
    _plugin(tmp_path)
    return m


def test_when_they_agree_it_says_on_how_many(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="blocked",
                     needs=PERMISSION)
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "blocked"),
                                    {"kind": "interactive", "state": None, "status": "idle"}])
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert code == 0 and "agrees with the job files on 1 background session" in out, out
    assert " WARN " not in out, out
    assert (tmp_path / "native.log").read_text().strip() == "agents --json --all"


def test_a_lasting_difference_is_a_warning_with_the_words_it_saw(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="active")
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "blocked")])
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert code == 0 and " WARN " in out and "1 of 1 background sessions" in out, out
    assert "job files may have changed" in out and SHORT not in out, out
    assert (tmp_path / "native.log").read_text().count("agents") == 1, "it asks once"
    _code, local, _err = _run(m, tmp_path, capsys, monkeypatch, "--local", native=True)
    assert SHORT in local and "blocked" in local and "working" in local, local


def test_a_job_done_with_a_cron_still_out_reads_working_natively(tmp_path, capsys, monkeypatch):
    """Measured on 101 real sessions: the one difference. Its turn ended, and its own cron wakes it."""
    m = _with_worker(tmp_path, monkeypatch, capsys, state="done", tempo="idle",
                     inFlight={"tasks": 1, "queued": 0, "kinds": ["session_cron"]})
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert " WARN " not in out and "agrees" in out, out


@pytest.mark.parametrize("in_flight", [{"tasks": 0}, {"tasks": True}, {"tasks": "1"}, None, "x"])
def test_done_with_nothing_really_in_flight_is_a_difference(tmp_path, capsys, monkeypatch, in_flight):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="done", tempo="idle", inFlight=in_flight)
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert " WARN " in out, out


def test_a_job_file_it_cannot_read_is_a_difference(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working")
    path = m.jobs / SHORT / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    stamp = time.time() - 600
    os.utime(path, (stamp, stamp))
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--local", native=True)
    assert " WARN " in out and f"{SHORT} reads working, job file unreadable" in out, out


def test_a_fifo_for_a_job_file_does_not_hang_it(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working")
    path = m.jobs / SHORT / "state.json"
    path.unlink()
    os.mkfifo(path)
    stamp = time.time() - 600
    os.utime(path, (stamp, stamp))
    read: list = []
    thread = threading.Thread(target=lambda: read.append(workers.read_job(str(path))), daemon=True)
    thread.start()
    thread.join(5)
    assert read == [None], "a read of a pipe with no writer never returns"
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--local", native=True)
    assert " WARN " in out and f"{SHORT} reads working, job file unreadable" in out, out


def test_a_job_file_nested_too_deep_is_only_unreadable(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working")
    path = m.jobs / SHORT / "state.json"
    path.write_text('{"state": "working", "x": ' + "[" * 50_000 + "]" * 50_000 + "}",
                    encoding="utf-8")
    stamp = time.time() - 600
    os.utime(path, (stamp, stamp))
    assert workers.read_job(str(path)) is None
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--local", native=True)
    assert code == 0 and f"{SHORT} reads working, job file unreadable" in out, out


def test_a_settings_file_nested_too_deep_is_only_unreadable(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[" * 50_000 + "]" * 50_000, encoding="utf-8")
    assert doctor._read_json(path) is None


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def test_the_nesting_limit_is_one_number_on_every_interpreter(tmp_path):
    """Well below what any supported Python can parse, so the limit decides and the interpreter
    never does: what a Python parses runs from about 1,000 levels to past 100,000 by version and build."""
    path = tmp_path / "settings.json"
    path.write_text(_nested(workers.MAX_JSON_DEPTH), encoding="utf-8")
    assert doctor._read_json(path) is not None
    path.write_text(_nested(workers.MAX_JSON_DEPTH + 1), encoding="utf-8")
    assert doctor._read_json(path) is None


def test_a_job_file_one_level_past_the_limit_is_unreadable(tmp_path):
    path = tmp_path / "state.json"
    inner = _nested(workers.MAX_JSON_DEPTH - 1)
    path.write_text('{"state": "working", "x": ' + inner + "}", encoding="utf-8")
    assert workers.read_job(str(path))["state"] == "working"
    inner = _nested(workers.MAX_JSON_DEPTH)
    path.write_text('{"state": "working", "x": ' + inner + "}", encoding="utf-8")
    assert workers.read_job(str(path)) is None


@pytest.mark.parametrize("text,expected", [
    pytest.param('{"a": "' + "[" * 5000 + '"}', {"a": "[" * 5000}, id="brackets in a string"),
    pytest.param('{"a": "\\"' + "[" * 5000 + '"}', {"a": '"' + "[" * 5000}, id="after an escaped quote"),
    pytest.param('{"' + "{" * 5000 + '": 1}', {"{" * 5000: 1}, id="brackets in a key"),
    pytest.param('{"a": "\\\\", "b": [1]}', {"a": "\\", "b": [1]}, id="an escaped backslash ends no string"),
])
def test_a_bracket_inside_a_string_is_not_nesting(text, expected):
    assert workers.loads_bounded(text) == expected


@pytest.mark.parametrize("raw", [
    pytest.param('{"a": [1, "\u00e9"]}'.encode(enc), id=enc) for enc in ("utf-8", "utf-16", "utf-32")
])
def test_bytes_read_as_json_itself_reads_them(raw):
    assert workers.loads_bounded(raw) == json.loads(raw)


@pytest.mark.parametrize("text", ["", "{", "[1,", '"never closed', "not json"])
def test_what_is_not_json_still_raises_what_json_raises(text):
    with pytest.raises(ValueError):
        workers.loads_bounded(text)


def test_an_unfinished_string_is_read_in_one_pass():
    """A job file cut off mid-write can end inside a string full of escaped quotes. Scanning from
    each quote again took 17 s for 80 KB, and a file may be 1 MB, on the per-call hook's path."""
    code = ("import sys, time; from agent_postmortem import workers\n"
            "for text in ('{\"x\":\"' + '\\\\\"' * 500_000, '{\"x\":\"' + '\\\\\"' * 500_000 + '\\\\'):\n"
            "    try:\n"
            "        workers.loads_bounded(text)\n"
            "    except ValueError:\n"
            "        pass\n"
            "    else:\n"
            "        sys.exit('parsed an unfinished string')\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("text", [
    pytest.param('{"a": "' + "[" * 5000, id="brackets in an unfinished string"),
    pytest.param('{"a": "x\\', id="ends on a lone backslash"),
    pytest.param('"', id="one quote"),
    pytest.param('"\\', id="a quote and a lone backslash"),
])
def test_an_unfinished_string_raises_what_json_raises(text):
    with pytest.raises(ValueError):
        workers.loads_bounded(text)


def _random_value(rng: random.Random, depth: int) -> object:
    if depth <= 0 or rng.random() < 0.3:
        return "".join(rng.choice('[]{}"\\,:x \n\u00e9') for _ in range(rng.randrange(8)))
    if rng.random() < 0.5:
        return [_random_value(rng, depth - 1) for _ in range(rng.randrange(3))]
    return {str(_random_value(rng, 0)): _random_value(rng, depth - 1) for _ in range(rng.randrange(3))}


def test_it_reads_whatever_json_reads_up_to_the_limit():
    rng = random.Random(20261105)
    for _ in range(2000):
        text = json.dumps(_random_value(rng, 6), ensure_ascii=rng.random() < 0.5)
        assert workers.loads_bounded(text) == json.loads(text), text
        assert workers.loads_bounded(text.encode("utf-8")) == json.loads(text), text


def test_it_refuses_whatever_json_refuses():
    rng = random.Random(20261106)
    for _ in range(5000):
        text = "".join(rng.choice('[]{}"\\,:x1 \n') for _ in range(rng.randrange(14)))
        try:
            expected = json.loads(text)
        except ValueError:
            with pytest.raises(ValueError):
                workers.loads_bounded(text)
        else:
            assert workers.loads_bounded(text) == expected, text


def test_doctor_reads_no_deep_transcript_line_and_no_deep_native_answer(tmp_path):
    deep = _nested(workers.MAX_JSON_DEPTH + 1)
    path = tmp_path / "main.jsonl"
    good = json.dumps({"timestamp": "2026-02-01T00:00:00.000Z", "cwd": "/tmp/zz"})
    path.write_text(deep + "\n" + good + "\n", encoding="utf-8")
    assert doctor._first_entry(path)["cwd"] == "/tmp/zz"
    assert doctor._native_rows(deep.encode()) is None


def test_a_state_neither_side_should_have_is_not_agreement(tmp_path, capsys, monkeypatch):
    """An update that adds a state word reaches both at once, and a worker notice cannot read it."""
    m = _with_worker(tmp_path, monkeypatch, capsys, state="suspended")
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "suspended")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert " WARN " in out and "1 in a state this build does not know" in out, out


@pytest.mark.parametrize("word", ["private-project", "ZZ private/text"])
@pytest.mark.parametrize("flag", ["--local", "--raw"])
def test_a_state_word_it_does_not_know_is_not_repeated(tmp_path, capsys, monkeypatch, word, flag):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="active")
    _native(tmp_path, monkeypatch, [_row_of(SHORT, word)])
    for flags in ((flag,), (flag, "--json")):
        _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, *flags, native=True)
        assert word not in out and "another word" in out, out
    m.job(state=word, tempo="idle", mtime_ago=600)
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, flag, native=True)
    assert word not in out and "job file another word" in out, out


def test_another_spelling_of_the_root_still_reads_the_jobs_beside_it(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="active")
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    monkeypatch.setattr(doctor, "_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(doctor, "_plugins_root", lambda: tmp_path / "plugins")
    spelled = m.main.parent / ".."  # the project folder, then back up
    assert main(["doctor", "--root", str(spelled), "--session", FAKE_UUID]) in (0, 3)
    out = capsys.readouterr().out
    assert "agrees with the job files on 1 background session" in out and "no job file" not in out, out


def test_an_arming_of_the_test_alert_does_not_run_it(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    _run(m, tmp_path, capsys, monkeypatch, "--test-alert", native=True)
    assert not (tmp_path / "native.log").exists()


def test_a_job_file_still_changing_is_not_compared(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="active", mtime_ago=1)
    _versioned(m)
    _plugin(tmp_path)
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "blocked")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert " WARN " not in out, out


def test_a_session_it_lists_with_no_job_file_is_a_warning(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="active")
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working"), _row_of("0a0b0c0d", "working")])
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert " WARN " in out and "no job file" in out, out


@pytest.mark.parametrize(
    "rows,word,detail",
    [
        pytest.param("not json", "WARN", "a shape this build does not read", id="not JSON"),
        pytest.param('{"sessions": []}', "WARN", "a shape this build does not read", id="not a list"),
        pytest.param('[{"id": "a1b2c3d4", "kind": "background"}]', "WARN",
                     "a shape this build does not read", id="no state field"),
        pytest.param('[{"kind": "background", "state": "working"}]', "WARN",
                     "a shape this build does not read", id="a background row with no id"),
        pytest.param('[{"id": "../../etc", "kind": "background", "state": "working"}]', "WARN",
                     "a shape this build does not read", id="an id that is no job name"),
        pytest.param('["a1b2c3d4"]', "WARN", "a shape this build does not read", id="rows not objects"),
        pytest.param(b"\xff\xfe", "WARN", "a shape this build does not read", id="not text"),
        pytest.param("[]", "INFO", "no background session to compare", id="empty"),
    ],
)
def test_what_the_list_can_look_like(tmp_path, capsys, monkeypatch, rows, word, detail):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _native(tmp_path, monkeypatch, rows)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert code == 0 and detail in out and f" {word} " in out, out


def test_a_command_that_fails_or_hangs_is_only_a_note(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _native(tmp_path, monkeypatch, "[]", code=1)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert code == 0 and "could not be compared" in out and " WARN " not in out, out
    _native(tmp_path, monkeypatch, "[]", delay="5")
    monkeypatch.setattr(doctor, "_NATIVE_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert time.monotonic() - started < 4 and "could not be compared" in out, out


def test_with_no_claude_on_path_it_says_so(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert "not on PATH" in out and " WARN " not in out, out


def test_another_root_is_not_compared_with_this_machines_list(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="active")
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "blocked")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, native=True)
    assert "another --root" in out and " WARN " not in out, out
    assert not (tmp_path / "native.log").exists(), "it does not even ask"


def test_the_hooks_never_run_it(tmp_path, capsys, monkeypatch):
    """The hot path stays on job files."""
    m = Manager(tmp_path)
    m.launched()
    _versioned(m)
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "working")])
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    assert not (tmp_path / "native.log").exists()


def test_the_native_rows_words_stay_out_of_the_output(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="active")
    _native(tmp_path, monkeypatch, [_row_of(SHORT, "blocked", waitingFor="zz-private-question")])
    for flags in ((), ("--local",), ("--json",)):
        _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, *flags, native=True)
        assert "zz-private" not in out and "zz-native-name" not in out, out




# --- 0.5.1 release review: findings against the whole release, each pinned by a test ------------


def _healthy(tmp_path, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path)
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {FAKE_UUID: "bg"})
    return m


def _bin_with_cli(folder):
    folder.mkdir(parents=True)
    (folder / "agent-postmortem").write_text("#!/bin/sh\n")
    (folder / "agent-postmortem").chmod(0o755)
    return folder / "agent-postmortem"


def _no_cli_anywhere(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


def test_hooks_with_no_cli_to_run_are_a_failure(tmp_path, capsys, monkeypatch):
    """A plugin-only user runs doctor through uvx: this copy is fine, and the hooks have none."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3, out
    assert out.splitlines()[0].startswith("DOCTOR  alerts will not reach you: hooks cli:"), out
    assert "uv tool install agent-postmortem" in _row(out, "hooks cli"), out


def test_the_bundled_copy_is_a_cli_for_the_hooks(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    _bin_with_cli(tmp_path / "plugins" / "data" / "agent-postmortem-agent-postmortem" / "bin")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert "hooks cli" not in out and code == 0, out


def test_the_fallback_data_directory_is_a_cli_for_the_hooks_too(tmp_path, capsys, monkeypatch):
    """run.sh: $CLAUDE_PLUGIN_DATA, else the XDG state directory."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    _bin_with_cli(tmp_path / "xdg" / "agent-postmortem" / "plugin" / "bin")
    monkeypatch.setattr("sys.argv", [str(tmp_path / "elsewhere" / "agent-postmortem")])
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert "hooks cli" not in out and code == 0, out
    assert "cli copies" in out, out  # and it is the copy compared with this one


def test_a_cli_on_path_is_a_cli_for_the_hooks(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    folder = tmp_path / "on-path"
    _bin_with_cli(folder)
    monkeypatch.setenv("PATH", str(folder))
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert "hooks cli" not in out and code == 0, out


def test_uvx_s_own_folder_on_path_is_no_cli_for_the_hooks(tmp_path, capsys, monkeypatch):
    """uvx puts its throwaway environment first on PATH for the command it runs, and no hook sees it."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    folder = tmp_path / "uv-cache" / "bin"
    _bin_with_cli(folder)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr("sys.executable", str(folder / "python"))
    monkeypatch.setattr("sys.base_prefix", "/nowhere")  # this interpreter is in an environment
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3 and "hooks cli" in out, out
    monkeypatch.setattr("sys.prefix", str(folder.parent))
    monkeypatch.setenv("VIRTUAL_ENV", str(folder.parent))  # an activated environment is on PATH for real
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 0 and "hooks cli" not in out, out


def test_another_project_s_activated_environment_does_not_make_uvx_s_folder_count(
        tmp_path, capsys, monkeypatch):
    """Round 2 of the release review: VIRTUAL_ENV names a project, and doctor runs through uvx."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    folder = tmp_path / "uv-cache" / "bin"
    _bin_with_cli(folder)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr("sys.executable", str(folder / "python"))
    monkeypatch.setattr("sys.prefix", str(folder.parent))
    monkeypatch.setattr("sys.base_prefix", "/nowhere")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "some-project" / ".venv"))
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3 and "hooks cli" in out, out


def test_the_fallback_is_not_searched_once_a_hook_has_been_given_a_data_directory(
        tmp_path, capsys, monkeypatch):
    """Round 2 of the release review: run.sh takes $CLAUDE_PLUGIN_DATA, or else the fallback."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    given = tmp_path / "plugins" / "data" / "agent-postmortem-agent-postmortem"
    given.mkdir(parents=True)
    _bin_with_cli(tmp_path / "xdg" / "agent-postmortem" / "plugin" / "bin")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3 and "hooks cli" in out, out
    _bin_with_cli(given / "bin")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 0 and "hooks cli" not in out, out


def test_another_plugin_s_data_directory_in_this_process_says_nothing(tmp_path, capsys, monkeypatch):
    """A session's commands can hold another plugin's $CLAUDE_PLUGIN_DATA: seen on a real machine."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    other = tmp_path / "plugins" / "data" / "some-other-plugin"
    _bin_with_cli(other / "bin")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(other))
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3 and "hooks cli" in out, out  # its CLI is no CLI for these hooks
    _bin_with_cli(tmp_path / "plugins" / "data" / "agent-postmortem-agent-postmortem" / "bin")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 0 and "hooks cli" not in out, out  # and it hides nothing from this check


def test_a_system_python_s_own_folder_on_path_is_a_cli_for_the_hooks(tmp_path, capsys, monkeypatch):
    """A plain `pip install` puts the script beside the interpreter, with no environment."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    folder = tmp_path / "usr-local" / "bin"
    _bin_with_cli(folder)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr("sys.executable", str(folder / "python"))
    monkeypatch.setattr("sys.base_prefix", sys.prefix)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 0 and "hooks cli" not in out, out


def test_with_no_plugin_the_hooks_need_no_cli(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _versioned(m)
    (tmp_path / "plugins").mkdir()
    _no_cli_anywhere(tmp_path, monkeypatch)
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert "hooks cli" not in out and "not installed" in out, out


@pytest.mark.parametrize("text", [FAKE_UUID, f"1.2.3-{FAKE_UUID}", "private-build", "7",
                                  "0.5.1+someone", "0.5.1-someone", "1.2.3+local.7"])
def test_a_manifest_version_that_is_no_version_is_not_printed(tmp_path, capsys, monkeypatch, text):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path, version=text)
    for flags in ((), ("--json",)):
        _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, *flags)
        assert text not in out and "version unknown" in out, out


@pytest.mark.parametrize("text", ["0.5.1", "2.1", "1.2.3-beta.1", "1.2.3-rc1", "1.2.3.4"])
def test_a_real_manifest_version_is_printed(tmp_path, capsys, monkeypatch, text):
    m = Manager(tmp_path)
    _versioned(m)
    _plugin(tmp_path, version=text)
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert f"version {text}," in out, out


def test_the_build_stamp_names_a_branch_so_it_waits_for_local(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    stamp = {"branch": "customer-private", "commit": "abc1234", "date": "2026-11-05", "dirty": False}
    monkeypatch.setattr("agent_postmortem.cli._read_build_info", lambda: stamp)
    for flags in ((), ("--json",)):
        _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, *flags)
        assert "customer-private" not in out and "agent-postmortem 0." in out, out
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--raw")
    assert "(customer-private abc1234, 2026-11-05)" in _row(out, "cli"), out
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch, "--local")
    assert "abc1234, 2026-11-05)" in _row(out, "cli"), out


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "[" * 50_000 + "]" * 50_000])
def test_a_settings_file_it_cannot_read_is_no_clean_bill(tmp_path, capsys, monkeypatch, text):
    """It may be the one that turns the hooks off."""
    m = _healthy(tmp_path, monkeypatch)
    (tmp_path / "settings.json").write_text(text, encoding="utf-8")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 0, out
    first = out.splitlines()[0]
    assert first.startswith("DOCTOR  alerts can reach you, with a limit: plugin:"), out
    assert " WARN " in _row(out, "plugin") and "could not be read" in _row(out, "plugin"), out


def test_a_project_settings_file_it_cannot_read_is_no_clean_bill(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(doctor, "_first_entry", lambda session: {"cwd": str(project)})
    _code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert " WARN " in _row(out, "plugin") and "could not be read" in _row(out, "plugin"), out


def test_settings_files_that_are_not_there_are_no_limit(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    (tmp_path / "settings.json").unlink()
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 0 and " WARN " not in out and "with the idle watcher" in _row(out, "plugin"), out


def test_what_turns_the_hooks_off_still_outranks_a_file_it_cannot_read(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text("{not json", encoding="utf-8")
    (project / ".claude" / "settings.json").write_text('{"disableAllHooks": true}', encoding="utf-8")
    monkeypatch.setattr(doctor, "_first_entry", lambda session: {"cwd": str(project)})
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 3 and "disableAllHooks" in out, out


@pytest.mark.parametrize("matcher,code", [("^Workflow$", 3), ("^Bash$", 3), ("*", 0), ("", 0), (".*", 0)])
def test_the_per_call_hook_must_cover_every_tool(tmp_path, capsys, monkeypatch, matcher, code):
    m = _healthy(tmp_path, monkeypatch)
    hooks_file = tmp_path / "market" / "plugin" / "hooks" / "hooks.json"
    hooks = json.loads(hooks_file.read_text())
    hooks["hooks"]["PostToolUse"][0]["matcher"] = matcher
    hooks_file.write_text(json.dumps(hooks))
    stamp = time.time() - 86400
    os.utime(hooks_file, (stamp, stamp))
    got, out, _err = _run(m, tmp_path, capsys, monkeypatch)
    assert got == code, out


def test_the_plugin_s_own_hooks_file_passes_the_per_call_check():
    from pathlib import Path

    hooks = json.loads((Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "hooks.json").read_text())
    assert doctor._binds(hooks, "PostToolUse", "post-tool", every_tool=True)


def test_a_transcript_line_nested_too_deep_is_skipped(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    body = m.main.read_text(encoding="utf-8")
    deep = '{"version": ' + "[" * 20_000 + "]" * 20_000 + "}\n"  # whole inside the 64 KB tail
    m.main.write_text(deep + body + deep, encoding="utf-8")
    code, out, err = _run(m, tmp_path, capsys, monkeypatch)
    assert code == 0 and out.startswith("DOCTOR ") and "2.1.318" in out, (out, err)


def test_another_spelling_of_the_root_counts_the_same_jobs(tmp_path, capsys, monkeypatch):
    m = _with_worker(tmp_path, monkeypatch, capsys, state="working", tempo="active")
    monkeypatch.setattr(doctor, "_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(doctor, "_plugins_root", lambda: tmp_path / "plugins")
    monkeypatch.setattr(doctor, "_claude_path", lambda: None)
    monkeypatch.setattr(doctor, "_hook_cli_found", lambda: True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(m.main.parent.parent.parent))
    spelled = m.main.parent / ".."  # the project folder, then back up
    assert main(["doctor", "--root", str(spelled), "--session", FAKE_UUID]) in (0, 3)
    out = capsys.readouterr().out
    assert "readable, 1 background session" in _row(out, "job files"), out


def test_a_home_with_no_transcripts_yet_says_what_is_missing(tmp_path, capsys, monkeypatch):
    """A first run on a new machine: nothing failed to read, there is nothing there yet."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(doctor, "_claude_path", lambda: None)
    assert main(["doctor"]) == 3  # the plugin is not installed either
    out = capsys.readouterr().out
    row = _row(out, "transcripts")
    assert " WARN " in row and "none yet" in row and "could not be read" not in out, out
    assert "none yet" in _row(out, "job files"), out
    assert out.splitlines()[0].startswith("DOCTOR  alerts will not reach you: plugin: not installed"), out


def test_a_home_with_no_plugin_and_no_transcripts_is_told_in_that_order_by_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(doctor, "_claude_path", lambda: None)
    assert main(["doctor", "--json"]) == 3
    rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)["checks"]}
    assert rows["plugin"]["status"] == "FAIL" and rows["transcripts"]["status"] == "WARN"


def test_the_wrapper_on_a_sessions_path_is_no_cli_for_the_hooks(tmp_path, capsys, monkeypatch):
    """plugin/bin is on the Bash tool's PATH and not on a hook's, and it only runs the copy."""
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    wrapper = tmp_path / "market" / "plugin" / "bin"
    shutil.copy(_WRAPPER, _bin_with_cli(wrapper))
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}{os.pathsep}{wrapper}")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3 and "hooks cli" in out, out


def test_a_link_to_the_wrapper_elsewhere_is_no_cli_for_the_hooks_either(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere-bin"
    elsewhere.mkdir()
    (elsewhere / "agent-postmortem").symlink_to(_WRAPPER)
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}{os.pathsep}{elsewhere}")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3 and "hooks cli" in out, out


def test_a_fifo_named_like_the_cli_does_not_hang_it(tmp_path, capsys, monkeypatch):
    fifo = tmp_path / "agent-postmortem"
    os.mkfifo(fifo)
    fifo.chmod(0o755)
    read: list = []
    thread = threading.Thread(target=lambda: read.append(doctor._is_plugin_wrapper(fifo)), daemon=True)
    thread.start()
    thread.join(5)
    assert read == [False], "opening a pipe with no writer never returns"


def test_a_data_directory_link_to_the_wrapper_is_no_cli_for_the_hooks(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    folder = tmp_path / "plugins" / "data" / "agent-postmortem-agent-postmortem" / "bin"
    folder.mkdir(parents=True)
    (folder / "agent-postmortem").symlink_to(_WRAPPER)
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert code == 3 and "hooks cli" in out, out


def test_a_real_cli_beside_an_unrelated_hooks_folder_counts(tmp_path, capsys, monkeypatch):
    m = _healthy(tmp_path, monkeypatch)
    _no_cli_anywhere(tmp_path, monkeypatch)
    project = tmp_path / "project"
    _bin_with_cli(project / "bin")
    (project / "hooks").mkdir()
    (project / "hooks" / "run.sh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}{os.pathsep}{project / 'bin'}")
    code, out, _err = _run(m, tmp_path, capsys, monkeypatch, hook_cli_checked=True)
    assert "hooks cli" not in out, out
