"""N5 fix 1, C3 and S1: `rescue plan` path rows and the UNKNOWN fact (docs/DESIGN.md 4.11, 5.3b).

C3: a row is keyed by the normalized absolute target and its group, never by the raw spelling a
tool call used, so one relative path written from two repositories is two rows, each with its own
group, observation, attempts, and ledger entries.
S1: a unit's rows come from its facts paths plus every ledger entry path of its attempts, and the
plan's UNKNOWN fact is true for any refused or unknown group and any group with unobserved paths.
"""

from __future__ import annotations

import itertools
import json

import pytest

from agent_postmortem import cli
from tests.n5c1_helpers import Att, Step, bash_step, build_run, init_repo, write_step
from tests.n5c2_helpers import age_files, build_scenario, run_cli


def _plan(capsys, run_dir, *flags):
    code = cli.main(["rescue", "plan", str(run_dir), "--json", *flags])
    return code, json.loads(capsys.readouterr().out)


def _plan_text(capsys, run_dir, *flags):
    code = cli.main(["rescue", "plan", str(run_dir), *flags])
    return code, capsys.readouterr().out


def multiedit_step(tool_id, path, cwd):
    return Step(tool_id, "MultiEdit", {"file_path": str(path), "edits": []}, "ok", False, str(cwd))


def _unit(payload, agent):
    (unit,) = [unit for unit in payload["units"] if unit["agent"] == agent]
    return unit


def _group_index(payload, repo):
    (index,) = [i for i, group in enumerate(payload["groups"]) if group["repo"] == str(repo)]
    return index


# --- C3: identical relative spellings --------------------------------------------------------

LEVELS = [
    pytest.param([], id="share"), pytest.param(["--local"], id="local"),
    pytest.param(["--raw"], id="raw"),
]


def _same_relative_path(tmp_path, monkeypatch, suffix, kind):
    """One logical unit, two failed attempts, both writing the relative `f.py`: attempt 1 from
    repository A (dirty), attempt 2 from repository B (clean). `kind` is `checked` (both
    repositories observed), `refused` (attempt 2 also ran a shell command, so B is refused), or
    `nested` (B is a repository inside A)."""
    repo_a = init_repo(tmp_path / "repo-a", {"f.py": "orig\n"})
    (repo_a / "f.py").write_text("changed\n", encoding="utf-8")
    if kind == "nested":
        repo_b = init_repo(repo_a / "inner", {"f.py": "orig\n"})
    else:
        repo_b = init_repo(tmp_path / "repo-b", {"f.py": "orig\n"})
    second = [write_step("u1", "f.py", repo_b)]
    if kind == "refused":
        second.append(bash_step("u2", "make", repo_b))
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Att(1, key="v2:x", steps=[write_step("t1", "f.py", repo_a)]),
        Att(2, key="v2:x", start=40, steps=second),
    ])
    return run_dir, repo_a, repo_b


@pytest.mark.parametrize("flags", LEVELS)
@pytest.mark.parametrize("kind", ["checked", "refused", "nested"])
def test_one_relative_path_written_from_two_repositories_is_two_rows(
    kind, flags, tmp_path, monkeypatch, capsys,
):
    run_dir, repo_a, repo_b = _same_relative_path(tmp_path, monkeypatch, "f31", kind)
    code, payload = _plan(capsys, run_dir, *flags)
    assert len(payload["groups"]) == 2
    (unit,) = payload["units"]
    rows = unit["paths"]
    assert len(rows) == 2
    assert [row["group"] for row in rows] == [0, 1]
    assert [row["attempts"] for row in rows] == [[1], [2]]
    assert [[entry["attempt"] for entry in row["ledger"]] for row in rows] == [[1], [2]]
    assert rows[0]["observation"] == {"state": "DIRTY_NOW", "reason": None}
    if kind == "refused":
        assert rows[1]["observation"] is None
        assert code == 4
    else:
        assert rows[1]["observation"] == {"state": "CLEAN_NOW", "reason": None}
        assert code == 0
    if not flags:
        assert str(tmp_path) not in json.dumps(payload)


def test_each_row_keeps_its_own_ledger_line_and_never_the_others(tmp_path, monkeypatch, capsys):
    run_dir, _a, _b = _same_relative_path(tmp_path, monkeypatch, "f32", "checked")
    _code, payload = _plan(capsys, run_dir, "--raw")
    rows = payload["units"][0]["paths"]
    assert len(rows[0]["ledger"]) == 1 and len(rows[1]["ledger"]) == 1
    assert rows[0]["ledger"][0]["line"] != rows[1]["ledger"][0]["line"]


def test_the_same_absolute_path_written_by_two_attempts_stays_one_row(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f33", [
        Att(1, key="v2:x", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:x", start=40, steps=[write_step("u1", "tracked.txt", repo)]),
    ])
    _code, payload = _plan(capsys, run_dir, "--raw")
    (row,) = payload["units"][0]["paths"]
    assert row["attempts"] == [1, 2]
    assert [entry["attempt"] for entry in row["ledger"]] == [1, 2]
    assert row["later_write_observed"] is True


