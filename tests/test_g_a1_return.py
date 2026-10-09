"""G-A1 (fix round for G-A, docs/DESIGN.md 4.13.1 "Return evidence"): GA-1 an unreadable issuer,
GA-4 a non-string tool_use id, GA-6 and GA-12 the rows the G-A mutation review found missing,
GA-7 a SendMessage addressed by an agent name.

One row per named case; every row builds a synthetic session in tmp_path. Fixtures use the
runtime's real SendMessage shape (both `to` and `recipient`, equal) unless a row tests otherwise.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from agent_postmortem import reader
from tests.g_helpers import (
    Session,
    X,
    Y,
    Z,
    async_launch,
    child_entries,
    note,
    queued,
    result,
    resumed,
    send,
    states,
    sync_launch,
    use,
)
from tests.n5c2_helpers import run_cli

RET = ("returned", "none")
PEND = ("pending", "none")
FAIL = ("reported_failed", "none")
KILL = ("reported_killed", "none")
NAME = "worker-one"


def unknown(reason):
    return ("unknown", reason)


# --- GA-1: an issuer transcript that cannot be read at all -------------------------------------


def ga1_unreadable_third_transcript_makes_every_child_unknown(s):
    """Z may hold a resume of X that nobody can see: X, Y and Z are all unknown."""
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Z, 3), sync_launch("toolu_3", Y, 6))
    s.child(X).child(Y)
    s.child(Z, child_entries(Z), send("toolu_s", 10, X, recipient=X), resumed("toolu_s", 11, X))
    s.unreadable = [Z]
    return {X: unknown("parent_unreadable"), Y: unknown("parent_unreadable"),
            Z: unknown("parent_unreadable")}


def ga1_readable_third_transcript_shows_the_resume(s):
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Z, 3), sync_launch("toolu_3", Y, 6))
    s.child(X).child(Y)
    s.child(Z, child_entries(Z), send("toolu_s", 10, X, recipient=X), resumed("toolu_s", 11, X))
    return {X: PEND, Y: RET, Z: RET}


def ga1_a_half_written_last_line_of_a_readable_issuer_is_only_a_limit(s):
    """A live transcript often ends in a half line; failing every child for it would make every
    live session unknown. The resume above the half line is still seen."""
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Z, 3))
    s.child(X)
    s.child(Z, child_entries(Z), send("toolu_s", 10, X, recipient=X), resumed("toolu_s", 11, X))
    s.tails = {Z: '{"type": "assistant", "message": {"role"'}
    return {X: PEND, Z: RET}


def ga1_a_malformed_line_in_a_readable_issuer_hides_nothing_else(s):
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Z, 3))
    s.child(X).child(Z)
    s.tails = {Z: "not json at all"}
    return {X: RET, Z: RET}


# --- GA-4: a non-string tool_use id ------------------------------------------------------------

BAD_IDS = {"numeric": 7, "null": None, "boolean": True}


def _bad_send_without_result(bad):
    def build(s):
        s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3))
        s.main(use(bad, 10, "SendMessage", {"to": X, "recipient": X}))
        s.child(X).child(Y)
        return {X: unknown("malformed"), Y: RET}
    return build


def _bad_send_with_resume_result(bad):
    def build(s):
        s.main(sync_launch("toolu_1", X, 1))
        s.main(use(bad, 10, "SendMessage", {"to": X, "recipient": X}))
        s.main(result(bad, 11, tur={"success": True, "resumedAgentId": X}))
        s.child(X)
        return {X: unknown("malformed")}
    return build


def _bad_launch_naming_the_child(tool, bad):
    def build(s):
        s.main(use(bad, 1, tool), result(bad, 2, tur={"agentId": X}))
        s.child(X)
        return {X: unknown("malformed")}
    return build


def _bad_launch_beside_a_valid_one(tool, bad):
    def build(s):
        s.main(sync_launch("toolu_1", X, 1))
        s.main(use(bad, 4, tool), result(bad, 5, tur={"agentId": X}))
        s.child(X)
        return {X: unknown("malformed")}
    return build


def _bad_launch_without_a_result(tool, bad):
    def build(s):
        s.main(sync_launch("toolu_1", X, 1), use(bad, 5, tool))
        s.child(X)
        return {X: RET}
    return build


def _bad_send_to_a_name(bad):
    def build(s):
        s.main(sync_launch("toolu_1", X, 1))
        s.main(use(bad, 10, "SendMessage", {"to": NAME, "recipient": NAME}))
        s.child(X)
        return {X: RET}
    return build


def ga4_sendmessage_numeric_id_in_a_subagent_transcript(s):
    """The request is in a subagent's own transcript, not the main one."""
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Z, 3)).child(X)
    s.child(Z, child_entries(Z), use(7, 10, "SendMessage", {"to": X, "recipient": X}))
    return {X: unknown("malformed"), Z: RET}


