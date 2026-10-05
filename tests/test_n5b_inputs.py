"""N5b: how the derived severity inputs are read (docs/DESIGN.md 4.3 "How the
derived inputs are read", 4.13.4): `recovered`, `work_risk` (S2's order),
`absence`, `position`, and the run's terminal state.
"""

from __future__ import annotations

import os

import pytest

from tests.n5b_helpers import (
    ledger_of,
    model_attempt,
    model_session_run,
    model_workflow_run,
    severity,
)

# --- the run is terminal ---------------------------------------------------------------


@pytest.mark.parametrize("terminal,expected", [
    ("completed", True), ("failed", True), ("stopped", True), ("journal_resolved", True),
    ("unknown", False), ("none", False), (None, False),
])
def test_run_is_terminal(terminal, expected):
    run = model_workflow_run([model_attempt(1)], terminal=terminal)
    assert severity.run_is_terminal(run) is expected


def test_a_session_is_never_terminal():
    assert severity.run_is_terminal(model_session_run([model_attempt(1)])) is False


# --- recovered: yes ----------------------------------------------------------------------


def _key_run(*attempts, terminal="completed"):
    return model_workflow_run(list(attempts), terminal=terminal)


def test_recovered_yes_when_a_later_attempt_was_closed_with_a_result():
    first = model_attempt(1, ordinal=1, outcome="killed", journal_state="superseded")
    second = model_attempt(2, ordinal=2, outcome="completed", journal_state="completed")
    run = _key_run(first, second)
    assert severity.recovered(run, first, include_self=False) == "yes"


def test_recovered_yes_when_any_later_attempt_completed_even_if_the_last_did_not():
    first = model_attempt(1, ordinal=1, outcome="failed", journal_state="failed")
    second = model_attempt(2, ordinal=2, outcome="completed", journal_state="completed")
    third = model_attempt(3, ordinal=3, outcome="killed", journal_state="superseded")
    run = _key_run(first, second, third)
    assert severity.recovered(run, first, include_self=False) == "yes"


def test_a_later_attempt_that_only_looks_completed_in_its_transcript_is_not_a_recovery():
    first = model_attempt(1, ordinal=1, outcome="failed", journal_state="failed")
    second = model_attempt(2, ordinal=2, outcome="completed", journal_state="open")
    assert severity.recovered(_key_run(first, second, terminal="completed"), first,
                              include_self=False) == "no"
    assert severity.recovered(_key_run(first, second, terminal="none"), first,
                              include_self=False) == "unknown"


def test_a_later_attempt_with_no_journal_record_is_not_a_recovery():
    first = model_attempt(1, ordinal=1, outcome="failed", journal_state="failed")
    second = model_attempt(2, ordinal=2, outcome="completed", journal_state=None)
    assert severity.recovered(_key_run(first, second, terminal="none"), first,
                              include_self=False) == "unknown"


def test_a_candidate_of_another_key_never_counts():
    first = model_attempt(1, key="v2:a", ordinal=1, outcome="failed", journal_state="failed")
    other = model_attempt(2, key="v2:b", ordinal=2, outcome="completed", journal_state="completed")
    run = _key_run(first, other)
    assert severity.recovered(run, first, include_self=False) == "no"


def test_an_earlier_attempt_that_completed_is_not_a_later_one():
    earlier = model_attempt(1, ordinal=1, outcome="completed", journal_state="completed")
    later = model_attempt(2, ordinal=2, outcome="failed", journal_state="failed")
    run = _key_run(earlier, later)
    assert severity.recovered(run, later, include_self=False) == "no"


# --- recovered: no and unknown -----------------------------------------------------------


@pytest.mark.parametrize("outcome,state", [
    ("failed", "failed"), ("killed", "superseded"), ("superseded", "superseded"),
    ("killed", "open"),
])
def test_recovered_no_when_the_last_attempt_ended_without_completing(outcome, state):
    first = model_attempt(1, ordinal=1, outcome="killed", journal_state="superseded")
    last = model_attempt(2, ordinal=2, outcome=outcome, journal_state=state)
    for terminal in ("completed", "none", "unknown"):
        run = _key_run(first, last, terminal=terminal)
        assert severity.recovered(run, first, include_self=False) == "no"


def test_recovered_unknown_when_the_last_attempt_has_no_terminal_record_and_the_run_is_not_terminal():
    first = model_attempt(1, ordinal=1, outcome="killed", journal_state="superseded")
    last = model_attempt(2, ordinal=2, outcome="completed", journal_state="open")
    for terminal in ("none", "unknown", None):
        run = _key_run(first, last, terminal=terminal)
        assert severity.recovered(run, first, include_self=False) == "unknown"


def test_recovered_no_when_the_last_attempt_has_no_terminal_record_but_the_run_is_terminal():
    first = model_attempt(1, ordinal=1, outcome="killed", journal_state="superseded")
    last = model_attempt(2, ordinal=2, outcome="completed", journal_state="open")
    for terminal in ("completed", "failed", "stopped", "journal_resolved"):
        run = _key_run(first, last, terminal=terminal)
        assert severity.recovered(run, first, include_self=False) == "no"


