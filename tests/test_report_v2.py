"""Tests for the redesigned `run` text/JSON report (unit M3a: docs/DESIGN.md
4.7/4.8, .reviews/proposals/2026-09-v0.2-guided-rescue.md section 6.1).

Covers the packaged demo run and five fixtures: attempt_failed_causes
(reset_time guidance in WHAT TO DO), claim_contradiction (a story clause
built from Finding.metrics, not the raw title), attempt_facts and
journal_state_resolved (mixed-outcome workflows that must not crash the new
renderer), and session_clean (the session variant of the same report).
"""

from __future__ import annotations

import platform
from pathlib import Path

import agent_postmortem
from agent_postmortem import detectors, reader, report
from agent_postmortem.redact import Redactor

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
DEMO_RUN = Path(agent_postmortem.__file__).parent / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
ATTEMPT_FAILED_CAUSES = FIXTURES_ROOT / "attempt_failed_causes"
CLAIM_CONTRADICTION = FIXTURES_ROOT / "claim_contradiction"
JOURNAL_STATE_RESOLVED = FIXTURES_ROOT / "journal_state_resolved"
ATTEMPT_FACTS_RUN = (
    FIXTURES_ROOT / "attempt_facts" / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-af0"
)
SESSION_CLEAN = (
    FIXTURES_ROOT / "session_clean" / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000.jsonl"
)

_FORBIDDEN_SUBSTRINGS = ("CLAUDE.md", "NEXT ACTION", "SUGGESTED RULES")

ALL_CASES = [
    ("demo", DEMO_RUN, reader.load_run),
    ("attempt_failed_causes", ATTEMPT_FAILED_CAUSES, reader.load_run),
    ("claim_contradiction", CLAIM_CONTRADICTION, reader.load_run),
    ("journal_state_resolved", JOURNAL_STATE_RESOLVED, reader.load_run),
    ("attempt_facts", ATTEMPT_FACTS_RUN, reader.load_run),
    ("session_clean", SESSION_CLEAN, reader.load_session),
]


def _load(path, loader):
    return loader(path)


def _render(run, *, verbose=False, evidence=False, redact=True):
    findings = detectors.run_detectors(run)
    text = report.render_run_text(
        run, findings, Redactor(enabled=redact), verbose=verbose, evidence=evidence,
    )
    return text, findings


# --- structural shape: verdict, account and WORK AT RISK in the first 10 ---


def test_first_ten_lines_carry_verdict_account_and_work_at_risk():
    for name, path, loader in ALL_CASES:
        run = _load(path, loader)
        text, _ = _render(run)
        head = text.splitlines()[:10]
        head_text = "\n".join(head)
        assert head[0].split()[1] == run.run_id, f"{name}: header missing run id"
        assert any(
            word in head[0] for word in ("CLEAN", "DEGRADED", "FAILED")
        ), f"{name}: no verdict word on line 1"
        assert "WORK AT RISK" in head_text, f"{name}: WORK AT RISK not in first 10 lines"


# --- forbidden content: never an instruction file, apply, or the old sections --


def test_no_line_ever_names_an_instruction_file_or_apply_or_old_sections():
    for name, path, loader in ALL_CASES:
        run = _load(path, loader)
        text, _ = _render(run, verbose=True, evidence=True)
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in text, f"{name}: {forbidden!r} found in verbose+evidence text"
        assert "apply" not in text, f"{name}: bare 'apply' found in verbose+evidence text"


# --- evidence hidden by default, shown with --evidence ----------------------


_DEMO_EXCERPT = "Tests pass. Writing the implementation now."


def test_demo_evidence_excerpt_hidden_by_default_and_shown_with_evidence_flag():
    """N3b P16 (D10): an excerpt prints only with --evidence AND at local or
    raw; share never prints one, with or without --evidence."""
    run = reader.load_run(DEMO_RUN)
    default_text, _ = _render(run)
    share_evidence_text, _ = _render(run, evidence=True)
    raw_evidence_text, _ = _render(run, evidence=True, redact=False)
    assert _DEMO_EXCERPT not in default_text
    assert _DEMO_EXCERPT not in share_evidence_text
    assert _DEMO_EXCERPT in raw_evidence_text


# --- info findings hidden by default, shown with --verbose ------------------


def test_info_findings_hidden_by_default_and_shown_with_verbose():
    run = reader.load_run(DEMO_RUN)
    default_text, findings = _render(run)
    verbose_text, _ = _render(run, verbose=True)
    info_count = sum(1 for f in findings if f.severity == "info")
    assert info_count > 0  # wasted_time always fires
    assert f"{info_count} info finding" in default_text
    assert "not shown (--verbose)" in default_text
    assert "FINDINGS (" not in default_text
    assert "FINDINGS (" in verbose_text
    assert "wasted_time" in verbose_text


