"""`status` and `scan` say what a blocked background session waits for, whose it is and what
unblocks it (docs/DESIGN.md 4.7, 4.14.4). Also the two false alarms of
that day: a Monitor asked to arm a deadline, and a deliberately silent wait told as hung.

Synthetic data only: the fake session id, paths under tmp_path, invented worker names.
"""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from live_helpers import FAKE_UUID, tool_result, tool_use, write_jsonl
from test_worker_notice import PERMISSION, SHORT, Manager, _ago, _hook

from agent_postmortem import hooks_cli, live, workers
from agent_postmortem.cli import main

OTHER = "0a0b0c0d"
FREE_TEXT = "zz-private: choose option a or b"


def _session(m: Manager, short: str) -> str:
    """The worker's own transcript, so its row has a public id."""
    path = m.main.parent / f"{short}-0000-4000-8000-000000000000.jsonl"
    write_jsonl(path, [])
    return live.session_public_id(path)


def _status(m: Manager, capsys, *flags) -> str:
    assert main(["status", str(m.main), *flags]) == 0
    return capsys.readouterr().out


def _row(text: str, word: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"  {word}"))


# --- status ------------------------------------------------------------------------------------


def test_a_permission_row_has_its_class_owner_and_next_action(tmp_path, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=900)
    public = _session(m, SHORT)
    share = _row(_status(m, capsys), "BLOCKED")
    assert public in share and "for 15m" in share
    assert "waiting for a permission approval" in share and "dispatched by this session" in share
    assert SHORT not in share and "night-run" not in share and "Entering" not in share, share
    local = _row(_status(m, capsys, "--local"), "BLOCKED")
    assert "night-run" in local and "(approve Entering worktree)" in local, local
    assert "dispatched by this session" in local and f"-> claude attach {SHORT}" in local, local


def test_a_live_prompt_reads_blocked_though_its_state_word_says_working(tmp_path, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="blocked", needs="approve Write: /tmp/zz-private", mtime_ago=240)
    _session(m, SHORT)
    text = _status(m, capsys, "--local")
    row = _row(text, "BLOCKED")
    assert "(approve Write)" in row and "dispatched by this session" in row, row
    assert "1 blocked" in text and "1 working" not in text and "zz-private" not in text, text


def test_a_new_prompt_after_delivering_reads_working(tmp_path, capsys):
    m = Manager(tmp_path)
    m.job(state="done", tempo="active", output={"result": "r"}, mtime_ago=60)
    _session(m, SHORT)
    assert "1 working" in _status(m, capsys, "--local")


def test_a_worker_of_another_session_is_not_called_ours(tmp_path, capsys):
    m = Manager(tmp_path)
    m.job(OTHER, state="blocked", needs="awaiting input", mtime_ago=900)
    _session(m, OTHER)
    row = _row(_status(m, capsys, "--local"), "BLOCKED")
    assert "waiting for an answer" in row and "dispatched" not in row, row


def test_a_finished_worker_gets_its_own_row_and_count(tmp_path, capsys):
    m = Manager(tmp_path)
    m.job(state="blocked", needs=FREE_TEXT, output={"result": "the report"}, mtime_ago=900)
    _session(m, SHORT)
    text = _status(m, capsys, "--local")
    row = _row(text, "DELIVERED")
    assert "delivered, waiting on a decision" in row and f"-> claude logs {SHORT}" in row, row
    assert "BLOCKED" not in text and "1 delivered" in text and "blocked" not in text.lower(), text
    assert "zz-private" not in text and "the report" not in text


def test_a_question_withholds_the_workers_words_at_every_level(tmp_path, capsys):
    m = Manager(tmp_path)
    m.job(state="blocked", needs=FREE_TEXT, mtime_ago=900)
    _session(m, SHORT)
    for flags in ((), ("--local",), ("--raw",)):
        text = _status(m, capsys, *flags)
        assert "waiting for an answer" in text and "zz-private" not in text, text


