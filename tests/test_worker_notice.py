"""The plugin tells the manager about its workers (Zeyu's direction, field reports 2026-11-04;
docs/DESIGN.md 4.14.5).

Claude Code records a background session's state in its job file and tells nobody: a worker sat
8.5 hours on a permission prompt while the session that launched it heard nothing. The test that
decides this feature: a manager session that does nothing special is told, within one tool call,
that its worker waits on "approve ...", and a worker that delivered is not reported as a problem.

These tests run the real gate (`hooks_cli.dispatch("post-tool")`) on the wall clock. Synthetic
data only: the fake session id, paths under tmp_path, invented worker names.
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from live_helpers import FAKE_UUID, tool_result, tool_use, user, write_jsonl
from test_live_session import SLUG

from agent_postmortem import hook_gate, hooks_cli, live, state, workers

SHORT = "a1b2c3d4"
LAUNCH = 'cd ~/work && claude --bg -n night-run --permission-mode auto "$(cat brief.md)"'
PERMISSION = "approve Entering worktree"


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stdout(short: str, name: str = "night-run") -> str:
    return (f"backgrounded · {short} · {name}\n"
            f"  claude attach {short}    open in this terminal\n"
            f"  claude logs {short}      show recent output\n"
            f"  claude stop {short}      end this session\n")


class Manager:
    """A manager session with a background worker it launched, and that worker's job file."""

    def __init__(self, tmp_path, launched_ago: float = 120.0):
        self.main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
        self.jobs = tmp_path / "jobs"
        self.entries = [user(None, _ago(launched_ago + 60), "start")]
        write_jsonl(self.main, self.entries)
        self.calls = 0

    def append(self, *entries) -> None:
        self.entries.extend(entries)
        write_jsonl(self.main, self.entries)

    def launched(self, short: str = SHORT, *, name: str = "night-run", ago: float = 120.0,
                 command: str = LAUNCH, stdout: str | None = None, job: dict | None = None,
                 created_ago: float | None = None) -> None:
        """The launch as the transcript holds it, and the worker's job file."""
        self.calls += 1
        tool = f"toolu_bg{self.calls}"
        stdout = _stdout(short, name) if stdout is None else stdout
        self.append(tool_use(None, _ago(ago), tool, "Bash", {"command": command}),
                    tool_result(None, _ago(ago - 1), tool, stdout,
                                toolUseResult={"stdout": stdout, "stderr": ""}))
        self.job(short, name=name, created_ago=ago if created_ago is None else created_ago,
                 **(job if job is not None else {"state": "working"}))

    def job(self, short: str = SHORT, *, mtime_ago: float | None = None, name: str = "night-run",
            created_ago: float = 120.0, **fields) -> None:
        job_dir = self.jobs / short
        job_dir.mkdir(parents=True, exist_ok=True)
        data = {"name": name, "sessionId": f"{short}-0000-4000-8000-000000000000", "cwd": "/tmp/w",
                "createdAt": _iso(_ago(created_ago)), "updatedAt": _iso(_ago(1)),
                "detail": "", "output": None, **fields}
        path = job_dir / "state.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        if mtime_ago is not None:
            stamp = time.time() - mtime_ago
            os.utime(path, (stamp, stamp))

    def payload(self, command: str = "ls", stdout: str = "") -> dict:
        return {"hook_event_name": "PostToolUse", "session_id": FAKE_UUID,
                "transcript_path": str(self.main), "tool_name": "Bash",
                "tool_input": {"command": command}, "tool_response": {"stdout": stdout}}


def _hook(monkeypatch, capsys, manager: Manager, payload: dict | None = None) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload or manager.payload())))
    assert hooks_cli.dispatch("post-tool") == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


# --- the acceptance test ---------------------------------------------------------------------


