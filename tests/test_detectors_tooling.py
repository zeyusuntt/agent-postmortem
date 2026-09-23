"""Tests for the eight tooling detectors (DESIGN.md 4.3): bash_backgrounded,
bash_timeout, tool_json_error, structured_output_failure, codex_write_denied,
classifier_unavailable, git_cwd_mismatch, missing_artifact.

Five detectors have a dedicated fixture run directory under
tests/fixtures/runs/ that already carries their marker (bash_backgrounded and
bash_timeout share one: bash_backgrounded_timeout). The other three
(tool_json_error, classifier_unavailable, git_cwd_mismatch) have no marker in
any fixture on disk, so their firing case is built in memory from the same
dataclasses reader.load_run() would produce, using the literal marker
constants from markers.py rather than retyped text. Every detector's
non-firing case is the shared clean_run fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_postmortem import reader
from agent_postmortem.detectors import (
    bash_backgrounded,
    bash_timeout,
    classifier_unavailable,
    codex_write_denied,
    git_cwd_mismatch,
    missing_artifact,
    structured_output_failure,
    tool_json_error,
)
from agent_postmortem.markers import (
    CLASSIFIER_UNAVAILABLE,
    GIT_NOT_REPO,
    SANDBOX_WRITE_DENIED,
    TOOL_INPUT_INVALID,
)
from agent_postmortem.models import Attempt, Event, LogicalAgent, Run

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
CLEAN_RUN = FIXTURES_ROOT / "clean_run"
BASH_BACKGROUNDED_TIMEOUT = FIXTURES_ROOT / "bash_backgrounded_timeout"
STRUCTURED_OUTPUT_CORRUPT = FIXTURES_ROOT / "structured_output_corrupt"
CODEX_WRITE_DENIED = FIXTURES_ROOT / "codex_write_denied"
MISSING_ARTIFACT = FIXTURES_ROOT / "missing_artifact"

FAKE_CWD = "/home/user/project"
FAKE_SESSION_ID = "00000000-0000-4000-8000-000000000000"

_EVIDENCE_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+): (?P<excerpt>.*)$")


def _load(path: Path) -> Run:
    return reader.load_run(path)


def _clean_run() -> Run:
    return _load(CLEAN_RUN)


def _assert_evidence_format(evidence: list[str], expected_file: str) -> None:
    assert evidence
    for line in evidence:
        match = _EVIDENCE_RE.match(line)
        assert match is not None, line
        assert match.group("file") == expected_file
        assert not match.group("file").startswith("/")
        assert int(match.group("line")) >= 1
        assert len(match.group("excerpt")) <= 160


def _build_run(run_id: str, agent_id: str, event_rows: list[dict]) -> Run:
    """A one-attempt, in-memory Run mirroring reader.load_run()'s shape.

    Used only for the three detectors with no marker anywhere in an on-disk
    fixture; event_rows entries carry the fields tool_result/attachment
    events need: kind, tool_name, text, exit_code, is_error.
    """
    run_path = Path(FAKE_CWD) / "subagents" / "workflows" / run_id
    transcript_path = run_path / f"agent-{agent_id}.jsonl"
    events = [
        Event(
            path=transcript_path,
            line_no=index,
            ts=None,
            kind=row.get("kind", "tool_result"),
            raw_type="user",
            tool_name=row.get("tool_name"),
            text=row["text"],
            exit_code=row.get("exit_code"),
            duration_ms=None,
            is_error=row.get("is_error", False),
            usage={},
            # reader.py computes this from the field's full text before
            # truncating it; mirror that here with the same scanner rather
            # than retyping which marker fragments matched.
            markers=reader._scan_markers(row["text"]),
            read_only_output=row.get("read_only_output", False),
        )
        for index, row in enumerate(event_rows, start=1)
    ]
    attempt = Attempt(
        agent_id=agent_id,
        key=f"v2:{run_id}",
        label="impl:in-memory",
        phase="Implement",
        agent_type="general-purpose",
        meta={},
        transcript_path=transcript_path,
        events=events,
        first_ts=None,
        last_ts=None,
        ordinal=1,
        outcome="completed",
        usage={},
    )
    agent = LogicalAgent(key=attempt.key, label=attempt.label, phase=attempt.phase, attempts=[attempt])
    return Run(
        run_id=run_id,
        path=run_path,
        project_slug="-home-user-project",
        session_id=FAKE_SESSION_ID,
        claude_version="2.1.267",
        journal=[],
        agents=[agent],
        attempts=[attempt],
        started_at=None,
        ended_at=None,
        diagnostics=[],
    )


# --- bash_backgrounded ------------------------------------------------------


def test_bash_backgrounded_fires_once_on_the_backgrounded_marker():
    run = _load(BASH_BACKGROUNDED_TIMEOUT)
    findings = bash_backgrounded.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "bash_backgrounded"
    assert finding.severity == "minor"
    assert finding.rule_id == "R-NO-LONG-BASH"
    assert finding.metrics == {"count": 1}
    assert "1 Bash command" in finding.title
    _assert_evidence_format(finding.evidence, "agent-ac300000000000001.jsonl")


def test_bash_backgrounded_does_not_fire_on_clean_run():
    assert bash_backgrounded.DETECTOR.run(_clean_run()) == []


def test_bash_backgrounded_ignores_an_unrelated_attachment_mentioning_command():
    """An attachment (a hook result) is not a Bash tool_result just because
    its text happens to contain the word 'command'; only a tool_result whose
    resolved tool_name is Bash should count."""
    text = "hook command failed: did not complete within its 10s timeout and was moved to the background."
    run = _build_run(
        "wf_00000000-790",
        "a7900000000000001",
        [{"kind": "attachment", "text": text}],
    )
    assert bash_backgrounded.DETECTOR.run(run) == []


# --- bash_timeout ------------------------------------------------------------


def test_bash_timeout_fires_once_on_exit_143():
    run = _load(BASH_BACKGROUNDED_TIMEOUT)
    findings = bash_timeout.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "bash_timeout"
    assert finding.severity == "minor"
    assert finding.rule_id == "R-NO-LONG-BASH"
    assert finding.metrics == {"count": 1}
    assert "1 Bash command" in finding.title
    _assert_evidence_format(finding.evidence, "agent-ac300000000000001.jsonl")


def test_bash_timeout_does_not_fire_on_clean_run():
    assert bash_timeout.DETECTOR.run(_clean_run()) == []


def test_bash_timeout_ignores_an_unrelated_attachment_mentioning_command():
    text = "hook command failed.\nExit code 143\nCommand timed out after 5s"
    run = _build_run(
        "wf_00000000-800",
        "a8000000000000001",
        [{"kind": "attachment", "text": text}],
    )
    assert bash_timeout.DETECTOR.run(run) == []


# --- tool_json_error -----------------------------------------------------


def _tool_json_error_run() -> Run:
    text = f"{TOOL_INPUT_INVALID[0]}: Read was called with input that could not be parsed as JSON."
    return _build_run(
        "wf_00000000-710",
        "a7100000000000001",
        [{"kind": "tool_result", "tool_name": "Read", "text": text, "is_error": True}],
    )


def test_tool_json_error_fires_once_on_input_validation_error():
    run = _tool_json_error_run()
    findings = tool_json_error.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "tool_json_error"
    assert finding.severity == "minor"
    assert finding.rule_id is None
    assert finding.metrics == {"count": 1, "tools": ["Read"]}
    assert "1 tool call" in finding.title
    _assert_evidence_format(finding.evidence, "agent-a7100000000000001.jsonl")


def test_tool_json_error_does_not_fire_on_clean_run():
    assert tool_json_error.DETECTOR.run(_clean_run()) == []


# --- structured_output_failure ----------------------------------------------


def test_structured_output_failure_fires_once_on_schema_mismatch():
    run = _load(STRUCTURED_OUTPUT_CORRUPT)
    findings = structured_output_failure.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "structured_output_failure"
    assert finding.severity == "major"
    assert finding.rule_id == "R-SCALAR-SCHEMA"
    assert finding.metrics["count"] == 1
    assert "1 StructuredOutput call" in finding.title
    _assert_evidence_format(finding.evidence, "agent-ac400000000000001.jsonl")


def test_structured_output_failure_omits_bytes_when_the_payload_cannot_be_recovered():
    """The reader truncates every event's text to a 400-char excerpt before a
    detector ever sees it (models.py: Event.text), so what the old code
    called 'bytes' was really just the length of that truncated marker text,
    not the recovered payload the DESIGN.md contract calls for. Since v0 has
    no way to recover the real payload, 'bytes' must be omitted rather than
    report a number that looks precise but is not what it claims to be."""
    run = _load(STRUCTURED_OUTPUT_CORRUPT)
    finding = structured_output_failure.DETECTOR.run(run)[0]
    assert "bytes" not in finding.metrics


def test_structured_output_failure_does_not_fire_on_clean_run():
    assert structured_output_failure.DETECTOR.run(_clean_run()) == []


# --- codex_write_denied -------------------------------------------------


def test_codex_write_denied_yields_two_findings_with_the_contract_and_artifact_rules():
    run = _load(CODEX_WRITE_DENIED)
    findings = codex_write_denied.DETECTOR.run(run)
    assert len(findings) == 2
    rule_ids = {finding.rule_id for finding in findings}
    assert rule_ids == {"R-CODEX-CONTRACT", "R-ASSERT-ARTIFACT"}
    for finding in findings:
        assert finding.detector == "codex_write_denied"
        assert finding.severity == "critical"
        assert finding.metrics["count"] == 2
        assert finding.metrics["marker"] == ["sandbox_write_denied", "shell_guard_block"]
        assert len(finding.evidence) == 2


def test_codex_write_denied_evidence_points_at_both_markers():
    run = _load(CODEX_WRITE_DENIED)
    findings = codex_write_denied.DETECTOR.run(run)
    evidence = findings[0].evidence
    assert any("WRITE_DENIED" in line for line in evidence)
    assert any("blocked in workspace" in line for line in evidence)
    for line in evidence:
        match = _EVIDENCE_RE.match(line)
        assert match is not None, line
        assert not match.group("file").startswith("/")
        assert len(match.group("excerpt")) <= 160


def test_codex_write_denied_does_not_fire_on_clean_run():
    assert codex_write_denied.DETECTOR.run(_clean_run()) == []


def test_codex_write_denied_ignores_a_python_script_printing_the_marker_token():
    """docs/DESIGN.md 4.3's D-C: a marker only counts on a Bash tool_result
    whose own command mentions 'codex' (the Codex plugin is the only real
    producer of these markers). tests/fixtures/runs/codex_write_denied's
    third agent runs a small python script that prints the literal
    WRITE_DENIED token -- exactly the shape that used to false-positive when
    a dev agent working on this repository printed fixture or marker text
    through a python/rg loop the read-only heuristic could not classify.
    The command is not read-only (it names 'python', which disqualifies the
    self-reference rule on its own), so this proves the codex-mention check
    is a real, independent restriction, not a restatement of read_only_output."""
    run = _load(CODEX_WRITE_DENIED)
    findings = codex_write_denied.DETECTOR.run(run)
    for finding in findings:
        assert finding.agent_id != "ac500000000000003"
        assert not any("ac500000000000003" in line for line in finding.evidence)


def test_codex_write_denied_fires_on_a_non_tool_result_event():
    """DESIGN.md 4.3: fires when 'any event text' contains the marker, not
    only a tool_result/attachment. An assistant message narrating a denial
    (for example after summarizing a sandboxed reviewer's own report) must
    still be caught."""
    text = f"The reviewer reported: {SANDBOX_WRITE_DENIED[0]}: patch rejected: writing outside of the project"
    run = _build_run(
        "wf_00000000-750",
        "a7500000000000001",
        [{"kind": "assistant", "text": text}],
    )
    findings = codex_write_denied.DETECTOR.run(run)
    assert len(findings) == 2


# --- classifier_unavailable -------------------------------------------------


def _classifier_unavailable_run() -> Run:
    text = (
        f"claude-sonnet-5[1m] is {CLASSIFIER_UNAVAILABLE[0]} (timed out), "
        "so auto mode cannot determine the safety of Bash right now."
    )
    return _build_run(
        "wf_00000000-720",
        "a7200000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text}],
    )


def test_classifier_unavailable_fires_once():
    run = _classifier_unavailable_run()
    findings = classifier_unavailable.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "classifier_unavailable"
    assert finding.severity == "minor"
    assert finding.rule_id is None
    assert finding.metrics == {"count": 1}
    assert "unavailable 1 time" in finding.title
    _assert_evidence_format(finding.evidence, "agent-a7200000000000001.jsonl")


def test_classifier_unavailable_does_not_fire_on_clean_run():
    assert classifier_unavailable.DETECTOR.run(_clean_run()) == []


def test_classifier_unavailable_ignores_an_unrelated_temporarily_unavailable_message():
    """The marker's only distinctive fragment used to be the generic phrase
    'temporarily unavailable'; an unrelated service outage must not be
    mislabeled as a Bash-safety classifier outage."""
    text = "The search index is temporarily unavailable; please retry your query later."
    run = _build_run(
        "wf_00000000-780",
        "a7800000000000001",
        [{"kind": "tool_result", "tool_name": "WebSearch", "text": text}],
    )
    assert classifier_unavailable.DETECTOR.run(run) == []


# --- git_cwd_mismatch --------------------------------------------------------


def _git_cwd_mismatch_run() -> Run:
    text = f"{GIT_NOT_REPO[0]} (or any of the parent directories): .git"
    return _build_run(
        "wf_00000000-730",
        "a7300000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text, "exit_code": 128, "is_error": True}],
    )


def test_git_cwd_mismatch_fires_once():
    run = _git_cwd_mismatch_run()
    findings = git_cwd_mismatch.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "git_cwd_mismatch"
    assert finding.severity == "minor"
    assert finding.rule_id == "R-GIT-CWD"
    assert finding.metrics == {"count": 1}
    assert "1 Bash command" in finding.title
    _assert_evidence_format(finding.evidence, "agent-a7300000000000001.jsonl")


def test_git_cwd_mismatch_does_not_fire_on_clean_run():
    assert git_cwd_mismatch.DETECTOR.run(_clean_run()) == []


def test_git_cwd_mismatch_ignores_an_unrelated_attachment_mentioning_command():
    text = f"hook command failed: {GIT_NOT_REPO[0]} (or any of the parent directories): .git"
    run = _build_run(
        "wf_00000000-810",
        "a8100000000000001",
        [{"kind": "attachment", "text": text}],
    )
    assert git_cwd_mismatch.DETECTOR.run(run) == []


# --- missing_artifact ---------------------------------------------------


def test_missing_artifact_covers_both_the_present_and_absent_path():
    """fixtures/README.md: missing-report.md is never created and must fire
    critical; present-report.md is created here before scanning and must not
    fire. The file (and any directory this test created) is removed in the
    finally block, so /tmp is left exactly as it was found.
    """
    present_path = Path("/tmp/agent-postmortem-fixture/present-report.md")
    pre_existing_dir = present_path.parent.exists()
    present_path.parent.mkdir(parents=True, exist_ok=True)
    present_path.write_text("synthetic audit findings\n", encoding="utf-8")
    try:
        run = _load(MISSING_ARTIFACT)
        findings = missing_artifact.DETECTOR.run(run)

        reported_paths = [path for finding in findings for path in finding.metrics["paths"]]
        assert "/tmp/agent-postmortem-fixture/present-report.md" not in reported_paths
        assert "/tmp/agent-postmortem-fixture/missing-report.md" in reported_paths
        assert len(findings) == 1

        finding = findings[0]
        assert finding.detector == "missing_artifact"
        assert finding.severity == "critical"
        assert finding.rule_id == "R-ASSERT-ARTIFACT"
        assert finding.metrics["verified"] is True
        assert "missing-report.md" in finding.title
        _assert_evidence_format(finding.evidence, "agent-ac600000000000001.jsonl")
    finally:
        present_path.unlink()
        if not pre_existing_dir:
            present_path.parent.rmdir()
    assert not present_path.exists()


def test_missing_artifact_reports_unverified_info_outside_home_or_tmp():
    text = "Please persist the audit to /opt/shared-fixture/audit.md before you finish."
    run = _build_run(
        "wf_00000000-740",
        "a7400000000000001",
        [{"kind": "user", "text": text}],
    )
    findings = missing_artifact.DETECTOR.run(run)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "info"
    assert finding.rule_id == "R-ASSERT-ARTIFACT"
    assert finding.metrics["verified"] is False
    assert "unverified" in finding.title


def test_missing_artifact_does_not_fire_on_clean_run():
    assert missing_artifact.DETECTOR.run(_clean_run()) == []


def test_missing_artifact_ignores_a_negated_instruction():
    text = "Do not write /tmp/forbidden.md; that path is reserved."
    run = _build_run(
        "wf_00000000-760",
        "a7600000000000001",
        [{"kind": "user", "text": text}],
    )
    assert missing_artifact.DETECTOR.run(run) == []


def test_missing_artifact_does_not_extract_a_path_with_a_trailing_suffix():
    """/tmp/archive.md.backup ends in .backup, not one of the supported
    artifact extensions; the regex must not carve out /tmp/archive.md from
    inside a longer filename that merely contains that substring."""
    text = "Please write /tmp/archive.md.backup as the rollup for this run."
    run = _build_run(
        "wf_00000000-770",
        "a7700000000000001",
        [{"kind": "user", "text": text}],
    )
    findings = missing_artifact.DETECTOR.run(run)
    paths = [path for finding in findings for path in finding.metrics["paths"]]
    assert "/tmp/archive.md" not in paths


def test_missing_artifact_tolerates_a_gettempdir_failure(monkeypatch):
    """docs/DESIGN.md 4.3 (M8 review D1): tempfile.gettempdir() is resolved
    lazily, at detection time, not at import time, and its failure (some
    sandboxes have no discoverable temp directory at all) is tolerated as
    simply 'no extra temp root', never propagated. A literal /tmp path is
    still verified through the static root, proving detection keeps working
    -- and, since detectors/__init__.py imports every module eagerly, that
    importing this module (and therefore `agent-postmortem demo`, which
    needs no filesystem access) never depends on gettempdir() succeeding."""

    def _raise() -> str:
        raise FileNotFoundError("no usable temporary directory found")

    monkeypatch.setattr(missing_artifact.tempfile, "gettempdir", _raise)
    text = "Please write /tmp/agent-postmortem-fixture/gettempdir-check.md when done."
    run = _build_run(
        "wf_00000000-820",
        "a8200000000000001",
        [{"kind": "user", "text": text}],
    )
    findings = missing_artifact.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].metrics["verified"] is True


# --- session main coverage (docs/DESIGN.md 4.3 tooling_attempts) -----------

FAKE_SESSION_PATH = Path(FAKE_CWD) / "-home-user-project" / f"{FAKE_SESSION_ID}.jsonl"


def _build_session_run(run_id: str, main_agent_id: str, event_rows: list[dict]) -> Run:
    """A session Run with no child attempts at all: the marker lives only on
    run.main. Proves detectors/base.tooling_attempts (docs/DESIGN.md 4.3)
    reaches the main thread, not just run.attempts (which never includes
    main by design)."""
    transcript_path = FAKE_SESSION_PATH
    events = [
        Event(
            path=transcript_path,
            line_no=index,
            ts=None,
            kind=row.get("kind", "tool_result"),
            raw_type="user",
            tool_name=row.get("tool_name"),
            text=row["text"],
            exit_code=row.get("exit_code"),
            duration_ms=None,
            is_error=row.get("is_error", False),
            usage={},
            markers=reader._scan_markers(row["text"]),
        )
        for index, row in enumerate(event_rows, start=1)
    ]
    main = Attempt(
        agent_id=main_agent_id,
        key=None,
        label="main",
        phase="",
        agent_type=None,
        meta={},
        transcript_path=transcript_path,
        events=events,
        first_ts=None,
        last_ts=None,
        ordinal=1,
        outcome="completed",
        usage={},
    )
    return Run(
        run_id=run_id,
        kind="session",
        path=transcript_path,
        project_slug="-home-user-project",
        session_id=FAKE_SESSION_ID,
        claude_version="2.1.267",
        journal=[],
        main=main,
        agents=[],
        attempts=[],
        started_at=None,
        ended_at=None,
        diagnostics=[],
    )


def test_bash_backgrounded_fires_on_a_session_main_thread_event():
    text = "Command did not complete within its 30s timeout and was moved to the background (ID: 7)."
    run = _build_session_run(
        "s_session-main-bash-bg",
        "a7910000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text}],
    )
    findings = bash_backgrounded.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].agent_id == "a7910000000000001"


def test_bash_timeout_fires_on_a_session_main_thread_event():
    run = _build_session_run(
        "s_session-main-bash-timeout",
        "a7920000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": "", "exit_code": 143}],
    )
    findings = bash_timeout.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].agent_id == "a7920000000000001"


def test_tool_json_error_fires_on_a_session_main_thread_event():
    text = f"{TOOL_INPUT_INVALID[0]}: Read was called with input that could not be parsed as JSON."
    run = _build_session_run(
        "s_session-main-tool-json",
        "a7930000000000001",
        [{"kind": "tool_result", "tool_name": "Read", "text": text, "is_error": True}],
    )
    findings = tool_json_error.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].agent_id == "a7930000000000001"


def test_structured_output_failure_fires_on_a_session_main_thread_event():
    text = "Output does not match required schema: root: must have required property 'x'"
    run = _build_session_run(
        "s_session-main-structured-out",
        "a7940000000000001",
        [{"kind": "tool_result", "tool_name": "StructuredOutput", "text": text, "is_error": True}],
    )
    findings = structured_output_failure.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].agent_id == "a7940000000000001"


def test_codex_write_denied_fires_on_a_session_main_thread_event():
    """The concrete failure the review flagged: a sandbox-denial marker on a
    session's main thread used to return zero findings, reportable as CLEAN
    even with a critical denial in it. docs/DESIGN.md 4.3's D-C: the marker
    only counts on a Bash tool_result whose own command mentions 'codex',
    so this main-thread event is built as that shape (a preceding Bash
    tool_use naming codex, then its tool_result carrying the marker)."""
    text = f"{SANDBOX_WRITE_DENIED[0]}: patch rejected: writing outside of the project"
    run = _build_session_run(
        "s_session-main-codex-denied",
        "a7950000000000001",
        [
            {"kind": "tool_use", "tool_name": "Bash", "text": '{"command": "codex apply patch.diff"}'},
            {"kind": "tool_result", "tool_name": "Bash", "text": text, "is_error": True},
        ],
    )
    findings = codex_write_denied.DETECTOR.run(run)
    assert len(findings) == 2
    assert {finding.agent_id for finding in findings} == {"a7950000000000001"}


def test_classifier_unavailable_fires_on_a_session_main_thread_event():
    text = (
        f"claude-sonnet-5[1m] is {CLASSIFIER_UNAVAILABLE[0]} (timed out), "
        "so auto mode cannot determine the safety of Bash right now."
    )
    run = _build_session_run(
        "s_session-main-classifier",
        "a7960000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text}],
    )
    findings = classifier_unavailable.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].agent_id == "a7960000000000001"


def test_git_cwd_mismatch_fires_on_a_session_main_thread_event():
    text = f"{GIT_NOT_REPO[0]} (or any of the parent directories): .git"
    run = _build_session_run(
        "s_session-main-git-cwd",
        "a7970000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text, "exit_code": 128, "is_error": True}],
    )
    findings = git_cwd_mismatch.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].agent_id == "a7970000000000001"


# --- registry -----------------------------------------------------------


def test_all_eight_tooling_detectors_are_registered():
    from agent_postmortem.detectors import all_detectors

    ids = {detector.id for detector in all_detectors()}
    assert ids >= {
        "bash_backgrounded",
        "bash_timeout",
        "tool_json_error",
        "structured_output_failure",
        "codex_write_denied",
        "classifier_unavailable",
        "git_cwd_mismatch",
        "missing_artifact",
    }


# --- self-reference: read_only_output suppresses the marker-based tooling
# detectors, but never bash_backgrounded/bash_timeout (docs/DESIGN.md 4.3,
# self-reference rule) ------------------------------------------------------

SELF_REFERENCE = FIXTURES_ROOT / "self_reference"


def test_self_reference_fixture_fires_codex_write_denied_only_on_the_real_command():
    """tests/fixtures/runs/self_reference: (a) a Read tool_result quoting
    WRITE_DENIED and 'read-only sandbox', (b) a Bash `sed -n '1,40p'
    src/x.py` result quoting the classifier-outage sentence, (c) a Bash
    `node codex-companion.mjs task run` result carrying a real WRITE_DENIED,
    (d) a USER prompt pasting a review comment that quotes WRITE_DENIED
    (docs/DESIGN.md 4.3's D-B), (e) a Bash `for f in ...; do ... cat "$f";
    done` loop result quoting WRITE_DENIED from a file on disk (D-B). Only
    (c) is a genuine denial: (a), (b), and (e) are read_only_output, and (d)
    is a user event, which codex_write_denied never scans."""
    run = _load(SELF_REFERENCE)
    findings = codex_write_denied.DETECTOR.run(run)
    assert len(findings) == 2  # the fixed denial+artifact pair, from (c) alone
    for finding in findings:
        assert finding.metrics["count"] == 1
        assert finding.agent_id == "af000000000000003"
        assert len(finding.evidence) == 1
        assert "WRITE_DENIED" in finding.evidence[0]


def test_self_reference_fixture_does_not_fire_on_a_user_prompt_quoting_the_marker():
    """docs/DESIGN.md 4.3's D-B: a USER prompt event that merely pastes a
    review or spec mentioning WRITE_DENIED (self_reference's agent
    af000000000000004) must never be mistaken for a real denial -- markers
    only ever count in a tool_result or assistant event, never a user one."""
    run = _load(SELF_REFERENCE)
    findings = codex_write_denied.DETECTOR.run(run)
    assert all(finding.agent_id != "af000000000000004" for finding in findings)


def test_self_reference_fixture_does_not_fire_on_a_bash_loop_around_cat():
    """docs/DESIGN.md 4.3's D-B: `for f in ...; do echo "=== $f ==="; cat
    "$f"; done` (self_reference's agent af000000000000005) is read-only --
    it contains 'cat' as a whole word and none of the write-indicating
    tokens -- even though it is not literally a leading 'cat' command."""
    run = _load(SELF_REFERENCE)
    findings = codex_write_denied.DETECTOR.run(run)
    assert all(finding.agent_id != "af000000000000005" for finding in findings)


def test_self_reference_fixture_never_fires_classifier_unavailable():
    run = _load(SELF_REFERENCE)
    assert classifier_unavailable.DETECTOR.run(run) == []


def test_read_only_read_tool_result_does_not_fire_codex_write_denied():
    run = _build_run(
        "wf_00000000-751",
        "a7510000000000001",
        [{
            "kind": "tool_result", "tool_name": "Read",
            "text": f"{SANDBOX_WRITE_DENIED[0]} and prose about the {SANDBOX_WRITE_DENIED[1]}",
            "read_only_output": True,
        }],
    )
    assert codex_write_denied.DETECTOR.run(run) == []


def test_read_only_bash_result_does_not_fire_classifier_unavailable():
    text = (
        f"claude-sonnet-5[1m] is {CLASSIFIER_UNAVAILABLE[0]} (timed out), "
        "so auto mode cannot determine the safety of Bash right now."
    )
    run = _build_run(
        "wf_00000000-752",
        "a7520000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text, "read_only_output": True}],
    )
    assert classifier_unavailable.DETECTOR.run(run) == []


def test_read_only_bash_result_does_not_fire_tool_json_error():
    text = f"{TOOL_INPUT_INVALID[0]}: Read was called with input that could not be parsed as JSON."
    run = _build_run(
        "wf_00000000-753",
        "a7530000000000001",
        [{"kind": "tool_result", "tool_name": "Read", "text": text, "read_only_output": True}],
    )
    assert tool_json_error.DETECTOR.run(run) == []


def test_read_only_bash_result_does_not_fire_git_cwd_mismatch():
    text = f"{GIT_NOT_REPO[0]} (or any of the parent directories): .git"
    run = _build_run(
        "wf_00000000-754",
        "a7540000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text, "read_only_output": True}],
    )
    assert git_cwd_mismatch.DETECTOR.run(run) == []


def test_read_only_output_never_suppresses_bash_backgrounded():
    """Exception (docs/DESIGN.md 4.3): a timed-out `cat` is still a timed-
    out command -- bash_backgrounded never consults read_only_output."""
    text = "Command did not complete within its 30s timeout and was moved to the background (ID: 1)."
    run = _build_run(
        "wf_00000000-755",
        "a7550000000000001",
        [{"kind": "tool_result", "tool_name": "Bash", "text": text, "read_only_output": True}],
    )
    findings = bash_backgrounded.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].metrics["count"] == 1


def test_read_only_output_never_suppresses_bash_timeout():
    text = "Exit code 143\nCommand timed out after 10m 0s"
    run = _build_run(
        "wf_00000000-756",
        "a7560000000000001",
        [{
            "kind": "tool_result", "tool_name": "Bash", "text": text, "exit_code": 143,
            "read_only_output": True,
        }],
    )
    findings = bash_timeout.DETECTOR.run(run)
    assert len(findings) == 1
    assert findings[0].metrics["count"] == 1
