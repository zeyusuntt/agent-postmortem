"""G-A, G2: `Attempt.return_state`, `return_reason` and `resumes` of a session subagent
(docs/DESIGN.md 4.1, 4.13.1 "Return evidence", plan rev 5 T1 to T5).

One row per named case of the truth table. Every row builds a synthetic session in tmp_path and
asserts the exact (state, reason) pair of the child under test.
"""

from __future__ import annotations

import os
import re

import pytest

from agent_postmortem import reader
from tests.g_helpers import (
    ABSENT,
    NULL,
    Session,
    X,
    Y,
    Z,
    async_launch,
    child_entries,
    envelope,
    note,
    queued,
    result,
    resumed,
    send,
    states,
    sync_launch,
    use,
)

RET = ("returned", "none")
PEND = ("pending", "none")
FAIL = ("reported_failed", "none")
KILL = ("reported_killed", "none")


def unknown(reason):
    return ("unknown", reason)


# --- T1 launcher ------------------------------------------------------------------------------


def t1_main_launcher(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X)
    return {X: RET}


def t1_nested_readable(s):
    """Y was launched by X (nested); its completion reaches the main transcript."""
    s.main(sync_launch("toolu_x", X, 1), note(20, Y, "toolu_y", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("toolu_y", Y, 4)).child(
        Y, meta={"parentAgentId": X, "spawnDepth": 2})
    return {X: RET, Y: RET}


def t1_nested_launch_without_a_structured_result(s):
    """18 of 38 real nested launches carry no toolUseResult; the notification links the child."""
    s.main(sync_launch("toolu_x", X, 1), note(20, Y, "toolu_y", "completed")).child(X)
    s.child(X, child_entries(X), use("toolu_y", 4), result("toolu_y", 5)).child(
        Y, meta={"parentAgentId": X})
    return {X: RET, Y: RET}


def t1_nested_without_notification_is_unestablished(s):
    s.main(sync_launch("toolu_x", X, 1)).child(X)
    s.child(X, child_entries(X), async_launch("toolu_y", Y, 4)).child(
        Y, meta={"parentAgentId": X})
    return {X: RET, Y: PEND}


def t1_nested_unreadable(s):
    s.main(sync_launch("toolu_x", X, 1), note(20, Y, "toolu_y", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("toolu_y", Y, 4)).child(
        Y, meta={"parentAgentId": X})
    s.unreadable = [X]
    return {Y: unknown("parent_unreadable")}


def t1_parent_names_no_subagent(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta={"parentAgentId": "a" + "0" * 16})
    return {X: unknown("parent_missing")}


def t1_parent_not_a_string(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta={"parentAgentId": 5})
    return {X: unknown("malformed")}


def t1_depth_two_without_parent(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta={"spawnDepth": 2})
    return {X: unknown("parent_missing")}


def t1_depth_one_is_main(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta={"spawnDepth": 1})
    return {X: RET}


# --- T2 launch --------------------------------------------------------------------------------


def t2_no_launch(s):
    s.main().child(X)
    return {X: unknown("no_evidence")}


def t2_two_launches(s):
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", X, 4)).child(X)
    return {X: unknown("conflict")}


def t2_duplicated_launch_id(s):
    s.main(use("toolu_1", 1), use("toolu_1", 2), result("toolu_1", 3, tur={"agentId": X})).child(X)
    return {X: unknown("conflict")}


def t2_two_distinct_results(s):
    s.main(
        use("toolu_1", 1), result("toolu_1", 2, tur={"agentId": X}),
        result("toolu_1", 3, tur={"agentId": X, "status": "completed", "extra": 1}),
    ).child(X)
    return {X: unknown("conflict")}


def t2_same_result_twice_is_one(s):
    s.main(
        use("toolu_1", 1), result("toolu_1", 2, tur={"agentId": X}),
        result("toolu_1", 2, tur={"agentId": X}),
    ).child(X)
    return {X: RET}


def t2_error_result(s):
    s.main(use("toolu_1", 1), result("toolu_1", 2, agent=X, is_error=True,
                                     tur={"agentId": X})).child(X)
    return {X: unknown("malformed")}


def t2_rejected_shape(s):
    s.main(use("toolu_1", 1), result("toolu_1", 2, agent=X, is_error=True, tur="rejected")).child(X)
    return {X: unknown("malformed")}


def t2_result_naming_another_agent(s):
    s.main(use("toolu_1", 1), result("toolu_1", 2, agent=X, tur={"agentId": Y})).child(X).child(Y)
    return {X: unknown("conflict"), Y: unknown("conflict")}


def t2_async_with_terminal_status_is_malformed(s):
    tur = {"agentId": X, "status": "completed", "isAsync": True}
    s.main(use("toolu_1", 1), result("toolu_1", 2, tur=tur)).child(X)
    return {X: unknown("malformed")}


def t2_malformed_result_non_string_id_only(s):
    """A result whose only id is not a string names nobody."""
    s.main(use("toolu_1", 1), result("toolu_1", 2, tur={"agentId": 5})).child(X)
    return {X: unknown("no_evidence")}


def t2_sync_completed(s):
    s.main(sync_launch("toolu_1", X, 1, "completed")).child(X)
    return {X: RET}


def t2_sync_no_status(s):
    s.main(sync_launch("toolu_1", X, 1, None)).child(X)
    return {X: RET}


def t2_sync_failed(s):
    s.main(sync_launch("toolu_1", X, 1, "failed")).child(X)
    return {X: FAIL}


def _sync_stopped(word):
    def build(s):
        s.main(sync_launch("toolu_1", X, 1, word)).child(X)
        return {X: KILL}
    return build


def t2_sync_unrecognized(s):
    s.main(sync_launch("toolu_1", X, 1, "weird")).child(X)
    return {X: unknown("status_unrecognized")}


def t2_sync_non_string_status(s):
    s.main(sync_launch("toolu_1", X, 1, 5)).child(X)
    return {X: unknown("malformed")}


def t2_async_no_notification(s):
    s.main(async_launch("toolu_1", X, 1)).child(X)
    return {X: PEND}


def t2_is_async_without_status(s):
    s.main(use("toolu_1", 1), result("toolu_1", 2, tur={"agentId": X, "isAsync": True})).child(X)
    return {X: PEND}


def t2_launch_notification_before_the_launch_line(s):
    s.main(note(1, X, "toolu_1", "completed"), async_launch("toolu_1", X, 2)).child(X)
    return {X: unknown("ambiguous")}


# --- T3 resume rounds -------------------------------------------------------------------------


def t3_delivered_resume_starts_a_round(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, X), resumed("toolu_s", 11, X)).child(X)
    return {X: PEND}


def t3_resumed_round_notification_returns(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X), resumed("toolu_s", 11, X),
        note(20, X, "toolu_s", "completed"),
    ).child(X)
    return {X: RET}


