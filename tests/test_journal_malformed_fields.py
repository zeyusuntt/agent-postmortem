"""F2 (M1 review finding 2): a journal `started` entry whose `agentId`,
`key`, `label`, or `phase` is not a string must never crash load_run, the
text report, or the JSON report. Before this fix, `_valid_agent_id(123, ...)`
raised TypeError for a non-string agentId, and a non-string key could reach
`report._key_digest`'s `.encode()` call the same way.

tests/fixtures/runs/journal_malformed_fields carries one clean attempt plus
four malformed 'started' entries: a non-string agentId, a non-string key, a
non-string label, and a non-string phase (see tests/fixtures/README.md). A
non-string key or agentId must skip the whole entry (no Attempt built) with a
line-specific diagnostic and force status INCOMPLETE; a non-string label or
phase must not skip the entry -- it becomes "" with its own diagnostic.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_postmortem import reader
from agent_postmortem.cli import main

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
WORKFLOW_DIR = (
    FIXTURES_ROOT
    / "journal_malformed_fields"
    / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000"
    / "subagents"
    / "workflows"
    / "wf_00000000-af1"
)


def _load():
    return reader.load_run(WORKFLOW_DIR)


# --- reader.load_run: no crash, correct skip/coerce behavior, INCOMPLETE ---


def test_load_run_does_not_raise():
    run = _load()  # must not raise TypeError
    assert run.run_id == "wf_00000000-af1"


def test_non_string_agent_id_skips_the_entry_and_is_diagnosed():
    run = _load()
    agent_ids = {attempt.key: attempt.agent_id for attempt in run.attempts}
    assert "v2:jmf-bad-agent-id" not in agent_ids
    assert any(
        "non-string agentId" in diag and "journal.jsonl:4" in diag
        for diag in run.diagnostics
    )


def test_non_string_key_skips_the_entry_and_is_diagnosed():
    run = _load()
    labels = {attempt.label for attempt in run.attempts}
    assert "impl:bad-key" not in labels
    assert any(
        "non-string key" in diag and "journal.jsonl:5" in diag for diag in run.diagnostics
    )


def test_non_string_label_becomes_empty_string_and_is_diagnosed():
    run = _load()
    attempt = next(a for a in run.attempts if a.key == "v2:jmf-bad-label")
    assert attempt.label == ""
    assert attempt.outcome == "completed"  # the entry itself is still usable
    assert any(
        "non-string label" in diag and "journal.jsonl:6" in diag for diag in run.diagnostics
    )


def test_non_string_phase_becomes_empty_string_and_is_diagnosed():
    run = _load()
    attempt = next(a for a in run.attempts if a.key == "v2:jmf-bad-phase")
    assert attempt.phase == ""
    assert attempt.outcome == "completed"
    assert any(
        "non-string phase" in diag and "journal.jsonl:8" in diag for diag in run.diagnostics
    )


def test_the_clean_attempt_is_unaffected():
    run = _load()
    attempt = next(a for a in run.attempts if a.key == "v2:jmf-good")
    assert attempt.label == "impl:good"
    assert attempt.phase == "Implement"
    assert attempt.outcome == "completed"


def test_run_status_is_incomplete():
    run = _load()
    assert run.status == "INCOMPLETE"


# --- CLI: text and JSON reports must not crash either ----------------------


def test_postmortem_run_text_does_not_crash(capsys):
    code = main(["run", str(WORKFLOW_DIR)])
    captured = capsys.readouterr()
    assert code in (0, 3, 4), captured.err
    assert "INCOMPLETE" in captured.out


def test_postmortem_run_json_does_not_crash(capsys):
    code = main(["run", str(WORKFLOW_DIR), "--json"])
    captured = capsys.readouterr()
    assert code in (0, 3, 4), captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "INCOMPLETE"
    # bad-key and bad-agent-id never became attempts (skipped); bad-label's
    # own label was coerced to "" rather than crashing json.dumps -- an
    # empty label is falsy, so Pseudonyms leaves it as "" even under the
    # default redaction this call uses.
    labels = {attempt["label"] for attempt in payload["run"]["attempts"]}
    assert "" in labels
    assert len(payload["run"]["attempts"]) == 3  # good, bad-label, bad-phase
