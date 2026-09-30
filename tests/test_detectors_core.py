"""Tests for the three core detectors (silent_retry, mislabeled_kill,
wasted_time) and the run report renderer, against the synthetic clean_run
and silent_retry_kill fixtures. Expected numbers come from
tests/fixtures/README.md.

silent_retry_kill itself lives in the package now (docs/DESIGN.md 5.4/6:
agent_postmortem/demo/wf_00000000-d30), not under tests/fixtures/, so it
is the only case here loaded from there instead of FIXTURES_ROOT.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_postmortem
from agent_postmortem import reader, report
from agent_postmortem.detectors import mislabeled_kill, silent_retry, wasted_time
from agent_postmortem.models import Attempt, Event, LogicalAgent, Run
from agent_postmortem.redact import Redactor

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
CLEAN_RUN = FIXTURES_ROOT / "clean_run"
SILENT_RETRY_KILL = Path(agent_postmortem.__file__).parent / "demo" / "wf_00000000-d30"


def _attempt(agent_id: str, key: str | None, ordinal: int = 1) -> Attempt:
    return Attempt(
        agent_id=agent_id, key=key, label=f"impl:{agent_id}", phase="Implement",
        agent_type=None, meta={}, transcript_path=None, events=[],
        first_ts=None, last_ts=None, ordinal=ordinal, outcome="completed", usage={},
    )


def _run_with_one_unkeyed_attempt() -> Run:
    """Two logical agents, one attempt each (zero real retries), plus one
    attempt whose journal 'started' event carried no key: reader.py leaves
    such an attempt out of run.agents entirely, so it must not be counted as
    a silent retry either."""
    attempt_a = _attempt("a1111111111111111", "v2:a")
    attempt_b = _attempt("a2222222222222222", "v2:b")
    unkeyed = _attempt("a3333333333333333", None)
    agents = [
        LogicalAgent(key="v2:a", label="impl:a", phase="Implement", attempts=[attempt_a]),
        LogicalAgent(key="v2:b", label="impl:b", phase="Implement", attempts=[attempt_b]),
    ]
    return Run(
        run_id="wf_00000000-900", path=Path("/home/user/project/subagents/workflows/wf_00000000-900"),
        project_slug="-home-user-project", session_id=None, claude_version=None, journal=[],
        agents=agents, attempts=[attempt_a, attempt_b, unkeyed],
        started_at=None, ended_at=None, diagnostics=[],
    )


def _load(path: Path):
    return reader.load_run(path)


def _all_core_findings(run):
    return silent_retry.DETECTOR.run(run) + mislabeled_kill.DETECTOR.run(run) + wasted_time.DETECTOR.run(run)


# --- silent_retry -----------------------------------------------------


def test_silent_retry_fires_once_per_retried_key():
    run = _load(SILENT_RETRY_KILL)
    findings = silent_retry.DETECTOR.run(run)
    assert len(findings) == 2
    for finding in findings:
        assert finding.detector == "silent_retry"
        assert finding.severity == "major"
        assert finding.rule_id == "R-RETRY-SAFE"
        assert finding.metrics["attempts"] == 2
        assert finding.metrics["keys"] == 1
        assert finding.metrics["burst"] is False
        # Each killed attempt's own span is exactly 600s (10 minutes).
        assert finding.metrics["wasted_seconds"] == 600.0
        # F5: 2 attempts is 1 restart, not 2 -- the title counts restarts,
        # not attempts.
        assert "restarted 1 time" in finding.title
        assert "10m wasted" in finding.title


def test_silent_retry_evidence_uses_relative_paths_and_line_numbers():
    run = _load(SILENT_RETRY_KILL)
    findings = silent_retry.DETECTOR.run(run)
    for finding in findings:
        assert len(finding.evidence) == 2
        for line in finding.evidence:
            path_part, rest = line.split(":", 1)
            assert not path_part.startswith("/")
            assert path_part.endswith(".jsonl")
            line_no, _, excerpt = rest.partition(":")
            assert line_no.strip().isdigit()
            assert len(excerpt) <= 161  # ": " + up to 160 chars


def test_silent_retry_no_burst_when_keys_start_far_apart():
    run = _load(SILENT_RETRY_KILL)
    findings = silent_retry.DETECTOR.run(run)
    assert all(finding.rule_id == "R-RETRY-SAFE" for finding in findings)
    assert not any(finding.rule_id == "R-CAP-CONCURRENCY" for finding in findings)


def test_silent_retry_does_not_fire_on_clean_run():
    run = _load(CLEAN_RUN)
    assert silent_retry.DETECTOR.run(run) == []


def test_silent_retry_rule_ids_carries_only_retry_safe_without_burst():
    """unit M1d: with no burst, rule_ids is the single-element tuple and
    rule_id (kept in sync) is its first element."""
    run = _load(SILENT_RETRY_KILL)
    findings = silent_retry.DETECTOR.run(run)
    assert findings
    for finding in findings:
        assert finding.rule_ids == ("R-RETRY-SAFE",)
        assert finding.rule_id == finding.rule_ids[0]


def test_silent_retry_evidence_never_quotes_a_prompt_and_carries_the_kill_sentinel():
    """unit M1d: evidence must never repeat the workflow preamble/prompt
    text (the privacy residual this unit removes) but must still point at
    the runtime's own kill-sentinel line for every non-final attempt."""
    run = _load(SILENT_RETRY_KILL)
    findings = silent_retry.DETECTOR.run(run)
    assert findings
    for finding in findings:
        joined = " ".join(finding.evidence)
        assert "Implement the silent-alpha module" not in joined
        assert "Implement the silent-beta module" not in joined
        assert "You may be a retry after a killed attempt" not in joined
        assert any("[Request interrupted by user]" in line for line in finding.evidence)


