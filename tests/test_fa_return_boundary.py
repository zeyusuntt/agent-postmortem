"""Part FA3 (field milestone F, T7): an async child's completion notification must
follow its launch call (docs/DESIGN.md 4.1 `Attempt.returned_to_parent`, 4.13.1
Return evidence; 4.13.3's lifecycle boundary after `result_line` is unchanged).

The three physical orderings of one launch, pinned for BOTH predicates:

    A  notification, launch tool_use, launch result : not returned; not lifecycle evidence
    B  launch tool_use, notification, launch result : returned;     not lifecycle evidence
    C  launch tool_use, launch result, notification : returned;     lifecycle evidence

B is the real race the corpus showed once in 382 async launches (a fast child).
"""

from __future__ import annotations

import pytest

from agent_postmortem import reader
from tests.test_field_v03 import (
    _envelope,
    _notification_entry,
    _queue_notification_entry,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)
from tests.test_n4b1_discovery import SESSION, T0, _launch, _session
from tests.test_n5c1_returned import X
from tests.test_n5fix1_returned import _async

TASK_FORMS = ("origin", "queue")


def _notice(task_id, tool_id, status, form):
    envelope = _envelope(task_id, tool_id, status)
    if form == "queue":
        return _queue_notification_entry(T0, envelope)
    return _notification_entry(T0, envelope)


def _agent_entries(order, form, status="completed"):
    launch, result = _launch("toolu_1"), _async("toolu_1", X)
    notice = _notice(X, "toolu_1", status, form)
    return {
        "A": [notice, launch, result],
        "B": [launch, notice, result],
        "C": [launch, result, notice],
    }[order]


@pytest.mark.parametrize("form", TASK_FORMS)
@pytest.mark.parametrize(
    "order,returned",
    [
        pytest.param("A", False, id="before the launch tool_use"),
        pytest.param("B", True, id="between tool_use and result"),
        pytest.param("C", True, id="after the result"),
    ],
)
def test_fa3_child_return_boundary_is_the_launch_tool_use(tmp_path, order, returned, form):
    main = _session(tmp_path, _agent_entries(order, form), children=[X])
    run = reader.load_session(main)
    assert [attempt.returned_to_parent for attempt in run.attempts] == [returned]


@pytest.mark.parametrize("form", TASK_FORMS)
def test_fa3_a_failed_notification_between_launch_and_result_is_still_no_return(tmp_path, form):
    main = _session(tmp_path, _agent_entries("B", form, status="failed"), children=[X])
    assert [a.returned_to_parent for a in reader.load_session(main).attempts] == [False]


def test_fa3_a_completed_notification_before_a_failed_one_after_the_launch_conflicts(tmp_path):
    """The early notification does not count, so only the later failed one is a pair member."""
    entries = [
        _notice(X, "toolu_1", "completed", "origin"), _launch("toolu_1"), _async("toolu_1", X),
        _notice(X, "toolu_1", "failed", "origin"),
    ]
    main = _session(tmp_path, entries, children=[X])
    assert [a.returned_to_parent for a in reader.load_session(main).attempts] == [False]


def test_fa3_an_early_failed_notification_is_ignored_and_a_later_completed_one_returns(tmp_path):
    entries = [
        _notice(X, "toolu_1", "failed", "origin"), _launch("toolu_1"), _async("toolu_1", X),
        _notice(X, "toolu_1", "completed", "origin"),
    ]
    main = _session(tmp_path, entries, children=[X])
    # G-A rev 6 (T4, FA3): a notification naming the launch before its tool_use line is
    # ambiguous evidence, so the child is not established as returned (was: ignored, True).
    assert [a.returned_to_parent for a in reader.load_session(main).attempts] == [False]


def test_fa3_the_view_of_named_subagents_uses_the_same_boundary(tmp_path):
    main = _session(tmp_path, _agent_entries("A", "origin"), children=[X])
    sub = main.parent / SESSION / "subagents" / f"agent-{X}.jsonl"
    run = reader.load_session_view(main, [sub])
    assert [attempt.returned_to_parent for attempt in run.attempts] == [False]
    main = _session(tmp_path / "b", _agent_entries("B", "origin"), children=[X])
    sub = main.parent / SESSION / "subagents" / f"agent-{X}.jsonl"
    run = reader.load_session_view(main, [sub])
    assert [attempt.returned_to_parent for attempt in run.attempts] == [True]


# --- the lifecycle predicate (4.13.3) on the same three orderings ----------------------


def _index(tmp_path, entries):
    path = tmp_path / "parent.jsonl"
    _write_jsonl(path, entries)
    return reader.ParentIndex._scan(path)


def _workflow_entries(order, form, status="completed"):
    launch = _workflow_launch("toolu_A", T0)
    result = _workflow_launch_result("toolu_A", T0, "wf_00000000-fa6", "tA")
    notice = _notice("tA", "toolu_A", status, form)
    return {
        "A": [notice, launch, result],
        "B": [launch, notice, result],
        "C": [launch, result, notice],
    }[order]


@pytest.mark.parametrize("form", TASK_FORMS)
@pytest.mark.parametrize(
    "order,evidence", [("A", False), ("B", False), ("C", True)],
    ids=["before the launch tool_use", "between tool_use and result", "after the result"],
)
def test_fa3_lifecycle_boundary_stays_the_launch_result_line(tmp_path, order, evidence, form):
    index = _index(tmp_path, _workflow_entries(order, form))
    (generation,) = index.generations
    if evidence:
        assert (generation.terminal, generation.terminal_source) == ("completed", "notification")
        assert generation.terminal_line == 3
    else:
        assert generation.terminal is None and generation.terminal_source is None
        assert generation.terminal_line is None
