"""The per-call hook tells a background command that stopped writing (item 2 of the field report
2026-11-03 "no silent failures"; docs/DESIGN.md 4.14.5).

At the session's next tool call or prompt, a background command whose files have not changed for
30 minutes, or whose log ends in codex's "Reading additional input from stdin..." with nothing
since for 2 minutes, is told once, the way an interrupted subagent is. The field worker had no
subagent at all, which the gate used to answer without reading anything.

These tests run the real gate (`hooks_cli.dispatch("post-tool")`) on the wall clock: transcript
times and file times are set relative to now. Synthetic data only: the fake session id, paths
under tmp_path.
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

from live_helpers import FAKE_UUID, tool_result, tool_use, user, write_jsonl
from test_live_session import SLUG

from agent_postmortem import hook_gate, hooks_cli, state

STDIN_LINE = "Reading additional input from stdin...\n"
TASK = "b00000001"
FIELD = 'codex exec --sandbox read-only "$(cat prompt.txt)" > review.md 2> review.log'


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


class Worker:
    def __init__(self, tmp_path, launched_ago: float, command: str = FIELD):
        self.main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()
        self.task_output = tmp_path / "tasks" / f"{TASK}.output"
        self.task_output.parent.mkdir()
        self.task_output.touch()
        self.set_time(self.task_output, launched_ago)
        start = _ago(launched_ago + 60)
        at = _ago(launched_ago)
        text = (f"Command running in background with ID: {TASK}. Output is being written to: "
                f"{self.task_output}. You will be notified when it completes.")
        entries = [
            user(None, start, "start"),
            tool_use(None, at, "toolu_sh1", "Bash", {"command": command,
                                                     "run_in_background": True}),
            tool_result(None, at, "toolu_sh1", text, toolUseResult={"backgroundTaskId": TASK}),
        ]
        for entry in entries:
            entry["cwd"] = str(self.cwd)
        write_jsonl(self.main, entries)

    @staticmethod
    def set_time(path, ago: float) -> None:
        stamp = time.time() - ago
        os.utime(path, (stamp, stamp))

    def file(self, name: str, text: str, ago: float):
        path = self.cwd / name
        path.write_text(text, encoding="utf-8")
        self.set_time(path, ago)
        return path

    def payload(self) -> dict:
        return {"hook_event_name": "PostToolUse", "session_id": FAKE_UUID,
                "transcript_path": str(self.main), "tool_name": "Bash",
                "tool_input": {"command": "ls"}, "tool_response": {"stdout": ""}}


def _hook(monkeypatch, capsys, worker: Worker) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(worker.payload())))
    assert hooks_cli.dispatch("post-tool") == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_the_field_command_waiting_on_stdin_is_told_at_the_next_call(tmp_path, monkeypatch,
                                                                      capsys):
    w = Worker(tmp_path, launched_ago=180)
    w.file("review.md", "", 180)
    w.file("review.log", STDIN_LINE, 178)
    text = _hook(monkeypatch, capsys, w)
    assert f"COMMAND {TASK}" in text and "waits on stdin" in text, text
    assert "TaskStop" in text and "</dev/null" in text
    assert len(text) <= 400


def test_a_command_silent_for_thirty_minutes_is_told(tmp_path, monkeypatch, capsys):
    w = Worker(tmp_path, launched_ago=40 * 60, command="make long > build.log 2>&1")
    w.file("build.log", "step 1\n", 35 * 60)
    text = _hook(monkeypatch, capsys, w)
    assert f"COMMAND {TASK}" in text and "written nothing for 35m" in text, text


def test_a_command_that_still_writes_is_not_told(tmp_path, monkeypatch, capsys):
    w = Worker(tmp_path, launched_ago=40 * 60, command="make long > build.log 2>&1")
    w.file("build.log", "step 9\n", 60)
    assert _hook(monkeypatch, capsys, w) == ""


def test_a_silent_command_is_told_once(tmp_path, monkeypatch, capsys):
    w = Worker(tmp_path, launched_ago=180)
    w.file("review.md", "", 180)
    w.file("review.log", STDIN_LINE, 178)
    assert "COMMAND" in _hook(monkeypatch, capsys, w)
    assert _hook(monkeypatch, capsys, w) == ""
    assert _hook(monkeypatch, capsys, w) == ""


def test_the_gate_opens_again_when_a_command_can_have_gone_silent(tmp_path, monkeypatch, capsys):
    w = Worker(tmp_path, launched_ago=60)
    out = w.file("review.md", "", 60)
    log = w.file("review.log", STDIN_LINE, 58)
    assert _hook(monkeypatch, capsys, w) == ""  # 58 s: not yet a wait on stdin
    gate_path = hook_gate._gate_path(str(w.main))
    gate = json.loads(state.read_private_bytes(gate_path))
    assert gate["recheck_at"] == __import__("pytest").approx(time.time() - 58 + 120, abs=5)
    # two minutes pass: every file ages, and the gate's recheck time is due
    for path, ago in ((w.task_output, 180), (out, 180), (log, 178)):
        w.set_time(path, ago)
    gate["recheck_at"] = time.time() - 1
    state.write_private_bytes(gate_path, json.dumps(gate).encode())
    text = _hook(monkeypatch, capsys, w)
    assert "waits on stdin" in text, text


def test_a_session_with_no_subagent_and_no_background_command_writes_nothing(tmp_path,
                                                                           monkeypatch, capsys):
    main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main, [user(None, _ago(60), "start")])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "PostToolUse", "session_id": FAKE_UUID, "transcript_path": str(main),
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {}})))
    assert hooks_cli.dispatch("post-tool") == 0
    assert capsys.readouterr().out == ""
    assert not hook_gate._gate_path(str(main)).exists()


# --- review round 1 ------------------------------------------------------------------------------


def _two_silent(tmp_path):
    w = Worker(tmp_path, launched_ago=180)
    w.file("review.md", "", 180)
    w.file("review.log", STDIN_LINE, 178)
    second = tmp_path / "tasks" / "b00000002.output"
    second.touch()
    Worker.set_time(second, 40 * 60)
    text = (f"Command running in background with ID: b00000002. Output is being written to: "
            f"{second}. You will be notified when it completes.")
    at = _ago(40 * 60)
    entries = [tool_use(None, at, "toolu_sh2", "Bash", {"command": "wait-for-it",
                                                        "run_in_background": True}),
               tool_result(None, at, "toolu_sh2", text,
                           toolUseResult={"backgroundTaskId": "b00000002"})]
    for entry in entries:
        entry["cwd"] = str(w.cwd)
    write_jsonl(w.main, entries, mode="a")
    return w


def test_every_silent_command_is_named_at_some_call(tmp_path, monkeypatch, capsys):
    w = _two_silent(tmp_path)
    named = set()
    for _ in range(3):
        text = _hook(monkeypatch, capsys, w)
        assert len(text) <= 400
        named |= {task for task in ("b00000001", "b00000002") if f"COMMAND {task}" in text}
    assert named == {"b00000001", "b00000002"}


def test_a_command_whose_files_cannot_be_inspected_is_checked_again(monkeypatch):
    from agent_postmortem import stalls
    shell = {"ts": time.time() - 40 * 60, "outputs": ["/nonexistent/dir/out.log"]}
    state = stalls.command_state("b00000001", shell, time.time())
    assert state["silent"] is False and state["recheck_seconds"] is not None


def test_endings_and_commands_together_stay_within_400_characters():
    # the largest endings message the budget leaves when commands wait (400 less the reserve)
    endings = "KILL   " + "x" * (hooks_cli._HOOK_BUDGET - hooks_cli._COMMAND_RESERVE - 7)
    commands = [{"task_id": f"b0000000{n}", "launched_seconds": 2400, "silent_seconds": 2100,
                 "waiting_on_stdin": False} for n in range(1, 4)]
    text, shown = hooks_cli._with_commands(endings, commands)
    assert len(text) <= 400
    assert shown < len(commands) and "more: agent-postmortem status" in text


# --- review round 2 ------------------------------------------------------------------------------


def test_a_command_dropped_while_claiming_is_not_recorded(tmp_path, monkeypatch, capsys):
    # another channel wins an ending between the check and its delivery; the endings left re-render
    # longer and the command no longer fits: its key must stay unclaimed, to be told at a later call
    from agent_postmortem import live

    w = Worker(tmp_path, launched_ago=40 * 60, command="make long > build.log 2>&1")
    w.file("build.log", "step 1\n", 35 * 60)
    real = hooks_cli._with_commands
    calls = []

    def squeezed(endings, commands, more=0):
        calls.append(len(commands))
        if len(calls) == 2:  # inside the claim: as if the endings left took the room
            return real("x" * 390, commands, more)
        return real(endings, commands, more)

    monkeypatch.setattr(hooks_cli, "_with_commands", squeezed)
    text = _hook(monkeypatch, capsys, w)
    assert "COMMAND" not in text
    assert "cmd:b00000001:silent" not in live.delivered_keys(w.main)
    monkeypatch.setattr(hooks_cli, "_with_commands", real)
    assert "COMMAND b00000001" in _hook(monkeypatch, capsys, w)


def test_an_output_that_cannot_be_read_is_checked_again(tmp_path, monkeypatch):
    from agent_postmortem import stalls
    log = tmp_path / "build.log"
    log.write_text("step 1\n")
    stamp = time.time() - 60
    os.utime(log, (stamp, stamp))
    real_open = type(log).open

    def denied(self, *args, **kwargs):
        if self == log:
            raise PermissionError("no read")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(log), "open", denied)
    shell = {"ts": time.time() - 40 * 60, "outputs": [str(log)]}
    state = stalls.command_state("b00000001", shell, time.time())
    assert state["recheck_seconds"] == 60.0
    assert state["silent_seconds"] is None
