"""N3b fix round 2, R2-3: the S13 dedupe of identical rendered evidence lines
applies at share and local only. Raw keeps today's v0.2 count and order in
FINDINGS, STORIES and the bug report, with and without --evidence. The
expected lines below are literals of the v0.2 output for Codex's exact input
(one finding, two records with the same pointer and different excerpts)."""

from __future__ import annotations

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests.test_bug_report import _make_findings, _make_run

POINTER = "agent-a1111111111111111.jsonl"
RECORDS = [f"{POINTER}:5: first secret", f"{POINTER}:5: second secret"]


def _run_and_findings():
    run = _make_run()
    findings = _make_findings(run)
    findings[0].evidence = list(RECORDS)
    return run, findings


def _raw() -> Redactor:
    return Redactor(context=DisclosureContext(level="raw"))


def _evidence_block(out: str, header_prefix: str, line_prefix: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header_prefix))
    block = []
    for line in lines[start + 1:]:
        if not line.startswith(line_prefix):
            break
        block.append(line)
    return block


def test_raw_findings_keep_both_pointer_lines_without_evidence():
    run, findings = _run_and_findings()
    out = report.render_run_text(run, findings, _raw(), verbose=True)
    findings_text = out[out.index("FINDINGS"):]
    block = _evidence_block(findings_text, "[", "  agent-")
    assert block == [f"  {POINTER}:5", f"  {POINTER}:5"]


def test_raw_findings_keep_both_excerpt_lines_with_evidence():
    run, findings = _run_and_findings()
    out = report.render_run_text(run, findings, _raw(), verbose=True, evidence=True)
    findings_text = out[out.index("FINDINGS"):]
    block = _evidence_block(findings_text, "[", "  agent-")
    assert block == [f"  {POINTER}:5: first secret", f"  {POINTER}:5: second secret"]


@pytest.mark.parametrize(("evidence", "expected"), [
    (False, [f"  - {POINTER}:5", f"  - {POINTER}:5"]),
    (True, [f"  - {POINTER}:5: first secret", f"  - {POINTER}:5: second secret"]),
])
def test_raw_bug_report_keeps_the_original_count(evidence, expected):
    run, findings = _run_and_findings()
    out = report.render_bug_report(run, findings, _raw(), evidence=evidence)
    section = out[out.index("## Evidence"):out.index("## Suggested fixes")]
    assert [ln for ln in section.splitlines() if ".jsonl:5" in ln] == expected


def test_raw_story_counts_a_duplicate_toward_its_two_lines():
    run, findings = _run_and_findings()
    out = report.render_run_text(run, findings, _raw(), verbose=True, evidence=True)
    stories = out[out.index("STORIES"):out.index("FINDINGS")]
    shown = [line.strip() for line in stories.splitlines() if ".jsonl:5" in line]
    assert shown == [f"{POINTER}:5: first secret", f"{POINTER}:5: second secret"]


@pytest.mark.parametrize("level", ["share", "local"])
def test_share_and_local_still_dedupe_identical_rendered_lines(level):
    run, findings = _run_and_findings()
    out = report.render_run_text(
        run, findings, Redactor(context=DisclosureContext(level=level)), verbose=True,
    )
    findings_text = out[out.index("FINDINGS"):]
    assert len([ln for ln in findings_text.splitlines() if ".jsonl:5" in ln]) == 1


def test_raw_story_and_findings_keep_identical_records_and_the_two_line_cap():
    run = _make_run()
    findings = _make_findings(run)
    same = f"{POINTER}:5: same text"
    findings[0].evidence = [same, same, f"{POINTER}:6: other"]
    out = report.render_run_text(run, findings, _raw(), verbose=True, evidence=True)
    stories = out[out.index("STORIES"):out.index("FINDINGS")]
    assert [ln.strip() for ln in stories.splitlines() if POINTER in ln] == [
        f"{POINTER}:5: same text", f"{POINTER}:5: same text",
    ]
    findings_text = out[out.index("FINDINGS"):]
    assert _evidence_block(findings_text, "[", "  agent-") == [
        f"  {POINTER}:5: same text", f"  {POINTER}:5: same text", f"  {POINTER}:6: other",
    ]
