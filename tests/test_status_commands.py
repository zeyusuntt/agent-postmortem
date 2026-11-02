"""`status` counts the session's background shell commands and says it does not check them
(docs/DESIGN.md 4.14.4, 0.4).

Field report 2026-11-02, "status does not see a background codex exec worker": a session whose
only worker was a `codex exec` run with the Bash tool in the background printed "0 subagents"
and nothing else, which reads as a clean bill for that worker too. Status now names every
backgrounded command of the session by its native end (task notice, the `queued_command`
attachment that sometimes carries the only copy of it, or a TaskStop result), and says their
output is not checked. Monitor tasks are not commands.
"""

from __future__ import annotations

import json
from datetime import timedelta

from live_helpers import FAKE_UUID, iso, notification, tool_result, tool_use, user, write_jsonl
from test_live_session import SLUG, T0

from agent_postmortem import live, state
from agent_postmortem.cli import main


def _bash(n: int, at, *, background: bool = True, auto: bool = False) -> list[dict]:
    """A Bash call whose result names a background task: run_in_background, or (auto) a
    foreground call the 120 s timeout moved to the background."""
    tool_id = f"toolu_sh{n}"
    tool_input = {"command": "sleep 1"}
    if background:
        tool_input["run_in_background"] = True
    result = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
              "noOutputExpected": False, "backgroundTaskId": f"b{n:08d}"}
    if auto:
        result["timedOutAfterMs"] = 120000
    return [
        tool_use(None, at, tool_id, "Bash", tool_input),
        tool_result(None, at, tool_id, "Command running in background", toolUseResult=result),
    ]


def _end(n: int, at, status: str, summary: str) -> dict:
    return notification(at, f"b{n:08d}", f"toolu_sh{n}", status, summary)


def _attachment_end(n: int, at, status: str, summary: str) -> dict:
    envelope = notification(at, f"b{n:08d}", f"toolu_sh{n}", status, summary)["message"]["content"]
    return {
        "type": "attachment", "sessionId": FAKE_UUID, "timestamp": iso(at),
        "attachment": {"type": "queued_command", "prompt": envelope,
                       "commandMode": "task-notification",
                       "origin": {"kind": "task-notification", "producer": "session-task"}},
    }


def _task_stop(n: int, at) -> list[dict]:
    tool_id = f"toolu_stop{n}"
    return [
        tool_use(None, at, tool_id, "TaskStop", {"task_id": f"b{n:08d}"}),
        tool_result(None, at, tool_id, "stopped",
                    toolUseResult={"message": "Successfully stopped task", "task_id": f"b{n:08d}",
                                   "task_type": "local_bash", "command": "sleep 1"}),
    ]


def _write(tmp_path, entries):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start"), *entries])
    return main_path


def _text(capsys, main_path) -> str:
    assert main(["status", str(main_path)]) == 0
    return capsys.readouterr().out


def _commands(capsys, main_path) -> dict:
    assert main(["status", str(main_path), "--json"]) == 0
    return json.loads(capsys.readouterr().out)["commands"]["counts"]


def _command_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("  COMMANDS")]


def _mixed(tmp_path):
    later = T0 + timedelta(minutes=10)
    return _write(tmp_path, [
        *_bash(1, T0), *_bash(2, T0), *_bash(3, T0), *_bash(4, T0, background=False, auto=True),
        _end(1, later, "completed", 'Background command "a" completed (exit code 0)'),
        _end(2, later, "failed", 'Background command "b" failed with exit code 1'),
        *_task_stop(3, later),
    ])


def test_a_session_whose_only_worker_is_a_background_command_says_status_does_not_check_it(
        tmp_path, capsys):
    main_path = _write(tmp_path, [*_bash(1, T0),
                                  _end(1, T0 + timedelta(minutes=10), "completed",
                                       'Background command "c" completed (exit code 0)')])
    out = _text(capsys, main_path)
    assert "0 subagents" in out
    lines = _command_lines(out)
    assert len(lines) == 1, out
    assert "1 in the background" in lines[0] and "1 completed" in lines[0]
    assert "status does not check their output" in lines[0]


