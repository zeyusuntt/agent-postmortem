"""G-B, G3 (v0.3.1): a returned result that reports a failure count (docs/DESIGN.md 4.3, 4.13.4).

The shared predicate `claims.self_reported_problems`, the private `SelfReport` fact the reader
keeps, and the `claim_contradiction` clauses `self_reported_failure` and `self_reported_blocked`.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import detectors, reader
from agent_postmortem.detectors import claim_contradiction
from tests.n5b_helpers import Spec, build_workflow
from tests.n5c2_helpers import run_cli

WF = "wf_00000000-ea1"
KEY = "ci310_failed"
SECRET = "SECRETRESULTTEXT"

# (name, result, failed_counts, blocked)
ROWS = [
    ("ci310_failed 1", {KEY: 1}, 1, False),
    ("ci310_failed 0", {KEY: 0}, 0, False),
    ("ci310_failed negative", {KEY: -2}, 0, False),
    ("ci310_failed bool", {KEY: True}, 0, False),
    ("ci310_failed float", {KEY: 1.0}, 0, False),
    ("ci310_failed string", {KEY: "1"}, 0, False),
    ("two keys", {KEY: 2, "lint_failed": 1}, 2, False),
    ("a bare _failed key", {"_failed": 3}, 1, False),
    ("a key with failed in the middle", {"x_failed_total": 2}, 0, False),
    ("a key that only contains failed", {"failed": 3, "failed_total": 3}, 0, False),
    ("the legacy keys are left to their own clauses",
     {"tests_failed": 4, "suite_failed": 2}, 0, False),
    ("blocked true", {"blocked": True}, 0, True),
    ("blocked yes", {"blocked": "yes"}, 0, False),
    ("blocked 1", {"blocked": 1}, 0, False),
    ("blocked false", {"blocked": False}, 0, False),
    ("blockers_fixed", {"blockers_fixed": 3}, 0, False),
    ("blocker_kind", {"blocker_kind": "network"}, 0, False),
    ("blocked_reason", {"blocked_reason": "waiting"}, 0, False),
    ("errors and failures", {"errors": 3, "failures": 2}, 0, False),
    ("nested failure key", {"detail": {"x_failed": 2}}, 0, False),
    ("nested blocked", {"detail": {"blocked": True}}, 0, False),
    ("a list of failure objects", {"items": [{"x_failed": 2}]}, 0, False),
    ("both", {KEY: 1, "blocked": True}, 1, True),
]


def _predicate():
    from agent_postmortem import claims

    return claims.self_reported_problems


@pytest.mark.parametrize("name,result,counts,blocked", ROWS, ids=[r[0] for r in ROWS])
def test_g3_the_shared_predicate_rows(name, result, counts, blocked):
    found = _predicate()(result)
    assert (found.failed_counts, found.blocked) == (counts, blocked)


def test_g3_the_predicate_ignores_a_value_that_is_not_a_dict():
    for value in ("text", None, 3, [KEY], True):
        found = _predicate()(value)
        assert (found.failed_counts, found.blocked) == (0, False)


def _load(tmp_path, monkeypatch, result, journal="completed"):
    run_dir = build_workflow(
        tmp_path, monkeypatch, WF, [Spec("v2:x", 1, journal=journal, result=result)],
    )
    return run_dir, reader.load_run(run_dir)


def _claim_findings(run):
    return [f for f in detectors.run_detectors(run) if f.detector == "claim_contradiction"]


def test_g3_a_failure_count_is_one_major_finding(tmp_path, monkeypatch):
    _dir, run = _load(tmp_path, monkeypatch, {KEY: 1, "note": SECRET})
    [finding] = _claim_findings(run)
    assert finding.severity == "major"
    assert finding.metrics["clauses"] == ["self_reported_failure"]
    assert finding.metrics["self_reported_failure_counts"] == 1
    assert finding.metrics["self_reported_blocked"] is False
    assert finding.title == "completed, but its own returned result reports failures in 1 field"
    assert finding.evidence == ["journal.jsonl:2"]
    assert finding.rule_id is None


def test_g3_two_failure_keys_say_failure_counts(tmp_path, monkeypatch):
    _dir, run = _load(tmp_path, monkeypatch, {KEY: 5, "lint_failed": 1})
    [finding] = _claim_findings(run)
    assert finding.title == "completed, but its own returned result reports failures in 2 fields"
    assert finding.metrics["self_reported_failure_counts"] == 2


def test_g3_blocked_true_alone_is_one_major_finding(tmp_path, monkeypatch):
    _dir, run = _load(tmp_path, monkeypatch, {"blocked": True})
    [finding] = _claim_findings(run)
    assert finding.severity == "major"
    assert finding.metrics["clauses"] == ["self_reported_blocked"]
    assert finding.metrics["self_reported_failure_counts"] == 0
    assert finding.metrics["self_reported_blocked"] is True
    assert finding.title == "completed, but its own returned result reports it was blocked"
    assert finding.evidence == ["journal.jsonl:2"]


def test_g3_failure_counts_and_blocked_make_one_finding_titled_by_the_counts(
    tmp_path, monkeypatch,
):
    _dir, run = _load(tmp_path, monkeypatch, {KEY: 1, "blocked": True})
    [finding] = _claim_findings(run)
    assert finding.metrics["clauses"] == ["self_reported_failure"]
    assert finding.metrics["self_reported_blocked"] is True
    assert finding.title == "completed, but its own returned result reports failures in 1 field"


@pytest.mark.parametrize(
    "result",
    [r[1] for r in ROWS if r[2] == 0 and not r[3] and "tests_failed" not in r[1]],
    ids=[r[0] for r in ROWS if r[2] == 0 and not r[3] and "tests_failed" not in r[1]],
)
def test_g3_results_that_report_nothing_make_no_finding(tmp_path, monkeypatch, result):
    _dir, run = _load(tmp_path, monkeypatch, result)
    assert _claim_findings(run) == []


@pytest.mark.parametrize(
    "result,clause",
    [
        ({"tests_failed": 2, KEY: 1}, "tests_failed"),
        ({"suite_failed": 1, KEY: 1, "blocked": True}, "suite_failed"),
        ({"ok": False, KEY: 1}, "ok_false"),
        ({"ready": False, "blocked": True}, "ready_false"),
        ({"blockers": ["a"], KEY: 3}, "blockers"),
    ],
)
def test_g3_an_attempt_the_legacy_clauses_catch_gets_exactly_one_finding(
    tmp_path, monkeypatch, result, clause,
):
    _dir, run = _load(tmp_path, monkeypatch, result)
    [finding] = _claim_findings(run)
    assert finding.severity == "major"
    assert finding.metrics["clauses"] == [clause]


def test_g3_a_report_path_only_finding_still_gets_the_major_self_report(tmp_path, monkeypatch):
    _dir, run = _load(
        tmp_path, monkeypatch, {KEY: 1, "report_path": "/tmp/agent-postmortem-fixture/g3/absent.md"},
    )
    findings = _claim_findings(run)
    assert len(findings) == 1
    assert findings[0].severity == "major"
    assert "self_reported_failure" in findings[0].metrics["clauses"]


@pytest.mark.parametrize("journal", ["failed", "open"])
def test_g3_only_a_completed_attempt_is_read(tmp_path, monkeypatch, journal):
    _dir, run = _load(tmp_path, monkeypatch, {KEY: 1}, journal=journal)
    assert _claim_findings(run) == []


def test_g3_the_reader_keeps_only_the_selfreport_and_it_is_never_exported(tmp_path, monkeypatch):
    _dir, run = _load(tmp_path, monkeypatch, {KEY: 1, "note": SECRET})
    attempt = run.attempts[0]
    kept = attempt._self_report
    assert (kept.failed_counts, kept.blocked) == (1, False)
    assert KEY not in json.dumps(attempt.to_dict()["reported"]["claims"])
    assert "_self_report" not in attempt.to_dict()
    assert SECRET not in json.dumps(attempt.to_dict())


def test_g3_a_text_result_keeps_an_empty_selfreport(tmp_path, monkeypatch):
    _dir, run = _load(tmp_path, monkeypatch, "blocked: true and x_failed: 3")
    kept = run.attempts[0]._self_report
    assert (kept.failed_counts, kept.blocked) == (0, False)


SINK_ARGVS = [
    ["run", "{dir}"],
    ["run", "{dir}", "--verbose", "--evidence"],
    ["run", "{dir}", "--json"],
    ["run", "{dir}", "--bug-report"],
    ["rescue", "plan", "{dir}"],
    ["rescue", "plan", "{dir}", "--json"],
]


def _reported_key_mentions(text: str) -> int:
    """How often the key sits in a `reported.keys` list (5.3, G-B1: real at local and raw, a
    pseudonym at share); G3 adds no other place. Nothing is removed from the text counted."""
    try:
        payload = json.loads(text)
    except ValueError:
        return 0
    run = payload.get("run") if isinstance(payload, dict) else None
    found = 0
    for attempt in (run or {}).get("attempts", []):
        reported = attempt.get("reported")
        if isinstance(reported, dict):
            found += sum(1 for key in reported.get("keys", []) if key == KEY)
    return found


@pytest.mark.parametrize("level", [[], ["--local"], ["--no-redact"]], ids=["share", "local", "raw"])
@pytest.mark.parametrize("result", [{KEY: 1, "note": SECRET}, {"blocked": True, "note": SECRET}],
                         ids=["failure", "blocked"])
def test_g3_no_key_name_and_no_result_text_in_any_sink(tmp_path, monkeypatch, capsys, level, result):
    run_dir, _run = _load(tmp_path, monkeypatch, result)
    root = str(tmp_path / "root")
    saw_finding = False
    for template in SINK_ARGVS:
        argv = [a.replace("{dir}", str(run_dir)) for a in template] + ["--root", root] + level
        _code, out, err = run_cli(capsys, argv)
        text = out + err
        assert text.count(KEY) == (_reported_key_mentions(out) if level else 0), template
        assert SECRET not in text, template
        saw_finding = saw_finding or "claim_contradiction" in out
    assert saw_finding


def test_g3_text_report_shows_the_title_and_the_story_phrase(tmp_path, monkeypatch, capsys):
    run_dir, _run = _load(tmp_path, monkeypatch, {KEY: 1})
    _code, out, _err = run_cli(
        capsys, ["run", str(run_dir), "--root", str(tmp_path / "root"), "--local", "--verbose"],
    )
    assert "[major] claim_contradiction  completed, but its own returned result reports " \
        "failures in 1 field" in out
    assert "completed but reported 1 failure count" in out
    assert "DEGRADED" in out


def test_g3_the_detector_module_still_registers():
    assert claim_contradiction.DETECTOR.id == "claim_contradiction"


def test_g3_a_failed_attempt_keeps_no_selfreport_even_if_its_event_carries_a_result(
    tmp_path, monkeypatch,
):
    run_dir = build_workflow(tmp_path, monkeypatch, WF, [Spec("v2:x", 1, journal="failed")])
    journal = run_dir / "journal.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    rows[-1]["result"] = {KEY: 3}
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    attempt = reader.load_run(run_dir).attempts[0]
    assert attempt.journal_state == "failed"
    assert attempt._self_report is None
