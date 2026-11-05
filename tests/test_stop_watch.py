"""`hook stop-watch` wakes a session that has gone idle (docs/DESIGN.md 4.14.5; field reports
2026-11-04). The per-call hook needs a tool call, and the manager of the field report made none
all night. The worst failure here is a session woken over and over, so most of these are about
when it must not wake.

The clock and the sleep are passed in; job files and the transcript are real, under tmp_path.
Synthetic data only.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

import pytest
from live_helpers import FAKE_UUID, assistant_text, user
from test_worker_notice import LAUNCH, PERMISSION, SHORT, Manager, _ago, _hook, _stdout

from agent_postmortem import hooks_cli, live, stalls, state, watcher, workers

VERSION = "2.1.318"


def _versioned(m: Manager, version: str | None = VERSION) -> None:
    """A last turn, with the version Claude Code writes on each entry."""
    entries = [*m.entries, assistant_text(None, _ago(30), "done for now")]
    for entry in entries:
        entry.pop("version", None)
        if version is not None:
            entry["version"] = version
    m.entries = []
    m.append(*entries)


def _manager(tmp_path, monkeypatch, capsys, *, job: dict | None = None) -> Manager:
    """A manager whose worker's launch the per-call hook has seen, ending its turn."""
    m = Manager(tmp_path)
    m.launched(job=job)
    _versioned(m)
    assert _hook(monkeypatch, capsys, m) == ""
    return m


def _stop_payload(m: Manager, **extra) -> dict:
    return {"hook_event_name": "Stop", "session_id": FAKE_UUID, "transcript_path": str(m.main),
            "background_tasks": [], **extra}


class Life:
    """Runs one watcher against a script of what happens during each sleep."""

    def __init__(self, m: Manager, events=None, *, max_polls: int = 200):
        self.m = m
        self.events = dict(events or {})
        self.polls = 0
        self.max_polls = max_polls
        self.now = time.time()
        self.parent = 4242

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds == watcher.POLL_SECONDS
        self.polls += 1
        assert self.polls <= self.max_polls, "the watcher never left"
        self.now += seconds
        event = self.events.pop(self.polls, None)
        if event is not None:
            event()

    def run(self, **extra) -> int:
        return watcher.watch(_stop_payload(self.m, **extra), clock=self.clock, sleep=self.sleep,
                             parent=lambda: self.parent)


def _still_up(capsys, life: Life, polls: int = 3) -> None:
    """The watcher is told to leave after `polls` polls: nothing woke it before."""
    life.events[polls] = lambda: setattr(life, "parent", 1)
    assert life.run() == 0
    assert capsys.readouterr().err == ""


# --- it wakes ----------------------------------------------------------------------------------


def test_an_idle_manager_is_woken_when_its_worker_waits_on_a_permission(
        tmp_path, monkeypatch, capsys):
    """Acceptance B: the field night. Nothing armed, the session idle."""
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m, {3: lambda: m.job(state="blocked", needs=PERMISSION, mtime_ago=10)})
    assert life.run() == 2
    err = capsys.readouterr().err
    assert f"WORKER night-run ({SHORT}) blocked" in err and "approve Entering worktree" in err
    assert f"claude attach {SHORT}" in err and "not an error" in err and len(err) <= 401
    assert life.polls == 3


def test_a_live_prompt_with_the_state_word_working_wakes_it(tmp_path, monkeypatch, capsys):
    """The first live run: the worker read `state: working, tempo: blocked`."""
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m, {2: lambda: m.job(state="working", tempo="blocked",
                                     needs="approve Write: /tmp/zz-private", mtime_ago=10)})
    assert life.run() == 2
    err = capsys.readouterr().err
    assert "(approve Write)" in err and "zz-private" not in err, err


def test_the_line_is_claimed_so_the_per_call_hook_does_not_repeat_it(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    assert Life(m, {1: lambda: m.job(state="blocked", needs=PERMISSION, mtime_ago=10)}).run() == 2
    capsys.readouterr()
    assert _hook(monkeypatch, capsys, m) == ""


def test_the_same_prompt_coming_back_after_a_wake_is_told_again(tmp_path, monkeypatch, capsys):
    """The gate must see what the watcher told, or `working` reads as nothing new to it."""
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=10)
    assert Life(m).run() == 2
    capsys.readouterr()
    m.job(state="working", tempo="active", mtime_ago=1)
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=1)
    assert PERMISSION in _hook(monkeypatch, capsys, m)


