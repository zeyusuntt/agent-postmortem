"""Tests for the attempt_failed detector (.reviews/proposals/2026-09-
v0.2-guided-rescue.md section 4.2, unit M1b): names the cause of a workflow
attempt the journal closed with a `failed` event. The cause comes from
exactly one place, the attempt's own last transcript entry, never a wider
window and never the attempt's first user prompt.

Fixture-backed: tests/fixtures/runs/attempt_failed_causes carries the four
runtime-produced markers (usage_limit_session, usage_limit_weekly,
machine_sleep, api_error), the prompt-mentions-API-Error decoy that must
still come back unattributed, and a completed attempt whose transcript
carries a usage-limit line that is not its last one (attempt_failed must
never fire on it). tests/fixtures/runs/journal_state_resolved (already used
by tests/test_journal_state.py for the journal state machine itself) supplies
the interrupted cause and both severities of the unattributed cause for
free, since its 'failed' keys already exercise exactly those shapes.
"""

from __future__ import annotations

from pathlib import Path

from agent_postmortem import reader
from agent_postmortem.detectors import attempt_failed

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
CAUSES = FIXTURES_ROOT / "attempt_failed_causes"
RESOLVED = FIXTURES_ROOT / "journal_state_resolved"
RETRY_TAIL = FIXTURES_ROOT / "attempt_failed_retry_tail"


def _findings_by_agent(run) -> dict:
    return {finding.agent_id: finding for finding in attempt_failed.DETECTOR.run(run)}


# --- the four runtime-produced markers -------------------------------------


def test_usage_limit_session_cause_and_reset_time():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af100000000000001"]
    assert finding.metrics["cause"] == "usage_limit_session"
    assert finding.metrics["retry_now"] == "no"
    assert finding.metrics["reset_time"] == "2pm"
    assert finding.severity == "major"
    assert "session usage limit reached, resets 2pm" in finding.title
    assert "the key was not retried" in finding.title


def test_usage_limit_weekly_cause_and_reset_time():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af100000000000002"]
    assert finding.metrics["cause"] == "usage_limit_weekly"
    assert finding.metrics["retry_now"] == "no"
    assert finding.metrics["reset_time"] == "Monday 9am"
    assert "weekly usage limit reached, resets Monday 9am" in finding.title


def test_usage_limit_reset_time_is_still_extracted_with_leading_whitespace():
    """docs/DESIGN.md 4.13.2: the anchored kill_sentinel/usage-limit check is
    computed on the flattened text AFTER leading whitespace is stripped.
    reset_time extraction must agree with it -- reading the same, stripped
    text -- rather than the raw, unstripped Event.text excerpt, else a cause
    that correctly fires (anchored on the stripped text) could silently lose
    its reset_time (the unstripped startswith check fails) for no reason a
    reader could see from the cause alone."""
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af100000000000007"]
    assert finding.metrics["cause"] == "usage_limit_session"
    assert finding.metrics["reset_time"] == "2pm"
    assert "session usage limit reached, resets 2pm" in finding.title


def test_machine_sleep_cause_has_no_reset_time_and_can_retry_now():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af100000000000003"]
    assert finding.metrics["cause"] == "machine_sleep"
    assert finding.metrics["retry_now"] == "yes"
    assert finding.metrics["reset_time"] is None
    assert finding.rule_id is None  # only the two usage limits carry R-USAGE-LIMIT
    assert "the machine went to sleep" in finding.title


def test_api_error_cause_has_no_reset_time_and_can_retry_now():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af100000000000004"]
    assert finding.metrics["cause"] == "api_error"
    assert finding.metrics["retry_now"] == "yes"
    assert finding.metrics["reset_time"] is None
    assert finding.rule_id is None
    assert "a server error occurred" in finding.title


# --- the decoy: a prompt that mentions "API Error" is never read -----------


def test_prompt_mentioning_api_error_does_not_leak_into_the_cause():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af100000000000005"]
    assert finding.metrics["cause"] == "unattributed"
    assert finding.metrics["retry_now"] == "unknown"
    assert "cause not recorded in the transcript" in finding.title


# --- the non-triggering fixture: outcome gates everything -------------------


def test_a_marker_that_is_not_the_last_line_of_a_completed_attempt_never_fires():
    run = reader.load_run(CAUSES)
    findings = _findings_by_agent(run)
    assert "af100000000000006" not in findings
    # Confirm the fixture really is completed, not merely absent from the run.
    attempt = next(a for a in run.attempts if a.agent_id == "af100000000000006")
    assert attempt.outcome == "completed"


# --- evidence line format ----------------------------------------------------


def test_evidence_line_is_path_colon_line_of_the_last_transcript_entry():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af100000000000001"]
    assert len(finding.evidence) == 1
    path_part, line_no, excerpt = finding.evidence[0].split(":", 2)
    assert path_part == "agent-af100000000000001.jsonl"
    assert line_no.strip() == "3"
    assert "resets 2pm" in excerpt


# --- F3: a marker quoted mid-sentence in the last assistant entry must -----
# --- never be attributed -- only str.startswith on the marker counts ------


