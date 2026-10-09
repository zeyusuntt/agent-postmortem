"""N5 fix 1, S2: an async Agent launch is not by itself a return (docs/DESIGN.md 4.1, 4.13.1).

A launch result whose status is `completed` (synchronous) is a return, as before. A result with
status `async_launched` or `isAsync` true is not by itself one: the child counts as returned only
when an accepted task notification (the same acceptance rules the workflow lifecycle uses, 4.13.3)
has task-id equal to the child's agent id, tool-use-id equal to that launch's tool_use id, and
status `completed`. Any other launch result status is not returned.
"""

from __future__ import annotations

import pytest

from agent_postmortem import reader
from tests.test_field_v03 import _envelope, _notification_entry, _queue_notification_entry
from tests.test_n4b1_discovery import MISSING, SESSION, SLUG, T0, _launch, _session
from tests.test_n5c1_returned import X, Y


def _launch_result(tool_id, *, agent=X, result=MISSING, block_agent=MISSING):
    """An Agent launch result whose `toolUseResult` is `result` (default: a bare agentId)."""
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "launched"}
    if block_agent is not MISSING:
        block["agentId"] = block_agent
    entry = {
        "type": "user",
        "message": {"role": "user", "content": [block]},
        "timestamp": "2026-09-01T12:00:02Z",
    }
    entry["toolUseResult"] = {"agentId": agent} if result is MISSING else result
    return entry


def _async(tool_id="toolu_1", agent=X, **extra):
    return _launch_result(
        tool_id, result={"agentId": agent, "status": "async_launched", "isAsync": True, **extra},
    )


def _flags(main):
    run = reader.load_session(main)
    return {attempt.agent_id: attempt.returned_to_parent for attempt in run.attempts}


def _notice(task_id, tool_id, status, *, origin=True):
    envelope = _envelope(task_id, tool_id, status)
    if origin:
        return _notification_entry(T0, envelope)
    return {
        "type": "user", "message": {"role": "user", "content": envelope},
        "timestamp": "2026-09-01T12:00:09Z",
    }


ROWS = [
    # name, entries after the launch, expected flag of X
    ("sync completed", [_launch_result("toolu_1", result={"agentId": X, "status": "completed"})],
     True),
    ("no status at all (the bare shape stays a return)", [_launch_result("toolu_1")], True),
    ("async with an accepted completed notification",
     [_async(), _notice(X, "toolu_1", "completed")], True),
    ("async with a completed notification in the queue form",
     [_async(), _queue_notification_entry(T0, _envelope(X, "toolu_1", "completed"))], True),
    ("isAsync true with status completed and an accepted completed notification",
     [_launch_result("toolu_1", result={"agentId": X, "status": "completed", "isAsync": True}),
      _notice(X, "toolu_1", "completed")], False),  # G-A T2: async with a terminal status is malformed (was True)
    ("async with no notification", [_async()], False),
    ("isAsync true with status completed and no notification",
     [_launch_result("toolu_1", result={"agentId": X, "status": "completed", "isAsync": True})],
     False),
    ("async with a failed notification", [_async(), _notice(X, "toolu_1", "failed")], False),
    ("async with a killed notification", [_async(), _notice(X, "toolu_1", "killed")], False),
    ("async with an unrecognized-status notification",
     [_async(), _notice(X, "toolu_1", "weird")], False),
    ("async with a quoted (unaccepted) notification",
     [_async(), {
         "type": "user", "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content":
                     "as quoted: " + _envelope(X, "toolu_1", "completed") + " (end)"},
         "timestamp": "2026-09-01T12:00:09Z"}], False),
    ("async with a notification naming another child",
     [_async(), _notice(Y, "toolu_1", "completed")], False),
    ("async with a notification carrying the wrong tool-use-id",
     [_async(), _notice(X, "toolu_other", "completed")], False),
    ("async with a completed and a failed notification (conflicting evidence)",
     [_async(), _notice(X, "toolu_1", "completed"), _notice(X, "toolu_1", "failed")], False),
    ("a synchronous result with a status that is not completed",
     [_launch_result("toolu_1", result={"agentId": X, "status": "failed"})], False),
    ("a synchronous result with a non-string status",
     [_launch_result("toolu_1", result={"agentId": X, "status": 5})], False),
]


@pytest.mark.parametrize(
    "name,entries,expected", [pytest.param(*row, id=row[0]) for row in ROWS],
)
def test_returned_to_parent_for_each_launch_result_shape(name, entries, expected, tmp_path):
    main = _session(tmp_path, [_launch("toolu_1"), *entries], children=[X, Y])
    assert _flags(main)[X] is expected