def test_a_manager_doing_nothing_special_is_told_its_worker_waits_on_a_permission(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""  # working: nothing to say
    m.job(state="blocked", needs=PERMISSION, mtime_ago=900)
    text = _hook(monkeypatch, capsys, m)
    assert text.startswith(f"WORKER night-run ({SHORT}) blocked 15m: "), text
    assert "waiting for a permission approval (approve Entering worktree)" in text
    assert f"A person must approve it: claude attach {SHORT}" in text
    assert len(text) <= 400
    assert _hook(monkeypatch, capsys, m) == ""  # once per state change


def test_a_delivered_worker_is_told_as_information_not_as_a_problem(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="done", output={"result": "the report"}, mtime_ago=60)
    text = _hook(monkeypatch, capsys, m)
    assert text.startswith(f"WORKER night-run ({SHORT}) finished"), text
    assert f"claude logs {SHORT}" in text
    for word in ("blocked", "failed", "stuck", "stalled", "must", "problem", "error"):
        assert word not in text.lower(), text
    assert "the report" not in text


def test_a_worker_that_delivered_and_waits_on_a_decision_is_not_a_problem_either(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="blocked", needs="Sam: choose option a or b", mtime_ago=60,
          output={"result": "the report"})
    text = _hook(monkeypatch, capsys, m)
    assert text.startswith(f"WORKER night-run ({SHORT}) delivered"), text
    assert "blocked" not in text and "must" not in text
    assert "Sam" not in text and "choose" not in text and "the report" not in text


# --- every state change, once ----------------------------------------------------------------


@pytest.mark.parametrize(
    "fields,expected",
    [
        pytest.param({"state": "blocked", "needs": "awaiting input"}, "waiting for an answer",
                     id="awaiting input"),
        pytest.param({"state": "blocked", "needs": "awaiting input: which branch?"},
                     "waiting for an answer", id="awaiting input with text"),
        pytest.param({"state": "blocked", "needs": "which of the two do you want?"},
                     "waiting for an answer", id="a question in free text"),
        pytest.param({"state": "blocked", "needs": "approve the plan with Sam"},
                     "waiting for an answer", id="free text starting with approve"),
        pytest.param({"state": "blocked", "needs": "send a prompt to start"},
                     "waits for its first prompt", id="no prompt yet"),
        pytest.param({"state": "blocked", "detail": "API Error: 529 overloaded"},
                     "stopped on an API error", id="API error"),
        pytest.param({"state": "blocked"}, "the reason is not recorded", id="nothing recorded"),
        pytest.param({"state": "failed"}, "failed", id="failed"),
    ],
)
def test_each_class_of_trouble_has_its_own_line(tmp_path, monkeypatch, capsys, fields, expected):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(mtime_ago=30, **fields)
    text = _hook(monkeypatch, capsys, m)
    assert text.startswith(f"WORKER night-run ({SHORT})") and expected in text, text
    for private in ("which branch", "which of the two", "the plan with Sam", "529"):
        assert private not in text, text


# Seen live on 2.1.318: `state` is written when a turn ends and `tempo` follows the session. A
# worker on a permission prompt reads `state: working, tempo: blocked`, which no fixture above has.


def test_a_live_permission_prompt_with_the_state_still_working_is_told(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(job={"state": "working", "tempo": "active"})
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="working", tempo="blocked", needs="approve Write: /tmp/zz-private.txt",
          detail="Writing /tmp/zz-private.txt", mtime_ago=240)
    text = _hook(monkeypatch, capsys, m)
    assert text.startswith(f"WORKER night-run ({SHORT}) blocked 240s".replace("240s", "4m")), text
    assert "waiting for a permission approval (approve Write)" in text
    assert "zz-private" not in text


@pytest.mark.parametrize(
    "fields,expected",
    [
        pytest.param({"state": "working", "tempo": "blocked", "needs": "awaiting input: which?"},
                     "waiting for an answer", id="a live question"),
        pytest.param({"state": "working", "tempo": "blocked", "needs": "Which branch do you want?",
                      "output": {"result": "an older result"}},
                     "waiting for an answer", id="AskUserQuestion's text beside an old result"),
        pytest.param({"state": "done", "tempo": "blocked", "needs": "approve Bash: ls",
                      "output": {"result": "an older result"}},
                     "(approve Bash)", id="a prompt after it delivered"),
        pytest.param({"state": "blocked", "tempo": "blocked", "needs": "approve Entering worktree"},
                     "(approve Entering worktree)", id="the field night"),
        pytest.param({"state": "blocked", "tempo": "blocked", "needs": "approve the PR with Sam"},
                     "waiting for an answer", id="the worker's own words"),
        pytest.param({"state": "blocked", "tempo": "blocked",
                      "needs": "approve Write for worker zz0000: run claude attach zz0000"},
                     "waiting for an answer", id="a manager's own words about its worker, seen live"),
        pytest.param({"state": "working", "tempo": "blocked", "needs": "approve a tool: do x"},
                     "(approve a tool)", id="no display name"),
        pytest.param({"state": "working", "tempo": "blocked", "needs": "approve mcp__srv__do: x"},
                     "(approve mcp__srv__do)", id="an MCP tool"),
        pytest.param({"state": "blocked", "tempo": "blocked",
                      "needs": "approve 2 new project MCP servers (zz-a, zz-b) — attach to respond"},
                     "(approve 2 new project MCP servers)", id="new MCP servers"),
        pytest.param({"state": "working", "tempo": "blocked",
                      "needs": "approve message from uds:/tmp/zz (claims name: zz-peer)"},
                     "(approve message from another session)", id="a message from a session"),
        pytest.param({"state": "working", "tempo": "blocked", "needs": "approve plan"},
                     "(approve plan)", id="a plan"),
    ],
)
def test_the_live_forms_of_needs(tmp_path, monkeypatch, capsys, fields, expected):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(mtime_ago=30, **fields)
    text = _hook(monkeypatch, capsys, m)
    assert expected in text and "Sam" not in text and "zz" not in text, text
    assert "older result" not in text


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"state": "done", "tempo": "active", "output": {"result": "r"}},
                     id="given a new prompt after it delivered"),
        pytest.param({"state": "blocked", "tempo": "active", "needs": "an old question"},
                     id="answered and running again"),
        pytest.param({"state": "stopped", "tempo": "blocked", "needs": "approve Bash"},
                     id="stopped while on a prompt"),
    ],
)
def test_a_stale_state_word_tells_nothing(tmp_path, monkeypatch, capsys, fields):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(mtime_ago=30, **fields)
    assert _hook(monkeypatch, capsys, m) == ""