def ga4_agent_null_id_in_a_nested_launcher_names_the_nested_child(s):
    s.main(sync_launch("toolu_x", X, 1), note(20, Y, "toolu_y", "completed")).child(X)
    s.child(X, child_entries(X), use(None, 4), result(None, 5, tur={"agentId": Y}))
    s.child(Y, meta={"parentAgentId": X})
    return {X: RET, Y: unknown("malformed")}


# --- GA-7: a SendMessage addressed by an agent name --------------------------------------------


def ga7_a_name_in_to_and_recipient_resumes_the_resumed_child(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME, recipient=NAME),
        resumed("toolu_s", 11, X),
    ).child(X)
    return {X: PEND}


def ga7_a_name_in_to_only(s):
    s.main(sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME), resumed("toolu_s", 11, X)).child(X)
    return {X: PEND}


def ga7_a_name_in_to_and_the_id_in_recipient(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME, recipient=X),
        resumed("toolu_s", 11, X),
    ).child(X)
    return {X: PEND}


def ga7_two_different_names(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME, recipient="other-name"),
        resumed("toolu_s", 11, X),
    ).child(X)
    return {X: PEND}


def ga7_a_name_resume_whose_notification_returns(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME, recipient=NAME),
        resumed("toolu_s", 11, X), note(20, X, "toolu_s", "completed"),
    ).child(X)
    return {X: RET}


def ga7_a_name_resume_whose_notification_failed(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME, recipient=NAME),
        resumed("toolu_s", 11, X), note(20, X, "toolu_s", "failed"),
    ).child(X)
    return {X: FAIL}


def ga7_the_id_of_another_child_is_still_a_contradiction(s):
    s.main(
        sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3),
        send("toolu_s", 10, Y, recipient=Y), resumed("toolu_s", 11, X),
    ).child(X).child(Y)
    return {X: unknown("malformed"), Y: unknown("malformed")}


def ga7_the_id_of_another_child_in_recipient_only(s):
    s.main(
        sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3),
        send("toolu_s", 10, X, recipient=Y), resumed("toolu_s", 11, X),
    ).child(X).child(Y)
    return {X: unknown("malformed"), Y: unknown("malformed")}


def ga7_to_and_recipient_naming_two_children_on_a_queued_request(s):
    s.main(
        sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3),
        send("toolu_s", 10, X, recipient=Y), queued("toolu_s", 11),
    ).child(X).child(Y)
    return {X: unknown("malformed"), Y: unknown("malformed")}


def ga7_a_name_on_a_queued_request_has_no_effect(s):
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME, recipient=NAME),
        queued("toolu_s", 11),
    ).child(X)
    return {X: RET}


def ga7_an_id_that_is_no_child_of_the_session_is_a_name(s):
    """Y has no transcript or meta file in this session."""
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, Y, recipient=Y), resumed("toolu_s", 11, X),
    ).child(X)
    return {X: PEND}


# --- GA-12: nested launches the G-A rows left out ------------------------------------------------