def test_silent_retry_final_attempt_evidence_is_a_plain_pointer_with_no_excerpt():
    """unit M1d: '<transcript>:<line>: attempt <ordinal> started', no quoted
    prompt text at all."""
    run = _load(SILENT_RETRY_KILL)
    findings = silent_retry.DETECTOR.run(run)
    alpha = next(f for f in findings if "silent-alpha" in f.title or "agent-1" in f.title)
    assert "agent-ac200000000000002.jsonl:1: attempt 2 started" in alpha.evidence
    beta = next(f for f in findings if f is not alpha)
    assert "agent-ac200000000000004.jsonl:1: attempt 2 started" in beta.evidence


def test_silent_retry_burst_carries_both_rule_ids_on_one_finding_per_agent():
    """unit M1d: the old code emitted a second, duplicate Finding (same
    finding_id, title, evidence) for R-CAP-CONCURRENCY on a burst. Now one
    Finding per retried agent carries every rule it backs."""

    def _event(path: Path, line_no: int, text: str, ts: datetime) -> Event:
        return Event(
            path=path, line_no=line_no, ts=ts, kind="user", raw_type="user",
            tool_name=None, text=text, exit_code=None, duration_ms=None,
            is_error=False, usage={},
        )

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = Path("/home/user/project/subagents/workflows/wf_00000000-950")

    def _agent(name: str, key: str, start: datetime) -> LogicalAgent:
        killed_path = root / f"agent-{name}1111111111111111.jsonl"
        killed_events = [
            _event(killed_path, 1, "Implement the thing.", start),
            _event(killed_path, 2, "[Request interrupted by user]", start + timedelta(seconds=600)),
        ]
        killed = Attempt(
            agent_id=f"{name}1111111111111111", key=key, label=f"impl:{name}", phase="Implement",
            agent_type=None, meta={}, transcript_path=killed_path, events=killed_events,
            first_ts=killed_events[0].ts, last_ts=killed_events[-1].ts, ordinal=1,
            outcome="killed", usage={},
        )
        completed_path = root / f"agent-{name}2222222222222222.jsonl"
        completed_start = start + timedelta(seconds=605)
        completed_events = [_event(completed_path, 1, "Retry: implement the thing.", completed_start)]
        completed = Attempt(
            agent_id=f"{name}2222222222222222", key=key, label=f"impl:{name}", phase="Implement",
            agent_type=None, meta={}, transcript_path=completed_path, events=completed_events,
            first_ts=completed_events[0].ts, last_ts=completed_events[0].ts, ordinal=2,
            outcome="completed", usage={},
        )
        return LogicalAgent(key=key, label=f"impl:{name}", phase="Implement", attempts=[killed, completed])

    agent_a = _agent("a", "v2:a", base)
    agent_b = _agent("b", "v2:b", base + timedelta(seconds=2))  # within the 5s burst window
    run = Run(
        run_id="wf_00000000-950", path=root, project_slug="-home-user-project",
        session_id=None, claude_version=None, journal=[], agents=[agent_a, agent_b],
        attempts=[a for agent in (agent_a, agent_b) for a in agent.attempts],
        started_at=base, ended_at=base + timedelta(seconds=610), diagnostics=[],
    )

    findings = silent_retry.DETECTOR.run(run)
    assert len(findings) == 2  # one per retried agent, never a duplicate per rule
    finding_ids = {finding.finding_id for finding in findings}
    assert len(finding_ids) == 2  # each agent's finding is distinct from the other's
    for finding in findings:
        assert finding.metrics["burst"] is True
        assert finding.rule_ids == ("R-RETRY-SAFE", "R-CAP-CONCURRENCY")
        assert finding.rule_id == "R-RETRY-SAFE"


