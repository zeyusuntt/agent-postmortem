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
import statistics
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple

import pytest
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


# docs/DESIGN.md 4.14.5: a call with nothing new takes under 150 ms through run.sh. A wall-clock
# budget on a shared CI runner is noise (inbox E47), so CI compares within rounds timed in the same
# test: the call with a bare start of the same interpreter through the same run.sh (under 2 times;
# 1.35 on the reference machine at rest, 1.15 under three busy processes per core), and that bare
# start with the bare CLI started directly (under 8 times; 2.6 at rest, about 4 under that load).
# The 150 ms is checked off CI, where it is meant.
NOTHING_NEW_BUDGET_SECONDS = 0.150
NOTHING_NEW_MAX_BARE_STARTS = 2.0
RUN_SH_MAX_BARE_STARTS = 8.0


def _on_ci() -> bool:
    # GitHub Actions and most CI services set CI=true
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def _nothing_new_cli(plugin_data: Path, *, bare: bool = False, startup_delay: float = 0.0) -> Path:
    """The CLI run.sh resolves first ($DATA_DIR/bin), started the way uv's script starts it: this
    tree's console entry, or with `bare` the same interpreter exiting at once. `startup_delay`
    stands in for a shared runner, where every process start costs more."""
    from test_plugin import _write_fake_cli

    code = "import sys; sys.exit(0)" if bare else (
        "import sys; from agent_postmortem.entry import main; sys.exit(main(sys.argv[1:]))")
    # each delayed start leaves a line in `delays`, so a test can tell the sleep ran
    delay = f"sleep {startup_delay}\necho x >> '{plugin_data / 'delays'}'\n" if startup_delay else ""
    return _write_fake_cli(plugin_data / "bin", "agent-postmortem", (
        "#!/bin/sh\n" + delay
        + f"PYTHONPATH='{REPO / 'src'}' exec '{sys.executable}' -c '{code}' \"$@\"\n"
    ))


class _Timings(NamedTuple):
    hook: list[float]  # the call with nothing new, through run.sh
    bare: list[float]  # the bare CLI through the same run.sh
    direct: list[float]  # the bare CLI started without run.sh
    hook_data: Path
    bare_data: Path


def _post_tool(command: list[str], plugin_data: Path, payload: str) -> subprocess.CompletedProcess:
    # the environment test_plugin._run_run_sh gives run.sh
    from test_plugin import SAFE_SYSTEM_PATH

    env = {"PATH": SAFE_SYSTEM_PATH, "CLAUDE_PLUGIN_DATA": str(plugin_data),
           "HOME": str(plugin_data)}
    return subprocess.run(command, input=payload, env=env, capture_output=True, text=True,
                          timeout=15)


def _time_nothing_new_calls(tmp_path, main_path: Path, *, pairs: int, startup_delay: float = 0.0,
                            hook_delay: float = 0.0, run_sh_delay: float = 0.0) -> _Timings:
    """Interleaved timings of a post-tool call with nothing new, of a bare start through the same
    run.sh and of the bare CLI started directly, so a burst of load falls on all three of a
    round. `hook_delay` slows the hook's CLI alone, `run_sh_delay` a copy of run.sh alone."""
    from test_plugin import RUN_SH

    script = RUN_SH
    if run_sh_delay:
        text = RUN_SH.read_text(encoding="utf-8")
        assert text.count("\nset -u\n") == 1
        script = tmp_path / "plugin" / "hooks" / "run.sh"
        script.parent.mkdir(parents=True)
        script.write_text(text.replace("\nset -u\n", f"\nset -u\nsleep {run_sh_delay}\n"),
                          encoding="utf-8")
    script = str(script)
    payload = json.dumps(_payload(main_path))
    hook_data, bare_data = tmp_path / "plugin-data", tmp_path / "plugin-data-bare"
    _nothing_new_cli(hook_data, startup_delay=startup_delay + hook_delay)
    bare_cli = _nothing_new_cli(bare_data, bare=True, startup_delay=startup_delay)
    runs = (
        (["bash", script, "post-tool"], hook_data),
        (["bash", script, "post-tool"], bare_data),
        ([str(bare_cli), "hook", "post-tool"], bare_data),
    )
    first = _post_tool(*runs[0], payload)
    assert first.returncode == 0 and first.stdout == "", first
    timings = _Timings([], [], [], hook_data, bare_data)
    for _ in range(pairs):
        for (command, data), samples in zip(runs, timings[:3], strict=True):
            start = time.perf_counter()
            result = _post_tool(command, data, payload)
            samples.append(time.perf_counter() - start)
            assert result.returncode == 0 and result.stdout == "", result
    return timings