def test_an_accepted_notification_for_one_child_does_not_return_another(tmp_path):
    main = _session(
        tmp_path,
        [
            _launch("toolu_1"), _async("toolu_1", X), _launch("toolu_2"), _async("toolu_2", Y),
            _notice(X, "toolu_1", "completed"),
        ],
        children=[X, Y],
    )
    assert _flags(main) == {X: True, Y: False}


def test_the_notification_is_read_from_the_same_session_file_the_children_belong_to(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1"), _async(), _notice(X, "toolu_1", "completed")], children=[X],
    )
    assert main.parent.name == SLUG and main.stem == SESSION
    run = reader.load_session(main)
    assert [attempt.returned_to_parent for attempt in run.attempts] == [True]


def test_a_view_of_named_subagents_uses_the_same_evidence(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1"), _async(), _notice(X, "toolu_1", "completed")], children=[X],
    )
    sub = main.parent / SESSION / "subagents" / f"agent-{X}.jsonl"
    run = reader.load_session_view(main, [sub])
    assert [attempt.returned_to_parent for attempt in run.attempts] == [True]
    bare = _session(tmp_path / "bare", [_launch("toolu_1"), _async()], children=[X])
    sub = bare.parent / SESSION / "subagents" / f"agent-{X}.jsonl"
    run = reader.load_session_view(bare, [sub])
    assert [attempt.returned_to_parent for attempt in run.attempts] == [False]


# --- the WORK AT RISK block ---------------------------------------------------------------------


def _async_main_entries(number, *, notice=None):
    from tests.test_field_v03 import _aid

    child = _aid(number)
    launch_id = f"toolu_launch{number}"
    entries = [
        {"type": "assistant", "timestamp": "2026-09-01T12:00:01Z",
         "message": {"role": "assistant", "usage": {}, "content": [
             {"type": "tool_use", "id": launch_id, "name": "Agent", "input": {}}]}},
        {"type": "user", "timestamp": "2026-09-01T12:00:02Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": launch_id, "content": "launched"}]},
         "toolUseResult": {"agentId": child, "status": "async_launched", "isAsync": True}},
    ]
    if notice is not None:
        entries.append(_notification_entry(T0, _envelope(child, launch_id, notice)))
    return entries


def _unpaired_write(number, repo):
    from tests.n5c1_helpers import Step

    return Step(f"w{number}", "Write", {"file_path": str(repo / "tracked.txt")}, None, False,
                str(repo))


@pytest.mark.parametrize("notice,expected_unknown", [
    # G-A (W1): the unknown cases now say why; the child's transcript is fresh, so a child with
    # no notification reads `possibly running`, one that reported failure `reported failed`
    pytest.param(None, "possibly running", id="no notification"),
    pytest.param("failed", "reported failed", id="failed notification"),
    pytest.param("completed", None, id="completed notification"),
])
def test_an_async_child_is_in_work_at_risk_unless_a_completed_notification_returned_it(
    notice, expected_unknown, tmp_path, capsys,
):
    from agent_postmortem import cli
    from tests.n5c1_helpers import init_repo, session_with_children

    repo = init_repo(tmp_path / "repo")
    main, ids = session_with_children(
        tmp_path, [(77, [_unpaired_write(77, repo)], False, False)],
        main_entries=_async_main_entries(77, notice=notice),
    )
    cli.main(["run", str(main), "--raw"])
    out = capsys.readouterr().out
    if expected_unknown:
        assert "WORK AT RISK  unknown: 1 subagent has no established return" in out
        assert f"  {ids[0]}  {expected_unknown}" in out
    else:
        assert "WORK AT RISK  none:" in out


def test_the_async_status_word_alone_is_enough_to_need_a_notification(tmp_path):
    entries = [_launch_result("toolu_1", result={"agentId": X, "status": "async_launched"})]
    bare = _session(tmp_path / "bare", [_launch("toolu_1"), *entries], children=[X])
    assert _flags(bare)[X] is False
    noticed = _session(
        tmp_path / "noticed",
        [_launch("toolu_1"), *entries, _notice(X, "toolu_1", "completed")], children=[X],
    )
    assert _flags(noticed)[X] is True


def test_is_async_alone_is_enough_to_need_a_notification(tmp_path):
    entries = [_launch_result("toolu_1", result={"agentId": X, "isAsync": True})]
    bare = _session(tmp_path / "bare", [_launch("toolu_1"), *entries], children=[X])
    assert _flags(bare)[X] is False
