"""The field report "how four worker sessions used the plugin today" (2026-11-04; docs/DESIGN.md
4.14.4, 4.14.5). Two workers with subagents never ran `status` because nothing reminded them; four
DEADLINE requests and a COMMAND line were wrong about commands bounded by their own perl alarm or
that were the deadline; and in a worker, `status` listed sessions it does not own.

Synthetic data only: the fake session id, paths under tmp_path.
"""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from live_helpers import FAKE_UUID, tool_result, tool_use
from test_worker_notice import PERMISSION, SHORT, Manager, _ago
from test_worker_rows import _row, _session, _silent_command, _status

from agent_postmortem import commands, hooks_cli, stalls

AGENT_A = "a" + "1" * 16
AGENT_B = "a" + "2" * 16


def _bg(monkeypatch, kind: str = "bg") -> None:
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {FAKE_UUID: kind})


def _subagent(m: Manager, agent: str, *, ago: float = 60.0, returned: bool = True) -> None:
    tool = f"toolu_{agent}"
    m.append(tool_use(None, _ago(ago + 30), tool, "Agent",
                      {"prompt": "x", "subagent_type": "general-purpose",
                       "run_in_background": not returned}))
    result = {"agentId": agent, "status": "completed" if returned else "async_launched"}
    m.append(tool_result(None, _ago(ago), tool, "done", toolUseResult=result))
    sub = m.main.parent / FAKE_UUID / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent}.jsonl").write_text("{}\n", encoding="utf-8")


def _ran(m: Manager, command: str, *, ago: float, tool: str = "Bash") -> None:
    name = f"toolu_ran{len(m.entries)}"
    m.append(tool_use(None, _ago(ago), name, tool, {"command": command}),
             tool_result(None, _ago(ago), name, "ok", toolUseResult={"stdout": "ok"}))


def _stop(m: Manager, capsys, monkeypatch, **extra) -> str:
    payload = {"hook_event_name": "Stop", "session_id": FAKE_UUID, "transcript_path": str(m.main),
               "background_tasks": [], "session_crons": [], **extra}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch("check") == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "") if out else ""


# --- 1. a worker with subagents is reminded once -------------------------------------------------


def test_a_worker_whose_subagents_finished_is_reminded_once(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A)
    _subagent(m, AGENT_B)
    _bg(monkeypatch)
    text = _stop(m, capsys, monkeypatch)
    assert text.startswith("2 subagents finished and agent-postmortem status has not run since"), text
    assert "before reporting" in text and len(text) <= 400
    assert _stop(m, capsys, monkeypatch) == ""


def test_one_that_ran_status_since_is_not_reminded(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A, ago=60)
    _ran(m, "agent-postmortem status --local", ago=30)
    _bg(monkeypatch)
    assert _stop(m, capsys, monkeypatch) == ""