def test_a_line_the_per_call_hook_told_is_not_woken_for(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    assert PERMISSION in _hook(monkeypatch, capsys, m)
    _still_up(capsys, Life(m))


def test_a_worker_blocked_before_the_turn_ended_is_told_at_once(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    life = Life(m)
    assert life.run() == 2 and life.polls == 0


@pytest.mark.parametrize(
    "fields",
    [{"state": "blocked", "needs": "awaiting input"}, {"state": "failed"},
     {"state": "blocked", "detail": "API Error: 500"}],
)
def test_every_trouble_wakes(tmp_path, monkeypatch, capsys, fields):
    m = _manager(tmp_path, monkeypatch, capsys)
    assert Life(m, {1: lambda: m.job(mtime_ago=10, **fields)}).run() == 2


# --- a delivered worker ------------------------------------------------------------------------


def test_a_delivered_worker_wakes_only_after_its_grace(tmp_path, monkeypatch, capsys):
    """Its own message usually wakes the manager first; one that never reports is still caught."""
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m, {1: lambda: m.job(state="done", output={"result": "r"}, mtime_ago=30)})
    real = workers.view

    def aged(short, path, data, now, printed=None):
        found = real(short, path, data, now, printed)
        found["age"] = 30 + (life.now - start)
        return found

    start = life.now
    monkeypatch.setattr(workers, "view", aged)
    assert life.run() == 2
    assert 4 <= life.polls <= 7, life.polls
    err = capsys.readouterr().err
    assert "finished" in err and "blocked" not in err


def test_a_delivered_worker_is_told_once(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="done", output={"result": "r"}, mtime_ago=300)
    assert Life(m).run() == 2
    capsys.readouterr()
    assert Life(m).run() == 0  # nothing left to watch
    assert capsys.readouterr().err == ""


def test_a_worker_on_a_schedule_wakes_the_session_at_most_once(tmp_path, monkeypatch, capsys):
    """Otherwise an hourly cron in a worker wakes an idle manager every hour: money."""
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="done", output={"result": "r"}, mtime_ago=300)
    assert Life(m).run() == 2
    capsys.readouterr()
    m.job(state="working", tempo="active", mtime_ago=1)
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="done", tempo="idle", output={"result": "r2"}, mtime_ago=300)
    life = Life(m)
    life.now += watcher.WAKE_COOLDOWN_SECONDS + 1
    assert life.run() == 0  # nothing to wake for, and nothing left to watch
    assert capsys.readouterr().err == ""
    assert "finished again" in _hook(monkeypatch, capsys, m)  # told at the next call


def test_the_session_acting_first_takes_the_delivered_line(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="done", output={"result": "r"}, mtime_ago=5)
    life = Life(m)
    life.events[1] = lambda: m.append(user(None, _ago(-3600), "the worker's message arrived"))
    assert life.run() == 0
    assert capsys.readouterr().err == ""


# --- it leaves ---------------------------------------------------------------------------------


def test_with_nothing_to_watch_it_leaves_at_once_and_imports_nothing_heavy(tmp_path):
    m = Manager(tmp_path)
    _versioned(m)
    payload = json.dumps(_stop_payload(m))
    code = ("import sys, json\n"
            "from agent_postmortem import entry\n"
            "rc = entry.main(['hook', 'stop-watch'])\n"
            "heavy = {'agent_postmortem.live', 'agent_postmortem.cli',\n"
            "         'agent_postmortem.hooks_cli'} & set(sys.modules)\n"
            "print(rc, sorted(heavy))\n")
    env = {**os.environ, "PYTHONPATH": "src"}
    run = subprocess.run([sys.executable, "-c", code], input=payload, text=True,
                         capture_output=True, env=env, timeout=30, check=True)
    assert run.stdout.strip() == "0 []", run.stdout + run.stderr