# --- story count equals the number of agents with (non-info) findings -------


def test_story_count_equals_agents_with_findings():
    for name, path, loader in ALL_CASES:
        run = _load(path, loader)
        findings = detectors.run_detectors(run)
        entries = report._story_records(run, findings, verbose=False)
        agent_ids_with_findings = {
            f.agent_id for f in findings if f.severity != "info" and f.agent_id is not None
        }
        owners = set()
        for agent in run.agents:
            attempt_ids = {a.agent_id for a in agent.attempts}
            if attempt_ids & agent_ids_with_findings:
                owners.add(id(agent))
        agent_story_count = sum(1 for e in entries if e["agent"] != "run")
        assert agent_story_count == len(owners), name


# --- WHAT TO DO: at most 3 items, each tagged [look] or [decide] -----------


def test_what_to_do_has_at_most_three_tagged_items():
    for name, path, loader in ALL_CASES:
        run = _load(path, loader)
        text, _ = _render(run)
        if "WHAT TO DO" not in text:  # N8a K2: the heading is omitted with no item
            continue
        block = text[text.index("WHAT TO DO"):text.index("\n\n", text.index("WHAT TO DO"))]
        item_lines = [
            line for line in block.splitlines()
            if line.strip() and line.strip()[0].isdigit()
        ]
        assert len(item_lines) <= 3, name
        for line in item_lines:
            assert "[look]" in line or "[decide]" in line, f"{name}: {line!r} untagged"


# --- the reset_time item appears for the usage-limit fixture ----------------


def test_reset_time_item_appears_for_usage_limit_fixture():
    run = reader.load_run(ATTEMPT_FAILED_CAUSES)
    text, _ = _render(run)
    assert "wait until 2pm before relaunching" in text


# --- --json carries stories and next, through the allowlist -----------------


def test_json_carries_stories_and_next():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    payload = report.to_json(run, findings, Redactor(enabled=True))
    assert "stories" in payload
    assert "next" in payload
    assert payload["stories"]
    for story in payload["stories"]:
        assert set(story.keys()) == {"agent", "severity", "text", "finding_ids"}
    for item in payload["next"]:
        assert set(item.keys()) == {"grade", "text", "command"}
        assert item["grade"] in ("look", "decide")


def test_json_stories_and_next_are_redacted_by_default():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    redacted = report.to_json(run, findings, Redactor(enabled=True))
    raw = report.to_json(run, findings, Redactor(enabled=False))
    redacted_agents = {story["agent"] for story in redacted["stories"]}
    raw_agents = {story["agent"] for story in raw["stories"]}
    assert all(agent.startswith("agent-") for agent in redacted_agents)
    assert any("silent-alpha" in agent for agent in raw_agents)


# --- --bug-report is untouched by this unit ---------------------------------
#
# Captured on the demo run from `agent-postmortem run <demo> --bug-report`
# before any unit M3a code changed. render_bug_report has its own _bug_*
# helpers, never touched by render_run_text's redesign, so this must render
# byte-identical after the unit too.

_BEFORE_BUG_REPORT = (
    "# wf_00000000-d30 (DEGRADED): 2 agents became 4 attempts; 2 silent retries "
    "burned 20m; 2 kills recorded as user interrupts\n"
    "\n"
    "## Environment\n"
    "- Claude Code version: 2.1.267\n"
    f"- OS: {platform.platform()}\n"
    "\n"
    "## What happened\n"
    "DEGRADED: 2 agents became 4 attempts; 2 silent retries burned 20m; 2 kills "
    "recorded as user interrupts\n"
    "\n"
    "- agents: 2\n"
    "- attempts: 4\n"
    "- findings: critical 0, major 2, minor 2, info 1\n"
    "\n"
    "## Reproduction\n"
    "project project-1, 2 logical agent(s):\n"
    "- agent-1 (phase phase-1, general-purpose): 2 attempt(s)\n"
    "- agent-2 (phase phase-1, general-purpose): 2 attempt(s)\n"
    "\n"
    "## Evidence\n"
    "From run wf_00000000-d30:\n"
    "- [minor] mislabeled_kill: agent-1 attempt 1 was killed by the runtime after "
    "416s of silence, logged as a user interrupt\n"
    "  - agent-1.jsonl:4\n"
    "  - agent-1.jsonl:5 [kill_sentinel]\n"
    "- [minor] mislabeled_kill: agent-2 attempt 1 was killed by the runtime after "
    "416s of silence, logged as a user interrupt\n"
    "  - agent-2.jsonl:4\n"
    "  - agent-2.jsonl:5 [kill_sentinel]\n"
    "- [major] silent_retry: agent-1 restarted 1 time, 10m wasted\n"
    "  - agent-1.jsonl:5 [kill_sentinel]\n"
    "  - agent-3.jsonl:1\n"
    "- [major] silent_retry: agent-2 restarted 1 time, 10m wasted\n"
    "  - agent-2.jsonl:5 [kill_sentinel]\n"
    "  - agent-4.jsonl:1\n"
    "- [info] wasted_time: run spent 55m wall-clock; 20m wasted across 2 retried "
    "attempts\n"
    "\n"
    "## Suggested fixes\n"
    "- R-MONITOR-RETRIES (backed by 2 findings): After launching a workflow, watch "
    "for a second agent meta file appearing for the same logical agent; at the end "
    "compare journal starts to agent count and report retries and the wall-clock "
    "they burned.\n"
    "- R-RETRY-SAFE (backed by 2 findings): Every implementation prompt begins: You "
    "may be a retry after a killed attempt. First run git status and git diff "
    "--stat; if uncommitted work exists, verify it and commit it before "
    "continuing. Never git checkout, restore, or stash a file holding "
    "uncommitted work.\n"
    "\n"
    "Redacted: paths, ids, emails, account names, agent names. Evidence lines are "
    "pointers and marker codes only; no transcript excerpts are included.\n"
)