def test_commands_are_counted_by_their_native_end(tmp_path, capsys):
    main_path = _mixed(tmp_path)
    lines = _command_lines(_text(capsys, main_path))
    assert len(lines) == 1, lines
    assert "4 in the background: 1 running (launched " in lines[0]
    assert " ago), 1 completed, 1 failed, 1 stopped;" in lines[0]
    assert _commands(capsys, main_path) == {
        "running": 1, "completed": 1, "failed": 1, "killed": 0, "stopped": 1,
    }


def test_an_end_carried_only_by_a_queued_command_attachment_counts(tmp_path, capsys):
    main_path = _write(tmp_path, [
        *_bash(1, T0), *_bash(2, T0),
        _attachment_end(1, T0 + timedelta(minutes=5), "completed",
                        'Background command "d" completed (exit code 0)'),
        _attachment_end(2, T0 + timedelta(minutes=6), "killed",
                        'Background command "e" was stopped because the system is running '
                        "low on memory"),
    ])
    commands = _commands(capsys, main_path)
    assert commands["running"] == 0 and commands["completed"] == 1 and commands["killed"] == 1


def test_a_native_notice_after_a_task_stop_keeps_its_own_word(tmp_path, capsys):
    main_path = _write(tmp_path, [
        *_bash(1, T0), *_task_stop(1, T0 + timedelta(minutes=1)),
        _end(1, T0 + timedelta(minutes=2), "killed", 'Background command "f" was killed'),
        *_bash(2, T0), _end(2, T0 + timedelta(minutes=1), "completed",
                            'Background command "g" completed (exit code 0)'),
        *_task_stop(2, T0 + timedelta(minutes=2)),
    ])
    commands = _commands(capsys, main_path)
    assert commands["killed"] == 1 and commands["completed"] == 1 and commands["stopped"] == 0


def test_a_notice_naming_another_tool_use_id_does_not_end_a_command(tmp_path, capsys):
    stray = notification(T0 + timedelta(minutes=5), "b00000001", "toolu_unrelated", "completed",
                         'Background command "h" completed (exit code 0)')
    main_path = _write(tmp_path, [*_bash(1, T0), stray])
    assert _commands(capsys, main_path)["running"] == 1


def test_a_monitor_task_and_its_events_are_not_commands(tmp_path, capsys):
    monitor = [
        tool_use(None, T0, "toolu_mon", "Monitor", {"command": "tail -f x"}),
        tool_result(None, T0, "toolu_mon", "watching",
                    toolUseResult={"taskId": "b00000009", "timeoutMs": 60000,
                                   "persistent": False}),
        notification(T0 + timedelta(minutes=1), "b00000009", "toolu_mon", "completed",
                     "Monitor ended"),
    ]
    main_path = _write(tmp_path, monitor)
    out = _text(capsys, main_path)
    assert _command_lines(out) == [], out
    assert sum(_commands(capsys, main_path).values()) == 0


def test_a_session_with_no_background_command_prints_no_commands_line(tmp_path, capsys):
    main_path = _write(tmp_path, [tool_use(None, T0, "toolu_fg", "Bash", {"command": "ls"}),
                                  tool_result(None, T0, "toolu_fg", "ok",
                                              toolUseResult={"stdout": "", "stderr": "",
                                                             "interrupted": False})])
    assert _command_lines(_text(capsys, main_path)) == []


def test_facts_stored_before_commands_were_kept_are_read_again(tmp_path, capsys):
    main_path = _mixed(tmp_path)
    assert main(["status", str(main_path)]) == 0
    capsys.readouterr()
    path = live.SessionFollower(main_path, caller_session=None).store_path
    with state.cache_transaction(path) as data:
        entry = data["transcripts"][str(main_path)]
        entry["facts"].pop("shells", None)
        entry["facts_version"] = 3  # the version before background commands were kept
    assert _commands(capsys, main_path)["failed"] == 1