# --- mislabeled_kill ----------------------------------------------------


def test_mislabeled_kill_fires_once_per_killed_attempt():
    """silent_retry_kill is the docs/DESIGN.md 4.4 runtime-cause case: each
    killed attempt's key has a later attempt (the restart), which is
    affirmative evidence the runtime killed and replaced it, not a human or
    the parent session (this fixture has no parent transcript at all)."""
    run = _load(SILENT_RETRY_KILL)
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 2
    for finding in findings:
        assert finding.detector == "mislabeled_kill"
        assert finding.severity == "major"
        assert finding.rule_id == "R-MONITOR-RETRIES"
        assert finding.metrics["cause"] == "runtime"
        # Silence is exactly 416s for every killed attempt in this fixture.
        assert finding.metrics["silence_seconds"] == 416.0
        assert "416s of silence" in finding.title
        assert "killed by the runtime" in finding.title
        assert finding.metrics["events"] > 0


def test_mislabeled_kill_evidence_points_at_the_kill_sentinel_line():
    run = _load(SILENT_RETRY_KILL)
    findings = mislabeled_kill.DETECTOR.run(run)
    for finding in findings:
        last_line = finding.evidence[-1]
        assert "Request interrupted by user" in last_line
        path_part, _, _ = last_line.partition(":")
        assert not path_part.startswith("/")


def test_mislabeled_kill_does_not_fire_on_clean_run():
    run = _load(CLEAN_RUN)
    assert mislabeled_kill.DETECTOR.run(run) == []


# --- wasted_time ----------------------------------------------------------


def test_wasted_time_always_produces_exactly_one_info_finding():
    for path in (CLEAN_RUN, SILENT_RETRY_KILL):
        run = _load(path)
        findings = wasted_time.DETECTOR.run(run)
        assert len(findings) == 1
        assert findings[0].detector == "wasted_time"
        assert findings[0].severity == "info"
        assert findings[0].rule_id is None


def test_wasted_time_totals_1200_seconds_for_silent_retry_kill():
    run = _load(SILENT_RETRY_KILL)
    finding = wasted_time.DETECTOR.run(run)[0]
    assert finding.metrics["wasted_seconds"] == 1200.0
    assert finding.metrics["wasted_tokens"] > 0


def test_wasted_time_is_zero_on_clean_run():
    run = _load(CLEAN_RUN)
    finding = wasted_time.DETECTOR.run(run)[0]
    assert finding.metrics["wasted_seconds"] == 0.0
    assert finding.metrics["wasted_tokens"] == 0


# --- report.render_run_text -----------------------------------------------


def test_render_run_text_line_one_has_verdict_line_two_has_retry_count():
    # unit M3a: the text report's line 2 is now the account's first sentence
    # ("N agents took M attempts."), not the old "N silent retries burned..."
    # summary line -- that wording moved into the account's own "killed by
    # the runtime and restarted" sentence instead.
    run = _load(SILENT_RETRY_KILL)
    findings = _all_core_findings(run)
    text = report.render_run_text(run, findings, Redactor(enabled=False))
    lines = text.splitlines()

    assert lines[0].startswith(f"RUN {run.run_id}")
    assert "DEGRADED" in lines[0]  # major findings, no critical

    assert lines[1] == "2 agents took 4 attempts."
    assert "killed by the runtime and restarted" in lines[2]


def test_render_run_text_clean_run_is_clean_verdict():
    # unit M3a: a clean run's account is just the base sentence -- every
    # other clause (killed/stopped/interrupted/failed/wasted) is omitted
    # outright when its own count is 0, so "0 silent retries" never renders.
    run = _load(CLEAN_RUN)
    findings = _all_core_findings(run)
    text = report.render_run_text(run, findings, Redactor(enabled=False))
    lines = text.splitlines()

    assert lines[0].startswith(f"RUN {run.run_id}")
    assert "CLEAN" in lines[0]
    assert lines[1] == "2 agents took 2 attempts."


