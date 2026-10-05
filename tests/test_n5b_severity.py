"""N5b: the severity table (docs/DESIGN.md 4.3 "Inputs and rule per detector",
4.13.4) and its contract-error path.

EXPECTED below is written out by hand from the DESIGN table, not read from
`severity.py`: every detector's every input tuple (the cross product of each
input's vocabulary) is listed with the severity the DESIGN rule gives it, or
`ERROR` for a tuple no row lists. The code under test must agree on all of
them. A new tuple, a changed row, or a dropped row fails here.
"""

from __future__ import annotations

import itertools

import pytest

from agent_postmortem import detectors
from tests.n5b_helpers import model_attempt, model_workflow_run, severity

ERROR = "contract error"

RECOVERED = ("yes", "no", "unknown")
WORK_RISK = ("none", "present", "unknown")
TERMINAL = ("completed", "failed", "stopped", "journal_resolved", "unknown", "none")
CAUSE = ("runtime", "unattributed", "parent_stop", "human")
SILENCE = ("long", "short", "unknown")
POSITION = ("final", "non_final")
CLAUSE = (
    "tests_failed", "suite_failed", "ok_false", "ready_false", "blockers", "report_path_missing",
)
ABSENCE = ("present_now", "absent_now", "unknown", "none")
OBLIGATION = ("none", "prompt_path", "claimed_path")

# detector -> (input names in order, vocabulary per input)
INPUTS = {
    "workflow_failed": (("terminal",), (TERMINAL,)),
    "attempt_failed": (("recovered",), (RECOVERED,)),
    "silent_retry": (("recovered", "work_risk"), (RECOVERED, WORK_RISK)),
    "mislabeled_kill": (("cause", "silence", "position"), (CAUSE, SILENCE, POSITION)),
    "claim_contradiction": (("clause", "absence"), (CLAUSE, ABSENCE)),
    "missing_artifact": (("obligation", "absence"), (OBLIGATION, ABSENCE)),
    "structured_output_failure": (("recovered",), (RECOVERED,)),
    "codex_write_denied": ((), ()),
    "bash_backgrounded": ((), ()),
    "bash_timeout": ((), ()),
    "tool_json_error": ((), ()),
    "classifier_unavailable": ((), ()),
    "git_cwd_mismatch": ((), ()),
    "wasted_time": ((), ()),
}


def _expected(detector: str, values: tuple) -> str:
    """The DESIGN rule, row by row."""
    if detector == "workflow_failed":
        return "major" if values == ("failed",) else ERROR
    if detector in ("attempt_failed", "structured_output_failure"):
        return {"yes": "minor", "no": "major", "unknown": "major"}[values[0]]
    if detector == "silent_retry":
        recovered, work_risk = values
        if recovered in ("no", "unknown"):
            return "major"
        return "minor" if work_risk == "none" else "major"
    if detector == "mislabeled_kill":
        cause, silence, position = values
        if cause in ("parent_stop", "human"):
            return "info"
        if silence in ("short", "unknown"):
            return "info"
        return "major" if position == "final" else "minor"
    if detector == "claim_contradiction":
        clause, absence = values
        if clause == "report_path_missing":
            return "info" if absence in ("absent_now", "unknown") else ERROR
        return "major" if absence == "none" else ERROR
    if detector == "missing_artifact":
        obligation, absence = values
        if obligation == "prompt_path" and absence in ("absent_now", "unknown"):
            return "info"
        return ERROR
    if detector == "codex_write_denied" or detector in (
        "bash_backgrounded", "bash_timeout", "tool_json_error", "classifier_unavailable",
        "git_cwd_mismatch",
    ):
        return "minor"
    if detector == "wasted_time":
        return "info"
    raise AssertionError(detector)


HIGHEST = {
    "workflow_failed": "major",
    "attempt_failed": "major",
    "silent_retry": "major",
    "mislabeled_kill": "major",
    "claim_contradiction": "major",
    "missing_artifact": "info",
    "structured_output_failure": "major",
    "codex_write_denied": "minor",
    "bash_backgrounded": "minor",
    "bash_timeout": "minor",
    "tool_json_error": "minor",
    "classifier_unavailable": "minor",
    "git_cwd_mismatch": "minor",
    "wasted_time": "info",
}

