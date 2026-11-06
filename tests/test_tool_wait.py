"""A worker waiting on one tool call that never returns: a worker stuck on a dialog or a prompt a
tool opened looks idle with no error, and its only sign is the last tool call never returning.
Measured on a real worker, 2.1.318: the job
reads `working / active` with no `needs`. A Bash call is bounded by Claude Code, which moves it to the
background at its timeout, so this is mostly an MCP tool's case.

On one machine over 14 days, of 33,453 calls in background sessions 26 ran past 15 minutes and 1
past 30, that one across a restart of the session, so the mark is 30 minutes, the same as a silent
command's. Synthetic data only: the fake session id,
paths under tmp_path.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from live_helpers import assistant_text, tool_result, tool_use, write_jsonl
from test_worker_notice import SHORT, Manager, _ago, _hook

from agent_postmortem import hook_gate, stalls, state, status, workers

WORKER_SESSION = f"{SHORT}-0000-4000-8000-000000000000"


def _transcript(m: Manager, entries: list[dict], ago: float) -> None:
    path = m.main.parent / f"{WORKER_SESSION}.jsonl"
    write_jsonl(path, entries)
    stamp = time.time() - ago
    os.utime(path, (stamp, stamp))


def _stuck_on(m: Manager, name: str = "Bash", ago: float = 2000.0, tool_input: dict | None = None,
              job_ago: float | None = None) -> None:
    _transcript(m, [tool_use(None, _ago(ago), "toolu_stuck", name, tool_input or {"command": "x"})], ago)
    m.job(state="working", tempo="active", mtime_ago=ago if job_ago is None else job_ago,
          created_ago=7200)


def _clock_is_up(m: Manager) -> None:
    gate = hook_gate._gate_path(str(m.main))
    data = json.loads(gate.read_bytes())
    data["recheck_at"] = time.time() - 1
    state.write_private_bytes(gate, json.dumps(data).encode())


def _live(monkeypatch, kind: str = "bg") -> None:
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {WORKER_SESSION: kind})


def _told(m: Manager, monkeypatch, capsys) -> str:
    _clock_is_up(m)
    return _hook(monkeypatch, capsys, m)


def _manager(tmp_path, monkeypatch, capsys) -> Manager:
    m = Manager(tmp_path)
    m.launched(ago=7200)
    assert _hook(monkeypatch, capsys, m) == ""
    _live(monkeypatch)
    return m


# --- the reading -----------------------------------------------------------------------------------


def _open(tmp_path, entries, ago=2000.0, now=None):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, entries)
    stamp = time.time() - ago
    os.utime(path, (stamp, stamp))
    return stalls.open_tool_call(path, now or time.time())


def test_a_call_with_no_result_past_the_mark_is_read(tmp_path):
    found = _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "Bash", {"command": "x"})])
    assert found is not None and found["tool"] == "Bash" and 1990 <= found["seconds"] <= 2010


def test_a_call_with_a_result_is_not(tmp_path):
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "Bash", {"command": "x"}),
                            tool_result(None, _ago(2000), "toolu_1", "done")]) is None


def test_a_call_under_the_mark_is_not(tmp_path):
    assert _open(tmp_path, [tool_use(None, _ago(1200), "toolu_1", "Bash", {"command": "x"})],
                 ago=1200) is None


def test_a_transcript_still_being_written_is_not(tmp_path):
    """Another call in the same turn answered a moment ago: the session is moving."""
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "Bash", {"command": "x"})],
                 ago=10) is None


@pytest.mark.parametrize("name", ["Agent", "Task", "Workflow", "AskUserQuestion", "Monitor",
                                  "ExitPlanMode", "EnterPlanMode"])
def test_a_tool_that_is_long_by_nature_is_not(tmp_path, name):
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", name, {})]) is None


def test_a_background_launch_is_not(tmp_path):
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "Bash",
                                     {"command": "x", "run_in_background": True})]) is None


def test_an_older_call_answered_and_a_later_one_open(tmp_path):
    found = _open(tmp_path, [tool_use(None, _ago(3000), "toolu_1", "Read", {}),
                             tool_result(None, _ago(2900), "toolu_1", "done"),
                             tool_use(None, _ago(2000), "toolu_2", "WebFetch", {})])
    assert found is not None and found["tool"] == "WebFetch"


def test_a_turn_that_ended_after_the_call_is_not_waiting_on_it(tmp_path):
    """A call left open and the session then wrote on: what is last is not the call."""
    assert _open(tmp_path, [tool_use(None, _ago(3000), "toolu_1", "Bash", {"command": "x"}),
                            assistant_text(None, _ago(2000), "moving on")]) is None


@pytest.mark.parametrize(
    "name,label",
    [("mcp__zz-private-server__click", "MCP"), ("Bash", "Bash"), ("not a plain name!", "tool"),
     ("x" * 80, "tool")],
)
def test_the_tool_is_named_only_in_words_that_say_nothing_private(tmp_path, name, label):
    found = _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", name, {})])
    assert found is not None and found["tool"] == label


def test_no_file_or_a_broken_tail_reads_as_nothing(tmp_path):
    assert stalls.open_tool_call(tmp_path / "absent.jsonl", time.time()) is None
    path = tmp_path / "broken.jsonl"
    path.write_bytes(b"\xff\xfe not json\n{ also not\n")
    stamp = time.time() - 2000
    os.utime(path, (stamp, stamp))
    assert stalls.open_tool_call(path, time.time()) is None


def test_a_very_long_transcript_is_read_only_at_its_end(tmp_path):
    entries = [assistant_text(None, _ago(9000), "x" * 1000) for _ in range(600)]
    found = _open(tmp_path, [*entries, tool_use(None, _ago(2000), "toolu_1", "Bash", {"command": "x"})])
    assert found is not None and found["tool"] == "Bash"


# --- the manager is told ---------------------------------------------------------------------------


def test_the_manager_is_told_once(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m, "mcp__zz-private-server__click", ago=2000)
    text = _told(m, monkeypatch, capsys)
    assert text.startswith(f"WORKER night-run ({SHORT}) has had no result from one MCP call for 33m"), text
    assert "may be stuck on something outside Claude Code" in text and "or just slow" in text, text
    assert f"claude attach {SHORT}" in text and "zz-private" not in text, text
    assert _told(m, monkeypatch, capsys) == ""


def test_the_command_it_waits_on_is_never_printed(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m, "Bash", tool_input={"command": "zz-private-command --secret"})
    assert "zz-private" not in _told(m, monkeypatch, capsys)


def test_another_stuck_call_is_told_again(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m)
    assert "no result from one Bash call" in _told(m, monkeypatch, capsys)
    _transcript(m, [tool_use(None, _ago(3000), "toolu_stuck", "Bash", {"command": "x"}),
                    tool_result(None, _ago(2200), "toolu_stuck", "done"),
                    tool_use(None, _ago(2000), "toolu_next", "WebFetch", {})], 2000)
    assert "no result from one WebFetch call" in _told(m, monkeypatch, capsys)


def test_a_call_that_ended_says_nothing_and_a_later_stick_speaks(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m)
    assert "no result" in _told(m, monkeypatch, capsys)
    _transcript(m, [tool_use(None, _ago(3000), "toolu_stuck", "Bash", {"command": "x"}),
                    tool_result(None, _ago(2000), "toolu_stuck", "done")], 2000)
    assert _told(m, monkeypatch, capsys) == ""


def test_under_the_mark_nothing_is_told_and_the_clock_is_set(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m, ago=1200)
    assert _told(m, monkeypatch, capsys) == ""
    gate = json.loads(hook_gate._gate_path(str(m.main)).read_bytes())
    assert gate["recheck_at"] < time.time() + 310, "it keeps looking, every 5 minutes"


def test_a_worker_that_is_blocked_or_done_is_not_read_for_it(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m)
    m.job(state="done", tempo="idle", output={"result": "r"}, mtime_ago=2000, created_ago=7200)
    text = _told(m, monkeypatch, capsys)
    assert "no result" not in text and "finished" in text, text


# --- status and scan -------------------------------------------------------------------------------


def _background(m: Manager, monkeypatch) -> dict:
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {WORKER_SESSION: "bg"})
    return status._background(m.jobs, m.main.parent.parent, time.time(), max_reads=None)


def test_status_lists_it_with_the_tool(tmp_path, monkeypatch, capsys):
    from agent_postmortem.redact import Redactor

    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m, "mcp__zz-private-server__click")
    found = _background(m, monkeypatch)
    assert found["counts"].get("waiting") == 1 and "working" not in found["counts"], found["counts"]
    (row,) = [entry for entry in found["sessions"] if entry["state"] == "waiting"]
    assert row["class"] == workers.TOOL_WAIT and row["label"] == "MCP", row
    assert workers.reason_words(row["class"], row["label"]) == "no result from one MCP call"
    assert "zz-private" not in json.dumps(row) and Redactor is not None


def test_a_long_build_under_the_mark_stays_working(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m, ago=1200)
    found = _background(m, monkeypatch)
    assert found["counts"].get("working") == 1 and "waiting" not in found["counts"], found["counts"]


def test_an_interactive_session_is_not_read_for_it(tmp_path, monkeypatch, capsys):
    """A person is there, and may have stepped away from a long call on purpose."""
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m)
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {WORKER_SESSION: "interactive"})
    found = status._background(m.jobs, m.main.parent.parent, time.time(), max_reads=None)
    assert "waiting" not in found["counts"], found["counts"]


# --- a slow build is not a stuck call --------------------------------------------------------------


def test_a_bash_call_inside_its_own_timeout_is_not_waiting(tmp_path):
    """Claude Code moves a Bash call to the background at its timeout, so until then nothing is odd."""
    call = {"command": "make", "timeout": 3_600_000}
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "Bash", call)]) is None


def test_a_bash_call_past_its_own_timeout_is(tmp_path):
    call = {"command": "make", "timeout": 3_600_000}
    found = _open(tmp_path, [tool_use(None, _ago(3700), "toolu_1", "Bash", call)], ago=3700)
    assert found is not None and found["tool"] == "Bash"


@pytest.mark.parametrize("timeout", ["3600000", True, None, -5, [1]])
def test_a_timeout_it_cannot_read_leaves_the_plain_mark(tmp_path, timeout):
    call = {"command": "make", "timeout": timeout}
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "Bash", call)]) is not None


def test_another_tools_timeout_field_says_nothing(tmp_path):
    """An MCP tool's input is the tool's own, and is never read: only Bash's own field means this."""
    call = {"timeout": 3_600_000}
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "mcp__x__y", call)]) is not None


