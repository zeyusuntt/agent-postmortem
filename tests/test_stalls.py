"""A background command that stopped writing, and a session idle on it (field reports 2026-11-03
"a background codex exec hung 23h on stdin and nothing flagged it" and "no silent failures";
docs/DESIGN.md 4.14.4).

The field shape: a worker launched `codex exec ... > review.md 2> review.log` with
run_in_background and no stdin redirect, ended its turn ("waiting on the review"), and sat idle
for 26 hours. Its log held one line, codex's "Reading additional input from stdin...", and
neither file changed again. `status` printed "1 running (launched 23h ago)" and nothing else.

Synthetic data only: the repository's fake session id, paths under tmp_path, and this test
process's own pid standing in for a live session.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from live_helpers import (
    FAKE_UUID,
    assistant_text,
    iso,
    notification,
    tool_result,
    tool_use,
    user,
    write_jsonl,
)
from test_live_session import SLUG, T0

from agent_postmortem import live, stalls, status

STDIN_LINE = "Reading additional input from stdin...\n"
LAUNCH = T0 + timedelta(minutes=5)
TASK = "b00000001"


def _at(dt) -> float:
    return dt.timestamp()


class Session:
    """A session tree under tmp_path: projects/<slug>/<uuid>.jsonl, sessions/, jobs/, and a
    working directory the command's redirects resolve against."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()
        self.task_output = tmp_path / "tasks" / f"{TASK}.output"
        self.task_output.parent.mkdir()
        self.entries: list[dict] = [self._here(user(None, T0, "start"))]

    def _here(self, entry: dict) -> dict:
        if "cwd" in entry:
            entry["cwd"] = str(self.cwd)
        return entry

    def launch(self, command: str, at=LAUNCH, *, task: str = TASK, tool_id: str = "toolu_sh1",
               output: Path | None = None) -> "Session":
        output = self.task_output if output is None else output
        output.touch()
        os.utime(output, (_at(at), _at(at)))
        text = (f"Command running in background with ID: {task}. Output is being written to: "
                f"{output}. You will be notified when it completes.")
        result = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
                  "noOutputExpected": False, "backgroundTaskId": task}
        self.entries += [
            self._here(tool_use(None, at, tool_id, "Bash",
                                {"command": command, "run_in_background": True})),
            self._here(tool_result(None, at, tool_id, text, toolUseResult=result)),
        ]
        return self

    def turn_end(self, at=LAUNCH + timedelta(minutes=2), text="waiting on the review") -> "Session":
        self.entries.append(self._here(assistant_text(None, at, text)))
        self.entries.append({"type": "system", "subtype": "turn_duration", "durationMs": 60000,
                             "sessionId": FAKE_UUID, "timestamp": iso(at)})
        return self

    def file(self, name: str, text: str, at=LAUNCH) -> Path:
        path = self.cwd / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (_at(at), _at(at)))
        return path

    def alive(self, kind: str = "bg", pid: int | None = None) -> "Session":
        sessions = self.root / "sessions"
        sessions.mkdir(exist_ok=True)
        pid = os.getpid() if pid is None else pid
        (sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": FAKE_UUID, "kind": kind, "status": "idle"}))
        return self

    def job(self, word: str = "working", at=LAUNCH + timedelta(minutes=2)) -> "Session":
        job = self.root / "jobs" / "00000000"
        job.mkdir(parents=True, exist_ok=True)
        (job / "state.json").write_text(json.dumps({"state": word, "sessionId": FAKE_UUID,
                                                    "detail": "synthetic"}))
        os.utime(job / "state.json", (_at(at), _at(at)))
        return self

    def write(self) -> Path:
        write_jsonl(self.main, self.entries)
        # a transcript's mtime is its last write: the newest entry's time
        from datetime import datetime
        stamps = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
                  for e in self.entries if isinstance(e.get("timestamp"), str)]
        os.utime(self.main, (max(stamps),) * 2)
        return self.main

    def snapshot(self, now) -> dict:
        self.write()
        return status.snapshot(self.main, now=lambda: _at(now))


FIELD = 'codex exec --sandbox read-only "$(cat prompt.txt)" > review.md 2> review.log'


def _field(tmp_path) -> Session:
    s = Session(tmp_path).launch(FIELD).turn_end().alive().job()
    s.file("review.md", "")
    s.file("review.log", STDIN_LINE)
    return s


# --- the command fact ----------------------------------------------------------------------