def test_the_state_word_catching_up_is_no_new_change(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=30)
    assert PERMISSION in _hook(monkeypatch, capsys, m)
    m.job(state="blocked", tempo="blocked", needs=PERMISSION, mtime_ago=30)
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_worker_on_a_schedule_is_told_finishing_twice_then_not_until_trouble(
        tmp_path, monkeypatch, capsys):
    """Field report 2026-11-04: a worker goes `done` after each scheduled check and wakes on its
    own cron, so "finished" would fire every hour. None of its own words go on the line."""
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""

    def finish(detail: str) -> str:
        m.job(state="working", tempo="active", mtime_ago=2)
        assert _hook(monkeypatch, capsys, m) == ""
        m.job(state="done", tempo="idle", detail=detail, output={"result": detail}, mtime_ago=1)
        return _hook(monkeypatch, capsys, m)

    first = finish("zz-nothing to report")
    assert f"({SHORT}) finished" in first and "again" not in first, first
    second = finish("zz-nothing to report")
    assert "finished again" in second and "later finishes are not told" in second, second
    assert "zz-" not in first + second
    assert finish("zz-nothing to report") == ""
    assert finish("zz-nothing to report") == ""
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=1)
    assert PERMISSION in _hook(monkeypatch, capsys, m)  # trouble always speaks, and resets the count
    assert "again" not in finish("zz-nothing to report")


def test_a_delivered_worker_waiting_on_a_decision_is_always_told(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    for question in ("one?", "two?", "three?"):
        m.job(state="blocked", tempo="blocked", needs=question, output={"result": "r"}, mtime_ago=1)
        assert "delivered" in _hook(monkeypatch, capsys, m)
        m.job(state="working", tempo="active", mtime_ago=1)
        assert _hook(monkeypatch, capsys, m) == ""


def test_a_class_change_with_the_state_unchanged_is_told(tmp_path, monkeypatch, capsys):
    """The field night: blocked on a question, a new brief, then blocked on a permission prompt
    with the state word `blocked` all along."""
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs="awaiting input", mtime_ago=40)
    assert "waiting for an answer" in _hook(monkeypatch, capsys, m)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=5)
    assert "approve Entering worktree" in _hook(monkeypatch, capsys, m)


def test_the_description_of_a_permission_is_withheld(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs="approve Bash: rm -rf /tmp/w/secret-name", mtime_ago=30)
    text = _hook(monkeypatch, capsys, m)
    assert "(approve Bash)" in text and "secret-name" not in text and "rm -rf" not in text, text


def test_a_second_permission_of_the_same_tool_is_a_new_change(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs="approve Bash: one", mtime_ago=30)
    assert "(approve Bash)" in _hook(monkeypatch, capsys, m)
    m.job(state="blocked", needs="approve Bash: two", mtime_ago=5)
    assert "(approve Bash)" in _hook(monkeypatch, capsys, m)


def test_the_same_prompt_again_after_working_is_told_again(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30)
    assert PERMISSION in _hook(monkeypatch, capsys, m)
    m.job(state="working")
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="blocked", needs=PERMISSION, mtime_ago=5)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


def test_a_stopped_worker_is_not_told(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="stopped", mtime_ago=30)
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_prompt_submit_tells_it_too(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": FAKE_UUID,
               "transcript_path": str(m.main), "prompt": "hello"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch("prompt-submit") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert PERMISSION in out["hookSpecificOutput"]["additionalContext"]


# --- the link between the manager and its worker ---------------------------------------------


def test_the_launch_call_itself_registers_the_worker_when_the_transcript_lags(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.job(state="blocked", needs=PERMISSION, created_ago=2, mtime_ago=1)
    payload = m.payload(LAUNCH, _stdout(SHORT))  # the transcript does not hold the launch yet
    assert PERMISSION in _hook(monkeypatch, capsys, m, payload)


@pytest.mark.parametrize("hint", ["attach", "logs", "stop"])
def test_a_launch_whose_first_lines_were_cut_by_tail_is_still_a_launch(
        tmp_path, monkeypatch, capsys, hint):
    m = Manager(tmp_path)
    m.launched(command=LAUNCH + " | tail -1", stdout=f"  claude {hint} {SHORT}    words\n")
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("grep -- --bg notes.txt", id="a grep that names the flag"),
        pytest.param("cat old-launch.log", id="a printed log"),
        pytest.param("echo claude --bg x", id="echo"),
        pytest.param("claude -p 'run claude --bg x'", id="inside a prompt"),
        pytest.param("claude --help", id="no flag"),
    ],
)
def test_a_call_that_only_mentions_a_launch_links_no_worker(tmp_path, monkeypatch, capsys, command):
    m = Manager(tmp_path)
    m.launched(command=command)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30)
    assert _hook(monkeypatch, capsys, m) == ""
    m.append(tool_use(None, _ago(1), "toolu_x", "Bash", {"command": "ls"}))
    assert _hook(monkeypatch, capsys, m, m.payload(command, _stdout(SHORT))) == ""