def t3_real_shape_to_and_recipient_equal_resume_returns(s):
    """The runtime's own SendMessage input carries both `to` and `recipient`, equal (71 of 71 in
    the real runs): a well-formed request, never malformed."""
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X, recipient=X),
        resumed("toolu_s", 11, X), note(20, X, "toolu_s", "completed"),
    ).child(X)
    return {X: RET}


def t3_real_shape_to_and_recipient_equal_queued_keeps_the_round(s):
    s.main(
        async_launch("toolu_1", X, 1), send("toolu_s", 10, X, recipient=X), queued("toolu_s", 11),
        note(20, X, "toolu_1", "completed"),
    ).child(X)
    return {X: RET}


def t3_earlier_round_never_decides(s):
    s.main(
        async_launch("toolu_1", X, 1), note(5, X, "toolu_1", "completed"),
        send("toolu_s", 10, X), resumed("toolu_s", 11, X),
    ).child(X)
    return {X: PEND}


def t3_earlier_round_failed_latest_completed(s):
    s.main(
        async_launch("toolu_1", X, 1), note(5, X, "toolu_1", "failed"),
        send("toolu_s", 10, X), resumed("toolu_s", 11, X), note(20, X, "toolu_s", "completed"),
    ).child(X)
    return {X: RET}


def t3_sync_failed_then_resume_completed(s):
    s.main(
        sync_launch("toolu_1", X, 1, "failed"), send("toolu_s", 10, X), resumed("toolu_s", 11, X),
        note(20, X, "toolu_s", "completed"),
    ).child(X)
    return {X: RET}


