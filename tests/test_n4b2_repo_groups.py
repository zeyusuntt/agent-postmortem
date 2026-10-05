"""N4b-2 coverage group 11 (repository grouping, L14): `ledger.group_by_repo`
returns exact `RepoGroups` objects (docs/DESIGN.md 4.13.1;
.reviews/v0.3/N4-plan.md L14, rev 6 wording: a shell call, not a ledger entry,
adds an attempt to `unknown`)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agent_postmortem import models
from agent_postmortem.models import Attempt, Lifecycle, Run, ToolCall
from tests.n4b2_helpers import T0, ledger

CLEAN = {"journal": [], "parent": [], "transcripts": [], "meta": []}
SHELL = "shell_context_unverified"
OPAQUE = "possible_opaque_command"


def entry(effect="claimed_target", basis="command_grammar", path=None, repo=None):
    return models.LedgerEntry(effect, basis, "success", path, repo, "t.jsonl:2")


def poss(repo=None):
    return entry("possible", "opaque_command", None, repo)


def obs(path, repo=None):
    return entry("observed", "tool_result", path, repo)


def led(entries, reasons=()):
    return models.Ledger(list(entries), "partial" if reasons else "complete", list(reasons))


def member(name, ledger_, *, tools=(), outcome="killed"):
    calls = [ToolCall(f"t{i}", tool, i + 1, None) for i, tool in enumerate(tools)]
    return Attempt(
        agent_id=name, key="k", label=name, phase="p", agent_type=None, meta={},
        transcript_path=Path(f"agent-{name}.jsonl"), events=[], first_ts=T0 + timedelta(seconds=10),
        last_ts=None, ordinal=1, outcome=outcome, usage={}, journal_state="completed",
        tool_calls=calls, ledger=ledger_,
    )


def workflow(*attempts):
    lifecycle = Lifecycle(terminal="completed", source="notification", activity="stale",
                          generations=0, diagnostics=[])
    return Run(run_id="wf_00000000-d01", kind="workflow", path=Path("."), project_slug="s",
               session_id=None, claude_version=None, journal=[], agents=[],
               attempts=list(attempts), started_at=None, ended_at=None, diagnostics=[],
               lifecycle=lifecycle)


def group(paths=(), attempts=(), refused=False, reasons=()):
    return ledger.RepoGroup(list(paths), list(attempts), refused, list(reasons))


def groups(repos=None, unknown=None):
    return ledger.RepoGroups(repos or {}, unknown or group())


A0, A1, A2 = "attempts[0]", "attempts[1]", "attempts[2]"


# --- the fourteen cases of L14 ---------------------------------------------------------


def test_1_a_missing_transcript_is_only_unknown_and_refused():
    run = workflow(member("a", led([], ["transcript_missing"])))
    assert ledger.group_by_repo(run) == groups(
        unknown=group([], [A0], True, ["transcript_missing"]))


def test_2_an_opaque_command_with_no_cwd_is_unknown_and_refused():
    run = workflow(member("a", led([poss(None)], [SHELL, OPAQUE]), tools=["Bash"]))
    assert ledger.group_by_repo(run) == groups(
        unknown=group([], [A0], True, [SHELL, OPAQUE]))


def test_3_a_partial_ledger_with_one_repository_keeps_the_path_and_is_also_unknown():
    run = workflow(member(
        "a", led([entry(path="/r/a", repo="/r"), poss("/r")], [SHELL, OPAQUE]), tools=["Bash"]))
    assert ledger.group_by_repo(run) == groups(
        {"/r": group(["/r/a"], [A0], True, [SHELL, OPAQUE])},
        group([], [A0], True, [SHELL, OPAQUE]),
    )


def test_4_a_partial_ledger_spanning_two_repositories_retains_both_refused():
    run = workflow(member(
        "a", led([entry(path="/r/a", repo="/r"), entry(path="/s/b", repo="/s")], ["truncated_field"]),
    ))
    assert ledger.group_by_repo(run) == groups({
        "/r": group(["/r/a"], [A0], True, ["truncated_field"]),
        "/s": group(["/s/b"], [A0], True, ["truncated_field"]),
    })


def test_5_a_complete_ledger_with_a_null_repo_is_unknown_and_not_refused():
    run = workflow(member("a", led([obs("/x/a", None)])))
    assert ledger.group_by_repo(run) == groups(unknown=group(["/x/a"], [A0], False, []))


def test_6_two_attempts_sharing_a_repository_are_merged_in_total_order():
    run = workflow(
        member("a", led([obs("/r/a", "/r")])), member("b", led([obs("/r/b", "/r")])))
    assert ledger.group_by_repo(run) == groups(
        {"/r": group(["/r/a", "/r/b"], [A0, A1], False, [])})


def test_7_a_partial_and_a_complete_ledger_sharing_a_repository_refuse_it():
    run = workflow(
        member("a", led([obs("/r/a", "/r")])),
        member("b", led([obs("/r/b", "/r")], ["truncated_field"])),
    )
    assert ledger.group_by_repo(run) == groups(
        {"/r": group(["/r/a", "/r/b"], [A0, A1], True, ["truncated_field"])})


def test_7b_the_complete_one_last_still_leaves_the_group_refused():
    run = workflow(
        member("a", led([obs("/r/a", "/r")], ["truncated_field"])),
        member("b", led([obs("/r/b", "/r")])),
    )
    assert ledger.group_by_repo(run).repos["/r"].refused is True


def test_8_two_partial_contributors_give_the_union_of_reasons_in_fixed_order():
    run = workflow(
        member("a", led([obs("/r/a", "/r")], ["possible_delegation", "discovery_incomplete"])),
        member("b", led([obs("/r/b", "/r")], ["unexpected_entry_shape", "possible_delegation"])),
    )
    assert ledger.group_by_repo(run).repos["/r"].reasons == [
        "unexpected_entry_shape", "possible_delegation", "discovery_incomplete"]


def test_9_one_partial_attempt_with_a_known_and_a_null_repository_entry_is_in_both_groups():
    run = workflow(member(
        "a", led([obs("/r/a", "/r"), obs("/x/n", None)], ["truncated_field"])))
    assert ledger.group_by_repo(run) == groups(
        {"/r": group(["/r/a"], [A0], True, ["truncated_field"])},
        group(["/x/n"], [A0], True, ["truncated_field"]),
    )


def test_10_duplicate_paths_and_attempts_are_deduplicated():
    run = workflow(member(
        "a", led([obs("/r/a", "/r"), obs("/r/a", "/r"), obs("/r/b", "/r")])))
    assert ledger.group_by_repo(run) == groups(
        {"/r": group(["/r/a", "/r/b"], [A0], False, [])})


def test_11_repository_keys_arrive_sorted_whatever_the_order_of_the_entries():
    run = workflow(
        member("a", led([obs("/z/a", "/z"), obs("/b/a", "/b")])),
        member("b", led([obs("/m/a", "/m")])),
    )
    result = ledger.group_by_repo(run)
    assert list(result.repos) == ["/b", "/m", "/z"]


def test_12_a_session_main_thread_comes_before_a_child():
    main = member("main", led([obs("/r/m", "/r")]))
    child = member("child", led([obs("/r/c", "/r")]))
    session = Run(run_id="s_0000000000", kind="session", path=Path("."), project_slug="s",
                  session_id=None, claude_version=None, journal=[], main=main, agents=[],
                  attempts=[child], started_at=None, ended_at=None, diagnostics=[], finished=None)
    for member_ in (main, child):
        member_.interrupt = object()
    assert ledger.group_by_repo(session) == groups(
        {"/r": group(["/r/c", "/r/m"], ["main", A0], False, [])})


def test_13_a_file_tool_entry_plus_a_read_only_bash_call_is_in_the_repo_and_unknown_refused():
    run = workflow(member(
        "a", led([obs("/r/a", "/r")], [SHELL]), tools=["Write", "Bash"]))
    assert ledger.group_by_repo(run) == groups(
        {"/r": group(["/r/a"], [A0], True, [SHELL])},
        group([], [A0], True, [SHELL]),
    )


def test_14_the_same_with_a_read_only_monitor_call():
    run = workflow(member(
        "a", led([obs("/r/a", "/r")], [SHELL]), tools=["Write", "Monitor"]))
    assert ledger.group_by_repo(run) == groups(
        {"/r": group(["/r/a"], [A0], True, [SHELL])},
        group([], [A0], True, [SHELL]),
    )


# --- more rows pinning the rules -------------------------------------------------------


def test_a_shell_call_adds_to_unknown_whether_or_not_it_produced_an_entry():
    with_entry = member("a", led([poss("/r")], [SHELL, OPAQUE]), tools=["Bash"])
    without = member("b", led([obs("/r/b", "/r")], [SHELL]), tools=["Write", "Bash"])
    result = ledger.group_by_repo(workflow(with_entry, without))
    assert result.unknown.attempts == [A0, A1]
    assert result.repos["/r"].attempts == [A0, A1]


def test_an_attempt_with_no_shell_call_and_a_known_repo_is_not_unknown():
    run = workflow(member("a", led([obs("/r/a", "/r")], ["truncated_field"]), tools=["Write"]))
    assert ledger.group_by_repo(run).unknown == group()


def test_a_partial_ledger_with_no_entries_is_unknown_even_without_a_missing_transcript():
    run = workflow(member("a", led([], ["undecodable_line"])))
    assert ledger.group_by_repo(run).unknown == group([], [A0], True, ["undecodable_line"])


def test_a_complete_ledger_with_no_entries_contributes_nothing():
    run = workflow(member("a", led([])))
    assert ledger.group_by_repo(run) == groups()


def test_attempts_outside_the_risk_population_contribute_nothing():
    fine = member("a", led([obs("/r/a", "/r")], [SHELL]), tools=["Bash"], outcome="completed")
    run = workflow(fine)
    run.lifecycle._generations = [ledger_generation()]
    assert ledger.group_by_repo(run) == groups()


def ledger_generation():
    from agent_postmortem.models import Generation
    return Generation(
        launch_line=1, tool_use_id="toolu_1", resume_from=None, result_line=2,
        run_id="wf_00000000-d01", task_id="t", launch_status="async_launched",
        terminal="completed", terminal_source="notification", terminal_line=3, diagnostics=[],
        result_ts=T0 + timedelta(seconds=1), launch_ts=T0 + timedelta(seconds=1),
    )


def test_an_attempt_ref_is_the_index_in_run_attempts_even_for_a_non_canonical_id():
    first = member("a", led([obs("/r/a", "/r")]))
    odd = member("not a canonical id", led([obs("/r/b", "/r")]))
    odd.agent_id = ""
    run = workflow(first, odd)
    assert ledger.group_by_repo(run).repos["/r"].attempts == [A0, A1]


def test_a_possible_entry_names_its_repository_without_a_path():
    run = workflow(member("a", led([poss("/r")], [OPAQUE])))
    assert ledger.group_by_repo(run) == groups({"/r": group([], [A0], True, [OPAQUE])})


@pytest.mark.parametrize("effect", ["observed", "claimed_target", "attempted"])
def test_observed_claimed_and_attempted_paths_are_all_grouped(effect):
    run = workflow(member("a", led([entry(effect, "tool_result", "/r/p", "/r")])))
    assert ledger.group_by_repo(run).repos["/r"].paths == ["/r/p"]


def test_paths_are_sorted():
    run = workflow(member("a", led([obs("/r/z", "/r"), obs("/r/a", "/r"), obs("/r/m", "/r")])))
    assert ledger.group_by_repo(run).repos["/r"].paths == ["/r/a", "/r/m", "/r/z"]


def test_a_run_with_nobody_at_risk_groups_nothing():
    assert ledger.group_by_repo(workflow()) == groups()


def test_group_by_repo_runs_no_subprocess_and_touches_no_file(monkeypatch):
    import subprocess

    def refuse(*args, **kwargs):
        raise AssertionError("group_by_repo must be pure")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("builtins.open", refuse)
    run = workflow(member("a", led([obs("/r/a", "/r")], ["truncated_field"])))
    assert ledger.group_by_repo(run).repos["/r"].paths == ["/r/a"]


def test_an_attempt_with_no_ledger_is_treated_as_a_missing_transcript():
    run = workflow(member("a", None))
    assert ledger.group_by_repo(run) == groups(
        unknown=group([], [A0], True, ["transcript_missing"]))