CASES = [
    (detector, values)
    for detector, (_names, vocab) in INPUTS.items()
    for values in itertools.product(*vocab)
]


def _run():
    return model_workflow_run([model_attempt(1)])


@pytest.mark.parametrize(
    "detector,values", CASES, ids=[f"{d}:{'/'.join(v) or '-'}" for d, v in CASES],
)
def test_every_input_tuple_of_every_detector(detector, values):
    names = INPUTS[detector][0]
    run = _run()
    result = severity.resolve(run, detector, **dict(zip(names, values)))
    expected = _expected(detector, values)
    if expected == ERROR:
        assert result == HIGHEST[detector]
        assert run.status == "INCOMPLETE"
        assert len(run.diagnostics) == 1
    else:
        assert result == expected
        assert run.status == "COMPLETE"
        assert run.diagnostics == []


def test_the_table_covers_exactly_the_registered_detectors():
    registered = {detector.id for detector in detectors.all_detectors()}
    assert set(INPUTS) == registered
    assert set(severity.RULES) == registered


@pytest.mark.parametrize("detector", sorted(HIGHEST))
def test_the_highest_row_of_each_detector(detector):
    assert severity.highest(detector) == HIGHEST[detector]


def test_every_detector_default_is_its_highest_row_and_none_is_critical():
    for detector in detectors.all_detectors():
        assert detector.severity_default == HIGHEST[detector.id], detector.id
        assert detector.severity_default != "critical"


def test_a_value_outside_a_vocabulary_is_a_contract_error_not_a_crash():
    run = _run()
    assert severity.resolve(run, "silent_retry", recovered="maybe", work_risk="none") == "major"
    assert run.status == "INCOMPLETE"
    run = _run()
    assert severity.resolve(run, "attempt_failed", recovered=True) == "major"
    assert run.status == "INCOMPLETE"
    run = _run()
    assert severity.resolve(run, "mislabeled_kill", cause="runtime", silence=300, position="final") == "major"
    assert run.status == "INCOMPLETE"


def test_the_diagnostic_names_the_detector_the_tuple_and_the_severity_used():
    run = _run()
    severity.resolve(run, "silent_retry", recovered="maybe", work_risk="none")
    assert run.diagnostics == [
        "severity: silent_retry has no row for (recovered=maybe, work_risk=none); "
        "used the highest row, major"
    ]
    run = _run()
    severity.resolve(run, "claim_contradiction", clause="report_path_missing", absence="present_now")
    assert run.diagnostics == [
        "severity: claim_contradiction has no row for (clause=report_path_missing, "
        "absence=present_now); used the highest row, major"
    ]


def test_the_same_tuple_is_reported_once_and_distinct_tuples_each_once():
    run = _run()
    for _ in range(3):
        severity.resolve(run, "silent_retry", recovered="maybe", work_risk="none")
    assert len(run.diagnostics) == 1
    severity.resolve(run, "silent_retry", recovered="maybe", work_risk="present")
    severity.resolve(run, "attempt_failed", recovered="maybe")
    assert len(run.diagnostics) == 3
    assert run.status == "INCOMPLETE"


def test_a_contract_error_keeps_earlier_diagnostics_and_a_listed_tuple_adds_none():
    run = _run()
    run.diagnostics.append("journal.jsonl:1: something earlier")
    severity.resolve(run, "silent_retry", recovered="yes", work_risk="none")
    assert run.diagnostics == ["journal.jsonl:1: something earlier"]
    assert run.status == "COMPLETE"
    severity.resolve(run, "silent_retry", recovered="maybe", work_risk="none")
    assert run.diagnostics[0] == "journal.jsonl:1: something earlier"
    assert len(run.diagnostics) == 2


def test_a_detector_without_a_table_is_a_contract_error_at_its_default():
    run = _run()
    with pytest.raises(KeyError):
        severity.resolve(run, "no_such_detector")


def test_the_silence_rule_is_the_one_300_second_rule():
    assert severity.silence_class(300.0) == "long"
    assert severity.silence_class(299.999) == "short"
    assert severity.silence_class(0.0) == "short"
    assert severity.silence_class(86400.0) == "long"
    assert severity.silence_class(None) == "unknown"