def test_a_status_from_before_the_last_ending_does_not_count(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _ran(m, "agent-postmortem status", ago=300)
    _subagent(m, AGENT_A, ago=60)
    _bg(monkeypatch)
    assert "1 subagent finished" in _stop(m, capsys, monkeypatch)


def test_a_session_watched_by_a_monitor_is_not_reminded(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A, ago=60)
    _ran(m, "agent-postmortem watch --session x", ago=30, tool="Monitor")
    _bg(monkeypatch)
    assert _stop(m, capsys, monkeypatch) == ""


def test_a_subagent_still_out_holds_the_reminder(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A)
    _subagent(m, AGENT_B, returned=False)
    _bg(monkeypatch)
    assert _stop(m, capsys, monkeypatch) == ""


def test_a_finished_workflow_counts(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.append(tool_use(None, _ago(90), "toolu_wf", "Workflow", {"script": "x"}),
             tool_result(None, _ago(60), "toolu_wf", "done",
                         toolUseResult={"runId": "wf_00000000-d30", "status": "completed"}))
    (m.main.parent / FAKE_UUID / "subagents").mkdir(parents=True)
    _bg(monkeypatch)
    assert "1 workflow finished" in _stop(m, capsys, monkeypatch)


def test_an_interactive_session_has_a_person_and_is_not_reminded(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A)
    _bg(monkeypatch, kind="interactive")
    assert _stop(m, capsys, monkeypatch) == ""


def test_the_opt_out_covers_it(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A)
    _bg(monkeypatch)
    monkeypatch.setenv("AGENT_POSTMORTEM_WATCH_HINT", "0")
    assert _stop(m, capsys, monkeypatch) == ""


def test_a_turn_already_continuing_from_a_stop_hook_is_not_reminded(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A)
    _bg(monkeypatch)
    assert _stop(m, capsys, monkeypatch, stop_hook_active=True) == ""


def test_a_session_with_no_subagent_reads_nothing_for_it(tmp_path, monkeypatch, capsys):
    from agent_postmortem import status

    m = Manager(tmp_path)
    _bg(monkeypatch)
    monkeypatch.setattr(status, "snapshot", lambda *a, **k: pytest.fail("read for no subagent"))
    assert _stop(m, capsys, monkeypatch) == ""


def test_it_waits_while_another_request_speaks(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A)
    _bg(monkeypatch, kind="interactive")
    monkeypatch.setattr(hooks_cli, "_status_reminder", lambda payload: pytest.fail("asked"))
    task = {"id": "b00000001", "type": "shell", "status": "running", "command": "make long"}
    assert "DEADLINE b00000001" in _stop(m, capsys, monkeypatch, background_tasks=[task])


# --- 2. what is already bounded ------------------------------------------------------------------


ALARM = "perl -e 'alarm 16200; exec @ARGV' ./run.sh pilot"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(ALARM, id="alone"),
        pytest.param(f"cd /tmp/work; {ALARM} > out.log 2>&1; echo finished; wc -l out.log", id="plain"),
        pytest.param(f"cd /tmp/work; {ALARM}; echo x; wc -l o; tail -n 5 o | cut -c1-80", id="tail -n"),
        pytest.param(f"mkdir -p /tmp/o; cd /tmp/o; {ALARM}; echo x; head -n 3 o", id="mkdir, head"),
        pytest.param(f"cd /tmp/work; caffeinate -i {ALARM}; echo x; ls o | wc -l", id="caffeinate"),
        pytest.param("cd /tmp/work && perl -e 'alarm shift; exec @ARGV' 1800 ./run.sh", id="alarm shift"),
        pytest.param(f"cd /tmp/work; {ALARM} < /dev/null | tee out.log | tail -n 3", id="a pipe"),
        pytest.param(f"A=1; export B=2; cd /tmp/work; {ALARM}", id="literal assignments"),
        pytest.param(f"cd /tmp/work; S=$(date +%s); caffeinate -i {ALARM}; echo rc=$? $S; exit 0",
                     id="a start time, and what it prints"),
        pytest.param(f"O=/tmp/work/out.log; {ALARM} > $O 2>&1; wc -l \"$O\"; tail -28 ${{O}}",
                     id="a path from a literal assignment"),
    ],
)
def test_the_plain_shapes_of_a_perl_alarm_are_bounded(command):
    # the call's own directory, which a `cd X;` that failed would leave it in
    assert commands.under_its_own_alarm(command, "/tmp") is True
    assert commands.quiet_by_design(command, "/tmp") is True


