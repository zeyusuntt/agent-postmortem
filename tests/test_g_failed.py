"""G-A, G1c: a session subagent that reported failure is an `attempt_failed` finding
(docs/DESIGN.md 4.3, 4.13.4, plan rev 5 G1c); `reported_killed` adds none; the internal outcome,
the risk population and WASTED do not move.
"""

from __future__ import annotations

from agent_postmortem import detectors, ledger, reader, report
from tests.g_helpers import Session, X, Y, async_launch, note, sync_launch
from tests.n5c2_helpers import run_cli


def load(tmp_path, entries, children=(X,)):
    s = Session(tmp_path)
    s.main(*entries)
    for child in children:
        s.child(child, meta={"description": "alpha" if child == X else "beta"})
    return s, reader.load_session(s.build())


def failed_findings(run):
    return [f for f in detectors.run_detectors(run) if f.detector == "attempt_failed"]


def test_a_synchronous_failure_is_one_major_finding(tmp_path):
    _s, run = load(tmp_path, [sync_launch("t1", X, 1, "failed")])
    [finding] = failed_findings(run)
    assert finding.severity == "major"
    assert finding.agent_id == X
    assert finding.metrics == {
        "cause": "launch_result_failed", "retry_now": "unknown", "reset_time": None,
    }
    assert finding.title == "alpha failed: its launch result reported failure"
    assert len(finding.evidence) == 1
    assert finding.evidence[0].startswith(f"{run.main.transcript_path.name}:3")


def test_an_asynchronous_failure_is_decided_by_the_notification(tmp_path):
    _s, run = load(tmp_path, [async_launch("t1", X, 1), note(5, X, "t1", "failed")])
    [finding] = failed_findings(run)
    assert finding.metrics["cause"] == "task_notification_failed"
    assert finding.title == "alpha failed: its task notification reported failure"
    assert finding.evidence[0].startswith(f"{run.main.transcript_path.name}:4")


def test_reported_killed_adds_no_finding(tmp_path):
    _s, run = load(tmp_path, [sync_launch("t1", X, 1, "killed")])
    assert failed_findings(run) == []
    assert report.verdict_of(run, detectors.run_detectors(run)).split()[0] == "CLEAN"


def test_the_verdict_is_degraded_for_a_reported_failure(tmp_path):
    _s, run = load(tmp_path, [sync_launch("t1", X, 1, "failed")])
    assert report.verdict_of(run, detectors.run_detectors(run)).split()[0] == "DEGRADED"


def test_account_story_and_scan_reason_use_the_cause_phrase(tmp_path, capsys):
    s, run = load(tmp_path, [sync_launch("t1", X, 1, "failed")])
    code, out, _err = run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root), "--local"])
    assert "1 failed (its launch result reported failure (1))." in out
    assert "its launch result reported failure" in out.split("STORIES")[1]
    findings = detectors.run_detectors(run)
    assert report.attention_reason(run, findings) == (
        "1 attempt failed: its launch result reported failure"
    )


def test_the_consumers_that_must_not_move(tmp_path):
    _s, run = load(
        tmp_path, [sync_launch("t1", X, 1, "failed"), sync_launch("t2", Y, 4, "killed")],
        children=(X, Y),
    )
    assert [a.outcome for a in run.attempts] == ["completed", "completed"]
    assert ledger.risk_population(run) == []
    assert not [f for f in detectors.run_detectors(run) if f.detector == "wasted_time"
                and f.metrics.get("wasted_seconds")]
    assert [a.interrupt for a in run.attempts] == [None, None]
