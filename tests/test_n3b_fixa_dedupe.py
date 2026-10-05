"""N3b fix A, S13: two evidence entries of one finding or story that render to
the same line (they differed only by an excerpt the level does not print) show
that line once, first order kept."""

from __future__ import annotations

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests.test_bug_report import _make_findings, _make_run

POINTER = "agent-a1111111111111111.jsonl"


def _findings(run):
    findings = _make_findings(run)
    findings[0].evidence = [
        f"{POINTER}:5: first excerpt text",
        f"{POINTER}:5: second excerpt text",
        f"{POINTER}:6: other line",
        f"{POINTER}:5: third excerpt text",
    ]
    return findings


def _redactor(level):
    return Redactor(context=DisclosureContext(level=level))


# R2-3: raw keeps the v0.2 count (tests/test_n3b_fixr2_raw_dupes.py)
@pytest.mark.parametrize("level", ["share", "local"])
def test_findings_section_shows_an_identical_pointer_line_once(level):
    run = _make_run()
    out = report.render_run_text(run, _findings(run), _redactor(level), verbose=True)
    findings = out[out.index("FINDINGS"):]
    pointer_lines = [line for line in findings.splitlines() if ".jsonl:5" in line]
    assert len(pointer_lines) == len(set(pointer_lines)) == 1, findings
    assert findings.index(".jsonl:5") < findings.index(".jsonl:6")


@pytest.mark.parametrize("level", ["share", "local", "raw"])
def test_the_story_shows_an_identical_pointer_line_once_and_keeps_two_distinct(level):
    run = _make_run()
    out = report.render_run_text(
        run, _findings(run), _redactor(level), verbose=True, evidence=True,
    )
    stories = out[out.index("STORIES"):out.index("FINDINGS")]
    lines = [line.strip() for line in stories.splitlines() if ".jsonl:" in line]
    if level == "share":
        assert len(lines) == len(set(lines))
        assert any(":5" in line for line in lines) and any(":6" in line for line in lines)


# R2-3: raw keeps the v0.2 count (tests/test_n3b_fixr2_raw_dupes.py)
@pytest.mark.parametrize("level", ["share", "local"])
def test_the_bug_report_shows_an_identical_pointer_line_once(level):
    run = _make_run()
    out = report.render_bug_report(run, _findings(run), _redactor(level))
    evidence = out[out.index("## Evidence"):out.index("## Suggested fixes")]
    pointer_lines = [line for line in evidence.splitlines() if ".jsonl:5" in line]
    assert len(pointer_lines) == 1, evidence


def test_with_excerpts_shown_distinct_excerpts_are_distinct_lines():
    run = _make_run()
    out = report.render_run_text(
        run, _findings(run), _redactor("raw"), verbose=True, evidence=True,
    )
    findings = out[out.index("FINDINGS"):]
    assert "first excerpt text" in findings and "second excerpt text" in findings