def test_a_background_command_keeps_its_output_files(tmp_path):
    s = _field(tmp_path)
    s.write()
    facts = live.MainFacts()
    facts.update([json.loads(line) for line in s.main.read_text().splitlines()])
    shell = facts.shells[TASK]
    assert sorted(shell["outputs"]) == sorted(
        [str(s.task_output), str(s.cwd / "review.md"), str(s.cwd / "review.log")])
    assert shell.get("unknown_outputs") is not True


@pytest.mark.parametrize(
    "command,unknown",
    [
        pytest.param("make build > /dev/null 2>&1", True, id="output thrown away"),
        pytest.param('run-it > "$LOG"', True, id="an output we cannot place"),
        pytest.param("run-it >> logs/a.log 2>&1", False, id="a relative append"),
    ],
)
def test_an_output_that_cannot_be_watched_is_marked(tmp_path, command, unknown):
    s = Session(tmp_path).launch(command)
    s.write()
    facts = live.MainFacts()
    facts.update([json.loads(line) for line in s.main.read_text().splitlines()])
    assert bool(facts.shells[TASK].get("unknown_outputs")) is unknown


def test_a_cd_before_the_command_moves_where_its_outputs_are(tmp_path):
    s = Session(tmp_path)
    sub = s.cwd / "sub"
    sub.mkdir()
    s.launch(f"cd {sub} && run-it > out.log")
    s.write()
    facts = live.MainFacts()
    facts.update([json.loads(line) for line in s.main.read_text().splitlines()])
    assert str(sub / "out.log") in facts.shells[TASK]["outputs"]


# --- silence ----------------------------------------------------------------------------------


def test_the_field_command_waits_on_stdin(tmp_path):
    snap = _field(tmp_path).snapshot(LAUNCH + timedelta(hours=23))
    (command,) = snap["commands"]["running"]
    assert command["waiting_on_stdin"] is True
    assert command["silent_seconds"] == pytest.approx(23 * 3600, abs=5)


def test_a_command_that_wrote_a_minute_ago_is_not_silent(tmp_path):
    s = _field(tmp_path)
    now = LAUNCH + timedelta(hours=2)
    s.file("review.log", STDIN_LINE + "[2026-10-30] OpenAI Codex\nthinking\n",
           at=now - timedelta(minutes=1))
    (command,) = s.snapshot(now)["commands"]["running"]
    assert command["waiting_on_stdin"] is False
    assert command["silent"] is False


def test_the_stdin_line_followed_by_more_output_is_not_a_wait(tmp_path):
    s = _field(tmp_path)
    s.file("review.log", STDIN_LINE + "[2026-10-30] OpenAI Codex v0\n")
    (command,) = s.snapshot(LAUNCH + timedelta(minutes=10))["commands"]["running"]
    assert command["waiting_on_stdin"] is False
    assert command["silent"] is False  # 10 minutes is under the 30-minute silence


def test_the_stdin_line_alone_waits_after_two_minutes_not_before(tmp_path):
    s = _field(tmp_path)
    early = s.snapshot(LAUNCH + timedelta(seconds=90))["commands"]["running"][0]
    late = s.snapshot(LAUNCH + timedelta(seconds=150))["commands"]["running"][0]
    assert early["waiting_on_stdin"] is False
    assert late["waiting_on_stdin"] is True


def test_a_command_with_no_output_is_silent_after_thirty_minutes(tmp_path):
    s = Session(tmp_path).launch("wait-for-it").turn_end().alive().job()
    assert s.snapshot(LAUNCH + timedelta(minutes=29))["commands"]["running"][0]["silent"] is False
    assert s.snapshot(LAUNCH + timedelta(minutes=31))["commands"]["running"][0]["silent"] is True


def test_output_thrown_away_is_never_judged_silent(tmp_path):
    s = Session(tmp_path).launch("make build > /dev/null 2>&1").turn_end().alive().job()
    (command,) = s.snapshot(LAUNCH + timedelta(hours=5))["commands"]["running"]
    assert command["silent"] is False
    assert command["silent_seconds"] is None


def test_an_ended_command_is_not_listed(tmp_path):
    s = _field(tmp_path)
    s.entries.append(notification(LAUNCH + timedelta(hours=1), TASK, "toolu_sh1", "completed",
                                  "done"))
    assert s.snapshot(LAUNCH + timedelta(hours=23))["commands"]["running"] == []


# --- the stalled session ---------------------------------------------------------------------


