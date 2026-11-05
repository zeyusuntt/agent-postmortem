"""Findings of the 0.5.0 release review (the whole branch against 0.4.0), each pinned by a test.

Synthetic data only: the fake session id, paths under tmp_path, invented worker names.
"""

from __future__ import annotations

import contextlib
import json
import os

import pytest
from live_helpers import FAKE_UUID, tool_result, tool_use
from test_live_plugin import _session_start_on_path
from test_plugin import _run_run_sh, _write_fake_cli
from test_stop_watch import TASK, Life, _bg_session, _manager, _stop_check, _versioned
from test_worker_notice import PERMISSION, Manager, _ago, _hook

from agent_postmortem import cli, commands, hooks_cli, live, pre_bash, state, watcher

RUN = "perl -e 'alarm 1; exec @ARGV' true"


# --- the alarm rule: a wrong True silences the only alarm for a real hang ------------------------


def test_a_failed_cd_before_and_and_a_step_after_a_semicolon_runs_in_the_old_directory(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    assert commands.under_its_own_alarm(f"cd /definitely-absent && {RUN}; wc -c fifo",
                                        str(tmp_path)) is False
    assert commands.under_its_own_alarm(f"cd /definitely-absent && {RUN} || wc -c fifo",
                                        str(tmp_path)) is False


def test_a_cd_guarding_the_whole_list_still_counts(tmp_path):
    (tmp_path / "work").mkdir()
    assert commands.under_its_own_alarm(f"cd {tmp_path}/work && {RUN}", str(tmp_path)) is True
    assert commands.under_its_own_alarm(f"cd {tmp_path}/work && {RUN} && echo done",
                                        str(tmp_path)) is True


def test_a_childs_assignment_shaped_argument_changes_nothing_in_the_shell(tmp_path):
    good = tmp_path / "finished.log"
    good.write_text("x\n")
    wrapper = f"OUT=/dev/zero\nperl -e 'alarm 1; exec @ARGV' env OUT={good} true\nwc -l \"$OUT\""
    assert commands.under_its_own_alarm(wrapper) is False


def test_a_prefix_assignment_changes_nothing_in_the_shell(tmp_path):
    good = tmp_path / "finished.log"
    good.write_text("x\n")
    wrapper = f"OUT=/dev/zero\nOUT={good} {RUN}\nwc -l \"$OUT\""
    assert commands.under_its_own_alarm(wrapper) is False


def test_an_append_is_not_a_replacement(tmp_path):
    (tmp_path / "zero").write_text("x\n")
    wrapper = f"OUT=/dev/; OUT+=zero\n{RUN}\nwc -l \"$OUT\""
    assert commands.under_its_own_alarm(wrapper, str(tmp_path)) is False


def test_printf_into_a_variable_changes_the_shell(tmp_path):
    good = tmp_path / "finished.log"
    good.write_text("x\n")
    wrapper = f"OUT={good}\nprintf -v OUT /dev/zero\n{RUN}\nwc -l \"$OUT\""
    assert commands.under_its_own_alarm(wrapper) is False


def test_a_plain_literal_assignment_still_counts(tmp_path):
    good = tmp_path / "finished.log"
    good.write_text("x\n")
    assert commands.under_its_own_alarm(f"OUT={good}\n{RUN}\nwc -l \"$OUT\"") is True


# --- the Stop deadline request is retired only where something else would catch the hang ---------


def test_a_command_whose_output_the_watcher_cannot_read_is_still_asked(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _versioned(m)
    _bg_session(monkeypatch)
    unknown = {**TASK, "command": 'python3 worker.py > "$LOG" 2>&1'}
    assert "DEADLINE b00000008" in _stop_check(m, capsys, monkeypatch, [unknown])


def test_a_command_with_a_literal_output_stays_with_the_watcher(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _versioned(m)
    _bg_session(monkeypatch)
    literal = {**TASK, "command": "python3 worker.py > /tmp/out.log 2>&1"}
    assert _stop_check(m, capsys, monkeypatch, [literal]) == ""


def test_a_cli_behind_a_plugin_with_no_watcher_hook_is_still_asked(tmp_path, monkeypatch, capsys):
    """A 0.5.0 CLI on PATH behind a 0.4.0 plugin, whose run.sh sets no mark, or a session still on
    the hooks of an older plugin copy: no watcher runs, so the request must stay."""
    m = Manager(tmp_path)
    _versioned(m)
    _bg_session(monkeypatch)
    monkeypatch.delenv(watcher.PLUGIN_MARK, raising=False)
    assert "DEADLINE b00000008" in _stop_check(m, capsys, monkeypatch, [TASK])


def test_run_sh_marks_the_cli_it_starts(tmp_path):
    seen = tmp_path / "mark"
    _write_fake_cli(tmp_path / "bin", "agent-postmortem", (
        "#!/usr/bin/env bash\n"
        'if [ "$2" = "capabilities" ]; then\n'
        "  printf '{\"protocol\": 1, \"version\": \"0.0.1\", \"hooks\": [\"stop-watch\"]}\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf "%s" "${{{watcher.PLUGIN_MARK}:-}}" > "{seen}"\n'
        "cat >/dev/null\n"
        "exit 0\n"))
    result = _run_run_sh("check", path_entries=[str(tmp_path / "bin")],
                         plugin_data=tmp_path / "plugin-data",
                         stdin=json.dumps({"hook_event_name": "Stop"}))
    assert result.returncode == 0
    assert seen.read_text() == "1"


# --- two watchers ---------------------------------------------------------------------------------


def _owner(session) -> dict:
    data = state.read_cache(session.store_path)
    return data["sessions"][live.session_key(session.main_path)]["watcher"]


def test_a_watcher_that_finishes_starting_late_does_not_take_a_newer_ones_place(
        tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    late = watcher._Watch(m.main, lambda: 1000.0)
    newer = watcher._Watch(m.main, lambda: 2000.0)
    assert newer.take_ownership() is True
    assert late.take_ownership() is False
    assert _owner(newer)["token"] == newer.token


def test_a_watcher_that_lost_its_place_leaves(tmp_path, monkeypatch, capsys):
    m = _manager(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(watcher._Watch, "take_ownership", lambda self: False)
    life = Life(m)
    assert life.run() == 0
    assert life.polls == 0


# --- the stall read's size guard -----------------------------------------------------------------


def test_a_cached_offset_of_another_version_counts_as_unread(tmp_path):
    main = tmp_path / f"{FAKE_UUID}.jsonl"
    with main.open("wb") as handle:
        handle.truncate(40 << 20)
    stale = {"transcripts": {str(main): {"offset": 40 << 20, "facts_version": 3}}}
    assert hooks_cli._unread_bytes(main, stale) == 40 << 20
    current = {"transcripts": {str(main): {"offset": 40 << 20, "fork_line": 0,
                                           "facts_version": live.MAIN_FACTS_VERSION}}}
    assert hooks_cli._unread_bytes(main, current) == 0


def test_a_subagents_cached_offset_of_another_version_counts_as_unread(tmp_path):
    main = tmp_path / f"{FAKE_UUID}.jsonl"
    main.write_text("")
    sub = tmp_path / FAKE_UUID / "subagents" / "agent-a1.jsonl"
    sub.parent.mkdir(parents=True)
    with sub.open("wb") as handle:
        handle.truncate(1 << 20)
    stale = {"transcripts": {str(sub): {"offset": 1 << 20, "sub_facts_version": 1}}}
    assert hooks_cli._unread_bytes(main, stale) == 1 << 20
    current = {"transcripts": {str(sub): {"offset": 1 << 20,
                                          "sub_facts_version": live.SUB_FACTS_VERSION}}}
    assert hooks_cli._unread_bytes(main, current) == 0


# --- smaller ones ---------------------------------------------------------------------------------


def _bash(command: str, tool_id: str) -> dict:
    return tool_use(None, _ago(60), tool_id, "Bash", {"command": command})


def test_a_launch_that_was_answered_never_counts_as_unlinked():
    facts = live.MainFacts()
    launch = "claude --bg -n child work"
    answer = tool_result(None, _ago(59), "toolu_launch", "backgrounded · deadbeef · child\n")
    facts.update([_bash(launch, "toolu_launch"), answer])
    assert "deadbeef" in facts.workers and facts.unlinked_launches == 0
    for number in range(80):
        facts.update([_bash("true", f"toolu_{number}")])
    assert facts.unlinked_launches == 0


@pytest.mark.parametrize(
    ("command", "warns"),
    [
        ("if true; then codex exec review > /tmp/review.md; fi", True),
        ("for f in a b; do codex exec review; done", True),
        ("while true; do codex exec review; done", True),
        ("if true; then codex exec review </dev/null; fi", False),
        ("while read x; do codex exec review; done < list.txt", False),
        ("if true; then echo no; else codex exec review; fi", True),
        ("cat list.txt | while read x; do codex exec review; done", False),
    ],
)
def test_the_stdin_warning_reads_conditional_and_loop_bodies(command, warns):
    assert pre_bash.unredirected_codex(command) is warns


def test_a_commit_that_fails_does_not_tell_a_worker_twice_in_one_answer(
        tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    m.launched(job={"state": "working", "tempo": "blocked", "needs": PERMISSION})
    real = state.cache_transaction

    @contextlib.contextmanager
    def fails_at_commit(path, *args, **kwargs):
        with real(path, *args, **kwargs) as data:
            yield data
        raise state.StateError("disk full")

    monkeypatch.setattr(state, "cache_transaction", fails_at_commit)
    text = _hook(monkeypatch, capsys, m)
    assert text.count("WORKER night-run") == 1, text


def test_a_slow_capabilities_probe_is_no_proof_of_an_old_cli(tmp_path):
    """The first probe chooses the CLI; a later one that prints nothing says nothing about it."""
    count = tmp_path / "probes"
    slow = (
        "#!/usr/bin/env bash\n"
        'if [ "$2" = "capabilities" ]; then\n'
        f'  n=$(cat "{count}" 2>/dev/null || echo 0); echo $((n+1)) > "{count}"\n'
        "  [ \"$n\" = 0 ] && printf '{\"protocol\": 1, \"version\": \"0.0.1\"}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "cat >/dev/null\n"
        "exit 0\n")
    assert _session_start_on_path(tmp_path, slow).stdout == ""


@pytest.mark.parametrize("task_id", ["/srv/private/plan.md", "a b", "x" * 200, "../up"])
def test_a_task_id_that_is_no_task_id_is_not_kept(task_id):
    facts = live.MainFacts()
    result = tool_result(None, _ago(50), "toolu_shell", "started",
                         toolUseResult={"backgroundTaskId": task_id})
    facts.update([_bash("make long", "toolu_shell"), result])
    assert task_id not in facts.shells


def test_a_real_task_id_is_kept():
    facts = live.MainFacts()
    result = tool_result(None, _ago(50), "toolu_shell", "started",
                         toolUseResult={"backgroundTaskId": "b8ljx9he2"})
    facts.update([_bash("make long", "toolu_shell"), result])
    assert "b8ljx9he2" in facts.shells


def test_the_stop_payloads_task_id_must_look_like_one(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    _versioned(m, "2.1.300")
    odd = {**TASK, "id": "/srv/private/plan.md"}
    assert "/srv/private" not in _stop_check(m, capsys, monkeypatch, [odd])


def test_a_stalled_worker_of_the_caller_comes_first_and_says_whose_it_is(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    stalled = [
        {"session": "s_other", "age_seconds": 3600.0, "stall": {}, "mine": False},
        {"session": "s_mine", "age_seconds": 1900.0, "stall": {}, "mine": True},
    ]
    candidates: list[dict] = []
    cli._fold_stalled(stalled, candidates, [], set())
    first = min(candidates, key=lambda c: c["sort_key"])
    assert first["run"].run_id == "s_mine"
    assert first["mark"] == "dispatched by this session"


# --- round 2 of the release review -----------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper",
    [
        'OUT=/dev/zero; cd /definitely-absent && OUT=/etc/hosts; RUN; wc -l "$OUT"',
        'OUT=/dev/zero; cd /definitely-absent && export OUT=/etc/hosts; RUN; wc -l "$OUT"',
        'OUT=/dev/zero; OUT=/etc/hosts | true; RUN; wc -l "$OUT"',
        'OUT=/dev/zero; OUT=/etc/hosts & RUN; wc -l "$OUT"',
        'OUT=/dev/zero; true || OUT=/etc/hosts; RUN; wc -l "$OUT"',
    ],
)
def test_an_assignment_that_may_not_have_run_here_leaves_the_variable_unknown(wrapper):
    assert commands.under_its_own_alarm(wrapper.replace("RUN", RUN)) is False


def test_an_assignment_that_surely_ran_here_still_counts():
    assert commands.under_its_own_alarm(f'OUT=/etc/hosts && {RUN} && wc -l "$OUT"') is True


def test_an_output_in_a_directory_that_is_not_there_is_not_one_the_watcher_can_read(
        tmp_path, monkeypatch, capsys):
    """A `cd X;` that failed left the command elsewhere, so its relative output is not at that
    path, and an output that cannot be inspected is never judged silent."""
    m = Manager(tmp_path)
    _versioned(m)
    _bg_session(monkeypatch)
    failed = {**TASK, "command": "cd /definitely-absent; tail -f /dev/null > out.log"}
    assert "DEADLINE b00000008" in _stop_check(m, capsys, monkeypatch, [failed])


def _kept(path, **fields) -> dict:
    return {"transcripts": {str(path): {"fork_line": 0, "facts_version": live.MAIN_FACTS_VERSION,
                                        **fields}}}


def test_a_replaced_transcript_counts_as_unread(tmp_path):
    main = tmp_path / f"{FAKE_UUID}.jsonl"
    with main.open("wb") as handle:
        handle.truncate(1 << 20)
    inode = main.stat().st_ino
    assert hooks_cli._unread_bytes(main, _kept(main, offset=1 << 20, inode=inode)) == 0
    assert hooks_cli._unread_bytes(main, _kept(main, offset=1 << 20, inode=inode + 1)) == 1 << 20


def test_a_transcript_shorter_than_its_offset_counts_as_unread(tmp_path):
    main = tmp_path / f"{FAKE_UUID}.jsonl"
    with main.open("wb") as handle:
        handle.truncate(1 << 20)
    assert hooks_cli._unread_bytes(main, _kept(main, offset=2 << 20)) == 1 << 20


@pytest.mark.parametrize(
    ("command", "warns"),
    [
        ("if true; then echo ok; fi </dev/null; codex exec review", True),
        ("cat list.txt | while read x; do echo y; done; codex exec review", True),
        ("while read x; do echo y; done < list.txt; codex exec review", True),
        ("for f in a; do if true; then codex exec review; fi; done < list.txt", False),
        ("codex exec first; while read x; do echo y; done < list.txt", True),
    ],
)
def test_a_compounds_stdin_covers_only_that_compound(command, warns):
    assert pre_bash.unredirected_codex(command) is warns


@pytest.mark.parametrize("task_id", ["-tmp-some-project", FAKE_UUID, "Upper", "with_underscore"])
def test_shapes_no_native_task_id_has_are_not_kept(task_id):
    """On the maintainer's machine every one of 2,872 real ids is 9 lowercase letters and digits."""
    facts = live.MainFacts()
    result = tool_result(None, _ago(50), "toolu_shell", "started",
                         toolUseResult={"backgroundTaskId": task_id})
    facts.update([_bash("make long", "toolu_shell"), result])
    assert task_id not in facts.shells


# --- round 3 of the release review, each with its exact reproduction --------------------------------


@pytest.mark.parametrize(
    "wrapper",
    [
        'OUT=/dev/zero; OUT=/etc/hosts && RUN & wc -l "$OUT"',  # the whole list is in a subshell
        "RUN &",
        "RUN & wc -c /etc/hosts",
    ],
)
def test_nothing_backgrounded_counts_as_under_its_own_alarm(wrapper):
    assert commands.under_its_own_alarm(wrapper.replace("RUN", RUN)) is False


def test_a_variable_that_splits_into_more_operands_is_unknown():
    command = f"OUT='/etc/hosts /dev/zero'; {RUN}; wc -l $OUT"
    assert commands.under_its_own_alarm(command) is False


def test_a_fifo_output_is_not_one_the_watcher_can_read(tmp_path):
    os.mkfifo(tmp_path / "out")
    assert hooks_cli._watcher_cannot_read(f"tail -f /dev/null > {tmp_path}/out", str(tmp_path))


def test_a_relative_output_after_an_unguarded_cd_is_not_one_the_watcher_can_read(tmp_path):
    (tmp_path / "w").mkdir()
    assert hooks_cli._watcher_cannot_read("cd /etc/hosts/..; tail -f /dev/null > out.log", "/tmp")
    assert hooks_cli._watcher_cannot_read(f"cd {tmp_path}/w; run > out.log", str(tmp_path))
    assert not hooks_cli._watcher_cannot_read(f"cd {tmp_path}/w && run > out.log", str(tmp_path))
    assert not hooks_cli._watcher_cannot_read(f"cd {tmp_path}/w; run > /tmp/out.log", str(tmp_path))


def test_a_skipped_exec_does_not_give_later_commands_a_stdin():
    assert pre_bash.unredirected_codex(
        "if false; then echo no; exec </dev/null; fi; codex exec review") is True
    assert pre_bash.unredirected_codex("exec </dev/null; codex exec review") is False


def test_a_cached_shell_whose_id_is_no_task_id_is_not_restored():
    good = {"tool": "t", "ts": 1.0, "end": None}
    facts = live.MainFacts.from_dict({"shells": {"/srv/private/plan.md": good, "b8ljx9he2": good}})
    assert list(facts.shells) == ["b8ljx9he2"]