@pytest.mark.parametrize("detector", sorted(name for name, (names, _v) in INPUTS.items() if names))
def test_a_value_outside_the_vocabulary_at_any_position_is_a_contract_error(detector):
    # Start from a listed tuple and replace one input at a time, including the
    # positions a row matches with "any value": an unknown value never matches.
    names, vocab = INPUTS[detector]
    listed = next(
        values for values in itertools.product(*vocab) if _expected(detector, values) != ERROR
    )
    for position in range(len(names)):
        values = list(listed)
        values[position] = "bogus"
        run = _run()
        result = severity.resolve(run, detector, **dict(zip(names, values)))
        assert result == HIGHEST[detector], (detector, names[position])
        assert run.status == "INCOMPLETE", (detector, names[position])
        assert len(run.diagnostics) == 1


@pytest.mark.parametrize("detector", sorted(name for name, (names, _v) in INPUTS.items() if names))
def test_a_missing_or_an_extra_input_is_a_contract_error(detector):
    names, vocab = INPUTS[detector]
    listed = next(
        values for values in itertools.product(*vocab) if _expected(detector, values) != ERROR
    )
    given = dict(zip(names, listed))
    missing = dict(given)
    missing.pop(names[0])
    run = _run()
    assert severity.resolve(run, detector, **missing) == HIGHEST[detector]
    assert run.status == "INCOMPLETE"
    run = _run()
    assert severity.resolve(run, detector, **given, extra="yes") == HIGHEST[detector]
    assert run.status == "INCOMPLETE"


def test_a_detector_with_no_inputs_given_an_input_is_a_contract_error():
    run = _run()
    assert severity.resolve(run, "wasted_time", surprise="yes") == "info"
    assert run.status == "INCOMPLETE"


def test_a_value_outside_the_vocabulary_is_rejected_at_every_position_of_every_listed_tuple():
    # Every listed tuple, every position: the positions a row matches with "any
    # value" must still refuse a value outside the input's vocabulary.
    for detector, (names, vocab) in INPUTS.items():
        for listed in itertools.product(*vocab):
            if _expected(detector, listed) == ERROR:
                continue
            for position in range(len(names)):
                values = list(listed)
                values[position] = "bogus"
                run = _run()
                result = severity.resolve(run, detector, **dict(zip(names, values)))
                assert result == HIGHEST[detector], (detector, listed, names[position])
                assert run.status == "INCOMPLETE", (detector, listed, names[position])


def test_an_unhashable_value_is_a_contract_error_not_a_crash():
    run = _run()
    assert severity.resolve(run, "silent_retry", recovered=["yes"], work_risk="none") == "major"
    assert run.status == "INCOMPLETE"
    run = _run()
    assert severity.resolve(run, "attempt_failed", recovered={"yes": 1}) == "major"
    assert run.status == "INCOMPLETE"


def test_the_vocabularies_are_exactly_the_documented_ones():
    assert severity.VOCAB == {
        "terminal": frozenset(TERMINAL),
        "recovered": frozenset(RECOVERED),
        "work_risk": frozenset(WORK_RISK),
        "cause": frozenset(CAUSE),
        "silence": frozenset(SILENCE),
        "position": frozenset(POSITION),
        "clause": frozenset(CLAUSE),
        "absence": frozenset(ABSENCE),
        "obligation": frozenset(OBLIGATION),
    }
    assert dict(severity.INPUT_NAMES) == {
        detector: names for detector, (names, _vocab) in INPUTS.items()
    }


def test_no_two_rows_of_a_detector_match_the_same_tuple():
    for detector, (names, vocab) in INPUTS.items():
        for values in itertools.product(*vocab):
            matching = [
                pattern for pattern, _severity in severity.RULES[detector]
                if all(
                    severity._element_matches(element, name, value)
                    for element, name, value in zip(pattern, names, values)
                )
            ]
            assert len(matching) <= 1, (detector, values)


def test_every_row_pattern_has_one_element_per_input():
    for detector, (names, _vocab) in INPUTS.items():
        for pattern, _severity in severity.RULES[detector]:
            assert len(pattern) == len(names), (detector, pattern)