# --- S1: rows from the ledger, UNKNOWN from the groups ---------------------------------------


def _two_repos(tmp_path):
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    (repo_a / "tracked.txt").write_text("changed a\n", encoding="utf-8")
    (repo_b / "tracked.txt").write_text("changed b\n", encoding="utf-8")
    return repo_a, repo_b


def test_a_shell_only_write_into_a_refused_group_is_an_unknown_fact(tmp_path, monkeypatch, capsys):
    repo_a, repo_b = _two_repos(tmp_path)
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f51", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[
            bash_step("u1", f"echo changed > {repo_b}/tracked.txt", repo_b),
        ]),
    ])
    code, payload = _plan(capsys, run_dir, "--raw")
    assert code == 4
    group_b = _group_index(payload, repo_b)
    assert payload["groups"][group_b]["refused"] is True
    rows = _unit(payload, "impl-2")["paths"]
    assert len(rows) == 1
    assert rows[0]["group"] == group_b and rows[0]["observation"] is None
    assert rows[0]["path"] == str(repo_b / "tracked.txt")
    text_code, out = _plan_text(capsys, run_dir, "--raw")
    assert text_code == 4
    assert "no path is dirty now" not in out


def test_a_multiedit_only_attempt_has_a_row_and_its_observed_state(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f52", [
        Att(1, key="v2:a", steps=[multiedit_step("t1", repo / "tracked.txt", repo)]),
    ])
    code, payload = _plan(capsys, run_dir, "--raw")
    (row,) = payload["units"][0]["paths"]
    assert row["path"] == "tracked.txt"
    assert row["group"] == 0
    assert row["observation"] == {"state": "DIRTY_NOW", "reason": None}
    assert row["attempts"] == [1]
    assert [entry["effect"] for entry in row["ledger"]] == ["observed"]
    assert code == 0
    _code, out = _plan_text(capsys, run_dir, "--raw")
    assert "no path is dirty now" not in out
    assert "git status --short -- tracked.txt" in out


def test_a_write_and_a_shell_write_across_two_repositories_each_get_their_rows(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b = _two_repos(tmp_path)
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f53", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[
            write_step("u1", repo_b / "tracked.txt", repo_b),
            bash_step("u2", f"touch {repo_b}/new.txt", repo_b),
        ]),
    ])
    code, payload = _plan(capsys, run_dir, "--raw")
    group_b = _group_index(payload, repo_b)
    rows = _unit(payload, "impl-2")["paths"]
    assert sorted(row["path"] for row in rows) == [
        str(repo_b / "new.txt"), str(repo_b / "tracked.txt"),
    ]
    assert {row["group"] for row in rows} == {group_b}
    assert all(row["observation"] is None for row in rows)
    (row_a,) = _unit(payload, "impl-1")["paths"]
    assert row_a["observation"] == {"state": "DIRTY_NOW", "reason": None}
    assert code == 4


def test_a_refused_group_with_an_attempt_and_no_path_is_an_unknown_fact(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b = _two_repos(tmp_path)
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f54", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[bash_step("u1", "make", repo_b)]),
    ])
    code, payload = _plan(capsys, run_dir, "--raw")
    assert code == 4
    assert _unit(payload, "impl-2")["paths"] == []
    _code, out = _plan_text(capsys, run_dir, "--raw")
    assert "no path is dirty now: what the killed attempts wrote is committed" not in (
        out.split("UNIT impl-2")[1]
    )


def test_the_unknown_group_alone_is_an_unknown_fact(tmp_path, monkeypatch, capsys):
    repo_a, _repo_b = _two_repos(tmp_path)
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f55", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[bash_step("u1", "make", None)]),
    ])
    code, _payload = _plan(capsys, run_dir, "--raw")
    assert code == 4


def test_a_group_with_unobserved_paths_is_an_unknown_fact_and_its_row_says_why(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    outside = tmp_path / "elsewhere" / "x.txt"
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f56", [
        Att(1, key="v2:a", steps=[
            write_step("t1", repo / "tracked.txt", repo), multiedit_step("t2", outside, repo),
        ]),
    ])
    code, payload = _plan(capsys, run_dir, "--raw")
    rows = payload["units"][0]["paths"]
    assert len(rows) == 2
    outside_row = [row for row in rows if row["path"] == str(outside)][0]
    assert outside_row["observation"] is None
    assert code == 4


def test_a_fully_observed_clean_group_stays_exit_0(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f57", [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    code, payload = _plan(capsys, run_dir, "--raw")
    assert payload["units"][0]["paths"][0]["observation"]["state"] == "CLEAN_NOW"
    assert code == 0


PLAN_CONDITIONS = ("lifecycle", "shell_only_unknown", "live")


def _plan_run(tmp_path, monkeypatch, suffix, *, lifecycle, shell_only_unknown, live):
    """Exactly the named conditions. `shell_only_unknown`: a second unit's only write is a shell
    command in repository B, so B is refused while repository A is checked and clean."""
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    attempts = [Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)])]
    if shell_only_unknown:
        attempts.append(Att(2, key="v2:b", start=40, steps=[
            bash_step("u1", f"touch {repo_b}/new.txt", repo_b),
        ]))
    if lifecycle:
        parent = "weird"
    elif live:
        parent = "running"
        attempts.append(Att(3, key="v2:c", journal="open", start=60))
    else:
        parent = "completed"
    scenario = build_scenario(tmp_path, monkeypatch, suffix, attempts, parent=parent)
    if not live:
        age_files(tmp_path)
    return scenario.run_dir