def test_render_run_text_has_no_suggested_rules_or_next_action_sections():
    # unit M3a/M6: NEXT ACTION and SUGGESTED RULES are removed from the text
    # report entirely (docs/DESIGN.md 4.7 rule 4); "rules propose" is the
    # way to see rules now (unit M6, section 9), surfaced through WHAT TO
    # DO's own "rules that would have prevented this" item instead of a
    # printed rule table.
    run = _load(SILENT_RETRY_KILL)
    findings = _all_core_findings(run)
    text = report.render_run_text(run, findings, Redactor(enabled=False))

    assert "SUGGESTED RULES" not in text
    assert "NEXT ACTION" not in text
    assert "CLAUDE.md" not in text
    assert "apply" not in text
    assert "agent-postmortem rules propose" in text


def test_render_run_text_what_to_do_replaces_suggested_rules_and_next_action():
    # unit M3a/M6: WHAT TO DO's rule item is the direct replacement for the
    # old SUGGESTED RULES/NEXT ACTION sections -- both rules this fixture
    # backs (R-RETRY-SAFE, R-MONITOR-RETRIES) still show up somewhere
    # findable (rules propose), even though `run` itself never scans another
    # run to print a rate or a rule table.
    run = _load(SILENT_RETRY_KILL)
    findings = _all_core_findings(run)
    text = report.render_run_text(run, findings, Redactor(enabled=False))

    assert "WHAT TO DO" in text
    what_to_do = text[text.index("WHAT TO DO"):text.index("AGENT")]
    assert "[decide]" in what_to_do
    assert "rules that would have prevented this" in what_to_do


def test_format_duration_hours_minutes_and_seconds():
    # format_duration itself lives in detectors/base.py now (the shared
    # home for logic report.py and every detector module both need); report
    # re-exports it via import, so this still exercises the same function.
    assert report.format_duration(7980) == "2h13m"
    assert report.format_duration(41 * 60) == "41m"
    assert report.format_duration(58) == "58s"


def test_agent_table_redacts_a_label_that_leaks_the_home_directory():
    run = _run_with_one_unkeyed_attempt()
    run.agents[0].label = "impl:/Users/alice/private-project"
    text = report.render_run_text(run, [], Redactor(enabled=True, home=Path("/Users/alice")))
    assert "/Users/alice" not in text


def test_bug_report_reproduction_redacts_a_label_that_leaks_the_home_directory():
    run = _run_with_one_unkeyed_attempt()
    run.agents[0].label = "impl:/Users/alice/private-project"
    text = report.render_bug_report(run, [], Redactor(enabled=True, home=Path("/Users/alice")))
    assert "/Users/alice" not in text


def test_to_json_rules_field_is_populated_from_findings():
    run = _load(SILENT_RETRY_KILL)
    findings = _all_core_findings(run)
    payload = report.to_json(run, findings, Redactor(enabled=False))
    assert payload["rules"]
    rule_ids = {row["rule_id"] for row in payload["rules"]}
    assert "R-RETRY-SAFE" in rule_ids
    for row in payload["rules"]:
        assert row["count"] > 0


def test_summary_line_does_not_count_an_unkeyed_attempt_as_a_retry():
    """attempts - agents overcounts by one here (3 attempts, 2 agents) even
    though neither logical agent was actually retried."""
    run = _run_with_one_unkeyed_attempt()
    line = report._summary_line(run)
    assert "3 attempts" in line
    assert "0 silent retries" in line


# --- to_json ----------------------------------------------------------


def test_to_json_round_trips_through_json_loads():
    run = _load(SILENT_RETRY_KILL)
    findings = _all_core_findings(run)
    payload = report.to_json(run, findings, Redactor(enabled=False))

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded == payload
    assert reloaded["verdict"] == "DEGRADED"
    assert len(reloaded["findings"]) == len(findings)
    assert reloaded["run"]["run_id"] == run.run_id


def test_to_json_with_redaction_still_round_trips():
    run = _load(SILENT_RETRY_KILL)
    findings = _all_core_findings(run)
    payload = report.to_json(run, findings, Redactor(enabled=True))

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)
    assert reloaded == payload
    # Redaction must not leave a real absolute path in the output.
    assert "/Users/" not in dumped