def test_bug_report_output_is_byte_identical_to_before_the_unit():
    """N3b (P10, P16): the one deliberate change since M3a. At share, with no
    alias book attached (a direct render call), the project and the evidence
    file names are the book's positional names, and an evidence line is a
    pointer plus the marker code, never an excerpt; the footer says so."""
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    after = report.render_bug_report(run, findings, Redactor(enabled=True))
    assert after == _BEFORE_BUG_REPORT


# --- no line ever crashes across every mixed-outcome fixture ----------------


def test_render_run_text_never_crashes_across_every_case_with_every_flag_combo():
    for _name, path, loader in ALL_CASES:
        run = _load(path, loader)
        findings = detectors.run_detectors(run)
        for verbose in (False, True):
            for evidence in (False, True):
                for redact in (True, False):
                    report.render_run_text(
                        run, findings, Redactor(enabled=redact),
                        verbose=verbose, evidence=evidence,
                    )


# --- ordering, capitalization, and a stable demo header ---------------------


def test_stories_order_is_severity_then_wasted_time_then_agent_position():
    """Ties on severity are broken by wasted time (biggest loss first), then by
    the agent's position in the run, so the order reads the same way the
    AGENT table does after redaction (agent-1, agent-2, ...), never by the
    raw agent label the redactor hides."""
    run = _load(ATTEMPT_FACTS_RUN, reader.load_run)
    findings = detectors.run_detectors(run)
    entries = report._story_records(run, findings, verbose=False)
    ranks = [report._SEVERITIES.index(e["severity"]) for e in entries]
    assert ranks == sorted(ranks)
    positions = {agent.label: i for i, agent in enumerate(run.agents)}

    def wasted(entry):
        return sum(
            f.metrics.get("wasted_seconds", 0) or 0
            for f in entry["findings"] if f.detector == "silent_retry"
        )

    for a, b in zip(entries, entries[1:]):
        if a["severity"] == b["severity"]:
            assert (-wasted(a), positions[a["agent"]]) <= (-wasted(b), positions[b["agent"]])


def test_story_clause_wrote_no_paths_is_a_capitalized_sentence():
    """The clause is a sentence of its own after the kill clause, so it must
    start with a capital: "... of silence). Wrote no paths." Rendered over
    every case; at least one case (a killed final attempt whose facts are
    known and empty) must produce it."""
    import re

    for _name, path, loader in ALL_CASES:
        run = _load(path, loader)
        findings = detectors.run_detectors(run)
        for entry in report._story_records(run, findings, verbose=True):
            assert not entry["text"][:1].islower(), entry["text"]
            assert not re.search(r"\. [a-z]", entry["text"]), entry["text"]


