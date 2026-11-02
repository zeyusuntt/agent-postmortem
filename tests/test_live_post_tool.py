"""docs/DESIGN.md 4.14.5 (0.4): the return-time checks run from hooks that fire anyway, so no
Monitor has to be armed. Usage on 2026-10-30: the watch hint reached 9 sessions and 1 armed it,
while agents called `status` 42 times on their own.

`hook post-tool` (PostToolUse, every tool) and `hook prompt-submit` (UserPromptSubmit) speak only
for a new KILL, FAILED or CLAIM ending of one of the session's subagents, claimed through the
same locked delivery gate as `watch --session`, the resume hint and the Stop check, so no channel
repeats another. A call where nothing changed reads a few stats, imports no live module and
prints nothing.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from live_helpers import (
    FAKE_UUID,
    KILL_SENTINEL_TOOL,
    aid,
    assistant_text,
    notification,
    tool_use,
    user,
    write_jsonl,
)
from test_live_session import SLUG, T0, _launch, _sub_path

from agent_postmortem import hooks_cli

REPO = Path(__file__).resolve().parent.parent


def _session(tmp_path) -> Path:
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    return main_path


def _payload(main_path: Path, event: str = "PostToolUse", **extra) -> dict:
    payload = {"hook_event_name": event, "session_id": FAKE_UUID,
               "transcript_path": str(main_path)}
    if event == "PostToolUse":
        payload.update({"tool_name": "Bash", "tool_input": {"command": "ls"},
                        "tool_response": {"stdout": ""}})
    payload.update(extra)
    return payload


def _hook(monkeypatch, capsys, command: str, payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch(command) == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return ""
    data = json.loads(out)
    return data["hookSpecificOutput"]["additionalContext"]


def _running_background_agent(main_path: Path, agent: str) -> Path:
    _launch(main_path, agent, "toolu_bg", T0, background=True)
    transcript = _sub_path(main_path, agent)
    write_jsonl(transcript, [
        user(agent, T0, "run the long job"),
        tool_use(agent, T0 + timedelta(seconds=2), "toolu_long", "Bash", {"command": "make all"}),
    ])
    return transcript


def _kill(transcript: Path, agent: str) -> None:
    write_jsonl(transcript, [
        user(agent, T0 + timedelta(seconds=700), [{"type": "text", "text": KILL_SENTINEL_TOOL}]),
    ], mode="a")


def test_a_background_subagent_killed_mid_run_is_told_at_the_next_tool_call(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    agent = aid(81)
    transcript = _running_background_agent(main_path, agent)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # still running
    _kill(transcript, agent)
    text = _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    assert "KILL" in text and "was interrupted; nothing in this session stopped it" in text, text
    assert "agent-postmortem run s_" in text
    assert agent not in text  # an alias, never the raw id
    assert len(text) <= 400
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # never twice


def test_the_watch_does_not_repeat_what_the_hook_told(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(82)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _kill(transcript, agent)
    assert "KILL" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    from agent_postmortem import live
    follower = live.SessionFollower(main_path, caller_session=None)
    events = [e["event"] for e in follower.poll()]
    events += [e["event"] for e in follower.poll()]
    assert "KILL" not in events, events


def test_the_hook_does_not_repeat_what_the_watch_told(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(83)
    transcript = _running_background_agent(main_path, agent)
    from agent_postmortem import live
    follower = live.SessionFollower(main_path, caller_session=None)
    follower.poll()
    _kill(transcript, agent)
    seen = [e["event"] for e in follower.poll()] + [e["event"] for e in follower.poll()]
    assert "KILL" in seen, seen
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""


def test_the_stop_check_does_not_repeat_an_ending_the_hook_told(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(84)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _kill(transcript, agent)
    assert "KILL" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    old = (main_path.stat().st_mtime - 3600)
    for path in (main_path, transcript):
        os.utime(path, (old, old))  # the session is quiet, so Stop analyzes it
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "Stop", "session_id": FAKE_UUID, "transcript_path": str(main_path),
        "stop_hook_active": False,
    })))
    assert hooks_cli.dispatch("check") == 0
    out = capsys.readouterr().out.strip()
    assert out == "", out  # the control below shows Stop would name this run and agent


def test_the_user_prompt_fallback_tells_an_ending_while_the_agent_was_idle(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    agent = aid(85)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "prompt-submit", _payload(main_path, "UserPromptSubmit",
                                                          prompt="go on"))
    _kill(transcript, agent)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        _payload(main_path, "UserPromptSubmit", prompt="any news?"))))
    assert hooks_cli.dispatch("prompt-submit") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "KILL" in data["hookSpecificOutput"]["additionalContext"]


def test_a_subagents_own_tool_call_is_never_checked(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(86)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _kill(transcript, agent)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path, agent_id=agent)) == ""
    assert "KILL" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))


def test_a_session_without_subagents_is_silent(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""


def _fresh_interpreter_call(tmp_path, main_path: Path) -> tuple[str, list[str]]:
    """One hook call in a new interpreter, the way run.sh starts the CLI: its stdout and the
    agent_postmortem modules it imported."""
    script = (
        "import io, json, sys\n"
        "from agent_postmortem import entry\n"
        f"sys.stdin = io.StringIO({json.dumps(json.dumps(_payload(main_path)))})\n"
        "code = entry.main(['hook', 'post-tool'])\n"
        "mods = sorted(m for m in sys.modules if m.startswith('agent_postmortem'))\n"
        "sys.stderr.write(json.dumps(mods))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout, json.loads(done.stderr.strip().splitlines()[-1])


def test_a_call_with_nothing_new_prints_nothing_and_loads_no_live_module(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    agent = aid(87)
    _running_background_agent(main_path, agent)
    first_out, _first_mods = _fresh_interpreter_call(tmp_path, main_path)
    assert first_out == ""
    out, mods = _fresh_interpreter_call(tmp_path, main_path)
    assert out == ""
    assert "agent_postmortem.live" not in mods and "agent_postmortem.cli" not in mods, mods


def test_the_hint_is_offered_only_for_a_background_launch_and_as_an_option(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    foreground = {**_payload(main_path), "tool_name": "Agent",
                  "tool_input": {"description": "d", "prompt": "p"},
                  "tool_response": {"agentId": aid(88), "status": "completed"}}
    assert _hook(monkeypatch, capsys, "agent-launched", foreground) == ""
    background = {**foreground, "tool_input": {"description": "d", "prompt": "p",
                                               "run_in_background": True},
                  "tool_response": {"agentId": aid(89), "status": "async_launched",
                                    "isAsync": True}}
    text = _hook(monkeypatch, capsys, "agent-launched", background)
    assert "checked when they return" in text and "next tool call" in text, text
    assert "watch --session" in text and "long" in text, text
    assert len(text) <= 400


def test_control_without_the_hook_the_stop_check_does_tell_that_ending(
    tmp_path, monkeypatch, capsys,
):
    # proves the dedupe test above is not vacuous: Stop reports this very fixture on its own
    main_path = _session(tmp_path)
    agent = aid(90)
    transcript = _running_background_agent(main_path, agent)
    _kill(transcript, agent)
    old = (main_path.stat().st_mtime - 3600)
    for path in (main_path, transcript):
        os.utime(path, (old, old))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "Stop", "session_id": FAKE_UUID, "transcript_path": str(main_path),
        "stop_hook_active": False,
    })))
    assert hooks_cli.dispatch("check") == 0
    out = capsys.readouterr().out.strip()
    assert out, "the Stop check said nothing even without the hook"
    assert "agents a:" in json.loads(out)["systemMessage"], out


def _stop_then_hook(tmp_path, monkeypatch, capsys, agent: str, *, stop: bool) -> str:
    main_path = _session(tmp_path)
    transcript = _running_background_agent(main_path, agent)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # baseline: empty
    _kill(transcript, agent)
    old = (main_path.stat().st_mtime - 3600)
    for path in (main_path, transcript):
        os.utime(path, (old, old))  # quiet, so Stop analyzes the session
    if stop:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "hook_event_name": "Stop", "session_id": FAKE_UUID,
            "transcript_path": str(main_path), "stop_hook_active": False,
        })))
        assert hooks_cli.dispatch("check") == 0
        assert "agents a:" in capsys.readouterr().out
    return _hook(monkeypatch, capsys, "post-tool", _payload(main_path))


def test_the_hook_does_not_repeat_an_ending_the_stop_check_told(tmp_path, monkeypatch, capsys):
    assert _stop_then_hook(tmp_path, monkeypatch, capsys, aid(91), stop=True) == ""


def test_control_without_stop_the_hook_tells_that_ending(tmp_path, monkeypatch, capsys):
    assert "KILL" in _stop_then_hook(tmp_path, monkeypatch, capsys, aid(92), stop=False)


def test_a_first_check_too_big_for_one_call_reads_on_over_the_next_calls(
    tmp_path, monkeypatch, capsys,
):
    # Field timing 2026-10-31: the first check of a session with 453 subagents read for 2.6 s.
    # Past run.sh's 4 s deadline it would be killed before saving and start over at every call,
    # so the first read stops at a time budget, saves its place and goes on at the next call.
    # The baseline waits for the complete read, so an old ending is never told as new.
    main_path = _session(tmp_path)
    old_agent, agent = aid(93), aid(94)
    _launch(main_path, old_agent, "toolu_old", T0, background=True)
    old_transcript = _sub_path(main_path, old_agent)
    write_jsonl(old_transcript, [
        user(old_agent, T0, "the old job"),
        user(old_agent, T0 + timedelta(seconds=5), [{"type": "text", "text": KILL_SENTINEL_TOOL}]),
    ])
    stale = time.time() - 7200
    os.utime(old_transcript, (stale, stale))  # ended two hours ago: baseline, never told
    transcript = _running_background_agent(main_path, agent)
    _kill(transcript, agent)
    monkeypatch.setattr(hooks_cli, "_FIRST_READ_BUDGET_SECONDS", 0.0)
    texts = [_hook(monkeypatch, capsys, "post-tool", _payload(main_path)) for _ in range(4)]
    assert texts[0] == "", texts  # one subagent read: nothing told and nothing baselined yet
    told = [text for text in texts if text]
    assert len(told) == 1, texts
    assert told[0].count("was interrupted") == 1, told[0]  # the new kill, never the old one


def test_run_sh_answers_a_call_with_nothing_new_silently_and_within_150_ms(tmp_path):
    # the whole path a real tool call takes: run.sh resolving the CLI, its deadline wrapper, then
    # the console entry and the gate; the CLI is this tree's, started the way uv's script does
    from test_plugin import _run_run_sh, _write_fake_cli

    main_path = _session(tmp_path)
    agent = aid(95)
    transcript = _running_background_agent(main_path, agent)
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", (
        "#!/bin/sh\n"
        f"PYTHONPATH='{REPO / 'src'}' exec '{sys.executable}' -c "
        "'import sys; from agent_postmortem.entry import main; sys.exit(main(sys.argv[1:]))' \"$@\"\n"
    ))
    payload = json.dumps(_payload(main_path))
    first = _run_run_sh("post-tool", path_entries=[], plugin_data=plugin_data, stdin=payload)
    assert first.returncode == 0 and first.stdout == "", first
    timings = []
    for _ in range(9):
        start = time.perf_counter()
        result = _run_run_sh("post-tool", path_entries=[], plugin_data=plugin_data, stdin=payload)
        timings.append(time.perf_counter() - start)
        assert result.returncode == 0 and result.stdout == "", result
    timings.sort()
    assert timings[4] < 0.150, f"median {timings[4] * 1000:.0f} ms: {timings}"
    # control: the same path does speak, so the silence above is the gate, not a failed start
    _kill(transcript, agent)
    told = _run_run_sh("post-tool", path_entries=[], plugin_data=plugin_data, stdin=payload)
    context = json.loads(told.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "KILL" in context, told


def test_a_return_notice_in_the_main_transcript_alone_opens_the_gate(
    tmp_path, monkeypatch, capsys,
):
    # R3: the child's last entry was written before the previous call, so its transcript does
    # not change again; only the main transcript's task notification can open the gate
    main_path = _session(tmp_path)
    agent = aid(96)
    _launch(main_path, agent, "toolu_l96", T0, background=True)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"),
        assistant_text(agent, T0 + timedelta(seconds=9), "API Error: Server error mid-response"),
    ])
    calls = []
    real = hooks_cli.post_tool_check
    monkeypatch.setattr(hooks_cli, "post_tool_check",
                        lambda *args: calls.append(1) or real(*args))
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # not returned yet
    write_jsonl(main_path, [assistant_text(None, T0 + timedelta(seconds=10), "still going")],
                mode="a")
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""
    assert len(calls) == 1, "an ordinary main entry opened the gate"
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=11), agent, "toolu_l96",
                                         "completed", 'Agent "d" finished')], mode="a")
    text = _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    assert "CLAIM" in text and "a server error occurred" in text, text
    assert len(calls) == 2


def _counting(monkeypatch) -> list:
    calls: list = []
    real = hooks_cli.post_tool_check
    monkeypatch.setattr(hooks_cli, "post_tool_check",
                        lambda *args: calls.append(1) or real(*args))
    return calls


def test_a_kill_answered_by_a_completed_notice_does_not_hold_the_gate_open(
    tmp_path, monkeypatch, capsys,
):
    # R3: "completed" while the child's own last entry is the kill marker is a CLAIM, and its
    # :end key is never delivered; the gate must still close once that CLAIM is told
    main_path = _session(tmp_path)
    agent = aid(97)
    transcript = _running_background_agent(main_path, agent)
    calls = _counting(monkeypatch)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _kill(transcript, agent)
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=701), agent, "toolu_bg",
                                         "completed", 'Agent "d" finished')], mode="a")
    assert "CLAIM" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    opened = len(calls)
    for _ in range(3):
        assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""
    # one check confirms the delivery went through (4.14.5: the gate is written before the
    # claims), then the gate stays shut
    assert len(calls) == opened + 1, "the gate stayed open after the ending was told"


def test_a_kill_marker_without_a_timestamp_is_still_told(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(98)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    marker = user(agent, T0 + timedelta(seconds=700),
                  [{"type": "text", "text": KILL_SENTINEL_TOOL}])
    marker.pop("timestamp")
    write_jsonl(transcript, [marker], mode="a")
    stale = time.time() - 60
    os.utime(transcript, (stale, stale))  # written a minute ago: the 2 s wait is over
    texts = [_hook(monkeypatch, capsys, "post-tool", _payload(main_path)) for _ in range(2)]
    assert any("KILL" in text for text in texts), texts


def test_a_fresh_kill_waits_for_its_native_notice_then_is_told_with_nothing_changed(
    tmp_path, monkeypatch, capsys,
):
    from datetime import datetime, timezone

    from agent_postmortem import live
    monkeypatch.setattr(live, "KILL_NOTICE_GRACE_SECONDS", 60.0)  # timestamps are whole seconds
    main_path = _session(tmp_path)
    agent = aid(99)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    write_jsonl(transcript, [user(agent, datetime.now(timezone.utc),
                                  [{"type": "text", "text": KILL_SENTINEL_TOOL}])], mode="a")
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # its wait
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # still waiting
    monkeypatch.setattr(live, "KILL_NOTICE_GRACE_SECONDS", 0.0)  # the wait is over; no file changed
    assert "KILL" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