def test_the_session_acting_again_ends_it(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m)
    life.events[2] = lambda: m.append(user(None, _ago(-3600), "next prompt"))
    assert life.run() == 0 and life.polls == 2


def test_a_final_message_flushed_late_is_not_the_session_acting(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m)
    life.events[1] = lambda: m.append(assistant_text(None, _ago(5), "the last words"))
    life.events[4] = lambda: m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    assert life.run() == 2 and life.polls == 4


def test_a_line_still_being_written_is_read_when_whole(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m)
    whole = json.dumps(user(None, _ago(-3600), "next prompt")) + "\n"

    def half():
        with m.main.open("a") as handle:
            handle.write(whole[:20])

    def rest():
        with m.main.open("a") as handle:
            handle.write(whole[20:])

    life.events[1] = half
    life.events[3] = rest
    assert life.run() == 0 and life.polls == 3


def test_its_parent_gone_ends_it(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m)
    life.events[2] = lambda: setattr(life, "parent", 1)
    assert life.run() == 0 and life.polls == 2


def test_a_newer_watcher_takes_over(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)

    def newer():
        with state.cache_transaction(state.live_store_path()) as data:
            record = live._store_session(data, live.session_key(m.main))
            record["watcher"] = {"token": "someone-newer", "start": time.time()}
        m.job(state="blocked", needs=PERMISSION, mtime_ago=10)

    life = Life(m, {2: newer})
    assert life.run() == 0
    assert capsys.readouterr().err == ""


def test_all_workers_finished_and_told_leaves(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="stopped", mtime_ago=5)
    life = Life(m)
    assert life.run() == 0 and life.polls <= 1


def test_a_worker_blocked_for_days_and_already_told_is_not_watched(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    assert PERMISSION in _hook(monkeypatch, capsys, m)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=4 * 86400)
    assert _hook(monkeypatch, capsys, m) == ""
    assert _hook(monkeypatch, capsys, m) == ""
    life = Life(m)
    assert life.run() == 0 and life.polls == 0


def test_at_the_deadline_it_wakes_once_and_says_so(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m)
    life.events[1] = lambda: setattr(life, "now", life.now + watcher.DEADLINE_SECONDS)
    assert life.run() == 2
    err = capsys.readouterr().err
    assert "after 14 days" in err and "End your turn" in err


# --- it must not wake --------------------------------------------------------------------------


