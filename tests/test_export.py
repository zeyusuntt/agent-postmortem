"""Tests for report.export_run(): the JSON export allowlist (docs/DESIGN.md
5.3). export_run, not Run.to_dict()/Attempt.to_dict(), is what --json must
go through; this file pins the exact key sets of the three object shapes it
produces (run, attempt, finding) so a field can never be added back in by
accident.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import agent_postmortem
from agent_postmortem import reader, report
from agent_postmortem.redact import Redactor

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
# silent_retry_kill lives in the package now (docs/DESIGN.md 5.4/6).
SILENT_RETRY_KILL = Path(agent_postmortem.__file__).parent / "demo" / "wf_00000000-d30"
READER_MINIMAL_RUN = (
    FIXTURES_ROOT / "reader_minimal" / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_b0000000-002"
)

RUN_KEYS = {
    "run_id", "kind", "status", "claude_version", "started_at", "ended_at",
    "counts", "diagnostics", "attempts",
}
COUNTS_KEYS = {"agents", "attempts", "interrupted"}
ATTEMPT_KEYS = {
    "agent_id", "key", "label", "phase", "agent_type", "ordinal", "outcome",
    "first_ts", "last_ts", "usage", "event_count", "interrupt",
}
INTERRUPT_KEYS = {"cause", "silence_seconds"}
FINDING_KEYS = {
    "detector", "severity", "title", "run_id", "agent_id", "evidence",
    "metrics", "rule_id", "finding_id",
}


def _export(run, redactor=None):
    from agent_postmortem import detectors

    findings = detectors.run_detectors(run)
    return report.export_run(run, findings, redactor or Redactor(enabled=False)), findings


def test_run_export_has_exactly_the_allowlisted_keys():
    run = reader.load_run(SILENT_RETRY_KILL)
    exported, _ = _export(run)
    assert set(exported["run"].keys()) == RUN_KEYS
    assert set(exported["run"]["counts"].keys()) == COUNTS_KEYS


def test_attempt_export_has_exactly_the_allowlisted_keys():
    run = reader.load_run(SILENT_RETRY_KILL)
    exported, _ = _export(run)
    attempts = exported["run"]["attempts"]
    assert attempts
    for attempt in attempts:
        assert set(attempt.keys()) == ATTEMPT_KEYS
        if attempt["interrupt"] is not None:
            assert set(attempt["interrupt"].keys()) == INTERRUPT_KEYS


def test_finding_export_has_exactly_the_allowlisted_keys():
    run = reader.load_run(SILENT_RETRY_KILL)
    exported, findings = _export(run)
    assert findings
    for finding in exported["findings"]:
        assert set(finding.keys()) == FINDING_KEYS


def test_run_export_has_no_meta_cwd_branch_event_list_or_text():
    run = reader.load_run(SILENT_RETRY_KILL)
    exported, _ = _export(run)
    for attempt in exported["run"]["attempts"]:
        assert "meta" not in attempt
        assert "cwd" not in attempt
        assert "branch" not in attempt
        assert "events" not in attempt
        assert "text" not in attempt
    assert "path" not in exported["run"]
    assert "project_slug" not in exported["run"]
    assert "journal" not in exported["run"]
    assert "main" not in exported["run"]
    assert "agents" not in exported["run"]  # the LogicalAgent list, not counts.agents


def test_attempt_key_is_a_sha256_digest_not_the_raw_key():
    run = reader.load_run(SILENT_RETRY_KILL)
    exported, _ = _export(run)
    attempts = exported["run"]["attempts"]
    raw_keys = {attempt.key for attempt in run.attempts if attempt.key is not None}
    assert raw_keys  # sanity: this fixture has keyed attempts
    for attempt in attempts:
        if attempt["key"] is not None:
            assert attempt["key"] not in raw_keys
            assert len(attempt["key"]) == 8
    expected = {hashlib.sha256(key.encode("utf-8")).hexdigest()[:8] for key in raw_keys}
    got = {attempt["key"] for attempt in attempts if attempt["key"] is not None}
    assert got == expected


def test_attempt_key_digest_is_unaffected_by_no_redact():
    """The key digest is a structural part of the export schema, not a
    redaction the --no-redact flag turns off."""
    run = reader.load_run(SILENT_RETRY_KILL)
    redacted, _ = _export(run, Redactor(enabled=True))
    raw, _ = _export(run, Redactor(enabled=False))
    redacted_keys = [a["key"] for a in redacted["run"]["attempts"]]
    raw_keys = [a["key"] for a in raw["run"]["attempts"]]
    assert redacted_keys == raw_keys


def test_label_and_phase_are_pseudonymized_by_default_and_raw_with_no_redact():
    run = reader.load_run(SILENT_RETRY_KILL)
    redacted, _ = _export(run, Redactor(enabled=True))
    raw, _ = _export(run, Redactor(enabled=False))

    redacted_labels = {a["label"] for a in redacted["run"]["attempts"]}
    raw_labels = {a["label"] for a in raw["run"]["attempts"]}
    assert "silent-alpha" not in " ".join(redacted_labels)
    assert any("silent-alpha" in label for label in raw_labels)
    assert all(label.startswith("agent-") for label in redacted_labels)


def test_built_in_agent_type_passes_through_but_a_custom_one_is_pseudonymized():
    run = reader.load_run(READER_MINIMAL_RUN)
    exported, _ = _export(run, Redactor(enabled=True))
    types = {a["agent_type"] for a in exported["run"]["attempts"] if a["agent_type"]}
    assert "workflow-subagent" not in types  # not in the built-in set
    assert any(t.startswith("type-") for t in types)


def test_finding_title_is_pseudonymized():
    """A detector's title mechanically embeds the agent's own label (see
    silent_retry's "{label} restarted N times..."); an evidence line, in
    contrast, is raw transcript prose that may happen to share a word with a
    label without being that label, and is not expected to be scrubbed of
    it -- only structural redact() (paths, uuids, emails) applies there."""
    run = reader.load_run(SILENT_RETRY_KILL)
    labels = {agent.label for agent in run.agents}
    exported, _ = _export(run, Redactor(enabled=True))
    silent_retry_findings = [f for f in exported["findings"] if f["detector"] == "silent_retry"]
    assert silent_retry_findings
    for finding in exported["findings"]:
        for label in labels:
            assert label not in finding["title"]
    for finding in silent_retry_findings:
        assert "agent-" in finding["title"]


def test_to_json_defaults_to_redaction_on():
    """docs/DESIGN.md 5.2/5.3: to_json must not fail open. Omitting the
    redactor argument redacts, exactly like passing Redactor(enabled=True)
    explicitly; raw output requires an explicit Redactor(enabled=False)."""
    run = reader.load_run(SILENT_RETRY_KILL)
    from agent_postmortem import detectors

    findings = detectors.run_detectors(run)
    default_payload = report.to_json(run, findings)
    raw_payload = report.to_json(run, findings, Redactor(enabled=False))

    default_labels = {a["label"] for a in default_payload["run"]["attempts"]}
    raw_labels = {a["label"] for a in raw_payload["run"]["attempts"]}
    assert all(label.startswith("agent-") for label in default_labels)
    assert any("silent-alpha" in label for label in raw_labels)


def test_to_json_wraps_export_run_and_adds_rules_verdict_status():
    run = reader.load_run(SILENT_RETRY_KILL)
    from agent_postmortem import detectors
    findings = detectors.run_detectors(run)
    payload = report.to_json(run, findings, Redactor(enabled=False))
    assert set(payload.keys()) == {"run", "findings", "rules", "verdict", "status"}
    assert set(payload["run"].keys()) == RUN_KEYS
    assert payload["status"] == run.status


def test_report_module_never_calls_to_dict():
    text = (REPO_ROOT / "src" / "agent_postmortem" / "report.py").read_text(encoding="utf-8")
    assert "to_dict(" not in text