def test_the_field_session_is_stalled(tmp_path):
    snap = _field(tmp_path).snapshot(LAUNCH + timedelta(hours=23))
    stall = snap["stalled"]
    assert stall["idle_seconds"] == pytest.approx(23 * 3600 - 120, abs=5)
    assert stall["commands"] == 1 and stall["waiting_on_stdin"] == 1


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("active", id="the session acted after its turn ended"),
        pytest.param("no turn end", id="the turn never ended"),
        pytest.param("idle 20 minutes", id="idle under thirty minutes"),
        pytest.param("wakeup", id="a wakeup is pending"),
        pytest.param("monitor", id="a monitor is still running"),
        pytest.param("dead", id="the session process is gone"),
        pytest.param("no session file", id="no live session file"),
        pytest.param("interactive", id="an interactive session"),
    ],
)
def test_a_session_that_is_not_stalled(tmp_path, change):
    s = Session(tmp_path).launch(FIELD)
    s.file("review.md", "")
    s.file("review.log", STDIN_LINE)
    now = LAUNCH + timedelta(hours=23)
    if change != "no turn end":
        s.turn_end()
    if change == "active":
        s.entries.append(s._here(user(None, now - timedelta(minutes=5), "are you there?")))
    if change == "idle 20 minutes":
        now = LAUNCH + timedelta(minutes=22)
    if change == "wakeup":
        s.entries.append(s._here(tool_use(None, now - timedelta(minutes=10), "toolu_w", "ScheduleWakeup",
                                          {"delaySeconds": 3600, "prompt": "check", "reason": "x"})))
    if change == "monitor":
        s.entries.append(s._here(tool_use(None, now - timedelta(minutes=10), "toolu_m", "Monitor",
                                          {"command": "tail -f x", "timeout_ms": 1800000,
                                           "description": "x"})))
    if change == "dead":
        s.alive(pid=2 ** 22 + 12345)  # no such process
    elif change == "interactive":
        s.alive(kind="interactive")
    elif change != "no session file":
        s.alive()
    s.job()
    assert s.snapshot(now).get("stalled") is None


def test_status_text_names_the_stall_and_the_next_step(tmp_path, capsys):
    s = _field(tmp_path)
    s.write()
    from agent_postmortem.redact import Redactor
    text = status.render_text(s.snapshot(LAUNCH + timedelta(hours=23)), Redactor())
    assert "1 running (launched 23h ago, silent 23h, waiting on stdin)" in text
    assert "STALLED" in text and "idle 22h" in text
    assert "</dev/null" in text


def test_a_command_with_no_file_to_watch_is_never_judged_silent(tmp_path):
    # a launch result that names no output file (an older Claude Code) leaves nothing to watch
    s = Session(tmp_path)
    s.entries += [
        s._here(tool_use(None, LAUNCH, "toolu_sh1", "Bash",
                         {"command": "wait-for-it", "run_in_background": True})),
        s._here(tool_result(None, LAUNCH, "toolu_sh1", "Command running in background",
                            toolUseResult={"backgroundTaskId": TASK})),
    ]
    s.turn_end().alive().job()
    (command,) = s.snapshot(LAUNCH + timedelta(hours=5))["commands"]["running"]
    assert command["silent"] is False and command["silent_seconds"] is None


# --- (4) BACKGROUND counts a stalled worker as stalled, not working ---------------------------


def test_background_counts_the_field_worker_as_stalled(tmp_path):
    snap = _field(tmp_path).snapshot(LAUNCH + timedelta(hours=23))
    background = snap["background"]
    assert background["counts"] == {"stalled": 1}
    (row,) = background["sessions"]
    assert row["state"] == "stalled" and row["stall"]["waiting_on_stdin"] == 1


def test_background_text_lists_the_stalled_worker_with_its_next_step(tmp_path):
    from agent_postmortem.redact import Redactor
    s = _field(tmp_path)
    text = status.render_text(s.snapshot(LAUNCH + timedelta(hours=23)), Redactor())
    assert "BACKGROUND 1 session on this machine: 1 stalled" in text
    (row,) = [line for line in text.splitlines() if line.strip().startswith("STALLED  ")
              and "idle" not in line.split()[1]]
    assert "background command" in row and "-> agent-postmortem status " in row


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("job state changed 5 minutes ago", id="a job that just changed"),
        pytest.param("command wrote 5 minutes ago", id="a command still writing"),
        pytest.param("job done", id="a finished job"),
    ],
)
def test_background_keeps_a_worker_that_is_not_stalled(tmp_path, case):
    s = Session(tmp_path).launch(FIELD).turn_end().alive()
    s.file("review.md", "")
    now = LAUNCH + timedelta(hours=23)
    s.file("review.log", STDIN_LINE if case != "command wrote 5 minutes ago" else "working\n",
           at=LAUNCH if case != "command wrote 5 minutes ago" else now - timedelta(minutes=5))
    if case == "job state changed 5 minutes ago":
        s.job(at=now - timedelta(minutes=5))
    elif case == "job done":
        s.job(word="done")
    else:
        s.job()
    counts = s.snapshot(now)["background"]["counts"]
    assert "stalled" not in counts