def test_off_by_environment(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    monkeypatch.setenv("AGENT_POSTMORTEM_WAKE", "0")
    assert Life(m).run() == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("version", ["2.1.313", "1.9.999", None])
def test_an_older_or_unknown_claude_code_gets_no_watcher(tmp_path, monkeypatch, capsys, version):
    """A version that ignores asyncRewake would run this as a blocking Stop hook."""
    m = Manager(tmp_path)
    m.launched()
    _versioned(m, version)
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    life = Life(m)
    assert life.run() == 0 and life.polls == 0


def test_a_subagents_stop_gets_no_watcher(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    assert Life(m).run(agent_id="a0000000000000000") == 0


def test_no_recorded_claim_means_no_wake(tmp_path, monkeypatch, capsys):
    """The opposite of the per-call hook's rule: a claim that cannot be written must not wake,
    or the next watcher wakes the session again for the same line."""
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    real = state.cache_transaction
    broken = {"on": True}

    def transaction(path):
        if broken["on"]:
            raise state.StateLocked("busy")
        return real(path)

    monkeypatch.setattr(state, "cache_transaction", transaction)
    life = Life(m, {3: lambda: broken.update(on=False)})
    assert life.run() == 2
    assert life.polls == 3
    capsys.readouterr()


def test_at_most_one_wake_per_five_minutes_and_the_line_waits(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    first = Life(m)
    assert first.run() == 2
    capsys.readouterr()
    m.job(state="blocked", needs="approve Bash", mtime_ago=1)
    second = Life(m)
    second.now = first.now + 60
    assert second.run() == 2
    assert second.polls == 12, second.polls  # 240 s more of the 300
    assert "(approve Bash)" in capsys.readouterr().err


def test_any_error_exits_zero_never_two(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)

    def boom(*args, **kwargs):
        raise RuntimeError("zz-private")

    monkeypatch.setattr(watcher, "watch", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_stop_payload(m))))
    assert hooks_cli.dispatch("stop-watch") == 0
    assert "zz-private" not in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hooks_cli.dispatch("stop-watch") == 0


def test_a_kill_before_the_claim_is_written_repeats_rather_than_loses(
        tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    real = state._write_cache

    def killed(path, data):
        if any("last_wake" in record for record in data.get("sessions", {}).values()):
            raise KeyboardInterrupt
        return real(path, data)

    monkeypatch.setattr(state, "_write_cache", killed)
    with pytest.raises(KeyboardInterrupt):
        Life(m).run()
    assert PERMISSION in capsys.readouterr().err
    monkeypatch.setattr(state, "_write_cache", real)
    assert Life(m).run() == 2
    assert PERMISSION in capsys.readouterr().err


# --- background commands -----------------------------------------------------------------------


def _with_silent_command(m: Manager, tmp_path, *, last_line: str = "", ago: float = 40 * 60):
    from test_worker_rows import _silent_command

    _silent_command(m, tmp_path, "make long", ago=ago, last_line=last_line)


TASK = {"id": "b00000008", "type": "shell", "status": "running", "command": "make long"}


def _bg_session(monkeypatch, *, kind: str = "bg") -> None:
    monkeypatch.setattr(stalls, "live_sessions", lambda root: {FAKE_UUID: kind})
    monkeypatch.setenv(watcher.PLUGIN_MARK, "1")  # what the plugin's run.sh sets


def test_a_silent_command_wakes_a_background_session(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _with_silent_command(m, tmp_path)
    _versioned(m)
    _bg_session(monkeypatch)
    assert Life(m).run(background_tasks=[TASK]) == 2
    assert "COMMAND b00000008" in capsys.readouterr().err


def test_a_silent_command_does_not_wake_an_interactive_session(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _with_silent_command(m, tmp_path)
    _versioned(m)
    _bg_session(monkeypatch, kind="interactive")
    assert Life(m).run(background_tasks=[TASK]) == 0
    assert capsys.readouterr().err == ""


def test_a_wait_on_stdin_wakes_an_interactive_session_too(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _with_silent_command(m, tmp_path, last_line="Reading additional input from stdin...\n", ago=300)
    _versioned(m)
    _bg_session(monkeypatch, kind="interactive")
    assert Life(m).run(background_tasks=[TASK]) == 2
    assert "waits on stdin" in capsys.readouterr().err


def test_a_command_goes_silent_while_it_watches(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _with_silent_command(m, tmp_path, ago=29 * 60)
    _versioned(m)
    _bg_session(monkeypatch)
    real = stalls.running_commands
    life = Life(m)

    def later(main, now):  # the command's files keep their time; the clock passes the mark
        return real(main, time.time() + (now - start))

    start = life.now
    monkeypatch.setattr(stalls, "running_commands", later)
    assert life.run(background_tasks=[TASK]) == 2
    assert 2 <= life.polls <= 5, life.polls
    assert "COMMAND b00000008" in capsys.readouterr().err


def test_a_silent_command_told_by_the_per_call_hook_does_not_wake(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _with_silent_command(m, tmp_path)
    _versioned(m)
    _bg_session(monkeypatch)
    assert "COMMAND b00000008" in _hook(monkeypatch, capsys, m)
    assert Life(m).run(background_tasks=[TASK]) == 0
    assert capsys.readouterr().err == ""


# --- the Stop-time deadline request ------------------------------------------------------------


def _stop_check(m: Manager, capsys, monkeypatch, tasks: list[dict]) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        _stop_payload(m, background_tasks=tasks, session_crons=[]))))
    assert hooks_cli.dispatch("check") == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_the_deadline_request_is_retired_where_the_watcher_covers_it(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _versioned(m)
    _bg_session(monkeypatch)
    assert _stop_check(m, capsys, monkeypatch, [TASK]) == ""


@pytest.mark.parametrize(
    "case",
    ["interactive", "off", "old"],
)
def test_the_deadline_request_stays_where_the_watcher_does_not_cover_it(
        tmp_path, monkeypatch, capsys, case):
    m = Manager(tmp_path)
    _versioned(m, "2.1.300" if case == "old" else VERSION)
    _bg_session(monkeypatch, kind="interactive" if case == "interactive" else "bg")
    if case == "off":
        monkeypatch.setenv("AGENT_POSTMORTEM_WAKE", "0")
    assert "DEADLINE b00000008" in _stop_check(m, capsys, monkeypatch, [TASK])


# --- the launch registers it for the watcher too -------------------------------------------------


def test_a_launch_in_the_last_call_of_the_turn_is_watched(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _versioned(m)
    m.job(state="working", created_ago=2, mtime_ago=1)
    assert _hook(monkeypatch, capsys, m, m.payload(LAUNCH, _stdout(SHORT))) == ""
    life = Life(m, {1: lambda: m.job(state="blocked", needs=PERMISSION, created_ago=2,
                                     mtime_ago=1)})
    assert life.run() == 2
    assert PERMISSION in capsys.readouterr().err


# --- privacy -----------------------------------------------------------------------------------


def test_the_wake_prints_none_of_the_workers_words(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs="approve Bash: zz-private-needs", detail="zz-private-detail",
          output={"result": "zz-private-result"}, mtime_ago=10)
    assert Life(m).run() == 2
    err = capsys.readouterr().err
    assert "(approve Bash)" in err and "zz-private" not in err, err


# --- review round 1 of this feature --------------------------------------------------------------


def test_a_claim_that_fails_at_its_commit_does_not_wake(tmp_path, monkeypatch, capsys):
    """stderr is written inside the transaction, so the commit can still fail after it. No record
    means the next watcher would wake for the same line: exit 0 and tell it then."""
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    real = state._write_cache
    broken = {"on": True}

    def write(path, data):
        if broken["on"] and any("last_wake" in r for r in data.get("sessions", {}).values()):
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(state, "_write_cache", write)
    life = Life(m)
    life.events[2] = lambda: setattr(life, "parent", 1)
    assert life.run() == 0
    capsys.readouterr()
    broken["on"] = False
    assert Life(m).run() == 2
    assert PERMISSION in capsys.readouterr().err


def test_a_line_the_per_call_hook_won_after_the_poll_read_does_not_wake(
        tmp_path, monkeypatch, capsys):
    """Who is due is decided again inside the claim's lock."""
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="blocked", needs=PERMISSION, mtime_ago=10)
    data = workers.read_job(workers.job_file(m.jobs, SHORT))
    real = watcher._Watch._claim

    def raced(self, *args, **kwargs):
        with state.cache_transaction(state.live_store_path()) as store:
            record = live._store_session(store, live.session_key(m.main))
            record["workers"] = {SHORT: workers.identity(data)}
        return real(self, *args, **kwargs)

    monkeypatch.setattr(watcher._Watch, "_claim", raced)
    life = Life(m)
    life.events[2] = lambda: setattr(life, "parent", 1)
    assert life.run() == 0
    assert capsys.readouterr().err == ""


def test_the_version_is_the_entrys_own_not_one_nested_in_a_tool_input(tmp_path):
    path = tmp_path / "t.jsonl"
    entry = {"type": "assistant", "version": "2.1.313", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"version": "9.0.0"}}]}}
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert watcher._version_ok(str(path)) is False
    entry["version"] = "2.1.318"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert watcher._version_ok(str(path)) is True


def test_a_watcher_already_orphaned_leaves_at_once(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m)
    life.parent = 1
    assert life.run() == 0 and life.polls == 0


def test_a_removed_job_keeps_no_watcher_alive(tmp_path, monkeypatch, capsys):
    import shutil

    m = _manager(tmp_path, monkeypatch, capsys)
    shutil.rmtree(m.jobs / SHORT)
    life = Life(m)
    assert life.run() == 0 and life.polls <= 1


def _at_deadline(life: Life) -> None:
    life.events[1] = lambda: setattr(life, "now", life.now + watcher.DEADLINE_SECONDS)


def test_the_deadline_wake_respects_a_newer_owner(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    real = watcher._Watch._last_wake

    def taken(self, *args, **kwargs):
        with state.cache_transaction(state.live_store_path()) as store:
            record = live._store_session(store, live.session_key(m.main))
            record["watcher"] = {"token": "someone-newer", "start": time.time()}
        return real(self, *args, **kwargs)

    monkeypatch.setattr(watcher._Watch, "_last_wake", taken)
    life = Life(m)
    _at_deadline(life)
    assert life.run() == 0
    assert capsys.readouterr().err == ""


def test_the_deadline_wake_waits_out_the_cooldown_and_counts_as_a_wake(
        tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    life = Life(m)

    def deadline_right_after_a_wake():
        life.now += watcher.DEADLINE_SECONDS
        with state.cache_transaction(state.live_store_path()) as store:
            record = live._store_session(store, live.session_key(m.main))
            record["last_wake"] = life.now - 10
    life.events[1] = deadline_right_after_a_wake
    assert life.run() == 2
    assert life.polls >= 15, life.polls  # 290 s more
    assert "after 14 days" in capsys.readouterr().err
    record = live._store_session(state.read_cache(state.live_store_path()),
                                 live.session_key(m.main))
    assert record["last_wake"] >= life.now - 25


def test_a_renewed_watcher_says_so_again_at_its_own_deadline(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    first = Life(m)
    _at_deadline(first)
    assert first.run() == 2
    capsys.readouterr()
    second = Life(m)
    second.now = first.now + 7200
    second.events[1] = lambda: setattr(second, "now", second.now + watcher.DEADLINE_SECONDS)
    assert second.run() == 2
    assert "after 14 days" in capsys.readouterr().err


# --- review round 2 of this feature --------------------------------------------------------------


def test_a_job_removed_during_the_cooldown_wakes_nothing(tmp_path, monkeypatch, capsys):
    import shutil

    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="working", tempo="blocked", needs=PERMISSION, mtime_ago=10)
    with state.cache_transaction(state.live_store_path()) as store:
        record = live._store_session(store, live.session_key(m.main))
        record["last_wake"] = time.time()
    life = Life(m)
    life.events[2] = lambda: shutil.rmtree(m.jobs / SHORT)
    assert life.run() == 0
    assert capsys.readouterr().err == ""


def test_one_failed_stat_does_not_end_the_watch(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    real = workers.job_signature
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return None if calls["n"] <= 6 else real(path)

    monkeypatch.setattr(workers, "job_signature", flaky)
    monkeypatch.setattr(workers, "job_gone", lambda path: False)
    life = Life(m, {4: lambda: m.job(state="working", tempo="blocked", needs=PERMISSION,
                                     mtime_ago=1)})
    assert life.run() == 2
    assert PERMISSION in capsys.readouterr().err


# --- review round 3 of this feature --------------------------------------------------------------


def test_a_decision_after_a_delivery_still_wakes(tmp_path, monkeypatch, capsys):
    """Only a repeat of an ordinary finish is held back: this one asks for something."""
    m = _manager(tmp_path, monkeypatch, capsys)
    m.job(state="done", output={"result": "r"}, mtime_ago=300)
    assert Life(m).run() == 2
    capsys.readouterr()
    m.job(state="working", tempo="active", mtime_ago=1)
    assert _hook(monkeypatch, capsys, m) == ""
    m.job(state="blocked", tempo="idle", needs="choose option", output={"result": "report"},
          mtime_ago=130)
    life = Life(m)
    life.now += watcher.WAKE_COOLDOWN_SECONDS + 1
    assert life.run() == 2
    assert "delivered" in capsys.readouterr().err


def test_the_watcher_reads_a_session_of_any_size_for_a_stall(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    seen = []
    real = hooks_cli._worker_stall

    def spy(follower, check, now, started, **kwargs):
        seen.append(check.stall_any_size)
        return real(follower, check, now, started, **kwargs)

    monkeypatch.setattr(hooks_cli, "_worker_stall", spy)
    life = Life(m)
    life.events[1] = lambda: setattr(life, "parent", 1)
    assert life.run() == 0
    assert seen and all(seen), seen