def test_a_second_status_reads_appended_ends_on_top_of_the_stored_facts(tmp_path, capsys):
    main_path = _write(tmp_path, [*_bash(1, T0)])
    assert _commands(capsys, main_path)["running"] == 1
    write_jsonl(main_path, [_end(1, T0 + timedelta(minutes=3), "failed",
                                 'Background command "i" failed with exit code 2')], mode="a")
    commands = _commands(capsys, main_path)
    assert commands["running"] == 0 and commands["failed"] == 1


def test_an_end_word_status_does_not_know_reads_ended(tmp_path, capsys):
    main_path = _write(tmp_path, [*_bash(1, T0),
                                  _end(1, T0 + timedelta(minutes=1), "lost", 'Background "j"')])
    commands = _commands(capsys, main_path)
    assert commands["ended"] == 1 and commands["running"] == 0
    assert "1 ended;" in _command_lines(_text(capsys, main_path))[0]


def test_a_queued_command_attachment_does_not_end_a_subagent(tmp_path, capsys):
    """The attachment shape is read for commands only: for a subagent it would change which
    endings the per-call checks tell (4.14.3 baseline rule), a separate change."""
    from live_helpers import aid
    from test_live_session import _launch as launch_agent

    agent = aid(31)
    main_path = _write(tmp_path, [])
    launch_agent(main_path, agent, "toolu_ag", T0)
    note = _attachment_end(1, T0 + timedelta(minutes=2), "completed", 'Agent "k" finished')
    note["attachment"]["prompt"] = note["attachment"]["prompt"].replace(
        "b00000001", agent).replace("toolu_sh1", "toolu_ag")
    write_jsonl(main_path, [note], mode="a")
    assert main(["status", str(main_path), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["subagents"]
    assert [row["state"] for row in rows] == ["running"]


def test_a_running_command_names_when_the_oldest_one_was_launched(tmp_path, capsys):
    """No end notice is "running" only as far as the transcript knows: a session that closed
    with a command still out never gets one, so the launch age is printed beside the count."""
    now = T0 + timedelta(days=2, minutes=1)
    main_path = _write(tmp_path, [*_bash(1, T0), *_bash(2, T0 + timedelta(days=2))])
    from agent_postmortem import status

    snap = status.snapshot(main_path, now=lambda: now.timestamp())
    assert snap["commands"]["counts"]["running"] == 2
    assert snap["commands"]["oldest_running_seconds"] == round((now - T0).total_seconds())
    from agent_postmortem.redact import Redactor

    text = status.render_text(snap, Redactor(), now=now.timestamp())
    assert "2 running (the oldest launched 2d ago)" in _command_lines(text)[0]


# --- round 1 review ----------------------------------------------------------------------------


def _queue(at, envelope: str) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "content": envelope,
            "sessionId": FAKE_UUID, "timestamp": iso(at)}


def _envelope(n: int, status: str, summary: str, *, tool: bool = True) -> str:
    return ("<task-notification>\n" f"<task-id>b{n:08d}</task-id>\n"
            + (f"<tool-use-id>toolu_sh{n}</tool-use-id>\n" if tool else "")
            + f"<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>")


def test_an_end_notice_with_no_tool_use_id_ends_its_command_in_every_shape(tmp_path, capsys):
    note = 'Background shell command "l" did not finish before the previous session ended'
    later = T0 + timedelta(minutes=5)
    user_entry = _end(1, later, "killed", note)
    user_entry["message"]["content"] = _envelope(1, "killed", note, tool=False)
    attachment = _attachment_end(3, later, "killed", note)
    attachment["attachment"]["prompt"] = _envelope(3, "killed", note, tool=False)
    main_path = _write(tmp_path, [
        *_bash(1, T0), *_bash(2, T0), *_bash(3, T0), user_entry,
        _queue(later, _envelope(2, "killed", note, tool=False)), attachment,
    ])
    commands = _commands(capsys, main_path)
    assert commands["killed"] == 3 and commands["running"] == 0


def test_a_notice_listing_several_commands_from_the_previous_session_ends_each(tmp_path, capsys):
    envelope = ("<task-notification>\n<task-id>b00000001</task-id>\n<task-id>b00000002</task-id>\n"
                "<task-id>__orphan_summary__:shell</task-id>\n<status>stopped</status>\n"
                "<summary>2 background shell command tasks did not finish before the previous "
                "session ended. Task ids: b00000001, b00000002.</summary>\n"
                "<note>No completion record was found for them.</note>\n</task-notification>")
    main_path = _write(tmp_path, [*_bash(1, T0), *_bash(2, T0), *_bash(3, T0),
                                  _queue(T0 + timedelta(minutes=5), envelope)])
    commands = _commands(capsys, main_path)
    assert commands["stopped"] == 2 and commands["running"] == 1


def test_an_end_written_before_its_launch_result_counts(tmp_path, capsys):
    """The enqueue can land before the tool result that names the task (21 of 2,162 launches
    on the reference machine); with no later copy the end must still count."""
    done = _queue(T0, _envelope(1, "completed", 'Background command "m" completed (exit code 0)'))
    use, result = _bash(1, T0)
    main_path = _write(tmp_path, [use, done, result])
    commands = _commands(capsys, main_path)
    assert commands["completed"] == 1 and commands["running"] == 0


def test_an_early_end_survives_the_store_between_two_reads(tmp_path, capsys):
    use, result = _bash(1, T0)
    done = _queue(T0, _envelope(1, "failed", 'Background command "n" failed with exit code 1'))
    main_path = _write(tmp_path, [use, done])
    assert sum(_commands(capsys, main_path).values()) == 0
    write_jsonl(main_path, [result], mode="a")
    assert _commands(capsys, main_path)["failed"] == 1


def test_an_early_end_naming_another_tool_use_id_is_not_taken(tmp_path, capsys):
    use, result = _bash(1, T0)
    stray = _queue(T0, _envelope(1, "completed", "x").replace("toolu_sh1", "toolu_other"))
    main_path = _write(tmp_path, [use, stray, result])
    assert _commands(capsys, main_path)["running"] == 1


def test_a_leftover_notice_does_not_replace_an_end_the_command_already_has(tmp_path, capsys):
    """"No completion record was found" (no tool-use id) is weaker than the command's own
    notice: on the reference machine it turned 4 killed commands into stopped ones."""
    envelope = ("<task-notification>\n<task-id>b00000001</task-id>\n<task-id>b00000002</task-id>\n"
                "<task-id>__orphan_summary__:shell</task-id>\n<status>stopped</status>\n"
                "<summary>2 background shell command tasks did not finish before the previous "
                "session ended.</summary>\n</task-notification>")
    main_path = _write(tmp_path, [
        *_bash(1, T0), *_bash(2, T0),
        _end(1, T0 + timedelta(minutes=1), "killed", 'Background command "o" was stopped because '
             "the system is running low on memory"),
        _queue(T0 + timedelta(minutes=5), envelope),
    ])
    commands = _commands(capsys, main_path)
    assert commands["killed"] == 1 and commands["stopped"] == 1


# --- round 2 review ----------------------------------------------------------------------------


def test_a_tool_less_end_written_before_its_launch_result_counts(tmp_path, capsys):
    use, result = _bash(1, T0)
    early = _queue(T0, _envelope(1, "stopped", "left over", tool=False))
    main_path = _write(tmp_path, [use, early, result])
    assert _commands(capsys, main_path)["stopped"] == 1


def test_a_grouped_notice_ends_a_known_command_and_one_launched_after_it(tmp_path, capsys):
    envelope = ("<task-notification>\n<task-id>b00000001</task-id>\n<task-id>b00000002</task-id>\n"
                "<status>stopped</status>\n<summary>2 left over</summary>\n</task-notification>")
    use, result = _bash(2, T0)
    main_path = _write(tmp_path, [*_bash(1, T0), use, _queue(T0, envelope), result])
    commands = _commands(capsys, main_path)
    assert commands["stopped"] == 2 and commands["running"] == 0


def test_early_ends_keep_the_newest_across_a_sorted_store_round_trip():
    """The live store is written with sorted keys, so arrival order must survive a restore."""
    facts = live.MainFacts()
    for n in range(100, 0, -1):  # b00000100 first ... b00000001 newest
        facts._early_end(f"b{n:08d}", f"toolu_sh{n}", "completed")
    facts = live.MainFacts.from_dict(json.loads(json.dumps(facts.to_dict(), sort_keys=True)))
    facts._early_end("b00000101", "toolu_sh101", "completed")
    use, result = _bash(1, T0)
    facts.update([use, result])
    counts, _oldest = facts.commands()
    assert counts["completed"] == 1 and counts["running"] == 0


def test_a_damaged_early_end_in_the_store_is_evicted_first_and_never_raises():
    facts = live.MainFacts.from_dict({"early_ends": {"b00000999": {"tool": "", "status": "x",
                                                                  "n": "bad"},
                                                     "b00000998": "not a dict"}})
    for n in range(1, 101):
        facts._early_end(f"b{n:08d}", f"toolu_sh{n}", "completed")
    assert len(facts.early_ends) == 100
    assert "b00000999" not in facts.early_ends and "b00000998" not in facts.early_ends


# --- round 3 review ----------------------------------------------------------------------------


def _early_then_launch(*early_notices) -> dict:
    use, result = _bash(1, T0)
    facts = live.MainFacts()
    facts.update([use, *early_notices])
    facts = live.MainFacts.from_dict(json.loads(json.dumps(facts.to_dict(), sort_keys=True)))
    facts.update([result])
    return facts.commands()[0]


def test_an_early_native_end_is_not_replaced_by_a_later_leftover_notice():
    counts = _early_then_launch(_queue(T0, _envelope(1, "killed", "native")),
                                _queue(T0, _envelope(1, "stopped", "leftover", tool=False)))
    assert counts["killed"] == 1 and counts["stopped"] == 0


def test_an_early_end_naming_another_tool_use_id_does_not_erase_the_launchs_own():
    counts = _early_then_launch(
        _queue(T0, _envelope(1, "killed", "native")),
        _queue(T0, _envelope(1, "completed", "x").replace("toolu_sh1", "toolu_other")),
    )
    assert counts["killed"] == 1 and counts["running"] == 0


def test_a_leftover_early_end_still_counts_when_the_launch_has_no_own_notice():
    counts = _early_then_launch(_queue(T0, _envelope(1, "stopped", "leftover", tool=False)))
    assert counts["stopped"] == 1


def test_a_command_the_timeout_moved_to_the_background_is_aged_from_its_start():
    from agent_postmortem import status

    use, result = _bash(1, T0, background=False, auto=True)  # timedOutAfterMs 120000
    result["timestamp"] = iso(T0 + timedelta(seconds=120))
    facts = live.MainFacts()
    facts.update([use, result])
    assert status._commands(facts, (T0 + timedelta(seconds=180)).timestamp())[
        "oldest_running_seconds"] == 180


# --- round 4 review ----------------------------------------------------------------------------


def test_facts_from_a_build_with_the_earlier_early_end_shape_are_read_again(tmp_path, capsys):
    """Builds 48ce52e to 89a0a41 wrote version 4 facts with early ends as {tool, status, n}
    and launch times from the result: those are read again, never restored."""
    use, result = _bash(1, T0)
    path = _write(tmp_path, [use, _queue(T0, _envelope(1, "killed", "native"))])
    _commands(capsys, path)
    cache = live.SessionFollower(path, caller_session=None).store_path
    with state.cache_transaction(cache) as data:
        entry = data["transcripts"][str(path)]
        entry["facts_version"] = 4
        entry["facts"]["early_ends"] = {
            "b00000001": {"tool": "toolu_sh1", "status": "killed", "n": 1},
        }
    write_jsonl(path, [result], mode="a")
    assert _commands(capsys, path)["killed"] == 1