def test_status_json_shows_task_ids_only_from_local(tmp_path):
    from agent_postmortem.redact import Redactor
    s = _field(tmp_path)
    snap = s.snapshot(LAUNCH + timedelta(hours=23))
    share = json.loads(status.render_json(snap, Redactor()))
    assert "task_id" not in share["commands"]["running"][0]
    assert "task_ids" not in share["stalled"]
    assert "task_ids" not in share["background"]["sessions"][0]["stall"]


# --- (3) scan's NEEDS ATTENTION lists a stalled worker -----------------------------------------


def _scan(tmp_path, monkeypatch, capsys, now, *flags):
    from datetime import timezone

    from agent_postmortem import report
    from agent_postmortem.cli import main
    monkeypatch.setattr(report, "_now", lambda: now.astimezone(timezone.utc))
    code = main(["scan", "--root", str(tmp_path / "projects"), *flags])
    return code, capsys.readouterr().out


def test_scan_lists_the_field_worker_as_stalled(tmp_path, monkeypatch, capsys):
    s = _field(tmp_path)
    s.write()
    _code, out = _scan(tmp_path, monkeypatch, capsys, LAUNCH + timedelta(hours=23))
    public = live.session_public_id(s.main)
    assert "NEEDS ATTENTION (1 of 1 runs)" in out
    assert f"  {public}  idle 22h  STALLED  1 background command silent 23h, waiting on stdin" in out
    assert f"    -> agent-postmortem status {public}" in out


def test_scan_json_carries_the_stalled_worker(tmp_path, monkeypatch, capsys):
    s = _field(tmp_path)
    s.write()
    _code, out = _scan(tmp_path, monkeypatch, capsys, LAUNCH + timedelta(hours=23), "--json")
    (entry,) = json.loads(out)["stalled"]
    assert entry["session"] == live.session_public_id(s.main)
    assert entry["waiting_on_stdin"] == 1 and entry["next_command"].startswith(
        "agent-postmortem status ")


@pytest.mark.parametrize(
    "case,flags",
    [
        pytest.param("job state changed 5 minutes ago", (), id="a worker that is not stalled"),
        pytest.param("stalled", ("--kind", "workflow"), id="workflows only"),
    ],
)
def test_scan_does_not_list_a_worker_that_is_not_stalled(tmp_path, monkeypatch, capsys, case,
                                                        flags):
    now = LAUNCH + timedelta(hours=23)
    s = _field(tmp_path)
    if case == "job state changed 5 minutes ago":
        s.job(at=now - timedelta(minutes=5))
    s.write()
    _code, out = _scan(tmp_path, monkeypatch, capsys, now, *flags)
    assert "STALLED" not in out


def test_a_stalled_session_replaces_its_own_candidate_and_live_entry():
    from types import SimpleNamespace

    from agent_postmortem import cli
    own = {"run": SimpleNamespace(run_id="s_aaaaaaaaaa"), "reason": "1 subagent failed",
           "sort_key": (1,)}
    other = {"run": SimpleNamespace(run_id="s_bbbbbbbbbb"), "reason": "x", "sort_key": (1,)}
    live_now = [{"run": SimpleNamespace(run_id="s_aaaaaaaaaa")}]
    candidates = [own, other]
    stall = {"commands": 1, "waiting_on_stdin": 0, "silent_seconds": 3600}
    extra = cli._fold_stalled(
        [{"session": "s_aaaaaaaaaa", "age_seconds": 4000, "stall": stall},
         {"session": "s_cccccccccc", "age_seconds": 2000, "stall": stall}],
        candidates, live_now, {"s_aaaaaaaaaa", "s_bbbbbbbbbb"})
    assert extra == 1
    assert live_now == []
    stalled = [c for c in candidates if c.get("verdict") == "STALLED"]
    assert [c["run"].run_id for c in stalled] == ["s_aaaaaaaaaa", "s_cccccccccc"]
    assert stalled[0]["reason"].endswith("(also: 1 subagent failed)")
    assert other in candidates and own not in candidates


