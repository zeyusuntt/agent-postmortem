"""Tests for the session-kind report contract (docs/DESIGN.md 4.7): the
session summary line, the wasted_time session metrics, an AGENT table that
lists subagents only, and pseudonym substitution of subagent labels.
"""

from __future__ import annotations

from pathlib import Path

import agent_postmortem
from agent_postmortem import reader, report
from agent_postmortem.detectors import wasted_time
from agent_postmortem.redact import Redactor

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
FAKE_SLUG = "-home-user-project"
FAKE_UUID = "00000000-0000-4000-8000-000000000000"
# silent_retry_kill lives in the package now (docs/DESIGN.md 5.4/6).
SILENT_RETRY_KILL = Path(agent_postmortem.__file__).parent / "demo" / "wf_00000000-d30"


def _load(case: str):
    return reader.load_session(FIXTURES_ROOT / case / FAKE_SLUG / f"{FAKE_UUID}.jsonl")


# --- wasted_time session metrics -------------------------------------------


def test_wasted_time_session_metrics_are_run_child_seconds_and_count():
    run = _load("session_clean")
    finding = wasted_time.DETECTOR.run(run)[0]
    assert finding.detector == "wasted_time"
    assert finding.severity == "info"
    assert set(finding.metrics) == {"run_seconds", "child_seconds", "child_count"}
    # main spans 09:00:00 to 09:04:05 (245s); each subagent spans 80s.
    assert finding.metrics["run_seconds"] == 245.0
    assert finding.metrics["child_seconds"] == 160.0
    assert finding.metrics["child_count"] == 2


def test_wasted_time_session_title_matches_the_contract_wording():
    run = _load("session_clean")
    finding = wasted_time.DETECTOR.run(run)[0]
    assert finding.title == "session ran 4m; 2 subagents used 2m of agent time"


def test_wasted_time_workflow_metrics_are_unchanged_by_the_session_branch():
    run = reader.load_run(SILENT_RETRY_KILL)
    finding = wasted_time.DETECTOR.run(run)[0]
    assert set(finding.metrics) == {"run_seconds", "attempt_seconds", "wasted_seconds", "wasted_tokens"}


# --- session summary line ---------------------------------------------------


def test_session_summary_line_reports_subagents_and_marks_retries_not_observable():
    run = _load("session_clean")
    line = report._session_summary_line(run)
    assert line == "session ran 4m; 2 subagents, 0 interrupted (0 unattributed); retries not observable"


def test_session_summary_line_counts_interrupted_and_unattributed():
    run = _load("session_unattributed_long")
    line = report._session_summary_line(run)
    assert "1 subagents, 1 interrupted (1 unattributed)" in line
    assert "retries not observable" in line


def test_render_run_text_uses_the_session_summary_not_the_workflow_one():
    # unit M3a: a session's header word is "SESSION" (not "RUN ... (session)")
    # and its account line 2 is the same session summary sentence as before,
    # with a trailing period since it is now a full account sentence.
    run = _load("session_clean")
    text = report.render_run_text(run, [], Redactor(enabled=False))
    lines = text.splitlines()
    assert lines[0].startswith(f"SESSION {run.run_id}")
    assert lines[1] == "session ran 4m; 2 subagents, 0 interrupted (0 unattributed); retries not observable."


# --- AGENT table lists subagents only ---------------------------------------


def test_agent_table_never_lists_main():
    run = _load("session_clean")
    text = report.render_run_text(run, [], Redactor(enabled=False))
    for line in text.splitlines():
        assert not line.startswith("main "), line


def test_agent_table_lists_exactly_the_subagents():
    run = _load("session_clean")
    text = report.render_run_text(run, [], Redactor(enabled=False))
    assert "impl:alpha" in text
    assert "impl:beta" in text


# --- pseudonyms for subagent labels ------------------------------------------


def test_agent_table_pseudonymizes_labels_when_redaction_is_on():
    run = _load("session_clean")
    text = report.render_run_text(run, [], Redactor(enabled=True))
    assert "impl:alpha" not in text
    assert "impl:beta" not in text
    assert "agent-1" in text
    assert "agent-2" in text


def test_agent_table_shows_raw_labels_with_no_redact():
    run = _load("session_clean")
    text = report.render_run_text(run, [], Redactor(enabled=False))
    assert "impl:alpha" in text
    assert "impl:beta" in text


# --- bug report on a session run --------------------------------------------


def test_bug_report_uses_the_session_summary_and_lists_subagents_only():
    run = _load("session_clean")
    text = report.render_bug_report(run, [], Redactor(enabled=False))
    assert "session ran 4m; 2 subagents, 0 interrupted (0 unattributed); retries not observable" in text
    assert "impl:alpha" in text
    assert "impl:beta" in text
    for line in text.splitlines():
        assert not line.strip().startswith("- main ")


def test_bug_report_pseudonymizes_session_subagent_labels_by_default():
    run = _load("session_clean")
    text = report.render_bug_report(run, [], Redactor(enabled=True))
    assert "impl:alpha" not in text
    assert "impl:beta" not in text
    assert "agent-1" in text
    assert "agent-2" in text


# --- JSON export on a session run -------------------------------------------


def test_json_export_of_a_session_run_excludes_main_and_pseudonymizes_labels():
    run = _load("session_clean")
    payload = report.to_json(run, [], Redactor(enabled=True))
    attempts = payload["run"]["attempts"]
    assert len(attempts) == 2  # subagents only; main is never exported
    labels = {a["label"] for a in attempts}
    assert labels == {"agent-1", "agent-2"}