def test_of_two_open_calls_the_one_past_its_mark_is_told(tmp_path):
    entries = [tool_use(None, _ago(2500), "toolu_1", "Bash", {"command": "m", "timeout": 3_600_000}),
               tool_use(None, _ago(2400), "toolu_2", "WebFetch", {})]
    found = _open(tmp_path, entries, ago=2400)
    assert found is not None and found["tool"] == "WebFetch"


# --- what a stuck MCP call really becomes (a real worker, 2.1.319) ---------------------------------
#
# After 120 s Claude Code moves it to the background as a task and tells the worker it keeps running.
# The worker's turn ends and its job reads `working / idle` with `inFlight.kinds: ["mcp_task"]`, with
# the job file unchanged from then on. Only that task's end would wake the worker.

ON_MCP_TASK = {"state": "working", "tempo": "idle",
               "inFlight": {"tasks": 1, "queued": 0, "kinds": ["mcp_task"], "drainableMonitors": 0}}


@pytest.mark.parametrize(
    "data,expected",
    [
        pytest.param(ON_MCP_TASK, True, id="the real shape"),
        pytest.param({**ON_MCP_TASK, "tempo": "active"}, False, id="its turn is still running"),
        pytest.param({**ON_MCP_TASK, "state": "done"}, False, id="it finished"),
        pytest.param({**ON_MCP_TASK, "tempo": "blocked"}, False, id="blocked on something else"),
        pytest.param({**ON_MCP_TASK, "inFlight": {"tasks": 1, "kinds": ["session_cron"]}}, False,
                     id="its own cron will wake it"),
        pytest.param({**ON_MCP_TASK, "inFlight": {"tasks": 2, "kinds": ["mcp_task", "session_cron"]}},
                     False, id="a cron will wake it anyway"),
        pytest.param({**ON_MCP_TASK, "inFlight": {"tasks": 0, "kinds": []}}, False, id="nothing out"),
        pytest.param({**ON_MCP_TASK, "inFlight": {"tasks": 1, "kinds": "mcp_task"}}, False, id="odd kinds"),
        pytest.param({**ON_MCP_TASK, "inFlight": None}, False, id="no inFlight"),
    ],
)
def test_which_job_waits_on_a_background_mcp_task(data, expected):
    assert workers.idle_on_mcp_task(data) is expected


