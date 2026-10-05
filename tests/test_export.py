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
SILENT_RETRY_KILL = Path(agent_postmortem.__file__).parent / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
READER_MINIMAL_RUN = (
    FIXTURES_ROOT / "reader_minimal" / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_b0000000-002"
)

RUN_KEYS = {
    "run_id", "kind", "status", "claude_version", "started_at", "ended_at",
    "counts", "diagnostics", "attempts", "lifecycle",
}
COUNTS_KEYS = {"agents", "attempts", "interrupted"}
ATTEMPT_KEYS = {
    "agent_id", "key", "label", "phase", "agent_type", "ordinal", "outcome",
    "first_ts", "last_ts", "usage", "event_count", "interrupt", "reported",
    "facts", "ledger",
}
INTERRUPT_KEYS = {"cause", "silence_seconds"}
REPORTED_DICT_KEYS = {"type", "keys", "claims"}
REPORTED_TEXT_KEYS = {"type", "text_length"}
FINDING_KEYS = {
    "detector", "severity", "title", "run_id", "agent_id", "evidence",
    "metrics", "rule_id", "rule_ids", "finding_id", "silenced",
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


def test_finding_export_rule_ids_matches_the_finding_and_needs_no_redaction():
    """unit M1d: rule_ids carries no user text, so it is identical with and
    without redaction, and rule_id (kept in sync) is always its first
    element."""
    run = reader.load_run(SILENT_RETRY_KILL)
    exported, findings = _export(run, Redactor(enabled=True))
    by_id = {finding.finding_id: finding for finding in findings}
    silent_retry_exports = [f for f in exported["findings"] if f["detector"] == "silent_retry"]
    assert silent_retry_exports
    for exported_finding in silent_retry_exports:
        source = by_id[exported_finding["finding_id"]]
        assert exported_finding["rule_ids"] == list(source.rule_ids)
        assert exported_finding["rule_id"] == exported_finding["rule_ids"][0]


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
    # unit M3a: stories and next join the top-level payload (docs/DESIGN.md
    # 4.7 rule 9); {run, findings, rules, verdict, status} keep their v0.1
    # meaning unchanged. unit M3c: schema (docs/DESIGN.md 5.3) is new. N5a-1:
    # schema 3 adds the disclosure level (5.3b).
    assert set(payload.keys()) == {
        "schema", "disclosure", "run", "findings", "rules", "verdict", "status", "stories",
        "next",
    }
    assert payload["schema"] == 3
    assert set(payload["run"].keys()) == RUN_KEYS
    assert payload["status"] == run.status


def test_report_module_never_calls_to_dict():
    text = (REPO_ROOT / "src" / "agent_postmortem" / "report.py").read_text(encoding="utf-8")
    assert "to_dict(" not in text


# --- Attempt.reported export (docs/DESIGN.md section 3/5.3, unit M1c) ------

CLAIM_CONTRADICTION = FIXTURES_ROOT / "claim_contradiction"


def test_reported_export_shape_for_dict_and_text_results_and_none_for_failed():
    run = reader.load_run(CLAIM_CONTRADICTION)
    exported, _ = _export(run)
    exported_by_agent = {exp["agent_id"]: exp for exp in exported["run"]["attempts"]}

    dict_attempt = next(a for a in run.attempts if a.label == "impl:clean-dict")
    text_attempt = next(a for a in run.attempts if a.label == "impl:text-result")
    failed_attempt = next(a for a in run.attempts if a.label == "impl:failed-attempt")

    assert set(exported_by_agent[dict_attempt.agent_id]["reported"].keys()) == REPORTED_DICT_KEYS
    assert exported_by_agent[dict_attempt.agent_id]["reported"]["type"] == "dict"
    assert set(exported_by_agent[text_attempt.agent_id]["reported"].keys()) == REPORTED_TEXT_KEYS
    assert exported_by_agent[failed_attempt.agent_id]["reported"] is None


def test_reported_report_path_is_redacted_by_default_and_raw_with_no_redact():
    run = reader.load_run(CLAIM_CONTRADICTION)
    redacted, _ = _export(run, Redactor(enabled=True))
    raw, _ = _export(run, Redactor(enabled=False))

    report_attempt = next(a for a in run.attempts if a.label == "impl:clean-dict")
    # N5a-1: the default level carries an alias, not the raw id, so the attempt
    # is found by its position in the same-order run.attempts.
    position = run.attempts.index(report_attempt)
    redacted_reported = redacted["run"]["attempts"][position]["reported"]
    raw_reported = next(
        a["reported"] for a in raw["run"]["attempts"] if a["agent_id"] == report_attempt.agent_id
    )
    raw_path = raw_reported["claims"]["report_path"]
    redacted_path = redacted_reported["claims"]["report_path"]
    assert raw_path != redacted_path
    # F1: report_path now gets its own path-N pseudonym (docs/DESIGN.md 5.1)
    # instead of the general "~/.../basename"-style structural collapse --
    # path-2 because impl:report-missing's own report_path (a different,
    # still-missing file) registers first, in journal order, as path-1.
    assert redacted_path == "path-2"


# --- F1: report_path never appears raw by default, in any shape ------------
# --- (relative, absolute outside home/tmp, absolute under home) ------------

_PATH_HOME = Path("/home/user")
_RELATIVE_REPORT_PATH = "clients/acme-secret/reports/internal.md"
_OUTSIDE_REPORT_PATH = "/var/reports/internal.md"
_HOME_REPORT_PATH = "/home/user/Documents/secret-client/report.md"


def _reported_with_path(path: str) -> dict:
    return {"type": "dict", "keys": ["report_path"], "claims": {"report_path": path}}


def _make_report_path_run():
    """Three attempts, one per F1 acceptance case: a relative report_path, an
    absolute one outside home and /tmp, and one under home. The finding's
    title and evidence also embed the relative path directly (a detector
    does not normally do this for report_path, but this proves the
    substitution reaches title/evidence too, via _register_names, not only
    the structured reported/metrics export fields)."""
    from agent_postmortem.models import Attempt, Finding, LogicalAgent, Run

    def _attempt(agent_id, key, label, path):
        return Attempt(
            agent_id=agent_id, key=key, label=label, phase="Implement", agent_type=None,
            meta={}, transcript_path=None, events=[], first_ts=None, last_ts=None,
            ordinal=1, outcome="completed", usage={}, reported=_reported_with_path(path),
        )

    attempt_relative = _attempt(
        "a1111111111111111", "v2:relpath", "impl:relpath", _RELATIVE_REPORT_PATH
    )
    attempt_outside = _attempt(
        "a2222222222222222", "v2:outside", "impl:outside", _OUTSIDE_REPORT_PATH
    )
    attempt_home = _attempt(
        "a3333333333333333", "v2:homepath", "impl:homepath", _HOME_REPORT_PATH
    )
    agents = [
        LogicalAgent(key=a.key, label=a.label, phase=a.phase, attempts=[a])
        for a in (attempt_relative, attempt_outside, attempt_home)
    ]
    run = Run(
        run_id="wf_00000000-003",
        path=_PATH_HOME / "project" / "subagents" / "workflows" / "wf_00000000-003",
        project_slug="-home-user-project", session_id=None,
        claude_version="2.1.267", journal=[], agents=agents,
        attempts=[attempt_relative, attempt_outside, attempt_home],
        started_at=None, ended_at=None, diagnostics=[],
    )
    finding = Finding(
        detector="claim_contradiction", severity="major",
        title=f"impl:relpath attempt 1 flagged {_RELATIVE_REPORT_PATH} as missing",
        run_id=run.run_id, agent_id=attempt_relative.agent_id,
        evidence=[f"journal.jsonl:3: report at {_RELATIVE_REPORT_PATH} is missing"],
        metrics={"clauses": ["report_path_missing"], "claims": {"report_path": _RELATIVE_REPORT_PATH}},
        rule_id=None, finding_id="deadbeef0003",
    )
    return run, finding, attempt_relative, attempt_outside, attempt_home


def test_report_path_never_appears_raw_by_default_relative_outside_or_home():
    run, finding, attempt_relative, attempt_outside, attempt_home = _make_report_path_run()
    redactor = Redactor(enabled=True, home=_PATH_HOME)

    # unit M3a: the default text report no longer echoes a finding's raw
    # title/evidence at all (that moved to STORIES' own synthesized text,
    # and to --verbose's FINDINGS list) -- the pseudonym pipeline is proven
    # here against --verbose output instead, the one place a finding's own
    # title (and hence an embedded report_path) is still printed.
    text = report.render_run_text(run, [finding], redactor)
    verbose_text = report.render_run_text(run, [finding], redactor, verbose=True)
    exported = report.export_run(run, [finding], Redactor(enabled=True, home=_PATH_HOME))
    exported_text = str(exported)

    for raw in (_RELATIVE_REPORT_PATH, _OUTSIDE_REPORT_PATH, _HOME_REPORT_PATH):
        assert raw not in text, f"{raw!r} leaked into the text report"
        assert raw not in verbose_text, f"{raw!r} leaked into the verbose text report"
        assert raw not in exported_text, f"{raw!r} leaked into the JSON export"

    # N5a-1: keyed by the raw id through the same-order run.attempts (the
    # exported agent_id is an alias at the default level).
    by_agent = {
        attempt.agent_id: shown
        for attempt, shown in zip(run.attempts, exported["run"]["attempts"])
    }
    relative_pseudo = by_agent[attempt_relative.agent_id]["reported"]["claims"]["report_path"]
    outside_pseudo = by_agent[attempt_outside.agent_id]["reported"]["claims"]["report_path"]
    home_pseudo = by_agent[attempt_home.agent_id]["reported"]["claims"]["report_path"]
    for pseudo in (relative_pseudo, outside_pseudo, home_pseudo):
        assert pseudo.startswith("path-")
    assert len({relative_pseudo, outside_pseudo, home_pseudo}) == 3  # three distinct paths

    # the finding's title and evidence embed the same relative path, and
    # must get the identical pseudonym already assigned to the attempt's own
    # reported claim (docs/DESIGN.md 5.1: the same path in two places gets
    # the same pseudonym in one render).
    assert relative_pseudo in verbose_text
    assert _RELATIVE_REPORT_PATH not in verbose_text


def test_report_path_shows_raw_with_no_redact():
    run, finding, *_ = _make_report_path_run()
    no_redact = Redactor(enabled=False)
    # unit M3a: see test_report_path_never_appears_raw_by_default_relative_
    # outside_or_home -- a raw title/evidence value only ever prints via
    # --verbose now, --no-redact or not.
    verbose_text = report.render_run_text(run, [finding], no_redact, verbose=True)
    exported = report.export_run(run, [finding], Redactor(enabled=False))
    exported_text = str(exported)

    assert _RELATIVE_REPORT_PATH in verbose_text
    for raw in (_RELATIVE_REPORT_PATH, _OUTSIDE_REPORT_PATH, _HOME_REPORT_PATH):
        assert raw in exported_text