@pytest.mark.parametrize("subset", [
    pytest.param(subset, id="+".join(subset) or "none")
    for size in range(len(PLAN_CONDITIONS) + 1)
    for subset in itertools.combinations(PLAN_CONDITIONS, size)
])
def test_plan_exit_code_for_every_subset_with_a_shell_only_unknown_fact(
    subset, tmp_path, monkeypatch, capsys,
):
    flags = {name: name in subset for name in PLAN_CONDITIONS}
    run_dir = _plan_run(tmp_path, monkeypatch, "f5a", **flags)
    code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--json"])
    assert code == (4 if any(flags.values()) else 0)
    assert json.loads(out)["exit_code"] == code


def test_a_unit_in_an_unseen_group_is_told_so_in_text_and_json(tmp_path, monkeypatch, capsys):
    repo_a, repo_b = _two_repos(tmp_path)
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f58", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[bash_step("u1", "make", repo_b)]),
    ])
    _code, out = _plan_text(capsys, run_dir, "--raw")
    section = out.split("UNIT impl-2")[1]
    assert "1. [look]   no path was observed for this unit" in section
    assert "repo state not seen for some of this unit's attempts" in section
    first = out.split("UNIT impl-1")[1].split("UNIT impl-2")[0]
    assert "repo state not seen" not in first
    _code, payload = _plan(capsys, run_dir, "--raw")
    assert any("repo state not seen" in text for text in _unit(payload, "impl-2")["unknown"])
    assert not any("repo state not seen" in text for text in _unit(payload, "impl-1")["unknown"])


def test_a_failed_multiedit_still_gets_a_row_with_an_attempted_ledger_entry(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f59", [
        Att(1, key="v2:a", steps=[
            Step("t1", "MultiEdit", {"file_path": str(repo / "tracked.txt"), "edits": []},
                 "boom", True, str(repo)),
        ]),
    ])
    _code, payload = _plan(capsys, run_dir, "--raw")
    (row,) = payload["units"][0]["paths"]
    assert row["path"] == "tracked.txt"
    assert [(entry["effect"], entry["completion"]) for entry in row["ledger"]] == [
        ("attempted", "error"),
    ]


def test_a_group_with_an_unobserved_path_of_a_non_qualifying_attempt_is_still_unknown(
    tmp_path, monkeypatch, capsys,
):
    """The open attempt is in the risk population (its journal never closed) but its outcome
    is neither killed, failed, nor superseded, so it is no unit: only the group says its path
    was not seen."""
    repo = init_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere" / "x.txt"
    scenario = build_scenario(tmp_path, monkeypatch, "f5b", [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:b", journal="open", start=40, steps=[
            multiedit_step("u1", outside, repo),
        ]),
    ])
    age_files(tmp_path)
    code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--json", "--raw"])
    payload = json.loads(out)
    assert [unit["agent"] for unit in payload["units"]] == ["impl-1"]
    assert payload["groups"][0]["unobserved_paths"] == [str(outside)]
    assert code == 4


def test_one_absolute_path_written_from_two_repositories_is_a_row_per_group(
    tmp_path, monkeypatch, capsys,
):
    """The same absolute target, outside both repositories, written from two working
    directories: two groups list it, so two rows, each with only its own attempt."""
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    shared = tmp_path / "shared" / "x.txt"
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f5c", [
        Att(1, key="v2:x", steps=[write_step("t1", shared, repo_a)]),
        Att(2, key="v2:x", start=40, steps=[write_step("u1", shared, repo_b)]),
    ])
    code, payload = _plan(capsys, run_dir, "--raw")
    rows = payload["units"][0]["paths"]
    assert [row["group"] for row in rows] == [
        _group_index(payload, repo_a), _group_index(payload, repo_b),
    ]
    assert [row["attempts"] for row in rows] == [[1], [2]]
    assert [[entry["attempt"] for entry in row["ledger"]] for row in rows] == [[1], [2]]
    assert code == 4


def test_a_unit_with_a_clean_path_and_an_unobserved_one_is_not_told_nothing_is_dirty(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere" / "x.txt"
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-f5d", [
        Att(1, key="v2:a", steps=[
            write_step("t1", repo / "tracked.txt", repo), multiedit_step("t2", outside, repo),
        ]),
    ])
    code, out = _plan_text(capsys, run_dir, "--raw")
    assert code == 4
    assert "no path is dirty now" not in out
    assert "1. [look]   no path was observed for this unit" in out