def test_the_manager_is_told_of_a_worker_idle_on_an_mcp_task(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    _transcript(m, [assistant_text(None, _ago(2000), "It keeps running in the background.")], 2000)
    m.job(mtime_ago=2000, created_ago=7200, **ON_MCP_TASK)
    text = _told(m, monkeypatch, capsys)
    assert f"({SHORT}) has had no result from one MCP call for 33m" in text, text
    assert _told(m, monkeypatch, capsys) == ""


def test_an_mcp_task_is_told_long_before_claude_code_ends_it(tmp_path, monkeypatch, capsys):
    """It kills the task at 1800 s and wakes the worker, so a 30 minute line would come too late."""
    m = _manager(tmp_path, monkeypatch, capsys)
    _transcript(m, [assistant_text(None, _ago(700), "It keeps running.")], 700)
    m.job(mtime_ago=700, created_ago=7200, **ON_MCP_TASK)
    assert "no result from one MCP call for 11m" in _told(m, monkeypatch, capsys)


def test_under_the_mark_an_mcp_task_says_nothing(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    m.job(mtime_ago=300, created_ago=7200, **ON_MCP_TASK)
    assert _told(m, monkeypatch, capsys) == ""


def test_any_other_working_job_past_ten_minutes_says_nothing_and_reads_no_stall(
        tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _transcript(m, [assistant_text(None, _ago(700), "Carrying on.")], 700)
    m.job(state="working", tempo="idle", mtime_ago=700, created_ago=7200)

    def no_read(*args, **kwargs):
        raise AssertionError("too soon for the STALLED reading")

    monkeypatch.setattr(status, "_job_stall", no_read)
    assert _told(m, monkeypatch, capsys) == ""


def test_status_lists_a_worker_idle_on_an_mcp_task(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _transcript(m, [assistant_text(None, _ago(2000), "It keeps running.")], 2000)
    m.job(mtime_ago=2000, created_ago=7200, **ON_MCP_TASK)
    found = _background(m, monkeypatch)
    assert found["counts"].get("waiting") == 1, found["counts"]
    (row,) = [entry for entry in found["sessions"] if entry["state"] == "waiting"]
    assert row["label"] == "MCP" and 1990 <= row["age_seconds"] <= 2010, row


def test_a_task_that_ended_says_nothing_more(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    _transcript(m, [assistant_text(None, _ago(2000), "It keeps running.")], 2000)
    m.job(mtime_ago=2000, created_ago=7200, **ON_MCP_TASK)
    assert "no result" in _told(m, monkeypatch, capsys)
    m.job(state="working", tempo="active", mtime_ago=1, created_ago=7200)
    assert _told(m, monkeypatch, capsys) == ""


# --- which block ends a wait, and which session is told ----------------------------------------------


def test_an_mcp_input_saying_background_means_nothing(tmp_path):
    """Only Bash's own field means that: an MCP tool's input is its own."""
    assert _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "mcp__x__y",
                                     {"run_in_background": True})]) is not None


def test_a_thinking_block_of_the_same_message_keeps_a_parallel_call(tmp_path):
    first = tool_use(None, _ago(2000), "toolu_a", "WebFetch", {})
    thought = assistant_text(None, _ago(2000), "thinking")
    second = tool_use(None, _ago(2000), "toolu_b", "WebFetch", {})
    for entry in (first, thought, second):
        entry["message"]["id"] = "msg_same"
    found = _open(tmp_path, [first, thought, second, tool_result(None, _ago(2000), "toolu_b", "ok")])
    assert found is not None and found["tool"] == "WebFetch"


def test_a_real_prompt_after_a_call_is_the_session_moving_on(tmp_path):
    from live_helpers import user

    assert _open(tmp_path, [tool_use(None, _ago(3000), "toolu_1", "Bash", {"command": "x"}),
                            user(None, _ago(2000), "carry on")]) is None


def test_an_interactive_session_is_not_told_by_the_hook_either(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m)
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {WORKER_SESSION: "interactive"})
    assert _told(m, monkeypatch, capsys) == ""


def test_a_session_with_no_live_process_is_not_told(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _stuck_on(m)
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {})
    assert _told(m, monkeypatch, capsys) == ""


def test_scan_says_waiting_not_blocked(tmp_path):
    from agent_postmortem import cli

    entry = {"session": "s_0000000001", "state": "waiting", "age_seconds": 2000,
             "class": workers.TOOL_WAIT, "label": "MCP", "short": SHORT, "mine": True}
    candidates: list[dict] = []
    cli._fold_blocked([entry], candidates, [], set(), "local")
    assert candidates[0]["verdict"] == "WAITING", candidates
    assert candidates[0]["reason"] == "no result from one MCP call", candidates


# --- a line it cannot read, and the idle manager ------------------------------------------------------


_OPEN = tool_use(None, _ago(2000), "toolu_open", "Bash", {"command": "x"})
_OPEN["message"]["id"] = "msg_open"  # the real shape, which is what let such a line raise


@pytest.mark.parametrize(
    "line",
    [
        pytest.param({"type": "user", "message": "not an object"}, id="a user message that is text"),
        pytest.param({"type": "assistant", "message": None}, id="an assistant message of null"),
        pytest.param({"type": "user"}, id="no message at all"),
    ],
)
def test_a_line_with_no_message_object_does_not_stop_the_read(tmp_path, line):
    """A hook that raises tells nothing on that call, about any worker. Such a line says nothing."""
    found = _open(tmp_path, [_OPEN, line])
    assert found is not None and found["tool"] == "Bash", found


@pytest.mark.parametrize("answer", [["toolu_open"], {"id": "toolu_open"}, None, 7])
def test_a_result_whose_call_id_is_not_text_answers_nothing(tmp_path, answer):
    line = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": answer}]}}
    found = _open(tmp_path, [_OPEN, line])
    assert found is not None and found["tool"] == "Bash", found


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00", "9999-12-31T23:59:59", 12, None])
def test_a_time_it_cannot_use_never_stops_the_read(tmp_path, stamp):
    line = tool_use(None, _ago(2000), "toolu_open", "Bash", {"command": "x"})
    line["timestamp"] = stamp
    _open(tmp_path, [line])  # told or not, it returns


def test_an_idle_manager_is_woken_for_a_worker_on_a_call_with_no_result(tmp_path, monkeypatch, capsys):
    from test_stop_watch import Life, _bg_session
    from test_stop_watch import _manager as _idle_manager

    m = _idle_manager(tmp_path, monkeypatch, capsys)
    _bg_session(monkeypatch)
    _live(monkeypatch)
    _stuck_on(m, "mcp__zz-private-server__click", ago=2000)
    assert Life(m).run() == 2
    err = capsys.readouterr().err
    assert f"({SHORT}) has had no result from one MCP call for" in err, err
    assert "zz-private" not in err, err


def test_an_idle_manager_is_woken_for_a_worker_idle_on_an_mcp_task(tmp_path, monkeypatch, capsys):
    from test_stop_watch import Life, _bg_session
    from test_stop_watch import _manager as _idle_manager

    m = _idle_manager(tmp_path, monkeypatch, capsys)
    _bg_session(monkeypatch)
    _live(monkeypatch)
    _transcript(m, [assistant_text(None, _ago(700), "It keeps running in the background.")], 700)
    m.job(mtime_ago=700, created_ago=7200, **ON_MCP_TASK)
    assert Life(m).run() == 2
    assert f"({SHORT}) has had no result from one MCP call for 11m" in capsys.readouterr().err


# --- one background MCP task from the next -----------------------------------------------------------


def _moved(call_id: str, ago: float) -> list[dict]:
    """What a real worker's transcript holds for an MCP call moved to the background (2.1.319)."""
    return [tool_use(None, _ago(ago), call_id, "mcp__zz-private-server__click", {}),
            tool_result(None, _ago(ago - 120), call_id, "It was moved to the background as a task."),
            assistant_text(None, _ago(ago - 122), "I will wait for it.")]


def test_a_second_stuck_task_is_told_though_no_check_saw_the_first_end(tmp_path, monkeypatch, capsys):
    """The job file holds only the kinds, so two stuck tasks must not read as one: the manager was
    busy on one long call while the first ended and the second stuck."""
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    first = _moved("toolu_first", 2000)
    _transcript(m, first, 1878)
    m.job(mtime_ago=1878, created_ago=7200, **ON_MCP_TASK)
    assert "no result" in _told(m, monkeypatch, capsys)
    _transcript(m, [*first, *_moved("toolu_second", 900)], 778)
    m.job(mtime_ago=778, created_ago=7200, **ON_MCP_TASK)
    text = _told(m, monkeypatch, capsys)
    assert "no result from one MCP call for 12m" in text and "zz-private" not in text, text


def test_a_touch_of_the_job_file_is_not_a_second_task(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    _transcript(m, _moved("toolu_first", 2000), 1878)
    m.job(mtime_ago=1878, created_ago=7200, **ON_MCP_TASK)
    assert "no result" in _told(m, monkeypatch, capsys)
    m.job(mtime_ago=700, created_ago=7200, **ON_MCP_TASK)
    assert _told(m, monkeypatch, capsys) == ""


def test_an_mcp_task_with_its_call_out_of_reach_is_still_told(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    _transcript(m, [assistant_text(None, _ago(2000), "x" * (stalls._TOOL_TAIL_BYTES + 10))], 2000)
    m.job(mtime_ago=2000, created_ago=7200, **ON_MCP_TASK)
    assert "no result from one MCP call" in _told(m, monkeypatch, capsys)


# --- private names, a FIFO, and one call told once -----------------------------------------------------


def test_a_tool_name_this_does_not_know_is_not_printed(tmp_path):
    """Any name not Claude Code's own may be the user's, however plain it looks."""
    found = _open(tmp_path, [tool_use(None, _ago(2000), "toolu_1", "PrivateClientLookup", {})])
    assert found is not None and found["tool"] == "tool", found


def test_a_transcript_that_is_a_fifo_never_blocks_the_read(tmp_path):
    """Opening a FIFO with no writer waits for ever, whatever the byte limit says."""
    import threading

    path = tmp_path / "pipe.jsonl"
    os.mkfifo(path)
    stamp = time.time() - 2000
    os.utime(path, (stamp, stamp))
    results: list[object] = []
    for reader in (lambda: stalls.open_tool_call(path, time.time()),
                   lambda: stalls.newest_mcp_call(path)):
        thread = threading.Thread(target=lambda r=reader: results.append(r()), daemon=True)
        thread.start()
        thread.join(5)
        if thread.is_alive():  # free the blocked open, or the interpreter waits on it at exit
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            pytest.fail("the read blocked on a FIFO")
    assert results == [None, None]


def test_a_touch_of_the_job_file_now_does_not_tell_the_same_call_again(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    _transcript(m, _moved("toolu_first", 2000), 1878)
    m.job(mtime_ago=1878, created_ago=7200, **ON_MCP_TASK)
    assert "no result" in _told(m, monkeypatch, capsys)
    m.job(mtime_ago=1, created_ago=7200, **ON_MCP_TASK)  # no longer past the mark
    assert _told(m, monkeypatch, capsys) == ""
    m.job(mtime_ago=700, created_ago=7200, **ON_MCP_TASK)
    assert _told(m, monkeypatch, capsys) == ""


def test_a_turn_between_two_looks_at_one_call_does_not_tell_it_again(tmp_path, monkeypatch, capsys):
    """A person or a manager writes to the worker while the call stays out."""
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    _stuck_on(m, "WebFetch", ago=2000, job_ago=2000)
    assert "no result from one WebFetch call" in _told(m, monkeypatch, capsys)
    m.job(state="working", tempo="active", mtime_ago=1, created_ago=7200)
    assert _told(m, monkeypatch, capsys) == ""
    m.job(state="working", tempo="active", mtime_ago=2000, created_ago=7200)
    assert _told(m, monkeypatch, capsys) == ""


def test_after_a_touch_a_new_stuck_call_is_still_told(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    first = _moved("toolu_first", 2000)
    _transcript(m, first, 1878)
    m.job(mtime_ago=1878, created_ago=7200, **ON_MCP_TASK)
    assert "no result" in _told(m, monkeypatch, capsys)
    m.job(mtime_ago=1, created_ago=7200, **ON_MCP_TASK)
    assert _told(m, monkeypatch, capsys) == ""
    _transcript(m, [*first, *_moved("toolu_second", 900)], 778)
    m.job(mtime_ago=778, created_ago=7200, **ON_MCP_TASK)
    assert "no result from one MCP call" in _told(m, monkeypatch, capsys)


def test_a_finished_call_between_does_not_read_as_a_new_stuck_task(tmp_path, monkeypatch, capsys):
    """The task told of stays out while the worker makes an MCP call that returns."""
    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    first = _moved("toolu_first", 2000)
    _transcript(m, first, 1878)
    m.job(mtime_ago=1878, created_ago=7200, **ON_MCP_TASK)
    assert "no result" in _told(m, monkeypatch, capsys)
    answered = [tool_use(None, _ago(900), "toolu_quick", "mcp__zz-private-server__look", {}),
                tool_result(None, _ago(899), "toolu_quick", "ok"),
                assistant_text(None, _ago(898), "Still waiting on the other one.")]
    _transcript(m, [*first, *answered], 898)
    m.job(mtime_ago=898, created_ago=7200, **ON_MCP_TASK)
    assert _told(m, monkeypatch, capsys) == ""


def test_with_no_word_of_a_move_the_newest_mcp_call_stands_in(tmp_path):
    """A later Claude Code may word the move otherwise: tasks still tell apart, if less exactly."""
    path = tmp_path / "s.jsonl"
    write_jsonl(path, [tool_use(None, _ago(900), "toolu_a", "mcp__s__t", {}),
                       tool_result(None, _ago(800), "toolu_a", "something else entirely")])
    first = stalls.newest_mcp_call(path)
    write_jsonl(path, [tool_use(None, _ago(900), "toolu_a", "mcp__s__t", {}),
                       tool_result(None, _ago(800), "toolu_a", "something else entirely"),
                       tool_use(None, _ago(700), "toolu_b", "mcp__s__t", {}),
                       tool_result(None, _ago(600), "toolu_b", "something else entirely")])
    assert first is not None and stalls.newest_mcp_call(path) not in (None, first)


@pytest.mark.parametrize("content", [
    "MCP tool \"s/t\" is still running after 120s. It was moved to the background as task k1.",
    [{"type": "text", "text": "MCP tool \"s/t\" is still running after 120s. It was moved to the "
                              "background as task k1."}],
], ids=["text", "blocks"])
def test_the_real_words_of_a_move_are_read_in_either_shape(tmp_path, content):
    path = tmp_path / "s.jsonl"
    moved = [tool_use(None, _ago(900), "toolu_a", "mcp__s__t", {}),
             tool_result(None, _ago(780), "toolu_a", content)]
    write_jsonl(path, moved)
    alone = stalls.newest_mcp_call(path)
    write_jsonl(path, [*moved, tool_use(None, _ago(500), "toolu_b", "mcp__s__t", {}),
                       tool_result(None, _ago(499), "toolu_b", "ok")])
    assert stalls.newest_mcp_call(path) == alone


# --- odd result ids, and a transcript that is no regular file ------------------------------------------


@pytest.mark.parametrize("answer", [["toolu_a"], {"id": "toolu_a"}, None, 7])
def test_newest_mcp_call_is_not_stopped_by_a_result_id_that_is_not_text(tmp_path, answer):
    path = tmp_path / "s.jsonl"
    write_jsonl(path, [tool_use(None, _ago(900), "toolu_a", "mcp__s__t", {}),
                       {"type": "user", "message": {"content": [
                           {"type": "tool_result", "tool_use_id": answer,
                            "content": "It was moved to the background as task k1."}]}}])
    assert stalls.newest_mcp_call(path) is not None


def test_a_transcript_that_is_no_regular_file_is_not_a_job_transcript(tmp_path):
    """A FIFO would hold every later reader's open for ever, this one's and the stall rule's alike."""
    project = tmp_path / "p"
    project.mkdir()
    os.mkfifo(project / f"{WORKER_SESSION}.jsonl")
    assert status._job_transcript(tmp_path, WORKER_SESSION) is None
    (project / "a-regular-one.jsonl").write_text("{}\n")
    assert status._job_transcript(tmp_path, "a-regular-one") == project / "a-regular-one.jsonl"
    (project / "a-dangling-link.jsonl").symlink_to(project / "gone")
    assert status._job_transcript(tmp_path, "a-dangling-link") is None


def test_a_worker_with_a_fifo_for_a_transcript_says_nothing(tmp_path, monkeypatch, capsys):
    """The whole hook, past both marks: run.sh would kill it at 3 s with nothing saved."""
    import threading

    m = _manager(tmp_path, monkeypatch, capsys)
    _live(monkeypatch)
    path = m.main.parent / f"{WORKER_SESSION}.jsonl"
    path.unlink(missing_ok=True)
    os.mkfifo(path)
    stamp = time.time() - 4000
    os.utime(path, (stamp, stamp))
    m.job(state="working", tempo="active", mtime_ago=4000, created_ago=7200)
    finished: list[str] = []
    thread = threading.Thread(target=lambda: finished.append(_told(m, monkeypatch, capsys)), daemon=True)
    thread.start()
    thread.join(10)
    if thread.is_alive():  # free the blocked open, or the interpreter waits on it at exit
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("the hook blocked on a FIFO")
    assert finished == [""], finished