def _nested_without_result(word, expected):
    def build(s):
        s.main(sync_launch("toolu_x", X, 1), note(20, Y, "toolu_y", word)).child(X)
        s.child(X, child_entries(X), use("toolu_y", 4), result("toolu_y", 5)).child(
            Y, meta={"parentAgentId": X})
        return {X: RET, Y: expected}
    return build


def ga12_a_nested_notification_at_the_instant_of_its_request_is_ambiguous(s):
    """The launch is in another transcript, so the notification is ordered by timestamp, and an
    equal instant is not after the request (iso(4) is the nested launch's own timestamp)."""
    s.main(sync_launch("toolu_x", X, 1), note(4, Y, "toolu_y", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("toolu_y", Y, 4)).child(Y, meta={"parentAgentId": X})
    return {X: RET, Y: unknown("ambiguous")}


def ga12_a_nested_notification_one_second_after_its_request_returns(s):
    s.main(sync_launch("toolu_x", X, 1), note(5, Y, "toolu_y", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("toolu_y", Y, 4)).child(Y, meta={"parentAgentId": X})
    return {X: RET, Y: RET}


ROWS = {
    name: fn for name, fn in sorted(globals().items())
    if re.fullmatch(r"ga(1|4|7|12)_\w+", name) and callable(fn)
}
for _label, _bad in BAD_IDS.items():
    ROWS[f"ga4_sendmessage_{_label}_id_without_a_result"] = _bad_send_without_result(_bad)
    ROWS[f"ga4_sendmessage_{_label}_id_with_a_resume_result"] = _bad_send_with_resume_result(_bad)
    ROWS[f"ga4_sendmessage_{_label}_id_to_a_name_touches_no_child"] = _bad_send_to_a_name(_bad)
    for _tool in ("Agent", "Task"):
        _key = f"ga4_{_tool.lower()}_{_label}_id"
        ROWS[f"{_key}_result_names_the_child"] = _bad_launch_naming_the_child(_tool, _bad)
        ROWS[f"{_key}_result_beside_a_valid_launch"] = _bad_launch_beside_a_valid_one(_tool, _bad)
        ROWS[f"{_key}_without_a_result_names_no_child"] = _bad_launch_without_a_result(_tool, _bad)
for _word, _expected in (("failed", FAIL), ("killed", KILL), ("stopped", KILL)):
    ROWS[f"ga12_nested_launch_without_a_result_{_word}_notification"] = _nested_without_result(
        _word, _expected)


def build_rows(name, tmp_path):
    s = Session(tmp_path)
    expected = ROWS[name](s)
    main = s.build()
    for child, tail in getattr(s, "tails", {}).items():
        with s.child_path(child).open("a", encoding="utf-8") as handle:
            handle.write(tail)
    return s, main, expected


@pytest.mark.parametrize("name", sorted(ROWS))
def test_a1_return_state_row(name, tmp_path):
    s, main, expected = build_rows(name, tmp_path)
    for child in getattr(s, "unreadable", []):
        os.chmod(s.child_path(child), 0)
    try:
        got = states(main)
    finally:
        for child in getattr(s, "unreadable", []):
            os.chmod(s.child_path(child), 0o644)
    for child, pair in expected.items():
        assert got[child] == pair, (name, child)


def test_every_accepted_row_of_the_a1_list_is_present():
    needed = {
        "ga1_unreadable_third_transcript_makes_every_child_unknown",
        "ga1_a_half_written_last_line_of_a_readable_issuer_is_only_a_limit",
        "ga4_sendmessage_numeric_id_without_a_result", "ga4_sendmessage_null_id_without_a_result",
        "ga4_agent_numeric_id_result_names_the_child", "ga4_agent_null_id_result_names_the_child",
        "ga4_task_numeric_id_result_names_the_child", "ga4_task_null_id_result_names_the_child",
        "ga7_a_name_in_to_and_recipient_resumes_the_resumed_child",
        "ga7_the_id_of_another_child_is_still_a_contradiction",
        "ga12_a_nested_notification_at_the_instant_of_its_request_is_ambiguous",
        "ga12_nested_launch_without_a_result_failed_notification",
        "ga12_nested_launch_without_a_result_killed_notification",
    }
    assert needed <= set(ROWS)


# --- resumes -----------------------------------------------------------------------------------


def test_a_resume_addressed_by_a_name_is_counted(tmp_path):
    s = Session(tmp_path)
    s.main(
        sync_launch("toolu_1", X, 1), send("toolu_s", 10, NAME, recipient=NAME),
        resumed("toolu_s", 11, X),
    ).child(X)
    run = reader.load_session(s.build())
    assert [a.resumes for a in run.attempts] == [1]


UNDELIVERED = {
    "rejected": lambda: [
        send("toolu_s", 10, X, recipient=X),
        result("toolu_s", 11, tur="no such agent", is_error=True),
    ],
    "queued": lambda: [send("toolu_s", 10, X, recipient=X), queued("toolu_s", 11)],
    "result_less": lambda: [send("toolu_s", 10, X, recipient=X)],
    "malformed_target": lambda: [send("toolu_s", 10, 5), resumed("toolu_s", 11, X)],
    "two_children_named": lambda: [
        send("toolu_s", 10, X, recipient=Y), resumed("toolu_s", 11, X)],
    "unrecognized_result": lambda: [
        send("toolu_s", 10, X, recipient=X), result("toolu_s", 11, tur={"success": True})],
}


@pytest.mark.parametrize("name", sorted(UNDELIVERED))
def test_resumes_does_not_count_an_undelivered_request(name, tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1), *UNDELIVERED[name]()).child(X).child(Y)
    run = reader.load_session(s.build())
    assert {a.agent_id: a.resumes for a in run.attempts} == {X: 0, Y: 0}


# --- public output of the new unknowns ---------------------------------------------------------


def _run_json(capsys, s):
    code, out, _err = run_cli(
        capsys, ["run", str(s.main_path), "--root", str(s.root), "--json", "--raw"])
    return code, json.loads(out)


def test_an_unreadable_issuer_reaches_json_end_and_work_at_risk(tmp_path, capsys):
    name = "ga1_unreadable_third_transcript_makes_every_child_unknown"
    s, _main, _expected = build_rows(name, tmp_path)
    os.chmod(s.child_path(Z), 0)
    try:
        code, payload = _run_json(capsys, s)
        _code, text, _err = run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root)])
    finally:
        os.chmod(s.child_path(Z), 0o644)
    got = {a["agent_id"]: a for a in payload["run"]["attempts"]}
    for child in (X, Y, Z):
        assert (got[child]["return_state"], got[child]["return_reason"]) == (
            "unknown", "parent_unreadable"), child
        assert got[child]["outcome"] == "unknown", child
    assert "WORK AT RISK  unknown" in text
    assert "possibly running" in text


def test_a_non_string_send_id_reaches_json_end_and_work_at_risk(tmp_path, capsys):
    s, _main, _expected = build_rows("ga4_sendmessage_numeric_id_without_a_result", tmp_path)
    _code, payload = _run_json(capsys, s)
    _code, text, _err = run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root)])
    got = {a["agent_id"]: a for a in payload["run"]["attempts"]}
    assert (got[X]["return_state"], got[X]["return_reason"], got[X]["outcome"]) == (
        "unknown", "malformed", "unknown")
    assert (got[Y]["return_state"], got[Y]["outcome"]) == ("returned", "completed")
    assert "WORK AT RISK  unknown" in text


# --- DESIGN words (GA-9, GA-10) ----------------------------------------------------------------


def _design():
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md").read_text(
        encoding="utf-8")


def test_design_4_7_has_no_never_returned_sentence_left():
    text = _design()
    assert "never returned a result to the main" not in text
    assert "no result returned to the main session" not in text


def test_design_4_3_says_a_reported_killed_child_adds_no_finding_and_why():
    text = _design()
    assert "A `reported_killed` child adds no finding" in text
    assert "deliberate stop" in text or "usually a deliberate stop" in text