def t3_queued_message_starts_no_round(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, X), queued("toolu_s", 11)).child(X)
    return {X: RET}


def t3_rejected_request_has_no_effect(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X),
        result("toolu_s", 11, tur="no such agent", is_error=True),
    ).child(X)
    return {X: RET}


def t3_request_without_result(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, X)).child(X)
    return {X: unknown("delivery_unknown")}


def t3_two_distinct_results(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X), queued("toolu_s", 11),
        result("toolu_s", 12, tur={"success": True, "resumedAgentId": X}),
    ).child(X)
    return {X: unknown("delivery_unknown")}


def t3_request_naming_another_child_does_not_touch_this_one(s):
    s.main(
        sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3),
        send("toolu_s", 10, Y), resumed("toolu_s", 11, Y),
    ).child(X).child(Y)
    return {X: RET, Y: PEND}


def t3_resume_from_a_third_transcript(s):
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Z, 3)).child(X)
    s.child(Z, child_entries(Z), send("toolu_s", 10, X), resumed("toolu_s", 11, X))
    return {X: PEND, Z: RET}


def t3_target_differs_from_resumed_child(s):
    """G-A1 (GA-7): Y is no child of this session, so it is an agent name and contradicts nothing;
    the resume's `resumedAgentId` decides (was: malformed; the contradiction with another CHILD is
    pinned by t0_resume_target_mismatch_marks_both_children)."""
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, Y), resumed("toolu_s", 11, X)).child(X)
    return {X: PEND}


def t3_non_string_target(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, 5), resumed("toolu_s", 11, X)).child(X)
    return {X: unknown("malformed")}


def t3_to_and_recipient_differ(s):
    """G-A1 (GA-7): Y is no child of this session (was: malformed; two different CHILDREN are
    pinned by tests/test_g_a1_return.py::ga7_the_id_of_another_child_in_recipient_only)."""
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X, recipient=Y), resumed("toolu_s", 11, X),
    ).child(X)
    return {X: PEND}


def t3_resume_without_a_timestamp(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X, ts=False), resumed("toolu_s", 11, X),
    ).child(X)
    return {X: unknown("ambiguous")}


def t3_malformed_target_on_a_queued_request(s):
    """G-A1 (GA-7): Y is no child of this session, so the queued request is well formed (was:
    malformed; two different CHILDREN are pinned by
    tests/test_g_a1_return.py::ga7_to_and_recipient_naming_two_children_on_a_queued_request)."""
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X, recipient=Y), queued("toolu_s", 11),
    ).child(X)
    return {X: RET}


def t3_same_timestamp_rounds_are_ambiguous(s):
    s.main(
        sync_launch("toolu_1", X, 1),
        send("toolu_a", 10, X), resumed("toolu_a", 11, X),
        send("toolu_b", 10, X), resumed("toolu_b", 12, X),
    ).child(X)
    return {X: unknown("ambiguous")}


def t3_rounds_are_ordered_by_timestamp_not_by_line(s):
    """The later line holds the earlier request; the latest round by time decides."""
    s.main(
        async_launch("toolu_1", X, 1),
        send("toolu_late", 30, X), resumed("toolu_late", 31, X),
        send("toolu_early", 10, X), resumed("toolu_early", 32, X),
        note(40, X, "toolu_late", "completed"),
    ).child(X)
    return {X: RET}


# --- T4 notifications -------------------------------------------------------------------------


def t4_unknown_tool_use_id(s):
    s.main(async_launch("toolu_1", X, 1), note(5, X, "toolu_other", "completed")).child(X)
    return {X: unknown("conflict")}


def t4_a_notification_without_a_tool_use_id_is_not_accepted(s):
    """4.13.3 accepts only a whole envelope with one tool-use-id; this one is ignored."""
    s.main(async_launch("toolu_1", X, 1), note(5, X, None, "completed")).child(X)
    return {X: PEND}


def t4_a_notification_naming_a_queued_request_is_unknown_id(s):
    s.main(
        async_launch("toolu_1", X, 1), send("toolu_s", 10, X), queued("toolu_s", 11),
        note(20, X, "toolu_s", "completed"),
    ).child(X)
    return {X: unknown("conflict")}