def test_scan_here_keeps_only_a_worker_in_the_callers_directory(tmp_path, monkeypatch, capsys):
    s = _field(tmp_path)
    s.write()
    state_file = tmp_path / "jobs" / "00000000" / "state.json"
    data = json.loads(state_file.read_text())
    data["cwd"] = str(s.cwd)
    state_file.write_text(json.dumps(data))
    os.utime(state_file, (_at(LAUNCH), _at(LAUNCH)))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    now = LAUNCH + timedelta(hours=23)
    monkeypatch.chdir(elsewhere)
    _code, out = _scan(tmp_path, monkeypatch, capsys, now, "--here")
    assert "STALLED" not in out
    monkeypatch.chdir(s.cwd)
    _code, out = _scan(tmp_path, monkeypatch, capsys, now, "--here")
    assert "STALLED" in out


# --- further cases ------------------------------------------------------------------------------

NOW = LAUNCH + timedelta(hours=23)


def _wake_tool(s: Session, name: str, tool_input: dict, result: dict | None, *, error=False,
               at=None, tool_id=None) -> None:
    at = NOW - timedelta(minutes=10) if at is None else at
    tool_id = tool_id or f"toolu_{name.lower()}{len(s.entries)}"
    s.entries.append(s._here(tool_use(None, at, tool_id, name, tool_input)))
    s.entries.append(s._here(tool_result(None, at, tool_id, "x", is_error=error,
                                         **({"toolUseResult": result} if result else {}))))


def _stalled_field(tmp_path) -> Session:
    """The field shape with its turn end moved past every wake the test adds."""
    s = Session(tmp_path).launch(FIELD).alive().job()
    s.file("review.md", "")
    s.file("review.log", STDIN_LINE)
    return s


def _cron(s, *, error=False, recurring=True, next_fire=None, cron_id="c0000001"):
    fire = (NOW + timedelta(hours=1)) if next_fire is None else next_fire
    _wake_tool(s, "CronCreate", {"cron": "0 * * * *", "prompt": "check", "recurring": recurring},
               None if error else {"id": cron_id, "recurring": recurring,
                                   "nextFireAt": _at(fire) * 1000, "durable": False},
               error=error, at=LAUNCH + timedelta(minutes=1))


def test_a_failed_cron_create_arms_nothing(tmp_path):
    s = _stalled_field(tmp_path)
    _cron(s, error=True)
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is not None


def test_a_failed_cron_delete_leaves_the_cron_armed(tmp_path):
    s = _stalled_field(tmp_path)
    _cron(s)
    _wake_tool(s, "CronDelete", {"id": "c0000001"}, None, error=True,
               at=LAUNCH + timedelta(minutes=1, seconds=30))
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is None


def test_a_deleted_cron_wakes_nothing(tmp_path):
    s = _stalled_field(tmp_path)
    _cron(s)
    _wake_tool(s, "CronDelete", {"id": "c0000001"}, {"id": "c0000001"},
               at=LAUNCH + timedelta(minutes=1, seconds=30))
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is not None


def test_a_one_shot_cron_that_already_fired_wakes_nothing(tmp_path):
    s = _stalled_field(tmp_path)
    _cron(s, recurring=False, next_fire=LAUNCH + timedelta(hours=1))
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is not None


def test_a_failed_monitor_arms_nothing(tmp_path):
    s = _stalled_field(tmp_path)
    s.turn_end()
    # an error result: the Monitor never started; it sits after the turn end, so add a new turn end
    # counted as armed, its hour would still run until 20 minutes from now
    _wake_tool(s, "Monitor", {"command": "x", "timeout_ms": 3600000, "description": "x"}, None,
               error=True, at=NOW - timedelta(minutes=40))
    s.turn_end(at=NOW - timedelta(minutes=40))
    assert s.snapshot(NOW)["stalled"] is not None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("(cd sub && run-it > review.md 2> review.log)", id="a subshell"),
        pytest.param("test -d sub && cd sub; run-it > out.log", id="a conditional cd"),
        pytest.param("cd sub | cat; run-it > out.log", id="a piped cd"),
        pytest.param("run-it > a.log; cd sub && run-more > b.log", id="a later cd"),
    ],
)
def test_outputs_behind_a_directory_change_that_cannot_be_followed_are_not_watched(tmp_path,
                                                                                 command):
    outputs, unknown = live.command_outputs(command, "/tmp/work")
    assert unknown is True
    assert not any(path.endswith(")") for path in outputs)


def test_a_leading_cd_is_followed(tmp_path):
    outputs, unknown = live.command_outputs("cd /tmp/w; run-it > out.log", "/tmp/work")
    assert outputs == ["/tmp/w/out.log"] and unknown is False


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"type": "queue-operation", "operation": "enqueue", "content": "hello"},
                     id="a queued message"),
        pytest.param({"type": "attachment", "attachment": {"type": "task_reminder"}},
                     id="an attachment"),
    ],
)
def test_an_entry_after_the_turn_end_read_from_the_transcript_is_activity(tmp_path, entry):
    s = _field(tmp_path)
    s.entries.append({**entry, "sessionId": FAKE_UUID, "timestamp": iso(NOW - timedelta(minutes=5))})
    assert s.snapshot(NOW)["stalled"] is None


