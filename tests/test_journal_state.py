"""Tests for the per-key journal state machine (docs/DESIGN.md 4.1, unit
M1a): real Claude Code journals carry a `failed` event type the reader used
to ignore entirely, and a real `result` event carries `agentId` but no
`outcome`. Both reader.py's Run.finished and hooks_cli._workflow_finished
must derive 'finished' from the same one rule, so a run whose every key
actually ended is never shown as unfinished forever.

The eight journal sequences below are exactly the ones docs/DESIGN.md 4.1
(and the proposal it is drawn from) calls out. `journal_state_resolved` and
`journal_state_unresolved` are synthetic fixture run directories (see
tests/fixtures/README.md); `failed` alone and the foreign-agentId case need
no transcript at all, so they are covered directly against the loaded Run.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_postmortem import hooks_cli, reader
from agent_postmortem.models import raw_diagnostic

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
RESOLVED = FIXTURES_ROOT / "journal_state_resolved"
UNRESOLVED = FIXTURES_ROOT / "journal_state_unresolved"
ORPHAN_FAILED = FIXTURES_ROOT / "journal_state_orphan_failed"


def _attempts_for_key(run, key: str):
    return sorted(
        (attempt for attempt in run.attempts if attempt.key == key),
        key=lambda a: a.ordinal,
    )


# --- 1. started, result -----------------------------------------------


def test_started_result_completes_the_attempt():
    run = reader.load_run(RESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-completed")
    assert len(attempts) == 1
    assert attempts[0].outcome == "completed"
    assert attempts[0].interrupt is None


# --- 2. started, failed -------------------------------------------------


def test_started_failed_fails_the_attempt():
    run = reader.load_run(RESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-failed")
    assert len(attempts) == 1
    assert attempts[0].outcome == "failed"
    assert attempts[0].interrupt is None


# --- 3. started, failed, started, result --------------------------------


def test_started_failed_started_result_resolves_both_attempts():
    run = reader.load_run(RESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-retry-after-fail")
    assert len(attempts) == 2
    assert attempts[0].outcome == "failed"
    assert attempts[1].outcome == "completed"


# --- 4. started, started, result -- first transcript ends in the sentinel


def test_superseded_attempt_with_sentinel_stays_killed():
    run = reader.load_run(RESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-killed-retry")
    assert len(attempts) == 2
    assert attempts[0].outcome == "killed"
    assert attempts[0].interrupt is not None
    assert attempts[1].outcome == "completed"


# --- 5. started, started, result -- first transcript has no sentinel ----


def test_superseded_attempt_without_sentinel_is_superseded_and_incomplete():
    run = reader.load_run(RESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-superseded")
    assert len(attempts) == 2
    assert attempts[0].outcome == "superseded"
    assert attempts[0].interrupt is None
    assert attempts[1].outcome == "completed"
    assert run.status == "INCOMPLETE"
    assert any("jsm-superseded" in raw_diagnostic(diag) for diag in run.diagnostics)


# --- 6. started, failed, failed ------------------------------------------


def test_second_failed_event_is_an_orphan_diagnostic():
    run = reader.load_run(RESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-double-fail")
    assert len(attempts) == 1
    assert attempts[0].outcome == "failed"
    assert run.status == "INCOMPLETE"
    assert any("jsm-double-fail" in raw_diagnostic(diag) for diag in run.diagnostics)


# --- failed closing an attempt whose own transcript also ends in the -----
# --- sentinel: outcome is 'failed', but the sentinel is kept as evidence -


def test_failed_wins_over_a_trailing_sentinel_but_keeps_the_interrupt():
    run = reader.load_run(RESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-failed-with-sentinel")
    assert len(attempts) == 1
    assert attempts[0].outcome == "failed"
    assert attempts[0].interrupt is not None


# --- Run.finished / Run.status over the whole resolved fixture ----------


def test_resolved_fixture_is_finished_but_incomplete():
    run = reader.load_run(RESOLVED)
    assert run.finished is True
    assert run.status == "INCOMPLETE"


# --- 7. failed alone (no started event at all) ---------------------------


def test_failed_alone_creates_no_attempt_and_is_unfinished():
    run = reader.load_run(ORPHAN_FAILED)
    assert run.attempts == []
    assert run.finished is False
    assert run.status == "INCOMPLETE"
    assert any("jsm-orphan-failed" in raw_diagnostic(diag) for diag in run.diagnostics)


# --- 8. started, then result carrying a foreign agentId ------------------


def test_result_with_foreign_agent_id_closes_nothing():
    run = reader.load_run(UNRESOLVED)
    attempts = _attempts_for_key(run, "v2:jsm-foreign-agent")
    assert len(attempts) == 1
    # Never closed by the mismatched result, so the reader falls back to
    # this attempt's own transcript, which has no kill sentinel.
    assert attempts[0].outcome == "completed"
    assert run.finished is False
    assert run.status == "INCOMPLETE"
    assert any("jsm-foreign-agent" in raw_diagnostic(diag) for diag in run.diagnostics)


# --- hooks_cli._workflow_finished uses the same rule ---------------------


def _write_journal(tmp_path: Path, entries: list[dict]) -> Path:
    journal_path = tmp_path / "journal.jsonl"
    with journal_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return journal_path


def test_hook_workflow_finished_true_for_started_failed(tmp_path):
    journal_path = _write_journal(tmp_path, [
        {"type": "started", "key": "v2:h1", "agentId": "ad900000000000010"},
        {"type": "failed", "key": "v2:h1", "agentId": "ad900000000000010"},
    ])
    assert hooks_cli._workflow_finished(journal_path) is True


def test_hook_workflow_finished_true_for_started_failed_started_result(tmp_path):
    journal_path = _write_journal(tmp_path, [
        {"type": "started", "key": "v2:h2", "agentId": "ad900000000000011"},
        {"type": "failed", "key": "v2:h2", "agentId": "ad900000000000011"},
        {"type": "started", "key": "v2:h2", "agentId": "ad900000000000012"},
        {"type": "result", "key": "v2:h2", "agentId": "ad900000000000012"},
    ])
    assert hooks_cli._workflow_finished(journal_path) is True


def test_hook_workflow_finished_false_for_started_alone(tmp_path):
    journal_path = _write_journal(tmp_path, [
        {"type": "started", "key": "v2:h3", "agentId": "ad900000000000013"},
    ])
    assert hooks_cli._workflow_finished(journal_path) is False