def test_old_blocked_rows_of_other_sessions_fold_into_one_line(tmp_path, capsys):
    m = Manager(tmp_path)
    for short in ("0a0b0c01", "0a0b0c02"):
        m.job(short, state="blocked", needs="awaiting input", mtime_ago=5 * 86400)
        _session(m, short)
    m.job(OTHER, state="blocked", needs="awaiting input", mtime_ago=3600)
    recent = _session(m, OTHER)
    m.launched(ago=5 * 86400)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=4 * 86400, created_ago=5 * 86400)
    mine = _session(m, SHORT)
    lines = [line for line in _status(m, capsys).splitlines() if line.startswith("  BLOCKED")]
    assert len(lines) == 3, lines
    assert mine in lines[0] and "dispatched by this session" in lines[0]  # own first, never folded
    assert recent in lines[1]
    assert "2 more" in lines[2] and "other sessions" in lines[2], lines[2]


def test_status_json_says_class_owner_label_and_id_by_level(tmp_path, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=900)
    _session(m, SHORT)
    (share,) = json.loads(_status(m, capsys, "--json"))["background"]["sessions"]
    assert share["class"] == "permission" and share["mine"] is True
    assert "label" not in share and "short" not in share
    (local,) = json.loads(_status(m, capsys, "--local", "--json"))["background"]["sessions"]
    assert local["label"] == PERMISSION and local["short"] == SHORT


# --- scan --------------------------------------------------------------------------------------


def _scan(m: Manager, capsys, monkeypatch, *flags) -> tuple[int, str]:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", FAKE_UUID)
    code = main(["scan", "--root", str(m.main.parent.parent), *flags])
    return code, capsys.readouterr().out


def test_scan_lists_a_permission_prompt_first_and_says_whose_it_is(tmp_path, capsys, monkeypatch):
    """The field report: "NEEDS ATTENTION (0 of 14 runs) nothing needs attention" with a worker
    blocked all night."""
    m = Manager(tmp_path)
    m.job(OTHER, state="blocked", needs="awaiting input", mtime_ago=7200, name="theirs")
    other = _session(m, OTHER)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=900)
    mine = _session(m, SHORT)
    _code, out = _scan(m, capsys, monkeypatch, "--since", "2099-01-01")
    assert "nothing needs attention" not in out, out
    assert out.index(mine) < out.index(other), out
    row = next(line for line in out.splitlines() if mine in line)
    assert "for 15m" in row and "BLOCKED" in row and "waiting for a permission approval" in row
    assert "dispatched by this session" in row, row
    assert "-> agent-postmortem status --local" in out and SHORT not in out
    _code, out = _scan(m, capsys, monkeypatch, "--since", "2099-01-01", "--local")
    assert f"-> claude attach {SHORT}" in out, out


def test_scan_lists_a_live_prompt_with_the_state_word_working(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=900)
    public = _session(m, SHORT)
    _code, out = _scan(m, capsys, monkeypatch)
    assert any(public in line and "BLOCKED" in line and "dispatched by this session" in line
               for line in out.splitlines()), out