def test_recovered_unknown_for_an_attempt_with_no_transcript_in_a_running_run():
    only = model_attempt(1, ordinal=1, outcome="unknown", journal_state="open")
    assert severity.recovered(_key_run(only, terminal="none"), only, include_self=True) == "unknown"


# --- recovered: include_self (structured_output_failure) -----------------------------------


def test_include_self_counts_the_attempt_that_completed_with_a_result():
    only = model_attempt(1, ordinal=1, outcome="completed", journal_state="completed")
    run = _key_run(only)
    assert severity.recovered(run, only, include_self=True) == "yes"
    assert severity.recovered(run, only, include_self=False) != "yes"


def test_include_self_does_not_count_an_attempt_that_failed_or_is_open():
    failed = model_attempt(1, ordinal=1, outcome="failed", journal_state="failed")
    assert severity.recovered(_key_run(failed), failed, include_self=True) == "no"
    open_one = model_attempt(2, ordinal=1, outcome="completed", journal_state="open")
    assert severity.recovered(_key_run(open_one, terminal="none"), open_one,
                              include_self=True) == "unknown"
    assert severity.recovered(_key_run(open_one, terminal="completed"), open_one,
                              include_self=True) == "no"


# --- recovered: sessions and unkeyed attempts -------------------------------------------------


def test_a_session_subagent_never_completed_with_a_result():
    completed = model_attempt(1, key="agent", outcome="completed", journal_state=None)
    run = model_session_run([completed])
    assert severity.recovered(run, completed, include_self=True) == "unknown"


def test_a_killed_session_subagent_did_not_recover():
    killed = model_attempt(1, key="agent", outcome="killed", journal_state=None)
    assert severity.recovered(model_session_run([killed]), killed, include_self=True) == "no"


def test_the_session_main_thread_is_read_like_a_keyless_attempt():
    main = model_attempt(1, key=None, outcome="completed", journal_state=None)
    run = model_session_run([], main=main)
    assert severity.recovered(run, main, include_self=True) == "unknown"
    killed_main = model_attempt(2, key=None, outcome="killed", journal_state=None)
    assert severity.recovered(model_session_run([], main=killed_main), killed_main,
                              include_self=True) == "no"


def test_an_unkeyed_workflow_attempt_is_alone():
    lone = model_attempt(1, key=None, outcome="failed", journal_state=None)
    other = model_attempt(2, key=None, outcome="completed", journal_state="completed")
    run = model_workflow_run([lone, other])
    assert severity.recovered(run, lone, include_self=False) == "no"


def test_a_transcript_less_session_attempt_is_unknown():
    absent = model_attempt(1, key="agent", outcome="unknown", journal_state=None)
    assert severity.recovered(model_session_run([absent]), absent, include_self=True) == "unknown"


# --- position -------------------------------------------------------------------------------------


def test_position_is_final_without_a_later_attempt_of_the_key_or_without_a_key():
    first = model_attempt(1, ordinal=1)
    second = model_attempt(2, ordinal=2)
    run = _key_run(first, second)
    assert severity.position(run, first) == "non_final"
    assert severity.position(run, second) == "final"
    keyless = model_attempt(3, key=None, ordinal=1)
    assert severity.position(model_workflow_run([keyless]), keyless) == "final"


def test_position_ignores_a_later_attempt_of_another_key():
    first = model_attempt(1, key="v2:a", ordinal=1)
    other = model_attempt(2, key="v2:b", ordinal=2)
    assert severity.position(_key_run(first, other), first) == "final"


def test_position_of_a_session_subagent_is_final():
    sub = model_attempt(1, key="agent", ordinal=1)
    assert severity.position(model_session_run([sub]), sub) == "final"


# --- work_risk: S2's order ----------------------------------------------------------------------------


EFFECTS = ("observed", "claimed_target", "attempted")


def _risk_run(ledger, *, completeness="complete"):
    attempt = model_attempt(1, outcome="killed", journal_state="superseded", ledger=ledger)
    return model_workflow_run([attempt], completeness=completeness)


@pytest.mark.parametrize("effect", EFFECTS)
def test_partial_population_with_each_effect_is_unknown(effect):
    run = _risk_run(ledger_of(effect), completeness="partial")
    assert severity.work_risk(run) == "unknown"


@pytest.mark.parametrize("effect", EFFECTS)
def test_partial_ledger_with_each_effect_is_unknown(effect):
    run = _risk_run(ledger_of(effect, completeness="partial"))
    assert severity.work_risk(run) == "unknown"


@pytest.mark.parametrize("effect", EFFECTS)
def test_complete_ledger_with_each_effect_is_present(effect):
    assert severity.work_risk(_risk_run(ledger_of(effect))) == "present"


def test_complete_and_empty_is_none():
    assert severity.work_risk(_risk_run(ledger_of())) == "none"