def test_with_no_known_directory_a_cd_joined_by_a_semicolon_proves_nothing():
    command = f"cd /tmp/work; {ALARM} > out.log"
    assert commands.under_its_own_alarm(command) is False
    assert commands.under_its_own_alarm(command.replace(";", " &&", 1)) is True


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"{ALARM}; tail -f out.log", id="tail -f after it"),
        pytest.param(f"{ALARM}; tail -F out.log", id="tail -F after it"),
        pytest.param(f"{ALARM}; ./another-long-run", id="an unknown command after it"),
        pytest.param(f"./a-long-setup; {ALARM}", id="an unknown command before it"),
        pytest.param(f"{ALARM}; cat", id="cat of its stdin"),
        pytest.param(f"while true; do {ALARM}; done", id="in a loop"),
        pytest.param("perl -e 'alarm 0; exec @ARGV' ./run.sh", id="alarm 0 cancels"),
        pytest.param("perl -e 'alarm 5' && ./long", id="an alarm with no exec"),
        pytest.param("perl -e '$t=1800; $p=fork; if(!$p){exec @ARGV; die 127} "
                     "$SIG{ALRM}=sub{kill 9,$p; exit 124}; alarm $t; waitpid $p,0' ./run.sh",
                     id="a forking wrapper: told once, since nothing here can prove it"),
    ],
)
def test_what_an_alarm_does_not_bound(command):
    assert commands.under_its_own_alarm(command) is False


