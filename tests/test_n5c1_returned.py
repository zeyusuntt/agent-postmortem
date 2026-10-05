"""N5c-1: `Attempt.returned_to_parent` (plan S11, DESIGN 4.1 and 4.13.1).

A typed private field, `bool | None` (None for a workflow attempt and for the
session main thread), computed in the reader from the same parent snapshot the
launch reconciliation uses: true only when exactly one assistant Agent or Task
launch block has the launch's `tool_use_id` and exactly one `tool_result` for it
carries this child's id (either id location, equal when both are present), the
result is not an error, not the rejected shape, and well formed. False for a
missing, error, rejected, malformed, duplicate, or conflicting result, and for a
discovered child with no matching launch. Fail closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_postmortem import reader
from tests.n5b_helpers import Spec, build_workflow
from tests.test_field_v03 import _aid
from tests.test_n4b1_discovery import (
    MISSING,
    SESSION,
    _agent_result,
    _launch,
    _session,
)

X = _aid(4)
Y = _aid(5)


def _flags(main: Path) -> dict[str, bool | None]:
    run = reader.load_session(main)
    return {attempt.agent_id: attempt.returned_to_parent for attempt in run.attempts}


def _result(tool_id, *, block=MISSING, entry=MISSING, is_error=False, entry_value=MISSING):
    """A launch result entry with full control of its shape."""
    result_block = {"type": "tool_result", "tool_use_id": tool_id, "content": "done"}
    if block is not MISSING:
        result_block["agentId"] = block
    if is_error:
        result_block["is_error"] = True
    out = {
        "type": "user",
        "message": {"role": "user", "content": [result_block]},
        "timestamp": "2026-09-01T12:00:02Z",
    }
    if entry_value is not MISSING:
        out["toolUseResult"] = entry_value
    elif entry is not MISSING:
        out["toolUseResult"] = {"agentId": entry, "status": "completed"}
    return out


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("how", ["entry", "block", "both"])
def test_success_through_either_id_location_is_true(tmp_path, tool, how):
    kwargs = {}
    if how in ("entry", "both"):
        kwargs["entry"] = X
    if how in ("block", "both"):
        kwargs["block"] = X
    main = _session(
        tmp_path, [_launch("toolu_1", tool), _agent_result("toolu_1", **kwargs)], children=[X]
    )
    assert _flags(main) == {X: True}


def test_the_same_result_written_twice_counts_once(tmp_path):
    result = _agent_result("toolu_1", entry=X)
    main = _session(tmp_path, [_launch("toolu_1"), result, result], children=[X])
    assert _flags(main) == {X: True}


def test_two_children_each_with_their_own_launch_are_each_true(tmp_path):
    main = _session(
        tmp_path,
        [
            _launch("toolu_1"), _agent_result("toolu_1", entry=X),
            _launch("toolu_2", "Task"), _agent_result("toolu_2", entry=Y),
        ],
        children=[X, Y],
    )
    assert _flags(main) == {X: True, Y: True}


def test_no_result_is_false(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1")], children=[X])
    assert _flags(main) == {X: False}


def test_a_child_with_no_launch_at_all_is_false(tmp_path):
    main = _session(tmp_path, [], children=[X])
    assert _flags(main) == {X: False}


def test_a_child_whose_launch_names_another_child_is_false(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1"), _agent_result("toolu_1", entry=X)], children=[X, Y],
    )
    flags = _flags(main)
    assert flags[Y] is False


@pytest.mark.parametrize("how", ["entry", "block"])
def test_an_error_result_is_false(tmp_path, how):
    kwargs = {how: X}
    entry = _result("toolu_1", is_error=True, **kwargs)
    main = _session(tmp_path, [_launch("toolu_1"), entry], children=[X])
    assert _flags(main) == {X: False}


def test_the_rejected_shape_is_false(tmp_path):
    rejected = _result("toolu_1", block=X, is_error=True, entry_value="rejected by the harness")
    main = _session(tmp_path, [_launch("toolu_1"), rejected], children=[X])
    assert _flags(main) == {X: False}


def test_two_different_results_for_one_launch_are_false(tmp_path):
    first = _agent_result("toolu_1", entry=X)
    second = _agent_result("toolu_1", entry=X)
    second["toolUseResult"]["status"] = "other"
    main = _session(tmp_path, [_launch("toolu_1"), first, second], children=[X])
    assert _flags(main) == {X: False}


def test_a_second_result_carrying_another_id_makes_both_children_false(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1"), _agent_result("toolu_1", entry=X), _agent_result("toolu_1", entry=Y)],
        children=[X, Y],
    )
    assert _flags(main) == {X: False, Y: False}


def test_conflicting_id_locations_are_false(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1"), _agent_result("toolu_1", entry=X, block=Y)],
        children=[X, Y],
    )
    assert _flags(main) == {X: False, Y: False}


@pytest.mark.parametrize("bad", [5, None, ["a"], {"x": 1}])
def test_a_non_string_id_in_either_location_is_false(tmp_path, bad):
    main = _session(
        tmp_path,
        [_launch("toolu_1"), _agent_result("toolu_1", entry=X, block=bad)],
        children=[X],
    )
    assert _flags(main) == {X: False}


def test_a_duplicated_launch_id_with_one_otherwise_valid_result_is_false(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1"), _launch("toolu_1"), _agent_result("toolu_1", entry=X)],
        children=[X],
    )
    assert _flags(main) == {X: False}


def test_a_launch_id_shared_with_another_tool_block_is_false(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1"), _launch("toolu_1", "Bash"), _agent_result("toolu_1", entry=X)],
        children=[X],
    )
    assert _flags(main) == {X: False}


def test_one_child_named_by_two_launches_is_false(tmp_path):
    main = _session(
        tmp_path,
        [
            _launch("toolu_1"), _agent_result("toolu_1", entry=X),
            _launch("toolu_2"), _agent_result("toolu_2", entry=X),
        ],
        children=[X],
    )
    assert _flags(main) == {X: False}


def test_a_tool_use_shaped_block_in_a_user_entry_is_no_launch(tmp_path):
    fake = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "Agent", "input": {}}],
        },
        "timestamp": "2026-09-01T12:00:03Z",
    }
    main = _session(tmp_path, [fake, _agent_result("toolu_1", entry=X)], children=[X])
    assert _flags(main) == {X: False}


def test_a_result_for_a_send_message_block_does_not_return_a_child(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1", "SendMessage"), _agent_result("toolu_1", entry=X)],
        children=[X],
    )
    assert _flags(main) == {X: False}


def test_a_view_of_named_subagents_computes_the_same_field(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1"), _agent_result("toolu_1", entry=X)], children=[X, Y],
    )
    sub = main.parent / SESSION / "subagents" / f"agent-{X}.jsonl"
    run = reader.load_session_view(main, [sub])
    assert [a.returned_to_parent for a in run.attempts] == [True]


def test_the_session_main_thread_has_none(tmp_path):
    main = _session(tmp_path, [], children=[X])
    run = reader.load_session(main)
    assert run.main.returned_to_parent is None


def test_a_workflow_attempt_has_none(tmp_path, monkeypatch):
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-a20", [Spec("v2:x", 1)])
    run = reader.load_run(run_dir)
    assert [a.returned_to_parent for a in run.attempts] == [None]


def test_the_field_is_private_and_not_in_the_attempt_dict(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1"), _agent_result("toolu_1", entry=X)], children=[X],
    )
    run = reader.load_session(main)
    assert "returned_to_parent" not in run.attempts[0].to_dict()