def test_a_session_whose_job_is_no_longer_working_is_not_stalled(tmp_path):
    # a stale session file whose pid now belongs to another process: the job settled
    s = Session(tmp_path).launch(FIELD).turn_end().alive().job(word="failed")
    s.file("review.md", "")
    s.file("review.log", STDIN_LINE)
    assert s.snapshot(NOW)["stalled"] is None


def test_a_launch_result_naming_no_output_file_leaves_redirects_unjudged(tmp_path):
    s = Session(tmp_path)
    s.file("err.log", "progress\n")
    s.entries += [
        s._here(tool_use(None, LAUNCH, "toolu_sh1", "Bash",
                         {"command": "run-it 2> err.log", "run_in_background": True})),
        s._here(tool_result(None, LAUNCH, "toolu_sh1", "Command running in background",
                            toolUseResult={"backgroundTaskId": TASK})),
    ]
    s.turn_end().alive().job()
    (command,) = s.snapshot(NOW)["commands"]["running"]
    assert command["silent"] is False and command["silent_seconds"] is None


@pytest.mark.parametrize("case", ["missing", "a directory"])
def test_an_output_that_cannot_be_inspected_leaves_silence_unjudged(tmp_path, case):
    s = Session(tmp_path).launch("run-it > out.log 2> err.log").turn_end().alive().job()
    s.file("err.log", "old\n")
    if case == "a directory":
        (s.cwd / "out.log").mkdir()
    (command,) = s.snapshot(NOW)["commands"]["running"]
    assert command["silent"] is False and command["silent_seconds"] is None


def test_background_reads_no_job_whose_transcript_changed_recently(tmp_path, monkeypatch):
    calls = []
    real = status.snapshot
    monkeypatch.setattr(status, "snapshot", lambda *a, **k: calls.append(1) or real(*a, **k))
    s = _field(tmp_path)
    s.write()
    os.utime(s.main, (_at(NOW - timedelta(minutes=5)),) * 2)
    status._background(tmp_path / "jobs", tmp_path / "projects", _at(NOW))
    assert calls == []


def test_background_reads_at_most_a_few_jobs_per_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(status, "snapshot", lambda *a, **k: calls.append(1) or {"stalled": None})
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {f"s{n}": "bg" for n in range(12)})
    projects = tmp_path / "projects" / SLUG
    projects.mkdir(parents=True)
    for n in range(12):
        session = f"s{n}"
        (projects / f"{session}.jsonl").write_text("{}\n")
        os.utime(projects / f"{session}.jsonl", (_at(LAUNCH),) * 2)
        job = tmp_path / "jobs" / f"j{n}"
        job.mkdir(parents=True)
        (job / "state.json").write_text(json.dumps({"state": "working", "sessionId": session}))
        os.utime(job / "state.json", (_at(LAUNCH),) * 2)
    status._background(tmp_path / "jobs", tmp_path / "projects", _at(NOW))
    assert 0 < len(calls) <= status._JOB_STALL_READS


@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param("run $(cat p) > a.log", ["/tmp/work/a.log"], id="an unquoted substitution"),
        pytest.param("{ cd sub; run > a.log; }", None, id="a brace group"),
    ],
)
def test_only_a_group_or_subshell_hides_every_output(command, expected):
    outputs, unknown = live.command_outputs(command, "/tmp/work")
    if expected is None:
        assert outputs == [] and unknown is True
    else:
        assert outputs == expected and unknown is False


# --- further cases ------------------------------------------------------------------------------


def _jobs(tmp_path, n: int, *, cwd: str | None = None) -> None:
    projects = tmp_path / "projects" / SLUG
    projects.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        session = f"s{i}"
        (projects / f"{session}.jsonl").write_text("{}\n")
        os.utime(projects / f"{session}.jsonl", (_at(LAUNCH),) * 2)
        job = tmp_path / "jobs" / f"j{i}"
        job.mkdir(parents=True, exist_ok=True)
        data = {"state": "working", "sessionId": session}
        if cwd is not None:
            data["cwd"] = cwd
        (job / "state.json").write_text(json.dumps(data))
        os.utime(job / "state.json", (_at(LAUNCH),) * 2)


