"""N5b: every detector's severity row, end to end, through the reader, the
ledger, the lifecycle, and `detectors.run_detectors`. The input functions have
their own tests (test_n5b_inputs.py) and the whole table is pinned tuple by
tuple in test_n5b_severity.py; this file proves each detector actually asks
the table, with inputs read from a real run.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from agent_postmortem import detectors, reader
from tests.n5b_helpers import BASE, Spec, build_session, build_workflow, severity
from tests.test_field_v03 import (
    FAKE_SLUG,
    FAKE_UUID,
    _assistant_text,
    _assistant_tool,
    _kill,
    _tool_result,
    _user,
    _write_jsonl,
)

WRITE_OBSERVED = ("t1", "Write", {"file_path": "/tmp/ws/a.txt", "content": "x"}, "ok", False)
OPAQUE_SHELL = (
    "t1", "Bash", {"command": "python3 -c \"open('/tmp/ws/o','w').write('x')\""}, "", False,
)
MISMATCH = (
    "t2", "StructuredOutput", {},
    "Output does not match required schema: root: must have required property 'ok'", True,
)
DENIED = (
    "t3", "Bash", {"command": "node /opt/codex/codex-companion.mjs task go"},
    "[codex] WRITE_DENIED: patch rejected: writing outside of the project", False,
)


def _findings(run, detector):
    return [f for f in detectors.run_detectors(run) if f.detector == detector]


def _load(tmp_path, monkeypatch, wf_suffix, specs, **kwargs):
    run_dir = build_workflow(tmp_path, monkeypatch, f"wf_00000000-{wf_suffix}", specs, **kwargs)
    return reader.load_run(run_dir)


# --- silent_retry: recovered x work_risk, nine tuples ---------------------------------------------

RECOVERED_SCENARIOS = {
    # name: (last attempt spec kwargs, parent state)
    "yes": (dict(journal="completed"), "completed"),
    "no": (dict(journal="failed"), "completed"),
    "unknown": (dict(journal="open"), "running"),
}
RISK_TOOLS = {"none": [], "present": [WRITE_OBSERVED], "unknown": [OPAQUE_SHELL]}


@pytest.mark.parametrize("risk", ["none", "present", "unknown"])
@pytest.mark.parametrize("recovered", ["yes", "no", "unknown"])
def test_silent_retry_severity_for_every_recovered_and_work_risk(
    tmp_path, monkeypatch, recovered, risk,
):
    last, parent = RECOVERED_SCENARIOS[recovered]
    specs = [
        Spec("v2:x", 1, journal="superseded", kill=True, tools=list(RISK_TOOLS[risk])),
        Spec("v2:x", 2, **last),
    ]
    run = _load(tmp_path, monkeypatch, "b01", specs, parent=parent)
    # The scenario builds the inputs it names, not merely the severity it expects.
    assert severity.recovered(run, run.attempts[0], include_self=False) == recovered
    assert severity.work_risk(run) == risk
    (finding,) = _findings(run, "silent_retry")
    expected = "minor" if (recovered, risk) == ("yes", "none") else "major"
    assert finding.severity == expected
    assert run.status == "COMPLETE"


def test_silent_retry_uses_the_runs_work_risk_not_the_attempts_own(tmp_path, monkeypatch):
    # The first key retried cleanly; a second key's killed attempt used an opaque
    # shell. work_risk is run-level, so the clean retry is still major.
    specs = [
        Spec("v2:x", 1, journal="superseded", kill=True),
        Spec("v2:x", 2, journal="completed"),
        Spec("v2:y", 3, journal="failed", tools=[OPAQUE_SHELL]),
    ]
    run = _load(tmp_path, monkeypatch, "b02", specs)
    assert severity.work_risk(run) == "unknown"
    (finding,) = _findings(run, "silent_retry")
    assert finding.severity == "major"


# --- attempt_failed: recovered ---------------------------------------------------------------------


def test_attempt_failed_recovered_yes_is_minor(tmp_path, monkeypatch):
    specs = [Spec("v2:x", 1, journal="failed"), Spec("v2:x", 2, journal="completed")]
    run = _load(tmp_path, monkeypatch, "b03", specs)
    (finding,) = _findings(run, "attempt_failed")
    assert finding.severity == "minor"
    assert finding.title.endswith("a later attempt completed")


def test_attempt_failed_not_retried_is_major(tmp_path, monkeypatch):
    run = _load(tmp_path, monkeypatch, "b04", [Spec("v2:x", 1, journal="failed")])
    (finding,) = _findings(run, "attempt_failed")
    assert finding.severity == "major"
    assert finding.title.endswith("the key was not retried")


def test_attempt_failed_retried_and_killed_is_major(tmp_path, monkeypatch):
    specs = [Spec("v2:x", 1, journal="failed"), Spec("v2:x", 2, journal="failed")]
    run = _load(tmp_path, monkeypatch, "b05", specs)
    findings = _findings(run, "attempt_failed")
    assert [f.severity for f in findings] == ["major", "major"]
    assert findings[0].title.endswith("retried, no later attempt completed")


@pytest.mark.parametrize("parent", ["completed", "running"])
def test_attempt_failed_whose_later_attempt_is_open_is_major_and_not_a_recovery(
    tmp_path, monkeypatch, parent,
):
    # The open attempt's transcript ends normally, which used to read as
    # "completed". No journal result closed it, so it recovered nothing.
    specs = [Spec("v2:x", 1, journal="failed"), Spec("v2:x", 2, journal="open")]
    run = _load(tmp_path, monkeypatch, "b06", specs, parent=parent)
    (finding,) = _findings(run, "attempt_failed")
    assert finding.severity == "major"
    assert finding.title.endswith("retried, no later attempt completed")


# --- mislabeled_kill: cause, silence, position --------------------------------------------------------


def test_mislabeled_kill_runtime_cause_with_the_default_short_silence_on_a_non_final_attempt_is_info(
    tmp_path, monkeypatch,
):
    specs = [Spec("v2:x", 1, journal="superseded", kill=True), Spec("v2:x", 2)]
    run = _load(tmp_path, monkeypatch, "b07", specs)
    assert run.attempts[0].interrupt.cause == "runtime"
    (finding,) = _findings(run, "mislabeled_kill")
    assert finding.severity == "info"
    assert "killed by the runtime" in finding.title


def test_mislabeled_kill_runtime_cause_with_a_short_silence_on_a_non_final_attempt_is_info(
    tmp_path, monkeypatch,
):
    specs = [Spec("v2:x", 1, journal="superseded", kill=True, silence=2), Spec("v2:x", 2)]
    run = _load(tmp_path, monkeypatch, "b08", specs)
    (finding,) = _findings(run, "mislabeled_kill")
    assert finding.severity == "info"


def test_mislabeled_kill_unattributed_long_silence_on_the_final_attempt_is_major(
    tmp_path, monkeypatch,
):
    specs = [Spec("v2:x", 1, journal="open", kill=True, silence=400)]
    run = _load(tmp_path, monkeypatch, "b09", specs, parent="running")
    assert run.attempts[0].interrupt.cause == "unattributed"
    (finding,) = _findings(run, "mislabeled_kill")
    assert finding.severity == "major"
    assert "consistent with a runtime kill" in finding.title


def test_mislabeled_kill_unattributed_exactly_300_seconds_is_major_299_is_info(
    tmp_path, monkeypatch,
):
    at = _load(tmp_path / "at", monkeypatch, "b0a",
               [Spec("v2:x", 1, journal="open", kill=True, silence=300)], parent="running")
    below = _load(tmp_path / "below", monkeypatch, "b0b",
                  [Spec("v2:x", 1, journal="open", kill=True, silence=299)], parent="running")
    assert _findings(at, "mislabeled_kill")[0].severity == "major"
    assert _findings(below, "mislabeled_kill")[0].severity == "info"


def test_mislabeled_kill_unattributed_short_silence_on_the_final_attempt_is_info(
    tmp_path, monkeypatch,
):
    run = _load(tmp_path, monkeypatch, "b0c",
                [Spec("v2:x", 1, journal="open", kill=True, silence=20)], parent="running")
    (finding,) = _findings(run, "mislabeled_kill")
    assert finding.severity == "info"


def test_mislabeled_kill_in_a_session_subagent_is_final_and_follows_silence(tmp_path):
    def events(aid, silence):
        return [
            _user("work", BASE + timedelta(seconds=5), aid),
            _assistant_text("working", BASE + timedelta(seconds=6), aid),
            _kill(BASE + timedelta(seconds=6 + silence), aid),
        ]

    long_main, _ = build_session(tmp_path / "long", lambda aid: events(aid, 600))
    short_main, _ = build_session(tmp_path / "short", lambda aid: events(aid, 20))
    long_run = reader.load_session(long_main)
    short_run = reader.load_session(short_main)
    assert _findings(long_run, "mislabeled_kill")[0].severity == "major"
    assert _findings(short_run, "mislabeled_kill")[0].severity == "info"


# --- structured_output_failure: recovered -------------------------------------------------------------


def test_structured_output_failure_in_a_completed_attempt_is_minor(tmp_path, monkeypatch):
    specs = [Spec("v2:x", 1, journal="completed", tools=[MISMATCH])]
    run = _load(tmp_path, monkeypatch, "b10", specs)
    (finding,) = _findings(run, "structured_output_failure")
    assert finding.severity == "minor"


def test_structured_output_failure_recovered_by_a_later_attempt_is_minor(tmp_path, monkeypatch):
    specs = [
        Spec("v2:x", 1, journal="superseded", kill=True, tools=[MISMATCH]),
        Spec("v2:x", 2, journal="completed"),
    ]
    run = _load(tmp_path, monkeypatch, "b11", specs)
    (finding,) = _findings(run, "structured_output_failure")
    assert finding.severity == "minor"


@pytest.mark.parametrize("journal,parent", [("failed", "completed"), ("open", "running")])
def test_structured_output_failure_in_an_attempt_that_did_not_complete_is_major(
    tmp_path, monkeypatch, journal, parent,
):
    specs = [Spec("v2:x", 1, journal=journal, tools=[MISMATCH])]
    run = _load(tmp_path, monkeypatch, "b12", specs, parent=parent)
    (finding,) = _findings(run, "structured_output_failure")
    assert finding.severity == "major"


def test_structured_output_failure_takes_the_highest_severity_across_attempts(
    tmp_path, monkeypatch,
):
    specs = [
        Spec("v2:x", 1, journal="completed", tools=[MISMATCH]),
        Spec("v2:y", 2, journal="failed", tools=[MISMATCH]),
    ]
    run = _load(tmp_path, monkeypatch, "b13", specs)
    (finding,) = _findings(run, "structured_output_failure")
    assert finding.severity == "major"
    assert finding.metrics["count"] == 2


def test_structured_output_failure_in_a_session_subagent_stays_major(tmp_path):
    def events(aid):
        return [
            _user("emit", BASE + timedelta(seconds=5), aid),
            _assistant_tool("t2", "StructuredOutput", {}, BASE + timedelta(seconds=6), aid),
            _tool_result("t2", MISMATCH[3], BASE + timedelta(seconds=7), aid, is_error=True),
            _assistant_text("done", BASE + timedelta(seconds=8), aid),
        ]

    main, _ = build_session(tmp_path, events)
    run = reader.load_session(main)
    (finding,) = _findings(run, "structured_output_failure")
    assert finding.severity == "major"


# --- codex_write_denied: minor, one finding per attempt ---------------------------------------------


def test_codex_write_denied_is_one_minor_finding_with_both_rule_ids(tmp_path, monkeypatch):
    specs = [Spec("v2:x", 1, journal="completed", tools=[DENIED])]
    run = _load(tmp_path, monkeypatch, "b14", specs)
    (finding,) = _findings(run, "codex_write_denied")
    assert finding.severity == "minor"
    assert finding.rule_ids == ("R-CODEX-CONTRACT", "R-ASSERT-ARTIFACT")
    assert finding.rule_id == "R-CODEX-CONTRACT"
    assert finding.metrics["count"] == 1
    assert finding.agent_id == run.attempts[0].agent_id


def test_codex_write_denied_is_one_finding_per_attempt(tmp_path, monkeypatch):
    two_hits = [DENIED, ("t4", *DENIED[1:])]
    specs = [
        Spec("v2:x", 1, journal="completed", tools=two_hits),
        Spec("v2:y", 2, journal="completed", tools=[DENIED]),
        Spec("v2:z", 3, journal="completed"),
    ]
    run = _load(tmp_path, monkeypatch, "b15", specs)
    findings = _findings(run, "codex_write_denied")
    assert [f.agent_id for f in findings] == [run.attempts[0].agent_id, run.attempts[1].agent_id]
    assert [f.metrics["count"] for f in findings] == [2, 1]
    assert all(f.severity == "minor" for f in findings)
    assert all(f.rule_ids == ("R-CODEX-CONTRACT", "R-ASSERT-ARTIFACT") for f in findings)
    assert len({f.finding_id for f in findings}) == 2


def test_codex_write_denied_in_a_session_main_thread_names_the_main_attempt(tmp_path):
    root = tmp_path / "sroot"
    main = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    _write_jsonl(main, [
        _user("run codex", BASE),
        _assistant_tool("t3", "Bash", DENIED[2], BASE + timedelta(seconds=1)),
        _tool_result("t3", DENIED[3], BASE + timedelta(seconds=2)),
    ])
    run = reader.load_session(main)
    (finding,) = _findings(run, "codex_write_denied")
    assert finding.severity == "minor"
    assert finding.agent_id == run.main.agent_id


def test_codex_write_denied_does_not_fire_without_a_denial(tmp_path, monkeypatch):
    run = _load(tmp_path, monkeypatch, "b16", [Spec("v2:x", 1)])
    assert _findings(run, "codex_write_denied") == []


# --- the fixed-severity detectors -----------------------------------------------------------------------


def test_the_fixed_severity_detectors_take_their_rows(tmp_path, monkeypatch):
    bash_timeout = ("t5", "Bash", {"command": "sleep 500"}, "Exit code 143", True)
    tool_error = (
        "t6", "Bash", {"command": 1},
        "<tool_use_error>InputValidationError: command must be a string</tool_use_error>", True,
    )
    specs = [Spec("v2:x", 1, journal="completed", tools=[bash_timeout, tool_error])]
    run = _load(tmp_path, monkeypatch, "b17", specs)
    by_detector = {f.detector: f.severity for f in detectors.run_detectors(run)}
    assert by_detector["wasted_time"] == "info"
    assert by_detector["bash_timeout"] == "minor"
    assert by_detector["tool_json_error"] == "minor"


# --- workflow_failed -------------------------------------------------------------------------------------


def test_workflow_failed_is_major_and_resolves_through_the_table(tmp_path, monkeypatch):
    run = _load(tmp_path, monkeypatch, "b18", [Spec("v2:x", 1)], parent="failed")
    (finding,) = _findings(run, "workflow_failed")
    assert finding.severity == "major"
    assert run.status == "COMPLETE"


# --- claim_contradiction ------------------------------------------------------------------------------------


def _claim_run(tmp_path, monkeypatch, suffix, result):
    return _load(tmp_path, monkeypatch, suffix, [Spec("v2:x", 1, result=result)])


@pytest.mark.parametrize("result", [
    {"tests_failed": 2}, {"suite_failed": 1}, {"ok": False}, {"ready": False},
    {"blockers": ["b"]},
])
def test_claim_contradiction_failure_clauses_are_major(tmp_path, monkeypatch, result):
    run = _claim_run(tmp_path, monkeypatch, "b20", result)
    (finding,) = _findings(run, "claim_contradiction")
    assert finding.severity == "major"
    assert "completed but its own result reports" in finding.title


def test_claim_contradiction_absent_report_path_alone_is_info_with_the_d2_title(
    tmp_path, monkeypatch,
):
    gone = str(tmp_path / "reports" / "report.md")
    run = _claim_run(tmp_path, monkeypatch, "b21", {"report_path": gone})
    (finding,) = _findings(run, "claim_contradiction")
    assert finding.severity == "info"
    assert finding.title == (  # F fix 7 (W7-2): no backquotes
        f"{gone} is absent now; the tool cannot tell whether it existed when the run ended"
    )
    assert finding.metrics["clauses"] == ["report_path_missing"]
    assert run.status == "COMPLETE"


def test_claim_contradiction_with_a_failure_and_an_absent_path_is_major_with_the_v02_title(
    tmp_path, monkeypatch,
):
    gone = str(tmp_path / "reports" / "report.md")
    run = _claim_run(tmp_path, monkeypatch, "b22", {"tests_failed": 3, "report_path": gone})
    (finding,) = _findings(run, "claim_contradiction")
    assert finding.severity == "major"
    assert "3 failed tests; a report_path that does not exist" in finding.title
    assert "is absent now" not in finding.title


def test_claim_contradiction_present_or_relative_report_path_makes_no_finding(
    tmp_path, monkeypatch,
):
    here = tmp_path / "report.md"
    here.write_text("x")
    present = _claim_run(tmp_path / "p", monkeypatch, "b23", {"report_path": str(here)})
    relative = _claim_run(tmp_path / "r", monkeypatch, "b24", {"report_path": "out/report.md"})
    assert _findings(present, "claim_contradiction") == []
    assert _findings(relative, "claim_contradiction") == []


def test_claim_contradiction_stat_failure_is_info_and_says_it_could_not_check(
    tmp_path, monkeypatch,
):
    target = str(tmp_path / "reports" / "report.md")
    run = _claim_run(tmp_path, monkeypatch, "b25", {"report_path": target})
    real_stat = os.stat

    def flaky(path, *args, **kwargs):
        if str(path) == target:
            raise PermissionError(13, "denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", flaky)
    (finding,) = _findings(run, "claim_contradiction")
    assert finding.severity == "info"
    assert finding.title == (  # F fix 7 (W7-2): no backquotes
        f"{target} could not be checked now; the tool cannot tell whether it existed "
        "when the run ended"
    )


# --- missing_artifact -------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["tmp", "outside"])
def test_missing_artifact_absent_path_is_info_with_the_d2_title(tmp_path, monkeypatch, where):
    target = (
        str(tmp_path / "out" / "result.md") if where == "tmp"
        else "/nonexistent-n5b-root/result.md"
    )
    spec = Spec("v2:x", 1, prompt=f"Write the summary to {target} when you finish.")
    run = _load(tmp_path, monkeypatch, "b30", [spec])
    (finding,) = _findings(run, "missing_artifact")
    assert finding.severity == "info"
    assert finding.title == (  # F fix 7 (W7-2): no backquotes
        f"{target} is absent now; the tool cannot tell whether it existed when the run ended"
    )
    assert finding.metrics["paths"] == [target]
    assert finding.rule_id == "R-ASSERT-ARTIFACT"
    assert run.status == "COMPLETE"


def test_missing_artifact_present_path_makes_no_finding(tmp_path, monkeypatch):
    target = tmp_path / "result.md"
    target.write_text("x")
    spec = Spec("v2:x", 1, prompt=f"Write the summary to {target} when you finish.")
    run = _load(tmp_path, monkeypatch, "b31", [spec])
    assert _findings(run, "missing_artifact") == []


def test_missing_artifact_stat_failure_is_info_and_says_it_could_not_check(
    tmp_path, monkeypatch,
):
    target = str(tmp_path / "out" / "result.md")
    spec = Spec("v2:x", 1, prompt=f"Write the summary to {target} when you finish.")
    run = _load(tmp_path, monkeypatch, "b32", [spec])
    real_stat = os.stat

    def flaky(path, *args, **kwargs):
        if str(path) == target:
            raise PermissionError(13, "denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", flaky)
    (finding,) = _findings(run, "missing_artifact")
    assert finding.severity == "info"
    assert finding.title == (  # F fix 7 (W7-2): no backquotes
        f"{target} could not be checked now; the tool cannot tell whether it existed "
        "when the run ended"
    )


def test_a_negated_prompt_still_makes_no_missing_artifact_finding(tmp_path, monkeypatch):
    target = str(tmp_path / "out" / "result.md")
    spec = Spec("v2:x", 1, prompt=f"Do not write {target} yourself.")
    run = _load(tmp_path, monkeypatch, "b33", [spec])
    assert _findings(run, "missing_artifact") == []


# --- a contract error, end to end ----------------------------------------------------------------------------


def test_a_contract_error_in_a_detector_makes_the_run_incomplete(tmp_path, monkeypatch):
    specs = [Spec("v2:x", 1, journal="superseded", kill=True), Spec("v2:x", 2)]
    run = _load(tmp_path, monkeypatch, "b40", specs)
    import agent_postmortem.severity as sev

    monkeypatch.setattr(sev, "recovered", lambda *args, **kwargs: "maybe")
    (finding,) = _findings(run, "silent_retry")
    assert finding.severity == "major"
    assert run.status == "INCOMPLETE"
    assert any(
        line.startswith("severity: silent_retry has no row for (recovered=maybe, work_risk=")
        for line in run.diagnostics
    )



# --- mislabeled_kill: the whole cause x silence x position matrix through the detector ---------------


def _kill_run(cause: str, silence: float | None, position: str):
    from datetime import datetime, timezone
    from pathlib import Path

    from agent_postmortem.models import Attempt, Event, Interrupt

    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    kill_event = Event(
        path=Path("/home/user/project/agent-a1111111111111111.jsonl"), line_no=1, ts=kill_ts,
        kind="user", raw_type="user", tool_name=None, text="[Request interrupted by user]",
        exit_code=None, duration_ms=None, is_error=False, usage={},
    )
    killed = Attempt(
        agent_id="a1111111111111111", key="v2:x", label="impl:matrix", phase="",
        agent_type=None, meta={}, transcript_path=None, events=[kill_event], first_ts=None,
        last_ts=None, ordinal=1, outcome="killed", usage={},
    )
    killed.interrupt = Interrupt(
        kill_ts=kill_ts, preceding=None, silence_seconds=silence, cause=cause, cause_evidence=None,
    )
    attempts = [killed]
    if position == "non_final":
        attempts.append(Attempt(
            agent_id="a2222222222222222", key="v2:x", label="impl:matrix", phase="",
            agent_type=None, meta={}, transcript_path=None, events=[], first_ts=None,
            last_ts=None, ordinal=2, outcome="completed", usage={},
        ))
    from tests.n5b_helpers import model_workflow_run

    return model_workflow_run(attempts)


_SILENCE_SECONDS = {"long": 400.0, "short": 20.0, "unknown": None}


@pytest.mark.parametrize("position", ["final", "non_final"])
@pytest.mark.parametrize("silence", ["long", "short", "unknown"])
@pytest.mark.parametrize("cause", ["runtime", "unattributed", "parent_stop", "human"])
def test_mislabeled_kill_every_cause_silence_and_position(cause, silence, position):
    from agent_postmortem.detectors import mislabeled_kill
    from tests.test_n5b_severity import _expected

    run = _kill_run(cause, _SILENCE_SECONDS[silence], position)
    (finding,) = mislabeled_kill.DETECTOR.run(run)
    assert finding.severity == _expected("mislabeled_kill", (cause, silence, position))
    assert run.status == "COMPLETE"
    # The title's tail follows the silence, not the severity.
    tail = "consistent with a runtime kill" in finding.title
    assert tail == (cause == "unattributed" and silence == "long")
    # The monitor rule follows the cause, and for an unattributed kill the severity.
    expected_rule = (
        "R-MONITOR-RETRIES"
        if cause == "runtime" or (cause == "unattributed" and finding.severity == "major")
        else None
    )
    assert finding.rule_id == expected_rule


# --- the attempt itself never recovers a key it did not complete -----------------------------------------


def test_silent_retry_a_completed_first_attempt_does_not_recover_a_failed_retry(
    tmp_path, monkeypatch,
):
    specs = [Spec("v2:x", 1, journal="completed"), Spec("v2:x", 2, journal="failed")]
    run = _load(tmp_path, monkeypatch, "b41", specs)
    assert severity.recovered(run, run.attempts[0], include_self=False) == "no"
    (finding,) = _findings(run, "silent_retry")
    assert finding.severity == "major"


def test_attempt_failed_two_unkeyed_attempts_are_not_a_retried_key():
    from agent_postmortem.detectors import attempt_failed
    from tests.n5b_helpers import model_attempt, model_workflow_run

    lone = model_attempt(1, key=None, ordinal=1, outcome="failed", journal_state=None)
    other = model_attempt(2, key=None, ordinal=2, outcome="completed", journal_state="completed")
    run = model_workflow_run([lone, other])
    (finding,) = attempt_failed.DETECTOR.run(run)
    assert finding.severity == "major"
    assert finding.title.endswith("the key was not retried")