def _ms(samples: list[float]) -> str:
    """The samples in the order they were timed, so a pair lines up across two lists."""
    return "[" + ", ".join(f"{s * 1000:.0f}" for s in samples) + "] ms"


def _paired_ratio(slow: list[float], fast: list[float]) -> float:
    """The median of each pair's ratio: both of a pair ran under the same load."""
    return statistics.median(s / f for s, f in zip(slow, fast, strict=True))


def _ci_budget_failure(hook: list[float], bare: list[float], direct: list[float]) -> str | None:
    """Why the nothing-new call misses the budget CI checks, or None."""
    ratio = _paired_ratio(hook, bare)
    if ratio >= NOTHING_NEW_MAX_BARE_STARTS:
        return (f"the call: {ratio:.2f} times a bare start through run.sh:"
                f" {_ms(hook)} against {_ms(bare)}")
    ratio = _paired_ratio(bare, direct)
    if ratio >= RUN_SH_MAX_BARE_STARTS:
        return (f"run.sh: {ratio:.2f} times the bare CLI started directly:"
                f" {_ms(bare)} against {_ms(direct)}")
    return None


def test_run_sh_answers_a_call_with_nothing_new_silently_and_close_to_a_bare_start(tmp_path):
    # the whole path a real tool call takes: run.sh resolving the CLI, its deadline wrapper, then
    # the console entry and the gate; the CLI is this tree's, started the way uv's script does
    from test_plugin import _run_run_sh

    main_path = _session(tmp_path)
    agent = aid(95)
    transcript = _running_background_agent(main_path, agent)
    timings = _time_nothing_new_calls(tmp_path, main_path, pairs=9)
    failure = _ci_budget_failure(timings.hook, timings.bare, timings.direct)
    assert failure is None, failure
    # control: the same path does speak, so the silence above is the gate, not a failed start
    _kill(transcript, agent)
    told = _run_run_sh("post-tool", path_entries=[], plugin_data=timings.hook_data,
                       stdin=json.dumps(_payload(main_path)))
    context = json.loads(told.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "KILL" in context, told


@pytest.mark.skipif(_on_ci(), reason="a wall-clock budget on a shared runner is noise (inbox E47);"
                    " CI checks the same call against a bare start instead")
def test_run_sh_answers_a_call_with_nothing_new_within_150_ms(tmp_path):
    main_path = _session(tmp_path)
    _running_background_agent(main_path, aid(95))
    hook = _time_nothing_new_calls(tmp_path, main_path, pairs=9).hook
    median = statistics.median(hook)
    assert median < NOTHING_NEW_BUDGET_SECONDS, f"median {median * 1000:.0f} ms: {_ms(hook)}"


def test_a_slow_runner_does_not_fail_the_ci_budget_of_a_correct_build(tmp_path):
    # inbox E47: on a shared macos-latest runner this build's call took a median of 152 ms
    # (samples 126 to 521 ms) and failed the 150 ms budget. Here every process start costs 120 ms
    # more, as on a slow runner; the build is the same, so the check CI runs must still pass.
    main_path = _session(tmp_path)
    _running_background_agent(main_path, aid(95))
    timings = _time_nothing_new_calls(tmp_path, main_path, pairs=5, startup_delay=0.12)
    # every timed path ran its delay: the hook's CLI at the first call and each round, the bare
    # CLI twice a round (through run.sh and directly)
    assert (timings.hook_data / "delays").read_text().count("x") == 6
    assert (timings.bare_data / "delays").read_text().count("x") == 10
    for samples in timings[:3]:
        assert min(samples) >= 0.12, _ms(samples)
    failure = _ci_budget_failure(timings.hook, timings.bare, timings.direct)
    assert failure is None, failure


def _fault_scale(tmp_path, main_path: Path) -> _Timings:
    """This machine's own timings, so a fault can be sized from them and read the same on a
    runner of any speed (review: a fixed 500 ms in run.sh reads under 8 direct starts once a
    direct start takes 70 ms)."""
    return _time_nothing_new_calls(tmp_path / "scale", main_path, pairs=3)


def test_the_ci_budget_fails_a_call_that_got_slower_through_run_sh(tmp_path):
    # review: the timing harness itself must see the call alone get slower (the hook's CLI alone
    # waits 3 bare starts), not just the comparator on numbers
    main_path = _session(tmp_path)
    _running_background_agent(main_path, aid(95))
    delay = 3 * statistics.median(_fault_scale(tmp_path, main_path).bare)
    timings = _time_nothing_new_calls(tmp_path, main_path, pairs=3, hook_delay=delay)
    failure = _ci_budget_failure(timings.hook, timings.bare, timings.direct)
    assert failure is not None and failure.startswith("the call:"), failure


def test_the_ci_budget_fails_a_run_sh_that_got_slower(tmp_path):
    # review: and run.sh alone get slower (12 direct starts more), which the bare start through it
    # shares
    main_path = _session(tmp_path)
    _running_background_agent(main_path, aid(95))
    delay = 12 * statistics.median(_fault_scale(tmp_path, main_path).direct)
    timings = _time_nothing_new_calls(tmp_path, main_path, pairs=3, run_sh_delay=delay)
    failure = _ci_budget_failure(timings.hook, timings.bare, timings.direct)
    assert failure is not None and failure.startswith("run.sh:"), failure


def test_the_ci_budget_fails_a_call_that_costs_more_than_two_bare_starts():
    # measured on the reference machine at rest: the bare CLI started directly 19 ms, through run.sh
    # 46 ms, the call 62 ms, and 112 ms when the console entry also imports cli (what the gate
    # exists to avoid)
    assert _ci_budget_failure([0.062] * 9, [0.046] * 9, [0.019] * 9) is None
    failure = _ci_budget_failure([0.112] * 9, [0.046] * 9, [0.019] * 9)
    assert failure is not None and "2.43 times" in failure, failure


def test_the_ci_budget_reads_each_pair_under_its_own_load():
    # review: load that changes during the test splits the two sets of samples, so their separate
    # medians compare a slow call with a fast bare start; each pair was timed under one load
    hook = [0.162] * 4 + [0.062] * 4 + [0.162]
    bare = [0.146] * 4 + [0.046] * 4 + [0.046]
    direct = [0.119] * 4 + [0.019] * 4 + [0.019]
    assert _ci_budget_failure(hook, bare, direct) is None


def test_the_ci_budget_fails_a_run_sh_that_costs_more_than_eight_bare_starts():
    # review: the bare start runs the same run.sh, so a slower run.sh slows both sides; the bare CLI
    # started without run.sh is the wrapper's own reference (2.6 times at rest, about 4 under three
    # busy processes per core). 200 ms more in run.sh: 262, 246 and 19 ms.
    failure = _ci_budget_failure([0.262] * 9, [0.246] * 9, [0.019] * 9)
    assert failure is not None and "run.sh" in failure and "12.95 times" in failure, failure


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
