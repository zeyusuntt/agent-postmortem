"""Tests for interrupt attribution (docs/DESIGN.md 4.4): the four causes,
the two 30s windows' boundaries, the silence-None cases, and the rule that
a session's main attempt is never attributed or reported by mislabeled_kill
(docs/DESIGN.md 4.3). Fixture-backed cases use the session_parent_stop,
session_human_interrupt, session_unattributed_long, session_unattributed_short,
evidence_noise, and incomplete_journal fixtures (tests/fixtures/README.md);
the exact 30s window edges are exercised against hand-built Run/Attempt/
ParentIndex objects, since no fixture pins a timestamp to the boundary
itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_postmortem
from agent_postmortem import reader, report
from agent_postmortem.detectors import mislabeled_kill, wasted_time
from agent_postmortem.detectors.base import (
    PARENT_SENTINEL_WINDOW_SECONDS,
    PARENT_STOP_WINDOW_SECONDS,
    attribute_interrupt,
)
from agent_postmortem.models import Attempt, Event, Interrupt, Run
from agent_postmortem.redact import Redactor

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
FAKE_SLUG = "-home-user-project"
FAKE_UUID = "00000000-0000-4000-8000-000000000000"


def _session_path(case: str) -> Path:
    return FIXTURES_ROOT / case / FAKE_SLUG / f"{FAKE_UUID}.jsonl"


def _load_session(case: str):
    return reader.load_session(_session_path(case))


def _load_workflow(case: str):
    if case == "silent_retry_kill":
        # Lives in the package now, not under tests/fixtures/ (docs/
        # DESIGN.md 5.4/6).
        return reader.load_run(Path(agent_postmortem.__file__).parent / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30")
    return reader.load_run(FIXTURES_ROOT / case)


# --- fixture-backed: each cause -------------------------------------------


def test_parent_stop_cause_and_evidence_points_at_the_taskstop_line():
    run = _load_session("session_parent_stop")
    attempt = run.attempts[0]
    assert attempt.interrupt.cause == "parent_stop"
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "info"
    assert "stopped by the parent session" in finding.title
    # Non-triggering: parent_stop never carries a rule id (docs/DESIGN.md
    # 4.3: R-MONITOR-RETRIES is only for runtime and unattributed-major).
    assert finding.rule_id is None
    evidence_line = finding.evidence[-1]
    path_part, line_no, _ = evidence_line.split(":", 2)
    assert path_part == f"{FAKE_UUID}.jsonl"
    assert line_no.strip() == "3"


def test_human_cause_and_evidence_points_at_the_parent_sentinel_line():
    run = _load_session("session_human_interrupt")
    attempt = run.attempts[0]
    assert attempt.interrupt.cause == "human"
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "info"
    assert "interrupted together with the parent session" in finding.title
    assert finding.rule_id is None
    evidence_line = finding.evidence[-1]
    path_part, line_no, _ = evidence_line.split(":", 2)
    assert path_part == f"{FAKE_UUID}.jsonl"
    assert line_no.strip() == "3"


def test_unattributed_major_at_or_above_300s_carries_the_monitor_rule():
    run = _load_session("session_unattributed_long")
    attempt = run.attempts[0]
    assert attempt.interrupt.cause == "unattributed"
    assert attempt.interrupt.silence_seconds == 400.0
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "major"
    assert "consistent with a runtime kill" in finding.title
    assert finding.rule_id == "R-MONITOR-RETRIES"


def test_unattributed_info_below_300s_carries_no_rule():
    run = _load_session("session_unattributed_short")
    attempt = run.attempts[0]
    assert attempt.interrupt.cause == "unattributed"
    assert attempt.interrupt.silence_seconds == 20.0
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "info"
    assert "consistent with a runtime kill" not in finding.title
    # Non-triggering: below the 300s threshold, no rule id.
    assert finding.rule_id is None


def test_runtime_cause_from_silent_retry_kill_carries_the_monitor_rule():
    run = _load_workflow("silent_retry_kill")
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 2
    for finding in findings:
        # N5b (4.13.4): a runtime kill on a non-final attempt is minor (the
        # silent_retry finding carries the loss); it was major before.
        assert finding.severity == "minor"
        assert "killed by the runtime" in finding.title
        assert finding.rule_id == "R-MONITOR-RETRIES"


# --- evidence quality: noise stripping ------------------------------------


def test_evidence_noise_preceding_is_the_last_real_assistant_text():
    run = _load_workflow("evidence_noise")
    attempt = run.attempts[0]
    assert attempt.interrupt.cause == "unattributed"
    preceding = attempt.interrupt.preceding
    assert preceding is not None
    assert preceding.kind == "assistant"
    assert "timeout root cause" in preceding.text
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    # Triggering: the finding fires and its first evidence line points at
    # the real text, not at the thinking block or the noise-only line.
    first_line = finding.evidence[0]
    path_part, line_no, _ = first_line.split(":", 2)
    assert path_part == "agent-acf00000000000001.jsonl"
    assert line_no.strip() == "2"
    # Non-triggering: 42s of silence is well below the major threshold.
    assert finding.severity == "info"
    assert finding.rule_id is None


# --- main attempt of a session is never evaluated -------------------------


def test_main_session_interrupt_is_never_attributed_or_reported():
    """docs/DESIGN.md 4.3: whether a main-session interrupt was human is not
    knowable from the transcript. session_human_interrupt's own main
    transcript ends with the kill sentinel too, so reader.py still sets
    Run.main.interrupt, but _attribute_interrupts must never touch it (it
    stays 'unattributed', the value _build_interrupt gives every interrupt
    before attribution runs), and mislabeled_kill must never report it."""
    run = _load_session("session_human_interrupt")
    assert run.main.outcome == "killed"
    assert run.main.interrupt is not None
    assert run.main.interrupt.cause == "unattributed"
    findings = mislabeled_kill.DETECTOR.run(run)
    assert all(finding.agent_id != run.main.agent_id for finding in findings)
    assert len(findings) == 1  # only the child's, attributed as human


# --- INCOMPLETE status and exit code 4 ------------------------------------


def test_incomplete_journal_status_and_verdict_suffix():
    run = _load_workflow("incomplete_journal")
    assert run.status == "INCOMPLETE"
    findings = wasted_time.DETECTOR.run(run)
    text = report.render_run_text(run, findings, Redactor(enabled=False))
    assert "(INCOMPLETE)" in text.splitlines()[0]
    payload = report.to_json(run, findings, Redactor(enabled=False))
    assert payload["status"] == "INCOMPLETE"
    assert "(INCOMPLETE)" in payload["verdict"]


# --- window boundaries: 29s inside, 31s outside ---------------------------


def _killed_attempt(kill_ts: datetime) -> Attempt:
    kill_event = Event(
        path=Path("/home/user/project/agent-a1111111111111111.jsonl"), line_no=1, ts=kill_ts,
        kind="user", raw_type="user", tool_name=None, text="[Request interrupted by user]",
        exit_code=None, duration_ms=None, is_error=False, usage={},
    )
    attempt = Attempt(
        agent_id="a1111111111111111", key=None, label="impl:boundary", phase="",
        agent_type=None, meta={}, transcript_path=None, events=[kill_event],
        first_ts=None, last_ts=None, ordinal=1, outcome="killed", usage={},
    )
    attempt.interrupt = Interrupt(
        kill_ts=kill_ts, preceding=None, silence_seconds=None,
        cause="unattributed", cause_evidence=None,
    )
    return attempt


def _session_run(attempt: Attempt) -> Run:
    return Run(
        run_id="s_boundary01", kind="session", path=Path("/home/user/project/00000000.jsonl"),
        project_slug=FAKE_SLUG, session_id=FAKE_UUID, claude_version=None, journal=[],
        agents=[], attempts=[attempt], started_at=None, ended_at=None, diagnostics=[],
    )


def test_parent_stop_window_matches_at_29_seconds_before_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    task_stop_ts = kill_ts - timedelta(seconds=29)
    assert 29 <= PARENT_STOP_WINDOW_SECONDS
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[reader._TaskStop(ts=task_stop_ts, ids=frozenset(), line_no=1, text="{}")],
        kill_sentinels=[], compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "parent_stop"
    assert evidence is not None


def test_parent_stop_window_misses_at_31_seconds_before_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    task_stop_ts = kill_ts - timedelta(seconds=31)
    assert 31 > PARENT_STOP_WINDOW_SECONDS
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[reader._TaskStop(ts=task_stop_ts, ids=frozenset(), line_no=1, text="{}")],
        kill_sentinels=[], compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "unattributed"
    assert evidence is None


def test_human_window_matches_at_29_seconds_from_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    sentinel_ts = kill_ts + timedelta(seconds=29)
    assert 29 <= PARENT_SENTINEL_WINDOW_SECONDS
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[],
        kill_sentinels=[reader._KillSentinel(ts=sentinel_ts, line_no=5, text="[Request interrupted by user]")],
        compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "human"
    assert evidence is not None


def test_human_window_misses_at_31_seconds_from_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    sentinel_ts = kill_ts + timedelta(seconds=31)
    assert 31 > PARENT_SENTINEL_WINDOW_SECONDS
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[],
        kill_sentinels=[reader._KillSentinel(ts=sentinel_ts, line_no=5, text="[Request interrupted by user]")],
        compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "unattributed"
    assert evidence is None


# --- exact 30s boundary, both windows, both sides -------------------------
#
# The 29s/31s tests above establish the windows are roughly right; these pin
# the boundary itself: docs/DESIGN.md 4.4 windows are inclusive ("within 30s"
# / "lands within ... of the kill"), so exactly 30.0s must still match and
# 30.001s must not. The sentinel window is also symmetric (either side of
# the kill, docs/DESIGN.md 4.4 clause 2), so both a sentinel before and a
# sentinel after the kill are exercised at the boundary; the TaskStop window
# is one-sided (it must land before the kill, clause 1), so only that side
# applies there.


def test_parent_stop_window_matches_at_exactly_30_seconds_before_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    task_stop_ts = kill_ts - timedelta(seconds=30)
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[reader._TaskStop(ts=task_stop_ts, ids=frozenset(), line_no=1, text="{}")],
        kill_sentinels=[], compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "parent_stop"
    assert evidence is not None


def test_parent_stop_window_misses_at_30_point_001_seconds_before_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    task_stop_ts = kill_ts - timedelta(seconds=30.001)
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[reader._TaskStop(ts=task_stop_ts, ids=frozenset(), line_no=1, text="{}")],
        kill_sentinels=[], compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "unattributed"
    assert evidence is None


def test_human_window_matches_at_exactly_30_seconds_after_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    sentinel_ts = kill_ts + timedelta(seconds=30)
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[],
        kill_sentinels=[reader._KillSentinel(ts=sentinel_ts, line_no=5, text="[Request interrupted by user]")],
        compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "human"
    assert evidence is not None


def test_human_window_misses_at_30_point_001_seconds_after_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    sentinel_ts = kill_ts + timedelta(seconds=30.001)
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[],
        kill_sentinels=[reader._KillSentinel(ts=sentinel_ts, line_no=5, text="[Request interrupted by user]")],
        compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "unattributed"
    assert evidence is None


def test_human_window_matches_at_exactly_30_seconds_before_the_kill():
    """The negative side of the sentinel window: a parent sentinel landing
    30s *before* the kill (the owner's Escape logged slightly earlier than
    the child's own kill timestamp) must match just as one landing after it
    does (docs/DESIGN.md 4.4 clause 2 is symmetric, 'either side')."""
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    sentinel_ts = kill_ts - timedelta(seconds=30)
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[],
        kill_sentinels=[reader._KillSentinel(ts=sentinel_ts, line_no=5, text="[Request interrupted by user]")],
        compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "human"
    assert evidence is not None


def test_human_window_misses_at_30_point_001_seconds_before_the_kill():
    kill_ts = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
    sentinel_ts = kill_ts - timedelta(seconds=30.001)
    attempt = _killed_attempt(kill_ts)
    run = _session_run(attempt)
    parent_index = reader.ParentIndex(
        path=Path("/home/user/project/00000000.jsonl"),
        task_stops=[],
        kill_sentinels=[reader._KillSentinel(ts=sentinel_ts, line_no=5, text="[Request interrupted by user]")],
        compaction_timestamps=[],
    )
    cause, evidence = attribute_interrupt(run, attempt, parent_index)
    assert cause == "unattributed"
    assert evidence is None


# --- mislabeled_kill's own 300s major-severity boundary ---------------------


def test_unattributed_at_exactly_300s_is_major_with_the_monitor_rule():
    attempt = _killed_attempt(datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc))
    attempt.interrupt.cause = "unattributed"
    attempt.interrupt.silence_seconds = 300.0
    run = _session_run(attempt)
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "major"
    assert finding.rule_id == "R-MONITOR-RETRIES"


def test_unattributed_at_299_point_999s_is_info_with_no_rule():
    attempt = _killed_attempt(datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc))
    attempt.interrupt.cause = "unattributed"
    attempt.interrupt.silence_seconds = 299.999
    run = _session_run(attempt)
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "info"
    assert finding.rule_id is None


# --- silence None cases ----------------------------------------------------


def test_unattributed_with_silence_none_says_silence_unknown_and_is_info():
    attempt = _killed_attempt(datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc))
    attempt.interrupt.cause = "unattributed"
    run = _session_run(attempt)
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "info"
    assert "silence unknown" in finding.title
    assert finding.metrics["silence_seconds"] is None
    assert finding.rule_id is None


def test_runtime_with_silence_none_on_a_final_attempt_is_info_and_says_unknown_silence():
    attempt = _killed_attempt(datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc))
    attempt.interrupt.cause = "runtime"
    run = Run(
        run_id="wf_boundary01", kind="workflow", path=Path("/home/user/project/wf_boundary01"),
        project_slug=FAKE_SLUG, session_id=None, claude_version=None, journal=[],
        agents=[], attempts=[attempt], started_at=None, ended_at=None, diagnostics=[],
    )
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    # N5b (4.13.4): (runtime, unknown silence, final) is info; the shape is
    # hand-built (a real runtime kill always has a later attempt), and it
    # was major before.
    assert finding.severity == "info"
    assert "unknown silence" in finding.title
    assert finding.metrics["silence_seconds"] is None
    assert finding.rule_id == "R-MONITOR-RETRIES"