def test_an_empty_ledger_in_an_incomplete_population_is_unknown():
    assert severity.work_risk(_risk_run(ledger_of(), completeness="partial")) == "unknown"


def test_an_empty_population_in_an_incomplete_population_is_unknown():
    run = model_session_run([model_attempt(1, key="agent", ledger=ledger_of("observed"))],
                            completeness="partial")
    assert severity.work_risk(run) == "unknown"


def test_a_run_whose_population_completeness_was_never_computed_is_unknown():
    assert severity.work_risk(_risk_run(ledger_of(), completeness=None)) == "unknown"


def test_an_attempt_with_no_ledger_in_the_population_is_unknown():
    assert severity.work_risk(_risk_run(None)) == "unknown"


def test_an_attempt_outside_the_population_does_not_vote():
    # A session subagent with no interrupt is outside the population, so its
    # entry does not make the run `present`, and its partial ledger does not
    # make it `unknown`.
    quiet = model_attempt(1, key="agent", ledger=ledger_of("observed"))
    partial = model_attempt(2, key="agent2", ledger=ledger_of(completeness="partial"))
    run = model_session_run([quiet, partial])
    assert severity.work_risk(run) == "none"


def test_one_partial_ledger_makes_the_run_unknown_even_beside_a_present_entry():
    complete = model_attempt(1, outcome="killed", journal_state="superseded",
                             ledger=ledger_of("observed"))
    partial = model_attempt(2, key="v2:y", outcome="killed", journal_state="superseded",
                            ledger=ledger_of(completeness="partial"))
    run = model_workflow_run([complete, partial])
    assert severity.work_risk(run) == "unknown"


def test_a_possible_entry_alone_does_not_make_a_complete_ledger_present():
    # `possible` always travels with a partial reason in a real ledger; the
    # entry itself is not one of the three present effects.
    ledger = ledger_of("possible")
    run = _risk_run(ledger)
    assert severity.work_risk(run) == "none"


# --- absence ----------------------------------------------------------------------------------------------


def test_absence_present_absent_and_not_absolute(tmp_path):
    present = tmp_path / "there.md"
    present.write_text("x")
    assert severity.absence(str(present)) == "present_now"
    assert severity.absence(str(tmp_path / "gone.md")) == "absent_now"
    assert severity.absence("relative/report.md") == "unknown"
    assert severity.absence("") == "unknown"


def test_absence_under_a_file_is_absent_not_unknown(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    assert severity.absence(str(plain / "child.md")) == "absent_now"


def test_absence_is_unknown_when_the_stat_call_fails(monkeypatch, tmp_path):
    def deny(path, *args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "stat", deny)
    assert severity.absence(str(tmp_path / "x.md")) == "unknown"


def test_absence_is_unknown_for_a_path_stat_rejects_outright():
    assert severity.absence("/tmp/has\x00null.md") == "unknown"


@pytest.mark.parametrize("terminal", ["none", "unknown", "completed"])
def test_the_key_s_last_attempt_decides_not_the_first_later_one(terminal):
    killed = model_attempt(1, ordinal=1, outcome="killed", journal_state="superseded")
    failed = model_attempt(2, ordinal=2, outcome="failed", journal_state="failed")
    open_last = model_attempt(3, ordinal=3, outcome="completed", journal_state="open")
    run = _key_run(killed, failed, open_last, terminal=terminal)
    expected = "no" if terminal == "completed" else "unknown"
    assert severity.recovered(run, killed, include_self=False) == expected
    # and the other way round: the last attempt failed, an earlier later one is open
    open_mid = model_attempt(2, ordinal=2, outcome="completed", journal_state="open")
    failed_last = model_attempt(3, ordinal=3, outcome="failed", journal_state="failed")
    run = _key_run(killed, open_mid, failed_last, terminal=terminal)
    assert severity.recovered(run, killed, include_self=False) == "no"


@pytest.mark.parametrize("outcome", ["killed", "failed", "superseded"])
def test_a_last_attempt_whose_outcome_is_not_a_completion_is_a_no_without_a_journal_state(outcome):
    last = model_attempt(1, key="agent", outcome=outcome, journal_state=None)
    run = model_workflow_run([last], terminal="none")
    assert severity.recovered(run, last, include_self=True) == "no"


def test_two_unkeyed_attempts_do_not_form_a_key():
    lone = model_attempt(1, key=None, ordinal=1, outcome="failed", journal_state=None)
    other = model_attempt(2, key=None, ordinal=2, outcome="completed", journal_state="completed")
    run = model_workflow_run([lone, other])
    assert severity.recovered(run, lone, include_self=False) == "no"
    assert severity.position(run, lone) == "final"


@pytest.mark.parametrize("state", ["failed", "superseded"])
def test_a_last_attempt_closed_failed_or_superseded_by_the_journal_is_a_no(state):
    last = model_attempt(1, key="v2:x", outcome="completed", journal_state=state)
    run = model_workflow_run([last], terminal="none")
    assert severity.recovered(run, last, include_self=True) == "no"