def test_status_reaches_every_eligible_job_across_calls(tmp_path, monkeypatch):
    seen: set[str] = set()
    monkeypatch.setattr(status, "snapshot",
                        lambda main, **k: seen.add(main.stem) or {"stalled": None})
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {f"s{n}": "bg" for n in range(6)})
    _jobs(tmp_path, 6)
    for minute in range(6):
        status._background(tmp_path / "jobs", tmp_path / "projects", _at(NOW) + 60 * minute)
    assert seen == {f"s{n}" for n in range(6)}


def test_scan_reads_every_eligible_job(tmp_path, monkeypatch):
    seen: set[str] = set()
    monkeypatch.setattr(status, "snapshot",
                        lambda main, **k: seen.add(main.stem) or {"stalled": None})
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {f"s{n}": "bg" for n in range(8)})
    _jobs(tmp_path, 8)
    from agent_postmortem import cli
    cli._attention_sessions(tmp_path / "projects", _at(NOW), None, None)
    assert seen == {f"s{n}" for n in range(8)}


def test_scan_here_filters_jobs_before_reading_them(tmp_path, monkeypatch):
    seen: set[str] = set()
    monkeypatch.setattr(status, "snapshot",
                        lambda main, **k: seen.add(main.stem) or {"stalled": None})
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {f"s{n}": "bg" for n in range(3)})
    _jobs(tmp_path, 3, cwd="/tmp/elsewhere")
    from agent_postmortem import cli
    cli._attention_sessions(tmp_path / "projects", _at(NOW), None, "/tmp/here")
    assert seen == set()


def test_a_persistent_monitor_stops_arming_once_it_ends(tmp_path):
    s = _stalled_field(tmp_path)
    _wake_tool(s, "Monitor", {"command": "tail -f x", "description": "x"},
               {"taskId": "m0000001", "timeoutMs": 0, "persistent": True},
               at=LAUNCH + timedelta(minutes=1), tool_id="toolu_mon")
    s.entries.append(notification(LAUNCH + timedelta(minutes=1, seconds=30), "m0000001",
                                  "toolu_mon", "completed", "Monitor ended"))
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is not None


def test_a_running_persistent_monitor_still_arms(tmp_path):
    s = _stalled_field(tmp_path)
    _wake_tool(s, "Monitor", {"command": "tail -f x", "description": "x"},
               {"taskId": "m0000001", "timeoutMs": 0, "persistent": True},
               at=LAUNCH + timedelta(minutes=1), tool_id="toolu_mon")
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is None


def test_facts_of_an_earlier_version_six_are_read_again():
    assert live.MAIN_FACTS_VERSION >= 8


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("if test -d sub; then cd sub; fi; run-it > out.log", id="cd inside if"),
        pytest.param("builtin cd sub && run-it > out.log", id="builtin cd"),
        pytest.param("for d in a b; do run-it > out.log; done", id="a loop"),
    ],
)
def test_control_flow_and_wrapped_directory_changes_are_not_watched(command):
    outputs, unknown = live.command_outputs(command, "/tmp/work")
    assert unknown is True and "/tmp/work/out.log" not in outputs


