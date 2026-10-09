"""Tests for detector kinds and detectors.run_detectors(): docs/DESIGN.md
4.3 says every detector declares which run kinds it applies to (silent_retry
and attempt_failed are workflow-only -- both read the journal, which only
exists for a workflow run; every other detector applies to both), and that
the runner calls a detector only on a run whose kind is in detector.kinds.
"""

from __future__ import annotations

from pathlib import Path

from agent_postmortem import detectors
from agent_postmortem.detectors import all_detectors, run_detectors
from agent_postmortem.models import Attempt, Lifecycle, LogicalAgent, Run


def _workflow_run() -> Run:
    attempt_a = Attempt(
        agent_id="a1111111111111111", key="v2:a", label="impl:a", phase="Implement",
        agent_type=None, meta={}, transcript_path=None, events=[],
        first_ts=None, last_ts=None, ordinal=1, outcome="completed", usage={},
    )
    attempt_b = Attempt(
        agent_id="a2222222222222222", key="v2:a", label="impl:a", phase="Implement",
        agent_type=None, meta={}, transcript_path=None, events=[],
        first_ts=None, last_ts=None, ordinal=2, outcome="completed", usage={},
    )
    return Run(
        run_id="wf_00000000-001", kind="workflow",
        path=Path("/home/user/project/subagents/workflows/wf_00000000-001"),
        project_slug="-home-user-project", session_id=None, claude_version=None, journal=[],
        agents=[LogicalAgent(key="v2:a", label="impl:a", phase="Implement", attempts=[attempt_a, attempt_b])],
        attempts=[attempt_a, attempt_b], started_at=None, ended_at=None, diagnostics=[],
    )


def _session_run() -> Run:
    main = Attempt(
        agent_id="00000000-0000-4000-8000-000000000000", key=None, label="main", phase="",
        agent_type=None, meta={}, transcript_path=None, events=[],
        first_ts=None, last_ts=None, ordinal=1, outcome="completed", usage={},
    )
    child = Attempt(
        agent_id="a3333333333333333", key="a3333333333333333", label="impl:child", phase="",
        agent_type=None, meta={}, transcript_path=None, events=[],
        first_ts=None, last_ts=None, ordinal=1, outcome="completed", usage={},
    )
    return Run(
        run_id="s_0000000001", kind="session",
        path=Path("/home/user/project/00000000-0000-4000-8000-000000000000.jsonl"),
        project_slug="-home-user-project", session_id="00000000-0000-4000-8000-000000000000",
        claude_version=None, journal=[], main=main,
        agents=[LogicalAgent(key="a3333333333333333", label="impl:child", phase="", attempts=[child])],
        attempts=[child], started_at=None, ended_at=None, diagnostics=[],
    )


def test_every_detector_declares_kinds():
    for detector in all_detectors():
        assert isinstance(detector.kinds, frozenset), detector.id
        assert detector.kinds, f"{detector.id} declares no kinds"
        assert detector.kinds <= {"workflow", "session"}, detector.id


def test_silent_retry_is_workflow_only():
    silent_retry = next(d for d in all_detectors() if d.id == "silent_retry")
    assert silent_retry.kinds == frozenset({"workflow"})


def test_attempt_failed_is_workflow_only():
    attempt_failed = next(d for d in all_detectors() if d.id == "attempt_failed")
    # G-A (G1c): both kinds now (was workflow only)
    assert attempt_failed.kinds == frozenset({"workflow", "session"})


def test_claim_contradiction_is_workflow_only():
    """claim_contradiction reads Attempt.reported, which only a workflow's
    journal 'result' event ever populates (docs/DESIGN.md section 3): a
    session has no journal at all, so this detector never applies to one."""
    claim_contradiction = next(d for d in all_detectors() if d.id == "claim_contradiction")
    assert claim_contradiction.kinds == frozenset({"workflow"})


def test_workflow_failed_is_workflow_only():
    """docs/DESIGN.md 4.13.3 (v0.3 N2): a session carries no Workflow launch
    and no Run.lifecycle at all (D11), so workflow_failed never applies to
    one. A Run built directly (Run.lifecycle defaults to None) must not
    crash the detector either -- it simply finds nothing to report."""
    workflow_failed = next(d for d in all_detectors() if d.id == "workflow_failed")
    assert workflow_failed.kinds == frozenset({"workflow"})
    assert workflow_failed.run(_workflow_run()) == []


def test_workflow_failed_finding_names_no_rule_id():
    """docs/DESIGN.md 4.13.3: workflow_failed's own paragraph ends 'No rule
    id (section 9 names none for it)' -- unlike every rule-backed detector,
    its one Finding must carry rule_id None and rule_ids () rather than
    silently defaulting to whatever Finding.__post_init__ would otherwise
    synthesize for a detector that does name one."""
    run = _workflow_run()
    run.lifecycle = Lifecycle(
        terminal="failed", source="notification", activity="stale",
        generations=1, diagnostics=[],
    )
    workflow_failed = next(d for d in all_detectors() if d.id == "workflow_failed")
    findings = workflow_failed.run(run)
    assert len(findings) == 1
    assert findings[0].rule_id is None
    assert findings[0].rule_ids == ()


def test_every_other_detector_applies_to_both_kinds():
    # claim_contradiction joins silent_retry and attempt_failed here (unit
    # M1c): it reads Attempt.reported, which only a workflow journal's
    # 'result' event ever populates. workflow_failed joins them in v0.3 N2:
    # a session carries no Workflow launch and no Run.lifecycle at all (D11).
    workflow_only = {"silent_retry", "attempt_failed", "claim_contradiction", "workflow_failed"}
    for detector in all_detectors():
        if detector.id in workflow_only:
            continue
        assert detector.kinds == frozenset({"workflow", "session"}), detector.id


def test_run_detectors_skips_silent_retry_on_a_session_run():
    run = _session_run()
    findings = run_detectors(run)
    assert not any(finding.detector == "silent_retry" for finding in findings)


def test_run_detectors_applies_silent_retry_on_a_retried_workflow_run():
    run = _workflow_run()
    findings = run_detectors(run)
    assert any(finding.detector == "silent_retry" for finding in findings)


def test_run_detectors_matches_manually_filtering_all_detectors():
    run = _workflow_run()
    expected = []
    for detector in all_detectors():
        if run.kind in detector.kinds:
            expected.extend(detector.run(run))
    got = run_detectors(run)
    assert [f.finding_id for f in got] == [f.finding_id for f in expected]


def test_module_exposes_run_detectors():
    assert detectors.run_detectors is run_detectors