def t4_a_notification_before_its_resume_request_is_ambiguous(s):
    s.main(
        async_launch("toolu_1", X, 1), note(5, X, "toolu_1", "completed"),
        note(8, X, "toolu_s", "completed"),
        send("toolu_s", 10, X), resumed("toolu_s", 11, X),
    ).child(X)
    return {X: unknown("ambiguous")}


def t4_a_nested_notification_older_than_the_launch_is_ambiguous(s):
    """The launch is in another transcript, so the notification is ordered by timestamp."""
    s.main(sync_launch("toolu_x", X, 1), note(2, Y, "toolu_y", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("toolu_y", Y, 4)).child(
        Y, meta={"parentAgentId": X})
    return {X: RET, Y: unknown("ambiguous")}


def t4_all_failed(s):
    s.main(async_launch("toolu_1", X, 1), note(5, X, "toolu_1", "failed")).child(X)
    return {X: FAIL}


def _note_stopped(word):
    def build(s):
        s.main(async_launch("toolu_1", X, 1), note(5, X, "toolu_1", word)).child(X)
        return {X: KILL}
    return build


def t4_mixed(s):
    s.main(
        async_launch("toolu_1", X, 1), note(5, X, "toolu_1", "completed"),
        note(6, X, "toolu_1", "failed"),
    ).child(X)
    return {X: unknown("conflict")}


def t4_unrecognized(s):
    s.main(async_launch("toolu_1", X, 1), note(5, X, "toolu_1", "weird")).child(X)
    return {X: unknown("status_unrecognized")}


def t4_two_delivery_forms_are_one(s):
    s.main(
        async_launch("toolu_1", X, 1), note(5, X, "toolu_1", "completed", "queue"),
        note(6, X, "toolu_1", "completed", "origin"),
    ).child(X)
    return {X: RET}


def t4_quoted_envelope_is_not_accepted(s):
    quoted = {
        "type": "user", "origin": {"kind": "task-notification"},
        "message": {"role": "user", "content": "as quoted: " + envelope(X, "toolu_1", "completed")
                    + " (end)"},
        "timestamp": "2026-09-01T12:00:09Z",
    }
    s.main(async_launch("toolu_1", X, 1), quoted).child(X)
    return {X: PEND}


def t4_notification_for_another_child(s):
    s.main(async_launch("toolu_1", X, 1), note(5, Y, "toolu_1", "completed")).child(X).child(Y)
    return {X: PEND, Y: unknown("conflict")}


# --- T5 decision ------------------------------------------------------------------------------


def t5_latest_round_empty(s):
    s.main(async_launch("toolu_1", X, 1), send("toolu_s", 10, X), resumed("toolu_s", 11, X)).child(X)
    return {X: PEND}


def t5_an_earlier_unknown_wins(s):
    s.main(
        sync_launch("toolu_1", X, 1), sync_launch("toolu_2", X, 4), note(5, X, "toolu_1", "completed"),
    ).child(X)
    return {X: unknown("conflict")}


def t5_ambiguity_never_clears(s):
    s.main(
        sync_launch("toolu_1", X, 1),
        send("toolu_a", 10, X), resumed("toolu_a", 11, X),
        send("toolu_b", 10, X), resumed("toolu_b", 12, X),
        note(40, X, "toolu_b", "completed"),
    ).child(X)
    return {X: unknown("ambiguous")}


# --- T0 measured shapes only, and the rev 6 rows of T3 and T5 -----------------------------------


def t0_meta_unreadable(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta="{not json")
    return {X: unknown("malformed")}


def t0_meta_not_an_object(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta="[1]")
    return {X: unknown("malformed")}


def t0_parent_empty_string(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta={"parentAgentId": ""})
    return {X: unknown("malformed")}


def t0_parent_is_the_child_itself(s):
    s.main(sync_launch("toolu_1", X, 1)).child(X, meta={"parentAgentId": X})
    return {X: unknown("malformed")}


def _depth_row(depth):
    def build(s):
        s.main(sync_launch("toolu_1", X, 1)).child(X, meta={"spawnDepth": depth})
        return {X: unknown("malformed")}
    return build


def t0_launch_result_with_a_non_string_second_id(s):
    s.main(use("toolu_1", 1), result("toolu_1", 2, agent=X, tur={"agentId": 5})).child(X)
    return {X: unknown("malformed")}


def t0_launch_result_not_an_object(s):
    s.main(use("toolu_1", 1), result("toolu_1", 2, agent=X, tur=[1])).child(X)
    return {X: unknown("malformed")}


def t0_launch_result_null(s):
    s.main(use("toolu_1", 1), result("toolu_1", 2, agent=X, tur=None)).child(X)
    return {X: unknown("malformed")}


def t0_launch_status_null(s):
    s.main(sync_launch("toolu_1", X, 1, NULL)).child(X)
    return {X: unknown("malformed")}


def t0_send_without_a_target(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, ABSENT), resumed("toolu_s", 11, X)).child(X)
    return {X: unknown("malformed")}