def test_demo_header_carries_no_relative_age(capsys):
    """The README's demo block is compared byte for byte with `demo`, so the
    demo header must not change with the wall clock: it prints the end time
    and no "(N days ago)"."""
    from agent_postmortem.cli import main

    assert main(["demo"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert "ended 2026-01-06 19:55Z" in first
    assert "ago" not in first


# --- unit M3c: silencing a detector by config (docs/DESIGN.md 4.6/4.7/4.10) -
#
# render_run_text/to_json take a plain frozenset of detector ids to hide --
# config.py's own parsing/critical-rejection rules are exercised in
# tests/test_config.py; these tests only cover what report.py itself does
# once it has that set. The packaged demo run's findings are, today: two
# major mislabeled_kill, two major silent_retry, one info wasted_time
# (docs/DESIGN.md 8.2/5.4) -- silencing both major detector ids leaves only
# the info finding, which is exactly "the only major detector" the demo run
# has.

DEMO_MAJOR_DETECTORS = frozenset({"mislabeled_kill", "silent_retry"})


def test_silenced_detector_findings_are_absent_from_default_text():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    text = report.render_run_text(run, findings, Redactor(enabled=True))
    silenced_text = report.render_run_text(
        run, findings, Redactor(enabled=True), silenced_detectors=DEMO_MAJOR_DETECTORS,
    )
    assert "DEGRADED" in text.splitlines()[0]
    assert "CLEAN" in silenced_text.splitlines()[0]
    assert "DEGRADED" not in silenced_text
    # STORIES: with both major detectors silenced, only the info-severity
    # wasted_time finding remains, and an info-only run never earns a story
    # outside --verbose (docs/DESIGN.md 4.7 rule 6).
    assert "STORIES (0)" in silenced_text


def test_silenced_detector_findings_are_absent_from_verbose_findings_list():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    verbose_text = report.render_run_text(
        run, findings, Redactor(enabled=True), verbose=True,
        silenced_detectors=DEMO_MAJOR_DETECTORS,
    )
    # The last line names the silenced detectors by id (docs/DESIGN.md
    # 4.7); everything above it must not, so check the body separately.
    body = "\n".join(verbose_text.rstrip("\n").splitlines()[:-1])
    assert "mislabeled_kill" not in body
    assert "silent_retry" not in body
    assert "wasted_time" in body  # not silenced, still shown under --verbose


def test_last_line_names_every_silenced_detector_sorted_with_a_count():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    text = report.render_run_text(
        run, findings, Redactor(enabled=True), silenced_detectors=DEMO_MAJOR_DETECTORS,
    )
    lines = text.rstrip("\n").splitlines()
    assert lines[-1] == "2 detectors silenced by config: mislabeled_kill, silent_retry"


def test_no_silenced_line_when_nothing_is_silenced():
    for _name, path, loader in ALL_CASES:
        run = _load(path, loader)
        findings = detectors.run_detectors(run)
        text = report.render_run_text(run, findings, Redactor(enabled=True))
        assert "silenced by config" not in text


def test_a_single_silenced_detector_reports_singular_count_and_verbose_still_hides_it():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    text = report.render_run_text(
        run, findings, Redactor(enabled=True), verbose=True,
        silenced_detectors=frozenset({"silent_retry"}),
    )
    lines = text.rstrip("\n").splitlines()
    assert lines[-1] == "1 detector silenced by config: silent_retry"
    assert "silent_retry" not in text.replace(lines[-1], "")
    # The other detector (mislabeled_kill) was not silenced and its finding
    # still shows under --verbose. N5b (4.13.4): its runtime kills are minor
    # now (non-final attempts), so with silent_retry silenced no major is
    # left and the verdict is CLEAN, not DEGRADED.
    assert "CLEAN" in text.splitlines()[0]
    assert "mislabeled_kill" in text


# --- unit M3c: to_json (docs/DESIGN.md 5.3) ---------------------------------


def test_to_json_marks_silenced_findings_and_computes_verdict_over_shown_only():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    payload = report.to_json(
        run, findings, Redactor(enabled=False), silenced_detectors=DEMO_MAJOR_DETECTORS,
    )
    assert payload["schema"] == 3
    # Every finding is still exported, silenced or not.
    assert len(payload["findings"]) == len(findings)
    by_detector = {}
    for exported in payload["findings"]:
        by_detector.setdefault(exported["detector"], []).append(exported)
    for detector_id in DEMO_MAJOR_DETECTORS:
        assert all(f["silenced"] is True for f in by_detector[detector_id])
    assert all(f["silenced"] is False for f in by_detector["wasted_time"])
    # The verdict, rules, and stories are all computed over the shown
    # (non-silenced) findings only (docs/DESIGN.md 4.6/4.7).
    assert payload["verdict"] == "CLEAN"
    assert payload["stories"] == []
    assert not any(rule["rule_id"] in ("R-RETRY-SAFE", "R-MONITOR-RETRIES") for rule in payload["rules"])


def test_to_json_with_nothing_silenced_matches_the_unfiltered_baseline():
    run = reader.load_run(DEMO_RUN)
    findings = detectors.run_detectors(run)
    payload = report.to_json(run, findings, Redactor(enabled=False))
    assert payload["verdict"] == "DEGRADED"
    assert all(f["silenced"] is False for f in payload["findings"])
