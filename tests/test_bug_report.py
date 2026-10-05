"""Tests for report.render_bug_report: section order and redaction.
See docs/DESIGN.md section 4.4.
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from pathlib import Path

from agent_postmortem import report
from agent_postmortem.models import Attempt, Finding, LogicalAgent, Run
from agent_postmortem.redact import Redactor

_FAKE_HOME = Path("/Users/tester")
_FAKE_UUID = "00000000-0000-4000-8000-000000000000"


def _make_run() -> Run:
    attempt = Attempt(
        agent_id="a1111111111111111",
        key="v2:demo0001",
        label="impl:demo",
        phase="Implement",
        agent_type="sonnet",
        meta={},
        transcript_path=_FAKE_HOME / "project" / "agent-a1111111111111111.jsonl",
        events=[],
        first_ts=datetime(2026, 9, 10, 19, 0, tzinfo=timezone.utc),
        last_ts=datetime(2026, 9, 10, 19, 10, tzinfo=timezone.utc),
        ordinal=1,
        outcome="completed",
        usage={},
    )
    agent = LogicalAgent(key="v2:demo0001", label="impl:demo", phase="Implement", attempts=[attempt])
    return Run(
        run_id="wf_00000000-001",
        path=_FAKE_HOME / "project" / "subagents" / "workflows" / "wf_00000000-001",
        project_slug="-Users-tester-Documents-demo",
        session_id=_FAKE_UUID,
        claude_version="2.1.267",
        journal=[],
        agents=[agent],
        attempts=[attempt],
        started_at=attempt.first_ts,
        ended_at=attempt.last_ts,
        diagnostics=[],
    )


def _make_findings(run: Run) -> list[Finding]:
    return [
        Finding(
            detector="silent_retry",
            severity="major",
            title="impl:demo restarted 2 times, 10m wasted",
            run_id=run.run_id,
            agent_id="a1111111111111111",
            evidence=[
                f"agent-a1111111111111111.jsonl:5: session {_FAKE_UUID} retried from "
                "/Users/tester/project",
            ],
            metrics={"attempts": 2},
            rule_id="R-RETRY-SAFE",
        ),
        Finding(
            detector="tool_json_error",
            severity="minor",
            title="1 tool_result failed JSON validation",
            run_id=run.run_id,
            agent_id="a1111111111111111",
            evidence=["agent-a1111111111111111.jsonl:8: InputValidationError"],
            metrics={"count": 1},
            rule_id=None,
        ),
    ]


def _redactor() -> Redactor:
    return Redactor(enabled=True, home=_FAKE_HOME)


def test_sections_appear_in_order():
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())

    assert text.startswith("# ")
    positions = [
        text.index("## Environment"),
        text.index("## What happened"),
        text.index("## Reproduction"),
        text.index("## Evidence"),
        text.index("## Suggested fixes"),
    ]
    assert positions == sorted(positions)


def test_environment_section_has_claude_version_and_platform():
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())

    assert "## Environment" in text
    assert "2.1.267" in text
    assert platform.platform() in text


def test_what_happened_carries_the_verdict():
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())
    assert "DEGRADED" in text  # a major finding, no critical


def test_reproduction_lists_agents_phase_and_model_with_no_redact():
    run = _make_run()
    findings = _make_findings(run)
    redactor = Redactor(enabled=False, home=_FAKE_HOME)
    text = report.render_bug_report(run, findings, redactor)
    assert "impl:demo" in text
    assert "Implement" in text
    assert "sonnet" in text


def test_reproduction_pseudonymizes_label_phase_and_agent_type_by_default():
    """docs/DESIGN.md 5.1: label, phase, and a non-built-in agent_type are
    user-authored names and must not appear verbatim in a report meant to be
    filed upstream by default."""
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())
    assert "impl:demo" not in text
    assert "Implement" not in text
    assert "sonnet" not in text
    assert "agent-1" in text
    assert "phase-1" in text
    assert "type-1" in text


def test_evidence_includes_finding_lines_and_run_id():
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())
    assert "wf_00000000-001" in text
    # N3b P16: at share an evidence line is a pointer, never its excerpt.
    assert "InputValidationError" not in text
    assert "a1111111111111111" not in text
    assert "  - agent-1.jsonl:8" in text
    # ... and with --evidence at raw the excerpt is back (D10).
    raw = report.render_bug_report(
        run, findings, Redactor(enabled=False, home=_FAKE_HOME), evidence=True,
    )
    assert "InputValidationError" in raw


def test_suggested_fixes_include_rule_text_and_detector_sentence():
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())
    assert "R-RETRY-SAFE" in text
    assert "You may be a retry after a killed attempt" in text
    assert "tool_json_error" in text
    assert "runtime defect" in text.lower()


def test_redacted_output_has_no_users_path_and_no_full_uuid():
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())
    assert "/Users/" not in text
    assert _FAKE_UUID not in text


def test_no_redact_mode_leaves_the_raw_path_and_uuid_visible():
    run = _make_run()
    findings = _make_findings(run)
    redactor = Redactor(enabled=False, home=_FAKE_HOME)
    # N3b D10: raw prints an excerpt only with --evidence.
    text = report.render_bug_report(run, findings, redactor, evidence=True)
    assert "/Users/tester" in text


def test_footer_warns_that_evidence_lines_are_unredacted_transcript_excerpts():
    """docs/DESIGN.md 5.2's D2: the report is redacted (paths, ids, emails,
    account names, agent names) but evidence lines are transcript excerpts
    quoted as-is -- the footer must say so plainly instead of ever calling
    the whole output safe to paste unread."""
    run = _make_run()
    findings = _make_findings(run)
    text = report.render_bug_report(run, findings, _redactor())
    # N3b P16: at share the evidence lines are pointers only, so the footer
    # says that instead of warning about excerpts.
    assert text.rstrip("\n").endswith(
        "Redacted: paths, ids, emails, account names, agent names. "
        "Evidence lines are pointers and marker codes only; "
        "no transcript excerpts are included."
    )
    assert "safe to paste" not in text.lower()
    raw = report.render_bug_report(
        run, findings, Redactor(enabled=False, home=_FAKE_HOME), evidence=True,
    )
    assert "Evidence lines include transcript excerpts (--evidence); read them first." in raw