def t0_send_null_target(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, None), resumed("toolu_s", 11, X)).child(X)
    return {X: unknown("malformed")}


def t0_resume_target_mismatch_marks_both_children(s):
    s.main(
        sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3),
        send("toolu_s", 10, Y), resumed("toolu_s", 11, X),
    ).child(X).child(Y)
    return {X: unknown("malformed"), Y: unknown("malformed")}


SEND_RESULT_SHAPES = {
    "success_only": {"success": True},
    "success_false": {"success": False, "resumedAgentId": X},
    "resumed_not_a_string": {"success": True, "resumedAgentId": 5},
    "queued_with_another_message": {"success": True, "message": "sent"},
    "queued_with_a_resumed_id": {
        "success": True, "resumedAgentId": None, "message": "Message queued for delivery"},
    "a_string": "ok",
    "a_list": [1],
}


def _send_shape_row(shape):
    def build(s):
        s.main(
            sync_launch("toolu_1", X, 1), send("toolu_s", 10, X),
            result("toolu_s", 11, tur=SEND_RESULT_SHAPES[shape]),
        ).child(X)
        return {X: unknown("malformed")}
    return build


def t0_send_result_without_a_structured_result(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, X), result("toolu_s", 11)).child(X)
    return {X: unknown("malformed")}


def t3_a_resume_key_carried_by_two_tool_use_blocks(s):
    s.main(
        sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Z, 3),
        send("toolu_s", 10, X), resumed("toolu_s", 11, X),
    ).child(X)
    s.child(Z, child_entries(Z), send("toolu_s", 12, X))
    return {X: unknown("conflict")}


def t3_the_launch_has_no_timestamp_and_the_child_was_resumed(s):
    s.main(
        sync_launch("toolu_1", X, 1, ts=False), send("toolu_s", 10, X), resumed("toolu_s", 11, X),
    ).child(X)
    return {X: unknown("ambiguous")}


def t3_a_resume_older_than_the_launch(s):
    s.main(
        sync_launch("toolu_1", X, 20), send("toolu_s", 10, X), resumed("toolu_s", 11, X),
    ).child(X)
    return {X: unknown("ambiguous")}


def t3_an_unparsable_resume_timestamp(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, X, ts="not a time"),
        resumed("toolu_s", 11, X),
    ).child(X)
    return {X: unknown("ambiguous")}


def t5_a_synchronous_value_and_a_different_notification_conflict(s):
    s.main(sync_launch("toolu_1", X, 1), note(5, X, "toolu_1", "failed")).child(X)
    return {X: unknown("conflict")}


def t5_a_synchronous_failure_and_a_failed_notification_agree(s):
    s.main(sync_launch("toolu_1", X, 1, "failed"), note(5, X, "toolu_1", "failed")).child(X)
    return {X: FAIL}


def t5_a_synchronous_value_and_the_same_notification_agree(s):
    s.main(sync_launch("toolu_1", X, 1), note(5, X, "toolu_1", "completed")).child(X)
    return {X: RET}


ROWS = {
    name: fn for name, fn in sorted(globals().items())
    if re.fullmatch(r"t[0-5]_\w+", name) and callable(fn)
}
for _depth in ["2", 0, True, 1.5, None, -1]:
    ROWS[f"t0_spawn_depth_{_depth!r}"] = _depth_row(_depth)