def test_session_marker_quoted_mid_sentence_is_unattributed():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af1000000000000a1"]
    assert finding.metrics["cause"] == "unattributed"
    assert finding.metrics["reset_time"] is None
    assert "cause not recorded in the transcript" in finding.title


def test_weekly_marker_quoted_mid_sentence_is_unattributed():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af1000000000000a2"]
    assert finding.metrics["cause"] == "unattributed"
    assert finding.metrics["reset_time"] is None


def test_machine_sleep_marker_quoted_mid_sentence_is_unattributed():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af1000000000000a3"]
    assert finding.metrics["cause"] == "unattributed"
    assert finding.metrics["reset_time"] is None


def test_api_error_marker_quoted_mid_sentence_is_unattributed():
    run = reader.load_run(CAUSES)
    finding = _findings_by_agent(run)["af1000000000000a4"]
    assert finding.metrics["cause"] == "unattributed"
    assert finding.metrics["reset_time"] is None


# --- F4: the title tail names three distinct retry states, not two --------


def test_retried_and_both_attempts_failed_gives_no_later_attempt_completed_tail():
    """started, failed, started, failed: the first attempt WAS retried, but
    the retry also failed -- the tail must say so, never claim it was "not
    retried"."""
    run = reader.load_run(RETRY_TAIL)
    finding = _findings_by_agent(run)["afa10000000000001"]
    assert finding.severity == "major"
    assert "retried, no later attempt completed" in finding.title
    assert "the key was not retried" not in finding.title
    assert "a later attempt completed" not in finding.title


def test_last_attempt_of_a_retried_key_still_says_not_retried():
    """The tail is decided per attempt, not per key: the second (last)
    attempt of the same key above has no later attempt of its own."""
    run = reader.load_run(RETRY_TAIL)
    finding = _findings_by_agent(run)["afa10000000000002"]
    assert finding.severity == "major"
    assert "the key was not retried" in finding.title


def test_retried_with_the_later_attempt_still_open_gives_no_later_attempt_completed_tail():
    """started, failed, started (second still open, no result/failed yet):
    a later attempt exists but none has completed -- same tail and severity
    as the both-failed case above, distinct from "not retried"."""
    run = reader.load_run(RETRY_TAIL)
    finding = _findings_by_agent(run)["afb10000000000001"]
    assert finding.severity == "major"
    assert "retried, no later attempt completed" in finding.title
    # the still-open second attempt is not itself failed, so it never gets
    # its own attempt_failed finding
    assert "afb10000000000002" not in _findings_by_agent(run)


# --- interrupted cause and both severities of unattributed, from the -------
# --- journal state machine fixture (no new fixture needed) -----------------


def test_interrupted_cause_when_the_last_event_is_the_kill_sentinel():
    run = reader.load_run(RESOLVED)
    finding = _findings_by_agent(run)["ad800000000000001"]
    assert finding.metrics["cause"] == "interrupted"
    assert finding.metrics["retry_now"] == "unknown"
    assert finding.severity == "major"


def test_unattributed_terminal_attempt_is_major():
    run = reader.load_run(RESOLVED)
    finding = _findings_by_agent(run)["ad300000000000001"]
    assert finding.metrics["cause"] == "unattributed"
    assert finding.severity == "major"
    assert "the key was not retried" in finding.title


def test_unattributed_attempt_retried_and_completed_later_is_minor():
    run = reader.load_run(RESOLVED)
    finding = _findings_by_agent(run)["ad400000000000001"]
    assert finding.metrics["cause"] == "unattributed"
    assert finding.severity == "minor"
    assert "a later attempt completed" in finding.title


def test_fires_once_per_failed_attempt_over_the_whole_resolved_fixture():
    """journal_state_resolved has four 'failed'-outcome attempts (jsm-failed,
    the first jsm-retry-after-fail attempt, jsm-double-fail, and
    jsm-failed-with-sentinel); attempt_failed must fire exactly once each,
    never on the completed attempts alongside them."""
    run = reader.load_run(RESOLVED)
    findings = attempt_failed.DETECTOR.run(run)
    assert len(findings) == 4
    agent_ids = {finding.agent_id for finding in findings}
    assert agent_ids == {
        "ad300000000000001",
        "ad400000000000001",
        "ad700000000000001",
        "ad800000000000001",
    }


# --- registry -----------------------------------------------------------


def test_attempt_failed_is_registered_and_workflow_only():
    from agent_postmortem.detectors import all_detectors

    detector = next(d for d in all_detectors() if d.id == "attempt_failed")
    assert detector.kinds == frozenset({"workflow"})


def test_reset_time_keeps_a_time_zone_name_readable():
    """Real markers end in "resets 11:20am (America/Los_Angeles)". The
    sanitizer must keep the slash, underscore, and parentheses a zone name
    needs, and still strip anything outside that small set."""
    assert attempt_failed._clean_reset_time("11:20am (America/Los_Angeles)") == (
        "11:20am (America/Los_Angeles)"
    )
    assert attempt_failed._clean_reset_time("2pm <b>x</b>; rm -rf /") == "2pm bx/b rm -rf /"
    assert len(attempt_failed._clean_reset_time("x" * 100)) == 40