def test_a_command_under_a_perl_alarm_is_not_asked_for_a_deadline(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _bg(monkeypatch, kind="interactive")
    command = f"cd /tmp/work; {ALARM} > o.log; wc -l o.log"
    task = {"id": "b00000001", "type": "shell", "status": "running", "command": command}
    assert _stop(m, capsys, monkeypatch, background_tasks=[task], cwd=str(tmp_path)) == ""


@pytest.mark.parametrize(
    "command",
    ['/bin/sleep 1500; echo "deadline reached"', "/usr/bin/gsleep 60", "sleep 1500; /bin/echo x"],
)
def test_a_timer_by_its_full_path_arms_the_deadline(tmp_path, monkeypatch, capsys, command):
    m = Manager(tmp_path)
    _bg(monkeypatch, kind="interactive")
    tasks = [{"id": "b00000001", "type": "shell", "status": "running", "command": "make long"},
             {"id": "b00000002", "type": "shell", "status": "running", "command": command}]
    assert _stop(m, capsys, monkeypatch, background_tasks=tasks) == ""


@pytest.mark.parametrize(
    "command", ["sleep 3600", '/bin/sleep 1500; echo "deadline reached"',
                "cd /tmp/work; perl -e 'alarm 16200; exec @ARGV' ./run.sh pilot > o.log"])
def test_a_timer_or_an_alarmed_run_is_not_told_as_silent(tmp_path, monkeypatch, capsys, command):
    from test_worker_notice import _hook

    m = Manager(tmp_path)
    _silent_command(m, tmp_path, command)
    (tmp_path / "o.log").touch()
    stamp = time.time() - 40 * 60
    os.utime(tmp_path / "o.log", (stamp, stamp))
    assert "COMMAND" not in _hook(monkeypatch, capsys, m)


def test_a_loop_of_sleeps_is_no_timer():
    assert commands.is_timer("while true; do sleep 60; done") is False
    assert commands.is_timer("/bin/sleep 0") is False


# --- 4. BACKGROUND in a session that is itself a background worker -------------------------------


def _others(m: Manager) -> None:
    for short in ("0a0b0c01", "0a0b0c02"):
        m.job(short, state="blocked", needs="awaiting input", mtime_ago=3600, name="not-ours")
        _session(m, short)


def test_a_worker_lists_only_what_it_launched(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _others(m)
    m.launched()
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=60)
    mine = _session(m, SHORT)
    _bg(monkeypatch)
    text = _status(m, capsys, "--local")
    line = next(row for row in text.splitlines() if row.startswith("BACKGROUND"))
    assert "3 sessions on this machine: 3 blocked" in line, line
    assert "this session launched 1" in line and "scan lists the rest" in line, line
    assert mine in _row(text, "BLOCKED") and "not-ours" not in text, text


def test_a_worker_that_launched_none_gets_one_line(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _others(m)
    _bg(monkeypatch)
    text = _status(m, capsys, "--local")
    assert "2 sessions on this machine: 2 blocked" in text and "launched none" in text, text
    assert "not-ours" not in text and "\n  BLOCKED" not in text, text


def test_an_interactive_session_still_sees_the_machine(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _others(m)
    _bg(monkeypatch, kind="interactive")
    text = _status(m, capsys, "--local")
    assert text.count("  BLOCKED") == 2 and "launched" not in text, text


def test_a_workers_json_keeps_every_row_with_its_owner(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _others(m)
    _bg(monkeypatch)
    rows = json.loads(_status(m, capsys, "--json"))["background"]["sessions"]
    assert len(rows) == 2 and all(row["mine"] is False for row in rows)


# --- review round 1 of this commit ---------------------------------------------------------------


TRUE = "perl -e 'alarm 1; exec @ARGV' true"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("perl -e 'exec @ARGV; alarm 1' tail -f /dev/null", id="the alarm after the exec"),
        pytest.param("perl -e 'alarm shift; exec @ARGV' 0 ./run.sh", id="alarm shift of 0"),
        pytest.param(f"S=$(tail -f /dev/null); {TRUE}", id="a wait in a substitution before it"),
        pytest.param(f"{TRUE}; echo `tail -f /dev/null`", id="a wait in backticks after it"),
        pytest.param(f"S=$(ls | wc -l); {TRUE}", id="a substitution of anything else"),
        pytest.param(f"S=$(cat); {TRUE}", id="cat of its stdin in a substitution"),
        pytest.param(f"S=$(cat relative); {TRUE}", id="cat of a file it cannot place"),
        pytest.param(f"{TRUE}; wc -l $UNSET", id="a reader of a variable it cannot read"),
        pytest.param(f"O=$(date); {TRUE} > $O", id="a target from a substitution"),
        pytest.param(f"{TRUE}; awk 'BEGIN{{while(1){{}}}}'", id="awk"),
        pytest.param(f"source ./setup.sh; {TRUE}", id="source"),
        pytest.param(f"{TRUE}; wc -l", id="wc reading its stdin"),
        pytest.param(f"{TRUE}; tail -n 5", id="tail reading its stdin"),
        pytest.param(f"{TRUE}; tail -n +5", id="tail -n +5 reading its stdin"),
        pytest.param(f"{TRUE}; grep x", id="grep reading its stdin"),
        pytest.param(f"{TRUE}; grep -e one -e two", id="grep -e -e"),
        pytest.param(f"find / -name x; {TRUE}", id="find"),
        pytest.param(f"git push; {TRUE}", id="a git that can wait"),
        pytest.param("perl -e 'alarm $t; exec @ARGV' tail -f /dev/null", id="alarm of an unset variable"),
        pytest.param("env python3 -c 'while True: pass' perl -e 'alarm 1; exec @ARGV' true",
                     id="perl only an argument"),
        pytest.param(f"{TRUE}; wc -c /dev/zero", id="a reader of an endless device"),
        pytest.param(f"{TRUE}; rm -i /tmp/existing", id="a prompt"),
        pytest.param(f"{TRUE}; printf x | sed -e :a -e ba", id="sed looping"),
    ],
)
def test_more_of_what_an_alarm_does_not_bound(command):
    assert commands.under_its_own_alarm(command) is False


@pytest.mark.parametrize("arrow", [">", "<"])
def test_a_fifo_blocks_before_the_alarm_exists(tmp_path, arrow):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    command = f"perl -e 'alarm 1; exec @ARGV' true {arrow} {fifo}"
    assert commands.under_its_own_alarm(command, str(tmp_path)) is False
    relative = f"perl -e 'alarm 1; exec @ARGV' true {arrow} fifo"
    assert commands.under_its_own_alarm(relative, str(tmp_path)) is False


def test_a_redirect_is_checked_where_the_cd_put_it(tmp_path):
    (tmp_path / "newdir").mkdir()
    os.mkfifo(tmp_path / "newdir" / "out")
    command = f"cd {tmp_path}/newdir; perl -e 'alarm 1; exec @ARGV' true > out"
    assert commands.under_its_own_alarm(command, str(tmp_path)) is False
    relative = "cd newdir; perl -e 'alarm 1; exec @ARGV' true > out"
    assert commands.under_its_own_alarm(relative, str(tmp_path)) is False
    assert commands.under_its_own_alarm(relative.replace("> out", "> fine"), str(tmp_path)) is True


def test_a_prompt_read_from_a_regular_file_is_quick_and_from_a_fifo_is_not(tmp_path):
    (tmp_path / "prompt").write_text("x", encoding="utf-8")
    os.mkfifo(tmp_path / "fifo")
    command = "perl -e 'alarm 1; exec @ARGV' true \"$(cat {})\""
    assert commands.under_its_own_alarm(command.format(tmp_path / "prompt")) is True
    assert commands.under_its_own_alarm(command.format(tmp_path / "fifo")) is False


def test_a_literal_assignment_is_followed_through_another(tmp_path):
    command = f"D={tmp_path}; O=\"$D/o.log\"; mkdir -p $D; perl -e 'alarm 1; exec @ARGV' true > \"$O\""
    assert commands.under_its_own_alarm(command) is True


def test_a_literal_assignment_is_followed_to_the_fifo(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    command = f"OUT={tmp_path}/fifo; perl -e 'alarm 1; exec @ARGV' true > \"$OUT\""
    assert commands.under_its_own_alarm(command) is False
    assert commands.under_its_own_alarm(command.replace("fifo;", "fine;")) is True


@pytest.mark.parametrize("target", ['"$OUT"', "$OUT", "out*"])
def test_a_redirect_target_it_cannot_read_is_no_bound(tmp_path, target):
    command = f"perl -e 'alarm 1; exec @ARGV' true > {target}"
    assert commands.under_its_own_alarm(command, str(tmp_path)) is False


def test_a_relative_target_with_no_known_directory_is_no_bound():
    assert commands.under_its_own_alarm("perl -e 'alarm 1; exec @ARGV' true > out") is False


def test_a_reader_of_a_fifo_is_no_quick_step(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    command = f"perl -e 'alarm 1; exec @ARGV' true; wc -l {tmp_path}/fifo"
    assert commands.under_its_own_alarm(command) is False


def test_one_of_two_launches_with_no_id_keeps_the_whole_list(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _others(m)
    stdout = f"backgrounded · {SHORT} · second\n"
    m.append(tool_use(None, _ago(60), "toolu_two", "Bash",
                      {"command": "claude --bg -n first x > /tmp/first.log; claude --bg -n second x"}),
             tool_result(None, _ago(59), "toolu_two", stdout, toolUseResult={"stdout": stdout}))
    _bg(monkeypatch)
    assert _status(m, capsys, "--local").count("  BLOCKED") == 2


def test_a_launch_whose_call_aged_out_keeps_the_whole_list(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _others(m)
    m.append(tool_use(None, _ago(120), "toolu_lost", "Bash", {"command": "claude --bg -n a x"}))
    m.append(*[tool_use(None, _ago(100), f"toolu_{n}", "Bash", {"command": "ls"}) for n in range(70)])
    _bg(monkeypatch)
    assert _status(m, capsys, "--local").count("  BLOCKED") == 2


def test_a_background_launch_with_no_id_keeps_the_whole_list(tmp_path, monkeypatch, capsys):
    """Its id is not known, so its row may be any of them."""
    m = Manager(tmp_path)
    _others(m)
    use = tool_use(None, _ago(60), "toolu_bgl", "Bash",
                   {"command": "claude --bg -n child x", "run_in_background": True})
    result = tool_result(None, _ago(60), "toolu_bgl", "Command running in background with ID: b1.",
                         toolUseResult={"backgroundTaskId": "b1"})
    m.append(use, result)
    _bg(monkeypatch)
    text = _status(m, capsys, "--local")
    assert text.count("  BLOCKED") == 2 and "this session launched" not in text, text


@pytest.mark.parametrize(
    "command",
    ["echo agent-postmortem status", "grep -n 'agent-postmortem status' notes.md",
     "printf 'agent-postmortem status\\n'", "uv run echo postmortem status"],
)
def test_a_command_only_naming_status_does_not_count_as_running_it(
        tmp_path, monkeypatch, capsys, command):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A, ago=60)
    _ran(m, command, ago=30)
    _bg(monkeypatch)
    assert "1 subagent finished" in _stop(m, capsys, monkeypatch)


@pytest.mark.parametrize(
    "command",
    ["agent-postmortem status", "cd /tmp && agent-postmortem status --local",
     "uvx agent-postmortem scan", "uv run postmortem status", "/x/bin/agent-postmortem run abc",
     "PYTHONPATH=src uv run python -m agent_postmortem.cli status"],
)
def test_the_ways_of_running_it_count(tmp_path, monkeypatch, capsys, command):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A, ago=60)
    _ran(m, command, ago=30)
    _bg(monkeypatch)
    assert _stop(m, capsys, monkeypatch) == ""


def test_a_status_call_that_was_rejected_ran_nothing(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A, ago=60)
    use = tool_use(None, _ago(30), "toolu_no", "Bash", {"command": "agent-postmortem status"})
    result = tool_result(None, _ago(30), "toolu_no", "denied")
    for block in result["message"]["content"]:
        block["is_error"] = True
    m.append(use, result)
    _bg(monkeypatch)
    assert "1 subagent finished" in _stop(m, capsys, monkeypatch)


def test_a_resumed_workflow_ending_after_status_is_a_new_ending(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)

    def flow(tool: str, ago: float) -> None:
        m.append(tool_use(None, _ago(ago + 5), tool, "Workflow", {"script": "x"}),
                 tool_result(None, _ago(ago), tool, "done",
                             toolUseResult={"runId": "wf_00000000-d30", "status": "completed"}))

    flow("toolu_wf1", 300)
    _ran(m, "agent-postmortem status", ago=200)
    flow("toolu_wf2", 60)
    (m.main.parent / FAKE_UUID / "subagents").mkdir(parents=True)
    _bg(monkeypatch)
    assert "1 workflow finished" in _stop(m, capsys, monkeypatch)


def test_an_interruption_after_status_known_only_from_its_transcript_counts(
        tmp_path, monkeypatch, capsys):
    from live_helpers import user, write_jsonl

    m = Manager(tmp_path)
    tool = f"toolu_{AGENT_A}"
    m.append(tool_use(None, _ago(300), tool, "Agent",
                      {"prompt": "x", "subagent_type": "g", "run_in_background": True}),
             tool_result(None, _ago(300), tool, "started",
                         toolUseResult={"agentId": AGENT_A, "status": "async_launched"}))
    _ran(m, "agent-postmortem status", ago=200)
    sub = m.main.parent / FAKE_UUID / "subagents"
    sub.mkdir(parents=True)
    write_jsonl(sub / f"agent-{AGENT_A}.jsonl", [
        user(AGENT_A, _ago(290), "work"), user(AGENT_A, _ago(100), "[Request interrupted by user]")])
    _bg(monkeypatch)
    assert "1 subagent finished" in _stop(m, capsys, monkeypatch)


def test_a_subagent_interrupted_with_no_notice_still_counts(tmp_path, monkeypatch, capsys):
    from live_helpers import write_jsonl

    m = Manager(tmp_path)
    tool = f"toolu_{AGENT_A}"
    m.append(tool_use(None, _ago(120), tool, "Agent",
                      {"prompt": "x", "subagent_type": "g", "run_in_background": True}),
             tool_result(None, _ago(120), tool, "started",
                         toolUseResult={"agentId": AGENT_A, "status": "async_launched"}))
    sub = m.main.parent / FAKE_UUID / "subagents"
    sub.mkdir(parents=True)
    from live_helpers import user

    write_jsonl(sub / f"agent-{AGENT_A}.jsonl", [
        user(AGENT_A, _ago(100), "work"), user(AGENT_A, _ago(90), "[Request interrupted by user]")])
    _bg(monkeypatch)
    assert "1 subagent finished" in _stop(m, capsys, monkeypatch)


# --- review round 3 of this change ---------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("perl -e 'alarm 1; exec @ARGV' -n", id="perl's input loop before the alarm"),
        pytest.param("perl -e 'alarm 1; exec @ARGV' -p ./x", id="-p"),
        pytest.param("perl -e 'alarm 4294967296; exec @ARGV' tail -f /dev/null", id="overflows to 0"),
        pytest.param("perl -e 'alarm shift; exec @ARGV' 4294967296 tail -f /dev/null",
                     id="the shift form overflowing"),
    ],
)
def test_round_three_alarms_that_are_no_bound(command):
    assert commands.under_its_own_alarm(command) is False


def test_a_failed_cd_leaves_the_old_directory_so_both_must_be_safe(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    run = "perl -e 'alarm 1; exec @ARGV' true"
    assert commands.under_its_own_alarm(f"cd /definitely-absent; {run}; wc -c fifo",
                                        str(tmp_path)) is False
    # `&&` skips only its own list: after the `;` it runs in the old directory (0.5.0 review)
    assert commands.under_its_own_alarm(f"cd /definitely-absent && {run}; wc -c fifo",
                                        str(tmp_path)) is False
    assert commands.under_its_own_alarm(f"cd /tmp; {run}; wc -c fine", str(tmp_path)) is True


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_does_not_count_as_having_looked(tmp_path, monkeypatch, capsys, flag):
    m = Manager(tmp_path)
    _subagent(m, AGENT_A, ago=60)
    _ran(m, f"postmortem status {flag}", ago=30)
    _bg(monkeypatch)
    assert "1 subagent finished" in _stop(m, capsys, monkeypatch)


# --- field report 2026-11-05: a timer written in Python ------------------------------------------


PY_TIMER = "python3 -c 'import time; time.sleep(1800)'"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(PY_TIMER, id="alone"),
        pytest.param(f'{PY_TIMER}; echo "deadline reached"', id="then a word"),
        pytest.param(f"{PY_TIMER} && true", id="and-joined"),
        pytest.param("/usr/bin/python3 -c 'import time; time.sleep(60)'", id="by its path"),
        pytest.param("python -c 'import time;time.sleep(1.5)'", id="python, no spaces, a fraction"),
    ],
)
def test_a_timer_written_in_python_is_a_timer(command):
    assert commands.is_timer(command) is True
    assert commands.quiet_by_design(command) is True


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("python3 -c 'import time; time.sleep(0)'", id="no wait at all"),
        pytest.param("python3 -c 'import time; time.sleep(1800); run()'", id="more after the sleep"),
        pytest.param("python3 -c 'import time; run(); time.sleep(1)'", id="more before the sleep"),
        pytest.param("python3 -c 'while True: pass'", id="a loop"),
        pytest.param("python3 -c 'import time; time.sleep(float(\"inf\"))'", id="forever"),
        pytest.param("python3 -c 'import time; time.sleep(1e9)'", id="not a plain number"),
        pytest.param("python3 script.py", id="a script"),
        pytest.param(f"{PY_TIMER}; make long", id="a real command after it"),
        pytest.param(f"{PY_TIMER} > /tmp/fifo", id="a redirect"),
        pytest.param(f'{PY_TIMER}; echo "$(tail -f x)"', id="a substitution after it"),
        pytest.param("ruby -c 'import time; time.sleep(1)'", id="another program"),
        pytest.param("python3 -m 'import time; time.sleep(1)'", id="not -c"),
    ],
)
def test_what_is_no_timer_in_python(command):
    assert commands.is_timer(command) is False


def test_a_python_timer_arms_the_deadline_it_was_asked_for(tmp_path, monkeypatch, capsys):
    """The field report: the request asked for a timer, and then asked about that timer."""
    m = Manager(tmp_path)
    _bg(monkeypatch, kind="interactive")
    tasks = [{"id": "b00000001", "type": "shell", "status": "running", "command": "make long"},
             {"id": "b00000002", "type": "shell", "status": "running",
              "command": f'{PY_TIMER}; echo "deadline reached"'}]
    assert _stop(m, capsys, monkeypatch, background_tasks=tasks) == ""