def test_a_transcript_that_could_not_be_read_now_is_never_stalled(tmp_path, monkeypatch):
    s = _field(tmp_path)
    assert s.snapshot(NOW)["stalled"] is not None  # read once: its facts are in the live store
    real = live.SessionFollower._read

    def failing(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        self.read_failed = True
        return result

    monkeypatch.setattr(live.SessionFollower, "_read", failing)
    assert status.snapshot(s.main, now=lambda: _at(NOW))["stalled"] is None


def test_a_folded_stalled_session_keeps_its_caller_marker(monkeypatch):
    from types import SimpleNamespace

    from agent_postmortem import cli
    own = {"run": SimpleNamespace(run_id="s_aaaaaaaaaa"), "reason": "x", "sort_key": (1,),
           "mark": "this session"}
    candidates = [own]
    stall = {"commands": 1, "waiting_on_stdin": 0, "silent_seconds": 3600}
    cli._fold_stalled([{"session": "s_aaaaaaaaaa", "session_id": FAKE_UUID, "age_seconds": 4000,
                        "stall": stall}], candidates, [], {"s_aaaaaaaaaa"})
    assert candidates[0]["mark"] == "this session"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", FAKE_UUID)
    fresh: list = []
    cli._fold_stalled([{"session": "s_aaaaaaaaaa", "session_id": FAKE_UUID, "age_seconds": 4000,
                        "stall": stall}], fresh, [], set())
    assert fresh[0]["mark"] == "this session"


# --- further cases -------------------------------------


@pytest.mark.parametrize(
    "command,unknown,outputs",
    [
        pytest.param("time (cd sub && run-it > out.log )", True, [], id="a subshell after a prefix"),
        pytest.param("run-it >& progress.log", False, ["/tmp/work/progress.log"],
                     id="the >& file spelling of &>"),
        pytest.param("run-it > a.log 2>&1", False, ["/tmp/work/a.log"], id="a dup is no output"),
    ],
)
def test_round_three_output_shapes(command, unknown, outputs):
    found, flagged = live.command_outputs(command, "/tmp/work")
    assert flagged is unknown and found == outputs


def test_evicted_bash_call_remains_unwatchable():
    facts = live.MainFacts()
    for n in range(65):
        facts._bash_call(str(n), {"command": "run-it > /tmp/progress.log"}, "/tmp", float(n))
    facts._shell_result("0", {"content": "Output is being written to: /tmp/task.output."},
                        {"backgroundTaskId": "b00000001"}, 65.0)
    assert facts.shells["b00000001"].get("unknown_outputs") is True


def _persistent_monitor_launch(s, *, end_first=False, end_tool="toolu_mon"):
    use_at = LAUNCH + timedelta(minutes=1)
    s.entries.append(s._here(tool_use(None, use_at, "toolu_mon", "Monitor",
                                      {"command": "tail -f x", "description": "x"})))
    end = notification(use_at + timedelta(seconds=1), "m0000001", end_tool, "completed", "ended")
    if end_first:
        s.entries.append(end)
    s.entries.append(s._here(tool_result(None, use_at + timedelta(seconds=2), "toolu_mon", "x",
                                         toolUseResult={"taskId": "m0000001", "timeoutMs": 0,
                                                        "persistent": True})))
    if not end_first:
        s.entries.append(end)


def test_a_notice_naming_another_tool_use_id_does_not_retire_a_monitor(tmp_path):
    s = _stalled_field(tmp_path)
    _persistent_monitor_launch(s, end_tool="toolu_other")
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is None


def test_a_monitor_end_written_before_its_launch_result_retires_it(tmp_path):
    s = _stalled_field(tmp_path)
    _persistent_monitor_launch(s, end_first=True)
    s.turn_end()
    assert s.snapshot(NOW)["stalled"] is not None


def test_a_line_still_being_written_holds_back_a_stall(tmp_path):
    s = _field(tmp_path)
    s.write()
    assert status.snapshot(s.main, now=lambda: _at(NOW))["stalled"] is not None
    with s.main.open("a", encoding="utf-8") as handle:  # a user entry, its newline not yet written
        handle.write(json.dumps(user(None, NOW - timedelta(minutes=1), "hello")))
    assert status.snapshot(s.main, now=lambda: _at(NOW))["stalled"] is None


# --- further cases -------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param("time(cd sub && run-it > out.log )", ([], True), id="an unspaced subshell"),
        pytest.param("printf marker >&1-", ([], False), id="a descriptor move"),
        pytest.param('run-it "(not a group)" > a.log', (["/tmp/work/a.log"], False),
                     id="a quoted paren"),
        pytest.param("run-it $(cat p) > a.log", (["/tmp/work/a.log"], False),
                     id="a substitution"),
    ],
)
def test_round_four_output_shapes(command, expected):
    assert live.command_outputs(command, "/tmp/work") == expected


def test_round_three_invalidates_v7_stall_facts():
    from types import SimpleNamespace

    path = Path("/tmp/session.jsonl")
    follower = SimpleNamespace(
        main_path=path, session_dir=path.with_suffix(""),
        main_tail=live.Tail(path), main=live.MainFacts(), subs={})
    old = {"shells": {"b00000001": {
        "tool": "toolu_sh", "ts": 0.0, "end": None, "outputs": ["/tmp/task.output"]}}}
    status._restore(follower, {"transcripts": {str(path): {
        "facts_version": 7, "fork_line": 0, "offset": 10, "lines": 1, "size": 10,
        "facts": old}}})
    assert follower.main_tail.offset == 0


def test_status_says_why_a_command_ending_in_tail_is_silent(tmp_path):
    """Field report 2026-11-05."""
    s = Session(tmp_path).launch("render-the-video 2>&1 | tail -n 20").turn_end().alive().job()
    snap = s.snapshot(LAUNCH + timedelta(minutes=31))
    assert snap["commands"]["running"][0]["end_only"] == "tail"
    from agent_postmortem.redact import Redactor
    text = status.render_text(snap, Redactor())
    assert "silent 31m, tail prints only at its end)" in text, text
