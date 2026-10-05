"""Tests for Attempt.reported (docs/DESIGN.md section 3/4.1) and the
claim_contradiction detector (.reviews/proposals/2026-09-v0.2-guided-rescue.md
section 4.3, unit M1c): a completed workflow attempt whose own self-reported
result contradicts the outcome the journal recorded.

Fixture-backed: tests/fixtures/runs/claim_contradiction carries eleven keys,
one per case (see tests/fixtures/README.md): each of the five single-clause
shapes alone, two clauses together, a clean dict with no failing clause and
extra non-allowlisted keys, a string result, a commits list with a non-hex
entry, and a failed attempt with no result at all. None of these attempts
carry a transcript: the evidence for this detector is the journal.jsonl
line of the result event, never a transcript excerpt, so no transcript is
needed to exercise it (docs/DESIGN.md section 3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import reader
from agent_postmortem.cli import main
from agent_postmortem.detectors import claim_contradiction
from tests.n3b_helpers import alias_of

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
CLAIM_CONTRADICTION = FIXTURES_ROOT / "claim_contradiction"

PRESENT_REPORT = Path("/tmp/agent-postmortem-fixture/claim/present-report.md")
MISSING_REPORT = Path("/tmp/agent-postmortem-fixture/claim/missing-report.md")

AGENT = {
    "tests_failed": "acd10000000000001",
    "suite_failed": "acd10000000000002",
    "ok_false": "acd10000000000003",
    "ready_false": "acd10000000000004",
    "blockers": "acd10000000000005",
    "report_missing": "acd10000000000006",
    "two_clauses": "acd10000000000007",
    "clean_dict": "acd10000000000008",
    "text_result": "acd10000000000009",
    "commits_mixed": "acd1000000000000a",
    "failed_attempt": "acd1000000000000b",
}


@pytest.fixture(autouse=True)
def _fixture_report_files():
    """impl:clean-dict's result names a report_path that must exist (the
    'no contradiction' case); impl:report-missing names one that must not.
    Created and removed here, mirroring missing_artifact's own fixture
    convention (tests/test_detectors_tooling.py), so /tmp is left exactly
    as it was found either way."""
    PRESENT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    PRESENT_REPORT.write_text("synthetic report\n", encoding="utf-8")
    if MISSING_REPORT.exists():
        MISSING_REPORT.unlink()
    try:
        yield
    finally:
        if PRESENT_REPORT.exists():
            PRESENT_REPORT.unlink()


def _load():
    return reader.load_run(CLAIM_CONTRADICTION)


def _attempt(run, name):
    return next(a for a in run.attempts if a.agent_id == AGENT[name])


def _findings_by_agent(run):
    return {finding.agent_id: finding for finding in claim_contradiction.DETECTOR.run(run)}


# --- Part 1: Attempt.reported (reader/models) -------------------------------


def test_dict_result_reported_shape_and_claims():
    run = _load()
    attempt = _attempt(run, "tests_failed")
    assert attempt.reported == {
        "type": "dict",
        "keys": ["ok", "tests_failed", "tests_passed"],
        "claims": {"ok": True, "tests_passed": 8, "tests_failed": 2},
    }
    assert attempt.reported_line == 3


def test_text_result_reported_is_length_only_never_the_string():
    run = _load()
    attempt = _attempt(run, "text_result")
    text = "All tests pass and the report has been written."
    # The private string itself must never survive onto the Attempt: only
    # 'type' and 'text_length' are present, nothing else.
    assert attempt.reported == {"type": "text", "text_length": len(text)}
    assert set(attempt.reported.keys()) == {"type", "text_length"}


def test_failed_attempt_has_no_reported_at_all():
    run = _load()
    attempt = _attempt(run, "failed_attempt")
    assert attempt.outcome == "failed"
    assert attempt.reported is None
    assert attempt.reported_line is None


def test_non_allowlisted_keys_appear_in_keys_but_never_in_claims():
    run = _load()
    attempt = _attempt(run, "clean_dict")
    assert attempt.reported["keys"] == [
        "note", "ok", "report_path", "summary", "tests_failed", "tests_passed",
    ]
    assert "note" not in attempt.reported["claims"]
    assert "summary" not in attempt.reported["claims"]
    assert attempt.reported["claims"] == {
        "ok": True,
        "tests_passed": 10,
        "tests_failed": 0,
        "report_path": "/tmp/agent-postmortem-fixture/claim/present-report.md",
    }


def test_commits_list_keeps_only_hex_entries_but_counts_the_whole_list():
    """['abc1234', 'not-a-hash!', 'deadbeefcafe0123']: the middle entry is
    not a commit-shaped hex string and is dropped from `commits`, but
    `commits_count` reports the agent's self-reported claim of how many
    commits it made -- the length of the list as given, not the length of
    the verified subset."""
    run = _load()
    attempt = _attempt(run, "commits_mixed")
    assert attempt.reported["claims"]["commits_count"] == 3
    assert attempt.reported["claims"]["commits"] == ["abc1234", "deadbeefcafe0123"]


# --- Part 2: claim_contradiction, one clause at a time ----------------------


def test_tests_failed_clause_alone():
    run = _load()
    finding = _findings_by_agent(run)[AGENT["tests_failed"]]
    assert finding.severity == "major"
    assert finding.rule_id is None
    assert finding.metrics["clauses"] == ["tests_failed"]
    assert "2 failed tests" in finding.title
    assert finding.title.startswith("impl:tests-failed attempt 1 completed")
    assert finding.evidence == ["journal.jsonl:3: 2 failed tests"]


def test_suite_failed_clause_alone():
    run = _load()
    finding = _findings_by_agent(run)[AGENT["suite_failed"]]
    assert finding.metrics["clauses"] == ["suite_failed"]
    assert "1 failed suite check" in finding.title
    assert finding.evidence == ["journal.jsonl:5: 1 failed suite check"]


def test_ok_false_clause_alone():
    run = _load()
    finding = _findings_by_agent(run)[AGENT["ok_false"]]
    assert finding.metrics["clauses"] == ["ok_false"]
    assert "ok is false" in finding.title
    assert finding.evidence == ["journal.jsonl:7: ok is false"]


def test_ready_false_clause_alone():
    run = _load()
    finding = _findings_by_agent(run)[AGENT["ready_false"]]
    assert finding.metrics["clauses"] == ["ready_false"]
    assert "ready is false" in finding.title
    assert finding.evidence == ["journal.jsonl:9: ready is false"]


def test_blockers_clause_alone():
    run = _load()
    finding = _findings_by_agent(run)[AGENT["blockers"]]
    assert finding.metrics["clauses"] == ["blockers"]
    assert finding.metrics["claims"]["blockers_count"] == 3
    assert "3 blockers" in finding.title
    assert finding.evidence == ["journal.jsonl:11: 3 blockers"]


def test_report_path_missing_clause_alone():
    run = _load()
    finding = _findings_by_agent(run)[AGENT["report_missing"]]
    assert finding.metrics["clauses"] == ["report_path_missing"]
    # N5b (4.13.4, D2): a report_path clause alone is info, with the D2 title.
    assert finding.severity == "info"
    assert finding.title == (
        "/tmp/agent-postmortem-fixture/claim/missing-report.md is absent now; "  # F fix 7 (W7-2)
        "the tool cannot tell whether it existed when the run ended"
    )
    assert finding.evidence == [
        "journal.jsonl:13: a report_path that does not exist"
    ]


def test_two_clauses_join_with_semicolon_in_one_finding():
    run = _load()
    finding = _findings_by_agent(run)[AGENT["two_clauses"]]
    assert finding.metrics["clauses"] == ["tests_failed", "ok_false"]
    assert "1 failed test; ok is false" in finding.title
    assert finding.evidence == ["journal.jsonl:15: 1 failed test; ok is false"]


# --- Part 2: the non-triggering cases ---------------------------------------


def test_clean_dict_with_no_failing_clause_gives_no_finding():
    run = _load()
    assert AGENT["clean_dict"] not in _findings_by_agent(run)


def test_text_result_gives_no_finding():
    run = _load()
    assert AGENT["text_result"] not in _findings_by_agent(run)


def test_commits_mixed_gives_no_finding():
    """Commits alone are never a contradiction clause in this milestone
    (the 'commit id not in the repo' check needs a repo query and is
    deferred to M2, docs/DESIGN.md section 4.3)."""
    run = _load()
    assert AGENT["commits_mixed"] not in _findings_by_agent(run)


def test_failed_attempt_gives_no_finding():
    run = _load()
    assert AGENT["failed_attempt"] not in _findings_by_agent(run)


def test_fires_exactly_on_the_seven_contradicting_attempts():
    run = _load()
    findings = claim_contradiction.DETECTOR.run(run)
    assert len(findings) == 7
    assert {f.agent_id for f in findings} == {
        AGENT["tests_failed"], AGENT["suite_failed"], AGENT["ok_false"],
        AGENT["ready_false"], AGENT["blockers"], AGENT["report_missing"],
        AGENT["two_clauses"],
    }


# --- registry ----------------------------------------------------------


def test_claim_contradiction_is_registered_and_workflow_only():
    from agent_postmortem.detectors import all_detectors

    detector = next(d for d in all_detectors() if d.id == "claim_contradiction")
    assert detector.kinds == frozenset({"workflow"})


# --- CLI: export allowlist, redaction, text rendering -----------------------


_ATTEMPT_EXPORT_KEYS = {
    "agent_id", "key", "label", "phase", "agent_type", "ordinal", "outcome",
    "first_ts", "last_ts", "usage", "event_count", "interrupt", "reported",
    "facts", "ledger",
}


def _shown_agent_id(real: str) -> str:
    """N5a-1: the default level carries the stable alias in its rendered form,
    never the raw agent id."""
    return f"a:{alias_of('agent', real)}"


def test_json_output_passes_the_export_allowlist_and_redacts_report_path(capsys):
    code = main(["run", str(CLAIM_CONTRADICTION), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)

    attempts = payload["run"]["attempts"]
    assert attempts
    for attempt in attempts:
        assert set(attempt.keys()) == _ATTEMPT_EXPORT_KEYS

    # F1: report_path is now pseudonymized (path-N, docs/DESIGN.md 5.1), so
    # it can no longer be found by a "present-report.md" substring match on
    # the already-redacted value -- look the attempt up by its (never
    # redacted) agent_id instead. path-2 because impl:report-missing's own
    # report_path registers first, in journal order, as path-1.
    clean_dict = next(
        a for a in attempts if a["agent_id"] == _shown_agent_id(AGENT["clean_dict"])
    )
    assert clean_dict["reported"]["claims"]["report_path"] == "path-2"
    assert any(f["detector"] == "claim_contradiction" for f in payload["findings"])


def test_json_output_gives_the_same_path_pseudonym_in_the_attempt_and_the_finding(capsys):
    """F1: the report_path claim on impl:report-missing's own attempt export
    and the same value copied into its claim_contradiction finding's metrics
    must get the identical path-N pseudonym in one render (docs/DESIGN.md
    5.1) -- the same mechanism as agent-N, never two different numbers for
    one raw value."""
    code = main(["run", str(CLAIM_CONTRADICTION), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)

    shown_id = _shown_agent_id(AGENT["report_missing"])
    attempt = next(a for a in payload["run"]["attempts"] if a["agent_id"] == shown_id)
    finding = next(
        f for f in payload["findings"]
        if f["detector"] == "claim_contradiction" and f["agent_id"] == shown_id
    )
    attempt_path = attempt["reported"]["claims"]["report_path"]
    finding_path = finding["metrics"]["claims"]["report_path"]
    assert attempt_path == "path-1"
    assert finding_path == "path-1"
    assert "missing-report.md" not in json.dumps(payload)


def test_json_output_with_no_redact_shows_the_raw_report_path(capsys):
    code = main(["run", str(CLAIM_CONTRADICTION), "--json", "--no-redact"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    attempts = payload["run"]["attempts"]
    clean_dict = next(
        a for a in attempts
        if a["reported"] and a["reported"].get("type") == "dict"
        and "present-report.md" in a["reported"].get("claims", {}).get("report_path", "")
    )
    assert clean_dict["reported"]["claims"]["report_path"] == (
        "/tmp/agent-postmortem-fixture/claim/present-report.md"
    )


def test_text_report_renders_the_finding(capsys):
    # unit M3a: the default report tells this story through STORIES, not a
    # FINDINGS line naming the detector id -- "2 failed tests" is the
    # reconstructed clause phrase and appears either way; the detector id
    # itself only shows up under --verbose (the full v0.1 FINDINGS list).
    code = main(["run", str(CLAIM_CONTRADICTION)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "claim_contradiction" not in captured.out
    assert "2 failed tests" in captured.out

    verbose_code = main(["run", str(CLAIM_CONTRADICTION), "--verbose"])
    verbose_out = capsys.readouterr().out
    assert verbose_code == 0, verbose_out
    assert "claim_contradiction" in verbose_out


# --- report_path is checked only for a run that ended within 72 hours -------
#
# A report written to a temp directory is routinely cleaned up days later; on
# this machine 31 of 50 real claim_contradiction findings were report_path
# clauses on runs weeks old. The clause is evidence only while the file could
# still be expected to exist (docs/DESIGN.md 4.3).


def _run_ending_at(ended_at):
    run = _load()
    run.ended_at = ended_at
    return run


def test_report_path_clause_skipped_when_run_is_older_than_72_hours(monkeypatch):
    from datetime import datetime, timedelta, timezone

    ended = datetime(2026, 9, 28, 9, 5, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(
        claim_contradiction, "_now", lambda: ended + timedelta(hours=72, seconds=1)
    )
    findings = _findings_by_agent(_run_ending_at(ended))
    assert AGENT["report_missing"] not in findings
    # the other clauses do not depend on the file system and still fire
    assert AGENT["tests_failed"] in findings


def test_report_path_clause_fires_when_run_is_recent(monkeypatch):
    from datetime import datetime, timedelta, timezone

    ended = datetime(2026, 9, 28, 9, 5, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(claim_contradiction, "_now", lambda: ended + timedelta(hours=71))
    findings = _findings_by_agent(_run_ending_at(ended))
    assert findings[AGENT["report_missing"]].metrics["clauses"] == ["report_path_missing"]


def test_report_path_clause_fires_when_run_end_time_is_unknown():
    # ended_at is None for a journal-only fixture: nothing says it is old.
    findings = _findings_by_agent(_run_ending_at(None))
    assert findings[AGENT["report_missing"]].metrics["clauses"] == ["report_path_missing"]
