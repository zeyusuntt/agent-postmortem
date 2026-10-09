"""G-B, S1 (v0.3.1): the severity registry is complete (docs/DESIGN.md 4.13.4).

Every registered detector id has its input names and rows, and every clause a detector can emit
has a row: a clause or a detector without one fails here, not at run time as a contract error.
"""

from __future__ import annotations

import pytest

from agent_postmortem import detectors, severity
from agent_postmortem.detectors import claim_contradiction
from tests.n5b_helpers import model_attempt, model_workflow_run


def test_every_registered_detector_has_inputs_and_rows():
    for detector in detectors.all_detectors():
        assert detector.id in severity.INPUT_NAMES, detector.id
        assert detector.id in severity.RULES, detector.id
        assert severity.RULES[detector.id], detector.id
    assert set(severity.INPUT_NAMES) == {d.id for d in detectors.all_detectors()}
    assert set(severity.RULES) == set(severity.INPUT_NAMES)


def test_every_claim_clause_the_detector_can_emit_has_a_row():
    clauses = claim_contradiction.CLAUSE_IDS
    assert set(clauses) == set(severity.VOCAB["clause"])
    for clause in clauses:
        for absence in ("absent_now", "unknown") if clause == "report_path_missing" else ("none",):
            run = model_workflow_run([model_attempt(1)])
            answer = severity.resolve(run, "claim_contradiction", clause=clause, absence=absence)
            assert run.status == "COMPLETE" and run.diagnostics == [], (clause, absence)
            assert answer == ("info" if clause == "report_path_missing" else "major")


@pytest.mark.parametrize("clause", ["self_reported_failure", "self_reported_blocked"])
def test_the_g3_clauses_are_major_rows(clause):
    run = model_workflow_run([model_attempt(1)])
    assert severity.resolve(run, "claim_contradiction", clause=clause, absence="none") == "major"
    assert run.status == "COMPLETE"


def test_the_clauses_the_function_emits_are_all_named():
    claims = {
        "tests_failed": 1, "suite_failed": 1, "ok": False, "ready": False, "blockers_count": 1,
        "report_path": "/tmp/agent-postmortem-fixture/g3/never-there.md",
    }
    from agent_postmortem.claims import SelfReport

    calls = [
        (claims, SelfReport(1, True)),
        ({}, SelfReport(1, False)),
        ({}, SelfReport(0, True)),
    ]
    emitted = {
        clause for given, report in calls
        for clause, _p, _a in claim_contradiction._clauses(given, True, report)
    }
    assert emitted == set(claim_contradiction.CLAUSE_IDS)