def test_scan_does_not_list_a_worker_that_delivered(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.job(state="blocked", needs=FREE_TEXT, output={"result": "r"}, mtime_ago=900)
    public = _session(m, SHORT)
    _code, out = _scan(m, capsys, monkeypatch)
    assert public not in out and "zz-private" not in out, out


@pytest.mark.parametrize(
    "fields,words",
    [
        ({"state": "failed"}, "FAILED"),
        ({"state": "blocked", "detail": "API Error: 500"}, "stopped on an API error"),
        ({"state": "blocked"}, "the reason is not recorded"),
    ],
)
def test_scan_lists_the_other_troubles(tmp_path, capsys, monkeypatch, fields, words):
    m = Manager(tmp_path)
    m.job(mtime_ago=900, **fields)
    public = _session(m, SHORT)
    _code, out = _scan(m, capsys, monkeypatch)
    assert any(public in line and words in line for line in out.splitlines()), out


def test_scan_counts_old_blocked_rows_of_other_sessions(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    for short in ("0a0b0c01", "0a0b0c02", "0a0b0c03"):
        m.job(short, state="blocked", needs="awaiting input", mtime_ago=4 * 86400)
        _session(m, short)
    _code, out = _scan(m, capsys, monkeypatch)
    assert "3 more blocked in other sessions for over 3 days" in out, out
    assert "BLOCKED  waiting" not in out, out


def test_scan_json_carries_the_blocked_list_and_keeps_its_shape_without_one(
        tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.launched()
    _code, out = _scan(m, capsys, monkeypatch, "--json")
    assert "blocked" not in json.loads(out)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=900)
    public = _session(m, SHORT)
    (entry,) = json.loads(_scan(m, capsys, monkeypatch, "--json")[1])["blocked"]
    assert entry["session"] == public and entry["class"] == "permission" and entry["mine"] is True
    assert "short" not in entry and "label" not in entry
    (entry,) = json.loads(_scan(m, capsys, monkeypatch, "--json", "--local")[1])["blocked"]
    assert entry["short"] == SHORT and entry["next_command"] == f"claude attach {SHORT}"


def test_scan_here_keeps_only_a_worker_in_the_callers_directory(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=900, cwd="/tmp/not-here")
    public = _session(m, SHORT)
    monkeypatch.chdir(tmp_path)
    _code, out = _scan(m, capsys, monkeypatch, "--here")
    assert public not in out


# --- the two false alarms ----------------------------------------------------------------------


def _stop(m: Manager, capsys, monkeypatch, tasks: list[dict]) -> str:
    payload = {"hook_event_name": "Stop", "session_id": FAKE_UUID,
               "transcript_path": str(m.main), "background_tasks": tasks, "session_crons": []}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch("check") == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def _monitor(m: Manager, task: str, *, ago: float = 60.0, timeout_ms: int = 1800000) -> None:
    m.append(tool_use(None, _ago(ago), "toolu_mon", "Monitor",
                      {"command": "agent-postmortem watch --session x", "timeout_ms": timeout_ms}),
             tool_result(None, _ago(ago - 1), "toolu_mon", "started",
                         toolUseResult={"taskId": task, "timeoutMs": timeout_ms}))


def test_a_monitor_listed_as_a_shell_task_is_not_asked_to_arm_a_deadline(
        tmp_path, capsys, monkeypatch):
    """The field report: DEADLINE at the end of three turns for a Monitor that was the deadline."""
    m = Manager(tmp_path)
    _monitor(m, "m00000001")
    task = {"id": "m00000001", "type": "shell", "status": "running",
            "command": "agent-postmortem watch --session x"}
    assert _stop(m, capsys, monkeypatch, [task]) == ""


def test_a_monitor_also_arms_a_deadline_for_the_commands_beside_it(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _monitor(m, "m00000001")
    tasks = [{"id": "m00000001", "type": "shell", "status": "running", "command": "x"},
             {"id": "b00000001", "type": "shell", "status": "running", "command": "codex exec y"}]
    assert _stop(m, capsys, monkeypatch, tasks) == ""


def test_a_plain_background_command_is_still_asked_about(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    task = {"id": "b00000001", "type": "shell", "status": "running", "command": "codex exec y"}
    assert "DEADLINE b00000001" in _stop(m, capsys, monkeypatch, [task])


def test_a_monitor_past_its_timeout_arms_nothing(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _monitor(m, "m00000001", ago=4000, timeout_ms=60000)
    tasks = [{"id": "b00000001", "type": "shell", "status": "running", "command": "codex exec y"}]
    assert "DEADLINE b00000001" in _stop(m, capsys, monkeypatch, tasks)


WAIT_LOOP = ("while true; do s=$(cat job.state); case $s in done) exit 0;; esac; sleep 60; done")


def _silent_command(m: Manager, tmp_path, command: str, *, ago: float = 40 * 60,
                    last_line: str = "") -> None:
    task = "b00000008"
    output = tmp_path / "tasks" / f"{task}.output"
    output.parent.mkdir(exist_ok=True)
    output.write_text(last_line, encoding="utf-8")
    stamp = time.time() - ago
    os.utime(output, (stamp, stamp))
    text = (f"Command running in background with ID: {task}. Output is being written to: "
            f"{output}. You will be notified when it completes.")
    use = tool_use(None, _ago(ago), "toolu_sh", "Bash",
                   {"command": command, "run_in_background": True})
    result = tool_result(None, _ago(ago), "toolu_sh", text,
                         toolUseResult={"backgroundTaskId": task})
    use["cwd"] = result["cwd"] = str(tmp_path)
    m.append(use, result)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"timeout 43200 bash -c '{WAIT_LOOP}' < /dev/null", id="the field command"),
        pytest.param(f"{WAIT_LOOP}  # postmortem: silent-wait", id="the marker"),
        pytest.param("# postmortem: silent-wait\nmake long", id="the marker on its own line"),
    ],
)
def test_a_wait_that_is_silent_by_design_is_not_told_as_hung(tmp_path, capsys, monkeypatch, command):
    m = Manager(tmp_path)
    _silent_command(m, tmp_path, command)
    text = _hook(monkeypatch, capsys, m)
    assert "COMMAND" not in text, text
    assert "silent" not in _status(m, capsys)


def test_a_run_under_its_own_perl_alarm_is_quiet_by_design(tmp_path, capsys, monkeypatch):
    """Field report 2026-11-04: `perl -e 'alarm 16200; exec ...'` ends on its own and wrote its
    results to a folder of its own, so its silent task output said nothing about it."""
    m = Manager(tmp_path)
    _silent_command(m, tmp_path, "caffeinate -i perl -e 'alarm 16200; exec @ARGV' run-pilot")
    assert "COMMAND" not in _hook(monkeypatch, capsys, m)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("bash -c 'while true; do sleep 60; done'",
                     id="a wait loop with no timeout, no marker"),
        pytest.param("make long > build.log", id="an ordinary command"),
        pytest.param("timeout 0 make long", id="timeout 0 is no timeout"),
    ],
)
def test_a_command_that_should_be_writing_is_still_told(tmp_path, capsys, monkeypatch, command):
    m = Manager(tmp_path)
    _silent_command(m, tmp_path, command)
    (tmp_path / "build.log").touch()
    stamp = time.time() - 40 * 60
    os.utime(tmp_path / "build.log", (stamp, stamp))
    assert "COMMAND b00000008" in _hook(monkeypatch, capsys, m)


def test_the_stdin_rule_still_speaks_for_a_quiet_command(tmp_path, capsys, monkeypatch):
    m = Manager(tmp_path)
    _silent_command(m, tmp_path, "timeout 3600 codex exec review", ago=300,
                    last_line="Reading additional input from stdin...\n")
    assert "waits on stdin" in _hook(monkeypatch, capsys, m)


# --- the help --------------------------------------------------------------------------------


def test_top_level_help_says_watch_follows_a_session_too(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--session" in text and "background worker" in text, text


def test_workers_module_has_no_heavy_import():
    assert workers.__doc__
    source = open(workers.__file__, encoding="utf-8").read()
    for heavy in ("from . import live", "import cli", "hooks_cli"):
        assert heavy not in source


def test_the_readmes_worker_line_is_what_the_hook_prints():
    """The README opens with this line, so it must be the hook's own words."""
    from pathlib import Path

    worker = {"short": "a1b2c3d4", "name": "night-run", "class": workers.PERMISSION,
              "label": "approve Entering worktree", "age": 900.0}
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert f"`{workers.line(worker)}`" in readme
