"""N5c-1: tests added after the mutation run, one per behavior a mutant of the
N5c-1 code survived (Attempt.returned_to_parent, the observation groups, the
WORK AT RISK block, the rescue rows). Public-rendering assertions live in this
tests/test_n5*.py file (the D17 label)."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from datetime import timedelta

from agent_postmortem import cli, observe, reader, report
from agent_postmortem.redact import Redactor
from tests.n5b_helpers import ledger_of, model_attempt, model_workflow_run
from tests.n5c1_helpers import (
    BASE,
    Att,
    Step,
    bash_step,
    build_run,
    init_repo,
    session_with_children,
    write_step,
)
from tests.test_field_v03 import _aid, _assistant_tool, _kill, _tool_result
from tests.test_n4b1_discovery import _agent_result, _launch, _session

X = _aid(4)


def _load(tmp_path, monkeypatch, suffix, atts, **kwargs):
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", atts, **kwargs)
    return run_dir, reader.load_run(run_dir)


def _block(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("WORK AT RISK"))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip():
            break
        block.append(line)
    return block


def _section(out: str, title: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip():
            break
        block.append(line)
    return block


def _run_text(capsys, target, *flags):
    cli.main(["run", str(target), *flags])
    return capsys.readouterr().out


# --- returned_to_parent ------------------------------------------------------------------------------


def test_a_workflow_launch_naming_a_child_does_not_return_it(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1", "Workflow"), _agent_result("toolu_1", entry=X)],
        children=[X],
    )
    run = reader.load_session(main)
    assert [a.returned_to_parent for a in run.attempts] == [False]


def test_a_launch_with_an_unhashable_id_never_crashes_and_the_next_launch_still_counts(tmp_path):
    main = _session(
        tmp_path,
        [_launch(["x"]), _launch("toolu_2"), _agent_result("toolu_2", entry=X)],
        children=[X],
    )
    run = reader.load_session(main)
    assert [a.returned_to_parent for a in run.attempts] == [True]


# --- observation: a fake git for the guard rows no real git produces ----------------------------------


def _fake_git(monkeypatch, overrides):
    """Runs the real git, except where `overrides(argv)` returns a CompletedProcess."""
    real = subprocess.run

    def fake(argv, **kwargs):
        replaced = overrides(list(argv))
        return replaced if replaced is not None else real(argv, **kwargs)

    monkeypatch.setattr(observe.subprocess, "run", fake)


def _one_group_run(tmp_path, monkeypatch, suffix):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    _dir, run = _load(tmp_path, monkeypatch, suffix, [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    return repo, run


def test_git_2_36_0_exactly_is_accepted(tmp_path, monkeypatch):
    _repo, run = _one_group_run(tmp_path, monkeypatch, "a91")
    _fake_git(monkeypatch, lambda argv: subprocess.CompletedProcess(
        argv, 0, stdout="git version 2.36.0\n", stderr="") if argv[1:] == ["--version"] else None)
    (group,) = observe.observe_groups(run).groups
    assert group.checked is True and group.git_version == "git version 2.36.0"


def test_a_filter_probe_that_fails_to_answer_is_a_refused_guard_and_status_never_runs(
    tmp_path, monkeypatch,
):
    _repo, run = _one_group_run(tmp_path, monkeypatch, "a92")
    seen = []

    def overrides(argv):
        seen.append(argv)
        if "config" in argv:
            return subprocess.CompletedProcess(argv, 129, stdout="", stderr="bad")
        return None

    _fake_git(monkeypatch, overrides)
    (group,) = observe.observe_groups(run).groups
    assert (group.checked, group.reason) == (False, "guard refused")
    assert not any("status" in argv for argv in seen)


def test_a_status_that_fails_is_a_refused_guard_with_no_path_states(tmp_path, monkeypatch):
    _repo, run = _one_group_run(tmp_path, monkeypatch, "a93")
    _fake_git(monkeypatch, lambda argv: subprocess.CompletedProcess(
        argv, 128, stdout="", stderr="fatal") if "status" in argv else None)
    (group,) = observe.observe_groups(run).groups
    assert (group.checked, group.reason, group.paths) == (False, "guard refused", {})
    assert len(group.unobserved_paths) == 1


def test_each_path_lists_only_the_attempts_that_recorded_it(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo", {"a.txt": "a\n", "b.txt": "b\n"})
    _dir, run = _load(tmp_path, monkeypatch, "a94", [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "a.txt", repo)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", repo / "b.txt", repo)]),
    ])
    (group,) = observe.observe_groups(run).groups
    by_path = {path.rsplit("/", 1)[-1]: obs for path, obs in group.paths.items()}
    assert [a["ordinal"] for a in by_path["a.txt"].attempts] == [1]
    assert [a["agent_id"] for a in by_path["a.txt"].attempts] == [_aid(1)]
    assert [a["agent_id"] for a in by_path["b.txt"].attempts] == [_aid(2)]
    assert by_path["a.txt"].later_write_observed is False


def test_an_attempted_write_is_recorded_against_its_attempt(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    failed = Step("t1", "Write", {"file_path": str(repo / "tracked.txt")}, "denied", True, str(repo))
    _dir, run = _load(tmp_path, monkeypatch, "a95", [Att(1, steps=[failed])])
    (group,) = observe.observe_groups(run).groups
    (obs,) = group.paths.values()
    assert [a["ordinal"] for a in obs.attempts] == [1]


def test_the_session_main_thread_is_named_main_in_a_path_observation(tmp_path):
    repo = init_repo(tmp_path / "repo")
    entries = [
        {**_assistant_tool("m1", "Write", {"file_path": str(repo / "tracked.txt")},
                           BASE + timedelta(seconds=5)), "cwd": str(repo)},
        {**_tool_result("m1", "ok", BASE + timedelta(seconds=6)), "cwd": str(repo)},
        _kill(BASE + timedelta(seconds=7)),
    ]
    main, _ids = session_with_children(tmp_path, [], main_entries=entries)
    run = reader.load_session(main)
    (group,) = observe.observe_groups(run).groups
    (obs,) = group.paths.values()
    assert obs.attempts == [{"agent_id": "main", "ordinal": 1}]


def test_the_run_root_is_the_first_checked_group_and_any_checked_group_counts(
    tmp_path, monkeypatch,
):
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    refused = init_repo(tmp_path / "repo-c")
    _dir, run = _load(tmp_path, monkeypatch, "a96", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", repo_b / "tracked.txt", repo_b)]),
        Att(3, key="v2:c", start=80, steps=[
            write_step("w1", refused / "tracked.txt", refused), bash_step("w2", "make", refused),
        ]),
    ])
    result = observe.observe_groups(run)
    assert [g.checked for g in result.groups[:3]] == [True, True, False]
    assert result.checked is True and result.repo_root == str(repo_a)


# --- the exports ----------------------------------------------------------------------------------------


def test_rescue_plan_exports_the_unknown_group_when_it_names_a_path(tmp_path, monkeypatch, capsys):
    loose = "/tmp/agent-postmortem-fixture/n5c1-loose.txt"
    run_dir, _run = _load(tmp_path, monkeypatch, "a97", [
        Att(1, steps=[Step("t1", "Write", {"file_path": loose}, "ok", False, None)]),
    ])
    cli.main(["rescue", "plan", str(run_dir), "--json", "--raw"])
    payload = json.loads(capsys.readouterr().out)
    (group,) = payload["groups"]
    assert group["repo"] is None and group["unobserved_paths"] == [loose]
    (row,) = payload["units"][0]["paths"]
    assert row["group"] == 0 and row["observation"] is None


def test_a_session_main_thread_is_not_aliased_in_the_exported_observation(tmp_path, capsys):
    repo = init_repo(tmp_path / "repo")
    entries = [
        {**_assistant_tool("m1", "Write", {"file_path": str(repo / "tracked.txt")},
                           BASE + timedelta(seconds=5)), "cwd": str(repo)},
        {**_tool_result("m1", "ok", BASE + timedelta(seconds=6)), "cwd": str(repo)},
        _kill(BASE + timedelta(seconds=7)),
    ]
    main, _ids = session_with_children(tmp_path, [], main_entries=entries)
    cli.main(["run", str(main), "--json", "--observe"])
    payload = json.loads(capsys.readouterr().out)
    (group,) = payload["observation"]["groups"]
    (obs,) = group["paths"].values()
    assert obs["attempts"] == [{"agent_id": "main", "ordinal": 1}]


def test_repo_state_shows_a_later_write(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    target = repo / "tracked.txt"
    run_dir, _run = _load(tmp_path, monkeypatch, "a98", [
        Att(1, key="v2:k", steps=[write_step("t1", target, repo)]),
        Att(2, key="v2:k", start=60, steps=[write_step("u1", target, repo)]),
    ])
    out = _run_text(capsys, run_dir, "--raw", "--observe")
    assert f"    {target}  CLEAN_NOW  later write: yes  attempts: 1, 2" in out


def test_share_numbers_the_repositories_and_paths_in_group_order(tmp_path, monkeypatch, capsys):
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    (repo_a / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir, _run = _load(tmp_path, monkeypatch, "a99", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[
            write_step("u1", repo_b / "tracked.txt", repo_b), bash_step("u2", "make", repo_b),
        ]),
    ])
    out = _run_text(capsys, run_dir, "--observe")
    repo_state = _section(out, "REPO STATE")
    assert repo_state[1:6] == [
        "  repository path-1  checked: yes",
        "    path-2  DIRTY_NOW  later write: no  attempts: 1",
        "  repository path-3  checked: no (its ledger is partial: incomplete evidence may hide "
        "more writes, so the tool does not run status for it)",
        "    run yourself: git status --short in path-3; --raw prints the exact command",
        "    path-4  not observed",
    ]
    assert any(line.startswith("  run yourself: git status --short in path-3;") for line in out.splitlines())


# --- rescue rows ----------------------------------------------------------------------------------------


def test_declared_checks_follow_the_observed_toplevel_not_the_candidate_directory(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo", {"src/a.py": "a\n"})
    (repo / "src" / "a.py").write_text("changed\n", encoding="utf-8")
    config = tmp_path / "cfg.toml"
    config.write_text(f'[[repos]]\npath = "{repo}"\nchecks = ["make check"]\n', encoding="utf-8")
    run_dir, _run = _load(tmp_path, monkeypatch, "aa0", [
        Att(1, steps=[write_step("t1", repo / "src" / "a.py", repo / "src")]),
    ])
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    cli.main(["rescue", "plan", str(run_dir), "--raw"])
    assert "CHECKS    declared for this repo: make check" in capsys.readouterr().out


def test_steps_list_the_unchecked_paths_beside_the_dirty_ones(tmp_path, monkeypatch, capsys):
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    (repo_a / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir, _run = _load(tmp_path, monkeypatch, "aa1", [
        Att(1, key="v2:k", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:k", start=40, steps=[
            write_step("u1", repo_b / "tracked.txt", repo_b), bash_step("u2", "make", repo_b),
        ]),
    ])
    cli.main(["rescue", "plan", str(run_dir), "--raw"])
    out = capsys.readouterr().out
    assert (
        f"git status --short -- tracked.txt {repo_b / 'tracked.txt'}"
        "   (repo state not checked for some paths; those are listed too)"
    ) in out


def test_the_not_checked_note_names_the_first_unchecked_group(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo-a")
    plain = tmp_path / "repo-b"
    plain.mkdir()
    run_dir, _run = _load(tmp_path, monkeypatch, "aa2", [
        Att(1, key="v2:a", steps=[
            Step("t1", "Write", {"file_path": str(repo / "tracked.txt")}, None, False, str(repo)),
        ]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", plain / "f.txt", plain)]),
    ])
    cli.main(["rescue", "plan", str(run_dir), "--raw"])
    out = capsys.readouterr().out
    assert "(repo state not checked: refused)" in out
    assert "(repo state not checked: not a git repository)" not in out


def test_a_unit_with_no_path_says_why_nothing_was_checked(tmp_path, monkeypatch, capsys):
    read_only = Step("t1", "Read", {"file_path": "/tmp/ws/x"}, "text", False, "/tmp/ws")
    run_dir, _run = _load(tmp_path, monkeypatch, "aa3", [Att(1, steps=[read_only])])
    cli.main(["rescue", "plan", str(run_dir), "--raw"])
    assert (
        "(repo state not checked: no paths written by a killed or failed attempt)"
        in capsys.readouterr().out
    )


def test_a_path_no_group_lists_is_unknown_with_a_null_group(tmp_path, monkeypatch, capsys):
    relative = Step("t1", "Write", {"file_path": "rel.txt"}, "ok", False, None)
    run_dir, _run = _load(tmp_path, monkeypatch, "aa4", [Att(1, steps=[relative])])
    cli.main(["rescue", "plan", str(run_dir), "--json", "--raw"])
    payload = json.loads(capsys.readouterr().out)
    (row,) = payload["units"][0]["paths"]
    assert row["group"] is None and row["observation"] is None
    cli.main(["rescue", "plan", str(run_dir), "--raw"])
    assert "UNKNOWN (not found in the observation)" in capsys.readouterr().out


# --- WORK AT RISK ---------------------------------------------------------------------------------------


def test_the_same_path_written_by_two_attempts_counts_once(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir, _run = _load(tmp_path, monkeypatch, "aa5", [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", repo / "tracked.txt", repo)]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[0] == "WORK AT RISK  2 attempts at risk: 1 path named, 0 commits reported."


def test_the_same_commit_reported_by_two_attempts_counts_once(tmp_path, monkeypatch, capsys):
    def commit(tool_id):
        return Step(tool_id, "Bash", {"command": "git commit -m x"}, "[main 1234567] x", False, "/tmp/ws")

    run_dir, _run = _load(tmp_path, monkeypatch, "aa6", [
        Att(1, key="v2:a", steps=[commit("t1")]),
        Att(2, key="v2:b", start=40, steps=[commit("u1")]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[0].startswith("WORK AT RISK  2 attempts at risk: 0 paths named, 1 commit reported;")


def test_an_incomplete_population_with_no_recorded_reason_still_names_one():
    attempt = model_attempt(1, outcome="killed", ledger=ledger_of("observed"))
    run = model_workflow_run([attempt], completeness="partial")
    lines = report._work_at_risk_lines(run, None, Redactor(enabled=False))
    assert (
        "  the list of attempts at risk may be incomplete: "
        "some run files could not be read or matched." in lines
    )


def test_an_attempt_that_could_not_be_placed_in_time_is_named(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir, run = _load(tmp_path, monkeypatch, "aa7", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ], parent=None)
    assert "unassignable_attempt" in run.risk_population_completeness_reasons
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert any(
        line.startswith(
            f"  the list of attempts at risk may be incomplete: {_aid(1)} could not be placed "
            "in a launch of this run (no launch of this run was found in the parent transcript)"
        ) for line in block
    )


def test_a_write_command_target_is_named_beside_the_shell_command(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    redirect = bash_step("t1", f"echo hi > {repo}/out.txt", repo)
    run_dir, _run = _load(tmp_path, monkeypatch, "aa8", [Att(1, steps=[redirect])])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[0].startswith("WORK AT RISK  1 attempt at risk: 1 path named, 0 commits reported;")
    assert block[1] == (
        f"  {_aid(1)} (impl-1)  named 1 path in write commands; "  # G-A (G6)
        "ran 1 shell command that may have written more"
    )


def test_the_guard_line_leaves_out_an_interrupted_child_already_listed(tmp_path, capsys):
    repo = init_repo(tmp_path / "repo")
    main, ids = session_with_children(tmp_path, [
        (80, [write_step("k1", repo / "tracked.txt", repo)], True, False),  # killed, no return
        (81, [], False, False),
    ])
    cli.main(["run", str(main), "--raw"])
    block = _block(capsys.readouterr().out)
    guard = [line for line in block if "outside this list" in line]
    assert guard == [
        f"  1 subagent outside this list has no established return: {ids[1]}"  # G-A (W1)
    ]


def test_with_observe_a_refused_group_is_not_reported_as_unchecked(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir, _run = _load(tmp_path, monkeypatch, "aa9", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo)]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw", "--observe"))
    assert not any("repo state not checked" in line for line in block)


def test_the_raw_command_quotes_a_repository_path_with_a_space(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "my repo")
    run_dir, _run = _load(tmp_path, monkeypatch, "ab0", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo)]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert f"  run yourself: git -C {shlex.quote(str(repo))} status --short" in block
    assert shlex.quote(str(repo)).startswith("'")


def test_the_hint_lists_at_most_three_absolute_directories(tmp_path, monkeypatch, capsys):
    steps = [bash_step("t9", "make", "relative/dir")] + [
        bash_step(f"t{i}", "make", f"/tmp/agent-postmortem-fixture/h{i}") for i in range(4)
    ]
    run_dir, _run = _load(tmp_path, monkeypatch, "ab1", [Att(1, steps=steps)])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    hints = [line for line in block if line.startswith("    hint: an attempt ran in ")]
    assert hints == [
        f"    hint: an attempt ran in /tmp/agent-postmortem-fixture/h{i}" for i in range(3)
    ]


def test_the_hint_names_a_directory_once(tmp_path, monkeypatch, capsys):
    steps = [bash_step(f"t{i}", "make", "/tmp/agent-postmortem-fixture/h0") for i in range(3)]
    run_dir, _run = _load(tmp_path, monkeypatch, "ab2", [Att(1, steps=steps)])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert len([line for line in block if line.startswith("    hint:")]) == 1


# --- WHAT TO DO and the observe suggestion --------------------------------------------------------------


def _what_to_do(out: str) -> list[str]:
    if "WHAT TO DO" not in out:  # N8a K2: the heading is omitted with no item
        return []
    return _section(out, "WHAT TO DO")[1:]


def test_the_observe_item_is_offered_for_an_observable_group_only_before_observe(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    run_dir, _run = _load(tmp_path, monkeypatch, "ab3", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    before = _what_to_do(_run_text(capsys, run_dir, "--raw"))
    assert any("--observe" in line for line in before)
    after = _what_to_do(_run_text(capsys, run_dir, "--raw", "--observe"))
    assert not any("--observe" in line for line in after)


def test_the_observe_item_is_not_offered_for_a_refused_group(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir, _run = _load(tmp_path, monkeypatch, "ab4", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo)]),
    ])
    assert not any("--observe" in line for line in _what_to_do(_run_text(capsys, run_dir, "--raw")))


def test_the_text_never_leaks_a_separator_into_the_manual_command_at_local(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "my repo")
    run_dir, _run = _load(tmp_path, monkeypatch, "ab5", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo)]),
    ])
    out = _run_text(capsys, run_dir, "--local")
    command = next(line for line in _block(out) if "git -C" in line)
    assert re.fullmatch(r"  run yourself: git -C (\S+|'[^']*') status --short", command)


def test_a_later_write_needs_the_same_key(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    target = repo / "tracked.txt"
    _dir, run = _load(tmp_path, monkeypatch, "ab6", [
        Att(1, key="v2:a", steps=[write_step("t1", target, repo)]),
        Att(2, key="v2:b", start=40, steps=[]),
        Att(3, key="v2:b", start=80, steps=[write_step("u1", target, repo)]),
    ])
    assert [a.ordinal for a in run.attempts] == [1, 1, 2]
    (group,) = observe.observe_groups(run).groups
    (obs,) = group.paths.values()
    assert [a["ordinal"] for a in obs.attempts] == [1, 2]
    assert obs.later_write_observed is False