for _shape in SEND_RESULT_SHAPES:
    ROWS[f"t0_send_result_{_shape}"] = _send_shape_row(_shape)
for _word in ("killed", "stopped", "cancelled"):
    ROWS[f"t2_sync_{_word}"] = _sync_stopped(_word)
    ROWS[f"t4_notification_{_word}"] = _note_stopped(_word)


@pytest.mark.parametrize("name", sorted(ROWS))
def test_return_state_row(name, tmp_path):
    s = Session(tmp_path)
    expected = ROWS[name](s)
    main = s.build()
    for child in getattr(s, "unreadable", []):
        os.chmod(s.child_path(child), 0)
    try:
        got = states(main)
    finally:
        for child in getattr(s, "unreadable", []):
            os.chmod(s.child_path(child), 0o644)
    for child, pair in expected.items():
        assert got[child] == pair, (name, child)


def test_every_plan_row_is_present():
    """The named rows of the plan's acceptance for T1 to T5 are all here."""
    needed = {
        "t1_main_launcher", "t1_nested_readable", "t1_nested_unreadable",
        "t1_parent_names_no_subagent", "t1_parent_not_a_string", "t1_depth_two_without_parent",
        "t2_no_launch", "t2_two_launches", "t2_duplicated_launch_id", "t2_two_distinct_results",
        "t2_error_result", "t2_rejected_shape", "t2_sync_completed", "t2_sync_no_status",
        "t2_sync_failed", "t2_sync_killed", "t2_sync_stopped", "t2_sync_cancelled",
        "t2_sync_unrecognized", "t2_async_no_notification",
        "t3_delivered_resume_starts_a_round", "t3_queued_message_starts_no_round",
        "t3_rejected_request_has_no_effect", "t3_request_without_result",
        "t3_two_distinct_results", "t3_same_timestamp_rounds_are_ambiguous",
        "t4_unknown_tool_use_id", "t4_all_failed", "t4_mixed", "t4_unrecognized",
        "t4_notification_killed", "t5_latest_round_empty", "t5_an_earlier_unknown_wins",
        "t5_ambiguity_never_clears",
    }
    assert needed <= set(ROWS)


# --- resumes, the compatibility predicate, and the view ------------------------------------------


def test_resumes_counts_delivered_resumes_only(tmp_path):
    s = Session(tmp_path)
    s.main(
        sync_launch("toolu_1", X, 1),
        send("toolu_a", 10, X), resumed("toolu_a", 11, X),
        send("toolu_b", 20, X), queued("toolu_b", 21),
        send("toolu_c", 30, X), resumed("toolu_c", 31, X),
    ).child(X).child(Y)
    run = reader.load_session(s.build())
    assert {a.agent_id: a.resumes for a in run.attempts} == {X: 2, Y: 0}


def test_returned_to_parent_is_exactly_the_returned_state(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1), async_launch("toolu_2", Y, 4)).child(X).child(Y)
    run = reader.load_session(s.build())
    for attempt in run.attempts:
        assert attempt.returned_to_parent is (attempt.return_state == "returned")
    assert run.main.return_state is None and run.main.resumes == 0


def test_a_workflow_attempt_has_no_return_state(tmp_path, monkeypatch):
    from tests.n5b_helpers import Spec, build_workflow

    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-a20", [Spec("v2:x", 1)])
    run = reader.load_run(run_dir)
    assert [(a.return_state, a.return_reason, a.resumes) for a in run.attempts] == [(None, None, 0)]


def test_a_view_of_named_subagents_decides_the_same(tmp_path):
    s = Session(tmp_path)
    s.main(
        sync_launch("toolu_x", X, 1), note(20, Y, "toolu_y", "failed"),
        send("toolu_s", 30, X), resumed("toolu_s", 31, X),
    ).child(X)
    s.child(X, child_entries(X), async_launch("toolu_y", Y, 4)).child(
        Y, meta={"parentAgentId": X})
    main = s.build()
    view = reader.load_session_view(main, [s.child_path(Y)])
    assert [(a.return_state, a.return_reason) for a in view.attempts] == [("reported_failed", "none")]
    assert [a.agent_id for a in view.attempts] == [Y]
    full = {a.agent_id: a.resumes for a in reader.load_session(main).attempts}
    assert full[X] == 1