def test_a_real_launch_printing_an_old_id_links_nothing(tmp_path, monkeypatch, capsys):
    """Provenance: the job must have been created by that call."""
    m = Manager(tmp_path)
    m.launched(created_ago=86400 * 3, job={"state": "blocked", "needs": PERMISSION})
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_launch_with_no_job_directory_is_quiet(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.append(tool_use(None, _ago(9), "toolu_a", "Bash", {"command": LAUNCH}),
             tool_result(None, _ago(8), "toolu_a", _stdout(SHORT),
                         toolUseResult={"stdout": _stdout(SHORT)}))
    assert _hook(monkeypatch, capsys, m) == ""


@pytest.mark.parametrize(
    "command",
    [
        "claude --bg 'x'",
        "claude --background 'x'",
        "FOO=1 claude --bg 'x'",
        "timeout 60 claude --bg 'x'",
        "env A=1 claude --bg 'x'",
        "nohup claude --bg 'x'",
        "cd /tmp && claude --bg 'x'",
        "cd /tmp; claude --bg 'x' | tail -3",
        "/usr/local/bin/claude --bg 'x'",
        "for n in a b; do claude --bg -n $n 'x'; done",
        "(cd /tmp && claude --bg 'x')",
        "claude --bg -n x \"$(cat <<'BRIEF'\nnote: claude --bg in here\nBRIEF\n)\"",
    ],
)
def test_the_forms_of_a_launch(command):
    assert workers.command_launches(command) is True


@pytest.mark.parametrize(
    "command",
    ["echo claude --bg", "grep claude --bg", "claude -p '--bg'", "git log --bg",
     "printf 'claude --bg x\\n'", "claude attach a1b2c3d4", "claude logs --bg"],
)
def test_what_is_no_launch(command):
    assert workers.command_launches(command) is False


# --- what the gate costs -----------------------------------------------------------------------


def test_with_nothing_changed_the_gate_reads_no_job_and_imports_no_full_check(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""  # the launch settled
    monkeypatch.setattr(hooks_cli, "post_tool_check",
                        lambda *a, **k: pytest.fail("the gate opened with nothing changed"))
    monkeypatch.setattr(workers, "read_job", lambda *a, **k: pytest.fail("a job file was parsed"))
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_job_file_that_changed_with_the_same_state_does_not_open_the_gate(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="working", tempo="active", mtime_ago=1)
    monkeypatch.setattr(hooks_cli, "post_tool_check",
                        lambda *a, **k: pytest.fail("the gate opened for the same state"))
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_session_with_no_launch_keeps_no_gate_state(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    assert _hook(monkeypatch, capsys, m) == ""
    assert not hook_gate._gate_path(str(m.main)).exists()


def test_at_most_eight_changed_job_files_are_parsed_per_call(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    shorts = [f"0000000{n:x}" for n in range(12)]
    for short in shorts:
        m.launched(short, name=f"w{short[-1]}")
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    for short in shorts:
        m.job(short, state="working", tempo="x", mtime_ago=1, name=f"w{short[-1]}")
    parsed = []
    real = workers.read_job

    def counted(*args, **kwargs):
        parsed.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(workers, "read_job", counted)
    monkeypatch.setattr(hooks_cli, "post_tool_check", lambda *a, **k: (lambda: None, False))
    assert _hook(monkeypatch, capsys, m) == ""
    assert len(parsed) == 8, len(parsed)


# --- the 400 characters ------------------------------------------------------------------------


def test_many_workers_stay_within_the_budget_and_the_rest_come_later(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    shorts = [f"0000000{n}" for n in range(6)]
    for short in shorts:
        m.launched(short, name=f"worker-{short[-1]}")
    assert _hook(monkeypatch, capsys, m) == ""
    for short in shorts:
        m.job(short, state="blocked", needs=PERMISSION, mtime_ago=30, name=f"worker-{short[-1]}")
    first = _hook(monkeypatch, capsys, m)
    assert len(first) <= 400 and "more" in first, first
    told = {s for s in shorts if s in first}
    assert told and told != set(shorts)
    seen = set(told)
    for _ in range(3):
        text = _hook(monkeypatch, capsys, m)
        seen |= {s for s in shorts if s in text}
    assert seen == set(shorts)
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_long_name_is_capped_and_control_characters_are_gone(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    long_name = "n\x1b[31m" + "x" * 200
    m.launched(name=long_name)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30, name=long_name)
    text = _hook(monkeypatch, capsys, m)
    assert "\x1b" not in text and "x" * 41 not in text and SHORT in text, text


def test_a_name_that_looks_like_a_path_stays_out(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(name="/tmp/zz-private/brief")
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30, name="/tmp/zz-private/brief")
    text = _hook(monkeypatch, capsys, m)
    assert f"WORKER worker ({SHORT})" in text and "zz-private" not in text, text


def test_workers_come_before_endings_and_info_comes_last(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched("00000001", name="done-one")
    m.launched("00000002", name="stuck-one")
    assert _hook(monkeypatch, capsys, m) == ""
    m.job("00000001", state="done", output={"result": "r"}, mtime_ago=30, name="done-one")
    m.job("00000002", state="blocked", needs=PERMISSION, mtime_ago=30, name="stuck-one")
    text = _hook(monkeypatch, capsys, m)
    assert text.index("stuck-one") < text.index("done-one"), text


def test_a_worker_is_told_before_the_subagent_baseline_is_ready(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30)
    monkeypatch.setattr(live.SessionFollower, "load_endings", lambda self, **k: False)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


def test_a_call_killed_before_the_claim_loses_nothing(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30)
    real = live.claim_deliveries

    def killed(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(live, "claim_deliveries", killed)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(m.payload())))
    with pytest.raises(KeyboardInterrupt):
        hook_gate.run("post-tool")
    capsys.readouterr()
    monkeypatch.setattr(live, "claim_deliveries", real)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


# --- first look --------------------------------------------------------------------------------


def test_an_old_blocked_worker_at_the_first_look_is_recorded_silently(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(ago=9 * 86400, job={"state": "blocked", "needs": PERMISSION})
    m.job(state="blocked", needs=PERMISSION, created_ago=9 * 86400, mtime_ago=8 * 86400)
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="blocked", needs="approve Bash", created_ago=9 * 86400, mtime_ago=5)
    assert "(approve Bash)" in _hook(monkeypatch, capsys, m)  # a later change is told


def test_a_delivery_older_than_ten_minutes_at_the_first_look_is_recorded_silently(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(ago=7200, job={"state": "done", "output": {"result": "r"}})
    m.job(state="done", output={"result": "r"}, created_ago=7200, mtime_ago=700)
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_blocked_worker_of_today_at_the_first_look_is_told(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(ago=7200, job={"state": "blocked", "needs": PERMISSION})
    m.job(state="blocked", needs=PERMISSION, created_ago=7200, mtime_ago=3600)
    assert "blocked 60m" in _hook(monkeypatch, capsys, m)


def test_a_gate_file_from_before_this_feature_opens_once(tmp_path, monkeypatch, capsys):
    """An old launch, past the 64 KB the gate reads, in a session that already had gate state."""
    m = Manager(tmp_path)
    m.launched(ago=600)
    m.job(state="blocked", needs=PERMISSION, created_ago=600, mtime_ago=300)
    gate = hook_gate._gate_path(str(m.main))
    info = os.stat(m.main)
    state.write_private_bytes(gate, json.dumps({
        "sig": "", "main": {"inode": info.st_ino, "offset": info.st_size, "partial": False},
        "pending": False}).encode())
    assert PERMISSION in _hook(monkeypatch, capsys, m)


# --- together with what the hook already told --------------------------------------------------


def _with_a_stall(m: Manager, tmp_path, monkeypatch) -> None:
    """The worker's own job says `working`, unchanged for 40 minutes; its session's own transcript
    is idle on a command that writes nothing."""
    from agent_postmortem import status

    m.job(state="working", mtime_ago=40 * 60, created_ago=7200)
    (m.main.parent / f"{SHORT}-0000-4000-8000-000000000000.jsonl").write_text("{}\n")
    monkeypatch.setattr(status, "_job_stall", lambda main, now, kinds, deadline=None: {
        "idle_seconds": 2400, "commands": 1, "waiting_on_stdin": 0, "silent_seconds": 2400,
        "task_ids": ["b00000001"]})


def test_a_worker_idle_on_a_silent_command_is_told_once(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(ago=7200)
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""  # the first look's record went through: gate shut
    _with_a_stall(m, tmp_path, monkeypatch)

    def clock_is_up():
        gate = hook_gate._gate_path(str(m.main))
        data = json.loads(gate.read_bytes())
        data["recheck_at"] = time.time() - 1
        state.write_private_bytes(gate, json.dumps(data).encode())

    assert _hook(monkeypatch, capsys, m) == ""  # same state, so the gate stays shut until the clock
    clock_is_up()
    text = _hook(monkeypatch, capsys, m)
    # field report 2026-11-04: it can only see the command's own files, so it says what it knows
    assert text.startswith(f"WORKER night-run ({SHORT}) idle 40m:"), text
    assert "may be hung or writing elsewhere" in text and "only its end would wake" in text, text
    assert "Check before stopping it" in text and f"claude attach {SHORT}" in text
    assert "nothing will wake" not in text and "writes nothing" not in text
    clock_is_up()
    assert _hook(monkeypatch, capsys, m) == ""


def test_a_worker_waiting_on_stdin_is_said_to_be_stalled_outright(tmp_path, monkeypatch, capsys):
    from agent_postmortem import status

    m = Manager(tmp_path)
    m.launched(ago=7200)
    assert _hook(monkeypatch, capsys, m) == ""
    _with_a_stall(m, tmp_path, monkeypatch)
    monkeypatch.setattr(status, "_job_stall", lambda main, now, kinds, deadline=None: {
        "idle_seconds": 2400, "commands": 1, "waiting_on_stdin": 1, "silent_seconds": 2400,
        "task_ids": ["b00000001"]})
    gate = hook_gate._gate_path(str(m.main))
    data = json.loads(gate.read_bytes())
    data["recheck_at"] = time.time() - 1
    state.write_private_bytes(gate, json.dumps(data).encode())
    text = _hook(monkeypatch, capsys, m)
    assert f"({SHORT}) stalled: idle 40m on a background command that waits on stdin" in text, text
    assert "nothing will wake it" in text


def test_the_stall_clock_opens_the_gate(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(ago=7200)
    assert _hook(monkeypatch, capsys, m) == ""
    gate = json.loads(hook_gate._gate_path(str(m.main)).read_bytes())
    # the job file was just written, so its 30 minutes run from now
    assert gate["recheck_at"] == pytest.approx(time.time() + 1800, abs=30)


def test_an_ending_waits_when_the_workers_fill_the_budget(tmp_path, monkeypatch, capsys):
    """Workers come first and what does not fit stays due: nothing is lost."""
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", needs=PERMISSION, mtime_ago=30)
    long_ending = "KILL " + "x" * 330
    calls = {"n": 0}

    def fake_check(main, payload):
        follower = live.SessionFollower(m.main, caller_session=None)
        follower._read()
        cache = state.read_cache(follower.store_path)
        check = hooks_cli._worker_check(follower, payload, cache, time.monotonic())
        return hooks_cli._make_deliver([({"event": "KILL"}, "k1")], [], 0, long_ending, {}, check,
                                       m.main, follower, None), True

    def fake_render(events, main_path, follower, budget=400):
        calls["n"] += 1
        return long_ending if events else None

    monkeypatch.setattr(hooks_cli, "post_tool_check", fake_check)
    monkeypatch.setattr(hooks_cli, "_render_endings", fake_render)
    first = _hook(monkeypatch, capsys, m)
    assert "WORKER" in first and long_ending not in first and "more" in first, first
    assert len(first) <= 400
    assert long_ending in _hook(monkeypatch, capsys, m)


# --- review round 1 of this feature --------------------------------------------------------------


def test_a_long_word_after_new_project_mcp_is_no_label(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", tempo="blocked", mtime_ago=30,
          needs="approve 1 new project MCP " + "Z" * 41)
    text = _hook(monkeypatch, capsys, m)
    assert "Z" * 10 not in text and len(text) <= 400, text


def test_eight_busy_workers_cannot_hide_a_ninth(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    shorts = [f"0000000{n:x}" for n in range(12)]
    for short in shorts:
        m.launched(short, name=f"w{short[-1]}")
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(shorts[11], state="working", tempo="blocked", needs=PERMISSION, mtime_ago=30, name="wb")
    seen = ""
    for tick in range(5):
        for short in shorts[:8]:  # every call finds them all moved, with nothing new to say
            m.job(short, state="working", tempo="active", mtime_ago=1 + tick * 0.01,
                  name=f"w{short[-1]}", detail=f"step {tick}")
        seen += _hook(monkeypatch, capsys, m)
    assert f"({shorts[11]})" in seen, seen


def test_one_failed_stat_does_not_end_a_workers_tracking(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    real = workers.job_signature
    monkeypatch.setattr(workers, "job_signature", lambda path: None)
    assert _hook(monkeypatch, capsys, m) == ""
    monkeypatch.setattr(workers, "job_signature", real)
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=1)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


def test_a_launch_whose_job_appears_while_the_transcript_lags_is_not_rejected_for_good(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    assert _hook(monkeypatch, capsys, m, m.payload(LAUNCH, _stdout(SHORT))) == ""  # no job yet
    m.job(state="working", tempo="blocked", needs=PERMISSION, created_ago=2, mtime_ago=1)
    assert _hook(monkeypatch, capsys, m) == ""  # the transcript does not hold the launch yet
    m.append(tool_use(None, _ago(3), "toolu_lag", "Bash", {"command": LAUNCH}),
             tool_result(None, _ago(1), "toolu_lag", _stdout(SHORT),
                         toolUseResult={"stdout": _stdout(SHORT)}))
    assert PERMISSION in _hook(monkeypatch, capsys, m)


def test_a_stale_needs_on_a_running_worker_does_not_take_it_off_the_stall_clock():
    data = {"state": "blocked", "tempo": "active", "needs": "an old question"}
    assert workers.identity(data) == "working/working/"


def test_every_idle_worker_gets_its_turn_at_the_stall_read(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_postmortem import status

    m = Manager(tmp_path)
    shorts = [f"0000000{n}" for n in range(5)]
    for short in shorts:
        m.job(short, state="working", mtime_ago=7200)
    (m.main.parent / "x.jsonl").touch()
    read = []
    monkeypatch.setattr(status, "_job_transcript", lambda root, session: session)
    monkeypatch.setattr(status, "_job_stall", lambda main, now, kinds, deadline=None: read.append(main))
    follower = SimpleNamespace(jobs_root=m.jobs, main_path=m.main)
    base = 1_800_000_000.0
    for turn in range(5):
        now = base + turn * hooks_cli._STALL_RECHECK_SECONDS
        table = {short: [(now - 7200) * 1e9, 1, "working/working/"] for short in shorts}
        check = hooks_cli._WorkerCheck(table=table, stall_turn=turn)
        hooks_cli._worker_stall(follower, check, now, time.monotonic())
    assert len(set(read)) == 5, read


@pytest.mark.parametrize(
    "command,launches",
    [
        pytest.param("env -S 'claude --bg x'", True, id="env -S"),
        pytest.param('clau"de" --bg x', True, id="a quote inside the word"),
        pytest.param("claude --print -- --bg", False, id="after the option terminator"),
    ],
)
def test_more_forms_of_a_launch(command, launches):
    assert workers.command_launches(command) is launches


def test_the_gate_keeps_no_more_workers_than_the_facts(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    assert _hook(monkeypatch, capsys, m) == ""
    for n in range(workers.WORKERS_KEPT + 3):
        m.launched(f"{n:08x}", name=f"w{n}", ago=7000.0 - n, job={"state": "stopped"})
        if n % 40 == 0:
            _hook(monkeypatch, capsys, m)
    for _ in range(25):
        _hook(monkeypatch, capsys, m)
    gate = json.loads(hook_gate._gate_path(str(m.main)).read_bytes())
    assert len(gate["workers"]) <= workers.WORKERS_KEPT, len(gate["workers"])


# --- review round 2 of this feature --------------------------------------------------------------


def test_a_short_word_after_new_project_mcp_is_no_label(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="blocked", tempo="blocked", mtime_ago=30, needs="approve 1 new project MCP ZZSECRET")
    text = _hook(monkeypatch, capsys, m)
    assert "ZZSECRET" not in text and "(approve)" in text, text


def test_a_background_launch_keeps_its_redirects_for_the_silence_rule():
    from live_helpers import iso

    from agent_postmortem.live import MainFacts

    facts = MainFacts()
    command = "claude --bg x > /tmp/launch.log"
    facts.update([
        tool_use(None, _ago(60), "toolu_b", "Bash", {"command": command,
                                                     "run_in_background": True}),
        tool_result(None, _ago(60), "toolu_b",
                    "Command running in background with ID: b1. Output is being written to: "
                    "/tmp/b1.output. You will be notified when it completes.",
                    toolUseResult={"backgroundTaskId": "b1"}),
    ])
    assert iso  # the helper's clock format is what tool_use used
    shell = facts.shells["b1"]
    assert "/tmp/launch.log" in shell["outputs"] and not shell.get("unknown_outputs"), shell


def test_eight_unreadable_jobs_cannot_hide_a_ninth(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    shorts = [f"0000000{n:x}" for n in range(9)]
    for short in shorts:
        m.launched(short, name=f"w{short[-1]}")
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    for short in shorts[:8]:
        (m.jobs / short / "state.json").write_text("{not json", encoding="utf-8")
    m.job(shorts[8], state="working", tempo="blocked", needs=PERMISSION, mtime_ago=30, name="w8")
    seen = "".join(_hook(monkeypatch, capsys, m) for _ in range(4))
    assert f"({shorts[8]})" in seen, seen


def test_a_job_that_stops_being_readable_is_told_when_it_reads_again(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    assert _hook(monkeypatch, capsys, m) == ""
    (m.jobs / SHORT / "state.json").write_text("{not json", encoding="utf-8")
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=1)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


def test_the_stall_read_carries_the_calls_time_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_postmortem import status

    m = Manager(tmp_path)
    m.job(SHORT, state="working", mtime_ago=7200)
    seen = {}
    monkeypatch.setattr(status, "_job_transcript", lambda root, session: session)

    def stall(main, now, kinds, deadline=None):
        seen["deadline"] = deadline

    monkeypatch.setattr(status, "_job_stall", stall)
    follower = SimpleNamespace(jobs_root=m.jobs, main_path=m.main)
    now = time.time()
    check = hooks_cli._WorkerCheck(table={SHORT: [(now - 7200) * 1e9, 1, "working/working/"]})
    started = time.monotonic()
    hooks_cli._worker_stall(follower, check, now, started)
    assert seen["deadline"] == started + hooks_cli._CALL_BUDGET_SECONDS


def test_a_stall_snapshot_cut_short_says_nothing_and_keeps_its_place(tmp_path):
    from agent_postmortem import status

    m = Manager(tmp_path)
    assert status._job_stall(m.main, time.time(), {}, deadline=time.monotonic() - 1) is None


def test_each_idle_worker_gets_its_turn_whatever_the_cadence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_postmortem import status

    m = Manager(tmp_path)
    shorts = ["00000000", "00000001"]
    for short in shorts:
        m.job(short, state="working", mtime_ago=7200)
    read = []
    monkeypatch.setattr(status, "_job_transcript", lambda root, session: session)
    monkeypatch.setattr(status, "_job_stall", lambda main, now, kinds, deadline=None: read.append(main))
    follower = SimpleNamespace(jobs_root=m.jobs, main_path=m.main)
    turn = 0
    for step in range(4):
        now = 1_800_000_000.0 + step * 600
        table = {short: [(now - 7200) * 1e9, 1, "working/working/"] for short in shorts}
        check = hooks_cli._WorkerCheck(table=table, stall_turn=turn)
        hooks_cli._worker_stall(follower, check, now, time.monotonic())
        turn = check.stall_turn
    assert len(set(read)) == 2, read


def test_the_stall_turn_is_kept_in_the_gate(tmp_path, monkeypatch, capsys):
    from agent_postmortem import status

    m = Manager(tmp_path)
    m.launched(ago=7200)
    m.job(state="working", created_ago=7200, mtime_ago=7000)
    (m.main.parent / f"{SHORT}-0000-4000-8000-000000000000.jsonl").write_text("{}\n")
    monkeypatch.setattr(status, "_job_stall", lambda main, now, kinds, deadline=None: None)
    assert _hook(monkeypatch, capsys, m) == ""
    gate = json.loads(hook_gate._gate_path(str(m.main)).read_bytes())
    assert gate["stall_turn"] == 1


# --- review round 3 of this feature --------------------------------------------------------------


def test_a_partial_stall_read_keeps_the_reported_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_postmortem import status

    m = Manager(tmp_path)
    m.job(SHORT, state="working", mtime_ago=7200)
    monkeypatch.setattr(status, "_job_transcript", lambda root, session: session)
    monkeypatch.setattr(status, "_job_stall",
                        lambda main, now, kinds, deadline=None: status.STALL_UNKNOWN)
    follower = SimpleNamespace(jobs_root=m.jobs, main_path=m.main,
                               main=SimpleNamespace(workers={}))
    now = time.time()
    check = hooks_cli._WorkerCheck(table={SHORT: [(now - 7200) * 1e9, 1, "working/stalled/"]})
    hooks_cli._worker_stall(follower, check, now, time.monotonic())
    assert check.table[SHORT][2] == "working/stalled/"
    assert check.observed == []


def test_a_snapshot_cut_short_reads_as_unknown_and_a_finished_one_as_no_stall(
        tmp_path, monkeypatch):
    from agent_postmortem import status

    m = Manager(tmp_path)
    assert status._job_stall(m.main, time.time(), {}, deadline=time.monotonic() + 9) is None
    monkeypatch.setattr(live.SessionFollower, "_read", lambda self, deadline=None: False)
    assert status._job_stall(m.main, time.time(), {}, deadline=time.monotonic() + 9) \
        is status.STALL_UNKNOWN


def test_a_prompt_returning_after_a_failed_store_write_is_told(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="blocked", needs=PERMISSION)
    assert PERMISSION in _hook(monkeypatch, capsys, m)
    m.job(state="working", tempo="active")

    def busy(*args, **kwargs):
        raise state.StateLocked("busy")

    with monkeypatch.context() as mp:
        mp.setattr(state, "cache_transaction", busy)
        assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="working", tempo="blocked", needs=PERMISSION)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


@pytest.mark.parametrize(
    "command,quiet",
    [
        pytest.param("perl -e 'alarm 1'; tail -f /tmp/input.log", False, id="an earlier command"),
        pytest.param("perl -e 'alarm 5; exec @ARGV' run; tail -f x", False, id="one more after it"),
        pytest.param("perl -e 'alarm 1'", False, id="no exec: it bounds nothing"),
        pytest.param("echo alarm 5", False, id="the word alone"),
        pytest.param("caffeinate -i perl -e 'alarm 16200; exec @ARGV' run-pilot", True,
                     id="the field command"),
        pytest.param("cd /tmp && caffeinate -i perl -e 'alarm 60; exec @ARGV' run > /tmp/o.log 2>&1", True,
                     id="after a cd, with redirects"),
    ],
)
def test_a_perl_alarm_bounds_only_what_it_execs(command, quiet):
    from agent_postmortem import commands

    assert commands.quiet_by_design(command) is quiet


def test_the_name_is_the_one_the_launch_printed_when_it_printed_one():
    data = {"name": "ZZ-RENAMED-FROM-THE-PROMPT"}
    assert workers.name_of(data, SHORT, "night-run") == "night-run"
    assert workers.name_of(data, SHORT, None) == "ZZ-RENAMED-FROM-THE-PROMPT"


def test_the_launch_lines_note_is_no_part_of_the_name():
    found = workers.launched_ids(f"backgrounded · {SHORT} · night-run (idle — send a prompt to start)\n")
    assert found == {SHORT: "night-run"}


def test_a_job_renamed_after_its_launch_keeps_the_launch_name_on_the_line(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched()
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=1,
          name="ZZ-RENAMED-FROM-THE-PROMPT")
    text = _hook(monkeypatch, capsys, m)
    assert f"WORKER night-run ({SHORT})" in text and "ZZ-" not in text, text


def test_a_call_does_not_start_a_stall_read_it_cannot_finish(tmp_path, monkeypatch):
    """run.sh kills the hook at 3 s and nothing is saved, so the next call would repeat it. The
    watcher has no such limit and does the read."""
    from types import SimpleNamespace

    from agent_postmortem import status

    m = Manager(tmp_path)
    m.job(SHORT, state="working", mtime_ago=7200)
    big = tmp_path / "big.jsonl"
    with big.open("wb") as handle:
        handle.truncate(hooks_cli._STALL_READ_BYTES + 1)
    monkeypatch.setattr(status, "_job_transcript", lambda root, session: big)
    read = []
    monkeypatch.setattr(status, "_job_stall",
                        lambda main, now, kinds, deadline=None: read.append(main))
    follower = SimpleNamespace(jobs_root=m.jobs, main_path=m.main, main=SimpleNamespace(workers={}))
    now = time.time()
    table = {SHORT: [(now - 7200) * 1e9, 1, "working/working/"]}
    check = hooks_cli._WorkerCheck(table=table, stall_any_size=False)
    hooks_cli._worker_stall(follower, check, now, time.monotonic(), cache={})
    assert read == []
    check = hooks_cli._WorkerCheck(table=table, stall_any_size=True)
    hooks_cli._worker_stall(follower, check, now, time.monotonic(), cache={})
    assert read == [big]
