"""N4b-2 coverage group 8 (risk population, generation assignment, completeness):
docs/DESIGN.md 4.13.1 and 4.13.11; .reviews/v0.3/N4-plan.md L13.

Most rows build a Run directly, so each inclusion branch has a witness that is in
the population only through that branch; a few rows read real runs.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agent_postmortem import reader
from agent_postmortem.models import (
    Attempt,
    Event,
    Generation,
    Interrupt,
    Lifecycle,
    Run,
)
from tests.n4b2_helpers import (
    T0,
    auto_parent,
    ledger,
    result,
    session_run,
    stamp,
    text,
    use,
    workflow_run,
)
from tests.test_field_v03 import _aid

CLEAN = {"journal": [], "parent": [], "transcripts": [], "meta": []}


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def attempt(name="a", *, outcome="completed", journal_state="completed", first=10, events=(),
            interrupt=None):
    return Attempt(
        agent_id=name, key="k", label=name, phase="p", agent_type=None, meta={},
        transcript_path=Path(f"agent-{name}.jsonl"), events=list(events),
        first_ts=at(first) if first is not None else None, last_ts=None, ordinal=1,
        outcome=outcome, usage={}, interrupt=interrupt, journal_state=journal_state,
    )


def generation(n, result_seconds, terminal="completed", launch_line=None):
    return Generation(
        launch_line=launch_line if launch_line is not None else n * 10, tool_use_id=f"toolu_{n}",
        resume_from=None, result_line=n * 10 + 1, run_id="wf_00000000-d01", task_id="t",
        launch_status="async_launched", terminal=terminal, terminal_source="notification",
        terminal_line=n * 10 + 2, diagnostics=[],
        result_ts=at(result_seconds) if result_seconds is not None else None,
        launch_ts=at(result_seconds) if result_seconds is not None else None,
    )


def workflow(attempts, gens=(), discovery=None):
    lifecycle = Lifecycle(
        terminal="completed", source="notification", activity="stale",
        generations=len(gens), diagnostics=[],
    )
    lifecycle._generations = list(gens)
    return Run(
        run_id="wf_00000000-d01", kind="workflow", path=Path("."), project_slug="s",
        session_id=None, claude_version=None, journal=[], agents=[], attempts=list(attempts),
        started_at=None, ended_at=None, diagnostics=[], lifecycle=lifecycle,
        discovery=discovery or {key: [] for key in CLEAN},
    )


def session(main, children=(), discovery=None):
    return Run(
        run_id="s_0000000000", kind="session", path=Path("."), project_slug="s", session_id=None,
        claude_version=None, journal=[], main=main, agents=[], attempts=list(children),
        started_at=None, ended_at=None, diagnostics=[], finished=None,
        discovery=discovery or {key: [] for key in CLEAN},
    )


G1 = generation(1, 1)


def population(run):
    return [a.agent_id for a in ledger.risk_population(run)]


def ev(kind, text_="x"):
    return Event(path=Path("t.jsonl"), line_no=1, ts=None, kind=kind, raw_type=kind,
                 tool_name=None, text=text_, exit_code=None, duration_ms=None, is_error=False,
                 usage={})


def interrupt():
    return Interrupt(kill_ts=None, preceding=None, silence_seconds=None, cause="unattributed",
                     cause_evidence=None)


# --- inclusion by the attempt's own outcome (workflow) -----------------------------------


@pytest.mark.parametrize("outcome,journal_state", [
    ("killed", "superseded"), ("failed", "failed"), ("superseded", "superseded"),
    ("unknown", "completed"),
])
def test_an_outcome_that_is_killed_failed_superseded_or_unknown_is_at_risk(outcome, journal_state):
    run = workflow([attempt("w", outcome=outcome, journal_state=journal_state)], [G1])
    assert population(run) == ["w"]


def test_an_attempt_the_journal_left_open_is_at_risk_even_when_its_transcript_ended_normally():
    run = workflow([attempt("w", outcome="completed", journal_state="open")], [G1])
    assert population(run) == ["w"]


def test_a_completed_assignable_attempt_in_a_completed_generation_is_not_at_risk():
    run = workflow([attempt("w")], [G1])
    assert population(run) == []


def test_an_unkeyed_completed_attempt_in_a_completed_generation_is_at_risk():
    # S2 (N4 fix): its journal can never close it, so it is in the population like an open one
    run = workflow([attempt("w", journal_state=None)], [G1])
    assert population(run) == ["w"]


# --- inclusion through the newest generation ----------------------------------------------


@pytest.mark.parametrize("terminal", ["failed", "stopped", "unknown"])
def test_a_completed_attempt_in_a_newest_generation_that_failed_is_at_risk(terminal):
    run = workflow([attempt("w")], [generation(1, 1, terminal)])
    assert population(run) == ["w"]


@pytest.mark.parametrize("terminal", ["completed", None])
def test_a_newest_generation_that_completed_or_has_no_terminal_adds_nobody(terminal):
    run = workflow([attempt("w")], [generation(1, 1, terminal)])
    assert population(run) == []


def test_only_the_newest_generation_counts_not_an_older_failed_one():
    gens = [generation(1, 1, "failed"), generation(2, 100, "completed")]
    run = workflow([attempt("old", first=10), attempt("new", first=150)], gens)
    assert population(run) == []


def test_an_attempt_of_the_newest_generation_is_at_risk_when_only_it_failed():
    gens = [generation(1, 1, "completed"), generation(2, 100, "failed")]
    run = workflow([attempt("old", first=10), attempt("new", first=150)], gens)
    assert population(run) == ["new"]


def test_the_newest_generation_is_the_one_with_the_greatest_launch_line():
    gens = [generation(2, 100, "failed", launch_line=50), generation(1, 1, "completed", launch_line=5)]
    run = workflow([attempt("old", first=10), attempt("new", first=150)], gens)
    assert population(run) == ["new"]


# --- inclusion in a session ----------------------------------------------------------------


def test_a_session_subagent_with_an_interrupt_is_at_risk():
    run = session(attempt("main", journal_state=None), [
        attempt("hit", outcome="killed", journal_state=None, interrupt=interrupt()),
        attempt("fine", journal_state=None),
    ])
    assert population(run) == ["hit"]


def test_a_session_subagent_is_never_at_risk_by_outcome_alone():
    run = session(attempt("main", journal_state=None),
                  [attempt("odd", outcome="failed", journal_state=None)])
    assert population(run) == []


def test_a_main_thread_whose_last_meaningful_event_is_the_interrupt_is_at_risk():
    main = attempt("main", outcome="killed", journal_state=None,
                   events=[ev("user", "hello"), ev("user", "[Request interrupted by user]")],
                   interrupt=interrupt())
    assert population(session(main)) == ["main"]


@pytest.mark.parametrize("after", [
    [("thinking", ""), ("attachment", "note"), ("assistant", "   "), ("compaction", "")],
    [],
])
def test_events_that_are_not_meaningful_after_the_interrupt_leave_the_main_at_risk(after):
    events = [ev("user", "hello"), ev("user", "[Request interrupted by user]")]
    events += [ev(kind, body) for kind, body in after]
    main = attempt("main", outcome="killed", journal_state=None, events=events,
                   interrupt=interrupt())
    assert population(session(main)) == ["main"]


@pytest.mark.parametrize("kind", ["thinking", "attachment"])
def test_a_thinking_or_attachment_event_with_text_is_still_not_activity(kind):
    events = [ev("user", "hello"), ev("user", "[Request interrupted by user]"), ev(kind, "some text")]
    main = attempt("main", outcome="killed", journal_state=None, events=events,
                   interrupt=interrupt())
    assert population(session(main)) == ["main"]


@pytest.mark.parametrize("kind", ["assistant", "tool_use", "tool_result", "unknown"])
def test_a_meaningful_event_after_the_interrupt_clears_the_main_thread(kind):
    events = [ev("user", "hello"), ev("user", "[Request interrupted by user]"), ev(kind, "go on")]
    main = attempt("main", outcome="killed", journal_state=None, events=events,
                   interrupt=interrupt())
    assert population(session(main)) == []


def test_a_main_thread_without_an_interrupt_is_not_at_risk():
    assert population(session(attempt("main", journal_state=None))) == []


def test_the_main_thread_comes_first_in_the_population():
    main = attempt("main", outcome="killed", journal_state=None,
                   events=[ev("user", "[Request interrupted by user]")], interrupt=interrupt())
    child = attempt("hit", outcome="killed", journal_state=None, interrupt=interrupt())
    assert population(session(main, [child])) == ["main", "hit"]


# --- generation assignment ----------------------------------------------------------------

GENS = [generation(1, 100), generation(2, 200)]


def assigned(first, gens=GENS, **kwargs):
    run = workflow([attempt("w", first=first, **kwargs)], gens)
    found = ledger.generation_of(run, run.attempts[0])
    return None if found is None else found.tool_use_id


@pytest.mark.parametrize("first,expected", [
    (100, "toolu_1"),     # exactly equal to the first result: that generation
    (99, None),           # before the first launch result
    (150, "toolu_1"),     # between two: the earlier one
    (199, "toolu_1"),
    (200, "toolu_2"),     # exactly equal to the second
    (900, "toolu_2"),
    (None, None),         # no first_ts
])
def test_an_attempt_belongs_to_the_latest_result_at_or_before_its_first_ts(first, expected):
    assert assigned(first) == expected


def test_a_run_with_no_generation_places_nobody():
    assert assigned(150, gens=[]) is None


def test_a_run_with_no_lifecycle_places_nobody():
    run = workflow([attempt("w", first=150)], GENS)
    run.lifecycle = None
    assert ledger.generation_of(run, run.attempts[0]) is None


@pytest.mark.parametrize("gens", [
    [generation(1, 100), generation(2, None)],                 # a missing timestamp
    [generation(1, None), generation(2, 200)],
    [generation(1, 100), generation(2, 100)],                   # an equal timestamp
    [generation(1, 200), generation(2, 100)],                   # a decreasing timestamp
])
def test_one_unusable_generation_timestamp_makes_every_attempt_unassignable(gens):
    run = workflow([attempt("w", first=500)], gens)
    assert ledger.generation_of(run, run.attempts[0]) is None


def test_launch_line_order_decides_whether_the_timestamps_increase():
    gens = [generation(2, 200, launch_line=50), generation(1, 100, launch_line=5)]
    run = workflow([attempt("w", first=150)], gens)
    found = ledger.generation_of(run, run.attempts[0])
    assert found is not None and found.tool_use_id == "toolu_1"


# --- unassignable attempts stay in the population -----------------------------------------


def test_a_completed_attempt_in_a_run_with_no_generations_is_in_the_population():
    run = workflow([attempt("w")], [])
    assert population(run) == ["w"]
    assert ledger.risk_population_completeness(run) == ("partial", ["unassignable_attempt"])


def test_a_completed_attempt_with_no_first_ts_is_in_the_population():
    run = workflow([attempt("w", first=None)], [G1])
    assert population(run) == ["w"]
    assert ledger.risk_population_completeness(run) == ("partial", ["unassignable_attempt"])


def test_an_attempt_before_the_first_launch_result_is_in_the_population():
    run = workflow([attempt("w", first=0)], [G1])
    assert population(run) == ["w"]


# --- completeness -------------------------------------------------------------------------


def test_an_empty_population_with_clean_discovery_is_complete():
    run = workflow([attempt("w")], [G1])
    assert ledger.risk_population_completeness(run) == ("complete", [])


def test_a_population_whose_members_are_all_placed_is_complete_too():
    run = workflow([attempt("w", outcome="killed")], [G1])
    assert population(run) == ["w"]
    assert ledger.risk_population_completeness(run) == ("complete", [])


@pytest.mark.parametrize("source", ["journal", "parent", "transcripts", "meta"])
def test_each_discovery_source_alone_makes_an_empty_population_partial(source):
    discovery = {**CLEAN, source: ["a diagnostic"]}
    run = workflow([attempt("w")], [G1], discovery)
    assert population(run) == []
    assert ledger.risk_population_completeness(run) == ("partial", ["discovery_incomplete"])


@pytest.mark.parametrize("source", ["journal", "parent", "transcripts", "meta"])
def test_each_discovery_source_alone_makes_a_non_empty_population_partial(source):
    discovery = {**CLEAN, source: ["a diagnostic"]}
    run = workflow([attempt("w", outcome="failed")], [G1], discovery)
    assert population(run) == ["w"]
    assert ledger.risk_population_completeness(run) == ("partial", ["discovery_incomplete"])


def test_both_reasons_are_listed_unassignable_first():
    run = workflow([attempt("w", first=None)], [G1], {**CLEAN, "meta": ["x"]})
    assert ledger.risk_population_completeness(run) == (
        "partial", ["unassignable_attempt", "discovery_incomplete"])


def test_a_session_with_clean_discovery_and_nobody_at_risk_is_complete():
    assert ledger.risk_population_completeness(session(attempt("main", journal_state=None))) == (
        "complete", [])


def test_a_session_with_a_discovery_diagnostic_is_partial():
    run = session(attempt("main", journal_state=None), discovery={**CLEAN, "transcripts": ["x"]})
    assert ledger.risk_population_completeness(run) == ("partial", ["discovery_incomplete"])


def test_a_session_attempt_is_never_unassignable():
    run = session(attempt("main", journal_state=None, first=None))
    assert ledger.risk_population_completeness(run) == ("complete", [])


# --- the real reader ------------------------------------------------------------------------

CLEAN_TRANSCRIPT = [use("t1", "Write", {"file_path": "/tmp/ws/a.py"}), result("t1"), text()]


def test_a_real_completed_run_with_one_generation_is_complete_with_an_empty_population(tmp_path):
    run = workflow_run(tmp_path, CLEAN_TRANSCRIPT)
    assert run.risk_population_completeness == "complete"
    assert run.risk_population_completeness_reasons == []
    assert ledger.risk_population(run) == []


def test_a_real_run_with_no_parent_transcript_puts_every_attempt_in_the_population(tmp_path):
    run = workflow_run(tmp_path, CLEAN_TRANSCRIPT, parent=None)
    assert run.risk_population_completeness == "partial"
    assert run.risk_population_completeness_reasons == ["unassignable_attempt"]
    assert ledger.risk_population(run) == [run.attempts[0]]
    assert run.attempts[0].ledger.reasons == ["unassignable_attempt"]


def test_a_real_open_attempt_is_at_risk(tmp_path):
    run = workflow_run(tmp_path, CLEAN_TRANSCRIPT, journal="open")
    assert ledger.risk_population(run) == [run.attempts[0]]
    assert run.risk_population_completeness == "complete"


def test_a_real_missing_transcript_is_at_risk_by_its_unknown_outcome(tmp_path):
    run = workflow_run(tmp_path, None, journal="open")
    assert run.attempts[0].outcome == "unknown"
    assert ledger.risk_population(run) == [run.attempts[0]]


def test_a_real_parent_diagnostic_makes_the_run_partial(tmp_path):
    run = workflow_run(tmp_path, CLEAN_TRANSCRIPT, parent=["not json"] + auto_parent())
    assert run.discovery["parent"] != []
    assert run.risk_population_completeness == "partial"
    assert run.risk_population_completeness_reasons == ["discovery_incomplete"]
    assert run.attempts[0].ledger.reasons == ["discovery_incomplete"]


def test_a_real_journal_diagnostic_makes_the_run_partial(tmp_path):
    run = workflow_run(tmp_path, CLEAN_TRANSCRIPT)
    journal = run.path / "journal.jsonl"
    journal.write_text(journal.read_text() + "not json\n", encoding="utf-8")
    run = reader.load_run(run.path)
    assert run.discovery["journal"] != []
    assert run.risk_population_completeness_reasons == ["discovery_incomplete"]


def test_a_real_meta_diagnostic_makes_the_run_partial(tmp_path):
    run = workflow_run(tmp_path, CLEAN_TRANSCRIPT)
    (run.path / f"agent-{_aid(1)}.meta.json").write_text("[1]", encoding="utf-8")
    run = reader.load_run(run.path)
    assert run.discovery["meta"] != []
    assert run.risk_population_completeness_reasons == ["discovery_incomplete"]


def test_a_real_transcript_diagnostic_makes_the_run_partial(tmp_path):
    entries = stamp(CLEAN_TRANSCRIPT) + ["{bad"]
    run = workflow_run(tmp_path, entries, stamped=False)
    assert run.discovery["transcripts"] != []
    assert run.risk_population_completeness_reasons == ["discovery_incomplete"]


def test_a_real_session_with_a_named_but_missing_child_is_partial_with_an_empty_population(tmp_path):
    main = [
        text(who="user", body="go"),
        use("m1", "Agent", {"prompt": "x"}),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "m1", "content": "done"}]},
         "toolUseResult": {"agentId": _aid(5)}},
    ]
    run = session_run(tmp_path, main)
    assert run.discovery["transcripts"] != []
    assert ledger.risk_population(run) == []
    assert run.risk_population_completeness == "partial"
    assert run.risk_population_completeness_reasons == ["discovery_incomplete"]


def test_a_real_session_with_an_interrupted_subagent_has_it_in_the_population(tmp_path):
    child = _aid(7)
    kill = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "[Request interrupted by user]"}]}}
    run = session_run(
        tmp_path, [text(who="user", body="go")],
        {child: [text(who="user", body="work"), kill]},
    )
    assert [a.agent_id for a in ledger.risk_population(run)] == [child]
    assert run.risk_population_completeness == "complete"


def test_a_real_session_main_thread_killed_with_nothing_after_is_in_the_population(tmp_path):
    kill = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "[Request interrupted by user]"}]}}
    run = session_run(tmp_path, [text(who="user", body="go"), text(), kill])
    assert ledger.risk_population(run) == [run.main]
