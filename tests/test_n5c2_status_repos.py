"""N5c-2: `rescue status` attributes checks and writes per declared repository
(docs/DESIGN.md 4.11, 5.3b; plan S7, rev 4).

The roots are the `[[repos]]` paths of the configuration, lexical, never resolved through git.
A ledger path, a candidate repository, or a write's working directory belongs to the longest
declared root that contains it component-wise; a check counts for a repository only at that
repository's exact working directory with one of its exact declared commands; writes after a
repository's last check, and check cycles, are computed per (unit, repository).

Every row of the plan's S7 test list is here by name. The "repositories" are plain directories.
"""

from __future__ import annotations

import json

import pytest

from tests.n5c2_helpers import (
    CHECK,
    Att,
    Call,
    build_scenario,
    file_write,
    monitor,
    notebook_write,
    run_cli,
    shell,
    write_step,
)

# --------------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------------


def _dirs(tmp_path, *names):
    out = []
    for name in names:
        path = tmp_path / "w" / name
        path.mkdir(parents=True, exist_ok=True)
        out.append(path)
    return out


def _status(tmp_path, monkeypatch, capsys, suffix, attempts, calls, repos, *flags, parent="completed"):
    scenario = build_scenario(
        tmp_path, monkeypatch, suffix, attempts, calls=calls, repos=repos, parent=parent,
    )
    code, out, err = run_cli(capsys, ["rescue", "status", str(scenario.run_dir), "--json", "--raw", *flags])
    payload = json.loads(out) if out.strip() else None
    return code, payload, err


def _row(payload, agent: str, repo=None):
    matches = [
        row for row in payload["units"]
        if row["agent"] == agent and (repo is None or row["repo"] == str(repo))
    ]
    assert len(matches) == 1, [(r["agent"], r["repo"]) for r in payload["units"]]
    return matches[0]


def _two_units(tmp_path):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    attempts = [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "f.py", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", repo_b / "f.py", repo_b)]),
    ]
    return repo_a, repo_b, attempts, [(str(repo_a), [CHECK]), (str(repo_b), [CHECK])]


# --------------------------------------------------------------------------------------------
# two declared repositories, checks with different results
# --------------------------------------------------------------------------------------------


def test_two_declared_repositories_with_checks_returning_different_results(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell(CHECK, repo_a, error=True), shell(CHECK, repo_b)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c0", attempts, calls, repos)
    unit_a = _row(payload, "impl-1")
    unit_b = _row(payload, "impl-2")
    assert (unit_a["repo"], unit_a["word"]) == (str(repo_a), "NONZERO_OBSERVED")
    assert (unit_b["repo"], unit_b["word"]) == (str(repo_b), "EXIT_0_OBSERVED")
    assert unit_a["last_counted_run"]["completion"] == "completed_error"
    assert unit_b["last_counted_run"]["completion"] == "completed_ok"
    assert unit_a["checks_declared"] == unit_b["checks_declared"] == 1
    assert code == 3


def test_a_check_counted_in_one_repository_does_not_count_for_another(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell(CHECK, repo_a)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c0", attempts, calls, repos)
    assert _row(payload, "impl-1")["word"] == "EXIT_0_OBSERVED"
    assert _row(payload, "impl-2")["word"] == "NOT_OBSERVED"
    assert code == 4


def test_a_unit_that_wrote_in_two_declared_repositories_gets_one_row_per_repository(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    attempts = [Att(1, key="v2:a", steps=[
        write_step("t1", repo_a / "f.py", repo_a), write_step("t2", repo_b / "f.py", repo_b),
    ])]
    repos = [(str(repo_a), [CHECK]), (str(repo_b), [CHECK])]
    calls = [shell(CHECK, repo_b, error=True)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c0", attempts, calls, repos)
    rows = [row for row in payload["units"] if row["agent"] == "impl-1"]
    assert [(row["repo"], row["word"]) for row in rows] == [
        (str(repo_a), "NOT_OBSERVED"), (str(repo_b), "NONZERO_OBSERVED"),
    ]
    assert code == 3


def test_a_unit_with_no_declared_repository_reports_its_checks_as_not_declared(
    tmp_path, monkeypatch, capsys,
):
    repo_a, other = _dirs(tmp_path, "a", "elsewhere")
    attempts = [Att(1, steps=[write_step("t1", other / "f.py", other)])]
    calls = [shell(CHECK, repo_a)]
    code, payload, _err = _status(
        tmp_path, monkeypatch, capsys, "5c0", attempts, calls, [(str(repo_a), [CHECK])],
    )
    (row,) = payload["units"]
    assert row["repo"] is None
    assert row["checks_declared"] == 0
    assert row["word"] == "NOT_OBSERVED"
    assert code == 4


def test_the_repo_key_is_present_on_every_row_even_with_no_configuration(
    tmp_path, monkeypatch, capsys,
):
    (other,) = _dirs(tmp_path, "a")
    attempts = [Att(1, steps=[write_step("t1", other / "f.py", other)])]
    scenario = build_scenario(tmp_path, monkeypatch, "5c0", attempts)
    _code, out, _err = run_cli(capsys, ["rescue", "status", str(scenario.run_dir), "--json"])
    (row,) = json.loads(out)["units"]
    assert set(row) == {
        "agent", "repo", "checks_declared", "last_counted_run", "word", "writes_after",
        "write_after_check", "cycles",
    }
    assert row["repo"] is None


# --------------------------------------------------------------------------------------------
# a refused observation group never stops a declared check counting
# --------------------------------------------------------------------------------------------


def test_one_repositorys_group_refused_while_its_declared_check_still_counts(
    tmp_path, monkeypatch, capsys,
):
    from tests.n5c2_helpers import bash_step

    (repo_a,) = _dirs(tmp_path, "a")
    # A shell call in the attempt makes its ledger partial, so the group is refused (S5).
    attempts = [Att(1, steps=[
        write_step("t1", repo_a / "f.py", repo_a), bash_step("t2", "make", repo_a),
    ])]
    calls = [shell(CHECK, repo_a)]
    code, payload, _err = _status(
        tmp_path, monkeypatch, capsys, "5c1", attempts, calls, [(str(repo_a), [CHECK])],
    )
    (row,) = payload["units"]
    assert (row["repo"], row["word"]) == (str(repo_a), "EXIT_0_OBSERVED")
    assert code == 0


def test_a_refused_group_is_never_the_reason_for_not_observed(tmp_path, monkeypatch, capsys):
    from tests.n5c2_helpers import bash_step

    (repo_a,) = _dirs(tmp_path, "a")
    attempts = [Att(1, steps=[
        write_step("t1", repo_a / "f.py", repo_a), bash_step("t2", "make", repo_a),
    ])]
    # No declared repository contains the unit's path at all: nothing to attribute, and the
    # row says its checks are not declared (word as before) rather than blaming the group.
    (other,) = _dirs(tmp_path, "elsewhere")
    code, payload, _err = _status(
        tmp_path, monkeypatch, capsys, "5c1", attempts, [shell(CHECK, other)],
        [(str(other), [CHECK])],
    )
    (row,) = payload["units"]
    assert row["repo"] is None and row["checks_declared"] == 0


# --------------------------------------------------------------------------------------------
# a check run from the wrong working directory
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["sub", "other_declared", "elsewhere", "no_cwd"])
def test_a_check_run_from_the_wrong_cwd_is_not_counted(where, tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    (elsewhere,) = _dirs(tmp_path, "elsewhere")
    sub = repo_a / "sub"
    sub.mkdir()
    # Repository B declares a different command, so a call of A's check from B's directory
    # matches only A's declaration and counts for nobody.
    repos = [(str(repo_a), [CHECK]), (str(repo_b), ["make other"])]
    cwd = {"sub": sub, "other_declared": repo_b, "elsewhere": elsewhere, "no_cwd": None}[where]
    calls = [shell(CHECK, cwd)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c2", attempts, calls, repos)
    unit_a = _row(payload, "impl-1")
    assert unit_a["word"] == "NOT_OBSERVED"
    assert unit_a["last_counted_run"] is None
    assert any(row["reason"] == "other cwd" for row in payload["not_counted"])
    assert code == 4


def test_the_same_command_declared_twice_counts_only_for_the_repository_it_ran_in(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell(CHECK, repo_b)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c2", attempts, calls, repos)
    assert _row(payload, "impl-1")["word"] == "NOT_OBSERVED"
    assert _row(payload, "impl-2")["word"] == "EXIT_0_OBSERVED"
    # The call counted somewhere, so it is not also listed as "not counted" for the other.
    assert payload["not_counted"] == []


# --------------------------------------------------------------------------------------------
# nested roots and a sibling whose name begins with another's
# --------------------------------------------------------------------------------------------


def _nested(tmp_path):
    repo, nested, _sub = _dirs(tmp_path, "repo", "repo/vendor/x", "repo/vendor/x/sub")
    (src,) = _dirs(tmp_path, "repo/src")
    attempts = [
        Att(1, key="v2:o", steps=[write_step("t1", src / "g.py", repo)]),
        Att(2, key="v2:n", start=40, steps=[write_step("u1", nested / "f.py", nested)]),
    ]
    repos = [(str(repo), [CHECK]), (str(nested), [CHECK])]
    return repo, nested, attempts, repos


def test_nested_roots_attribute_a_path_to_the_longest_root(tmp_path, monkeypatch, capsys):
    repo, nested, attempts, repos = _nested(tmp_path)
    calls = [shell(CHECK, repo), shell(CHECK, nested)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c3", attempts, calls, repos)
    assert _row(payload, "impl-1")["repo"] == str(repo)
    assert _row(payload, "impl-2")["repo"] == str(nested)
    assert [row["word"] for row in payload["units"]] == ["EXIT_0_OBSERVED", "EXIT_0_OBSERVED"]
    assert code == 0


def test_a_check_in_the_outer_root_does_not_count_for_a_nested_repository(
    tmp_path, monkeypatch, capsys,
):
    repo, nested, attempts, repos = _nested(tmp_path)
    calls = [shell(CHECK, repo)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c3", attempts, calls, repos)
    assert _row(payload, "impl-1")["word"] == "EXIT_0_OBSERVED"
    assert _row(payload, "impl-2")["word"] == "NOT_OBSERVED"
    assert code == 4


def test_a_check_in_the_nested_root_does_not_count_for_the_outer_repository(
    tmp_path, monkeypatch, capsys,
):
    repo, nested, attempts, repos = _nested(tmp_path)
    calls = [shell(CHECK, nested)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c3", attempts, calls, repos)
    assert _row(payload, "impl-1")["word"] == "NOT_OBSERVED"
    assert _row(payload, "impl-2")["word"] == "EXIT_0_OBSERVED"


def test_sibling_roots_with_a_common_name_prefix_do_not_contain_each_other(
    tmp_path, monkeypatch, capsys,
):
    repo, repo2 = _dirs(tmp_path, "repo", "repo2")
    attempts = [Att(1, steps=[write_step("t1", repo2 / "f.py", repo2)])]
    repos = [(str(repo), [CHECK]), (str(repo2), [CHECK])]
    calls = [shell(CHECK, repo2)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c4", attempts, calls, repos)
    (row,) = payload["units"]
    assert row["repo"] == str(repo2)
    assert row["word"] == "EXIT_0_OBSERVED"
    assert code == 0


def test_a_candidate_repository_owns_a_path_no_root_contains(tmp_path, monkeypatch, capsys):
    # The path is spelled outside every declared root but its group's candidate repository
    # (the call's directory) is inside one: the candidate decides.
    repo, other = _dirs(tmp_path, "repo", "other")
    attempts = [Att(1, steps=[write_step("t1", other / "f.py", repo)])]
    calls = [shell(CHECK, repo)]
    code, payload, _err = _status(
        tmp_path, monkeypatch, capsys, "5c4", attempts, calls, [(str(repo), [CHECK])],
    )
    (row,) = payload["units"]
    assert row["repo"] == str(repo)


# --------------------------------------------------------------------------------------------
# writes after a repository's last check
# --------------------------------------------------------------------------------------------


WRITE_ROWS = [
    # name, builder(repo_a, repo_b, sub_of_a, elsewhere) -> Call, expected (A, B)
    ("write in A", lambda a, b, s, e: shell("touch x", a), (1, 0)),
    ("write in B", lambda a, b, s, e: shell("touch x", b), (0, 1)),
    ("write from a subdirectory of A", lambda a, b, s, e: shell("touch x", s), (1, 0)),
    ("literal absolute leading cd into B from A",
     lambda a, b, s, e: shell(f"cd {b} && touch x", a), (0, 1)),
    ("missing cwd", lambda a, b, s, e: shell("touch x", None), (1, 1)),
    ("relative cd leaves the base unknown",
     lambda a, b, s, e: shell("cd sub && touch x", a), (1, 1)),
    ("a cwd no declared root owns", lambda a, b, s, e: shell("touch x", e), (1, 1)),
    ("a Monitor call in B", lambda a, b, s, e: monitor("tail -f x > y", b), (0, 1)),
    ("a Monitor call with no cwd", lambda a, b, s, e: monitor("tail -f x > y", None), (1, 1)),
    ("a file-tool write to A's own path",
     lambda a, b, s, e: file_write(a / "f.py", a), (1, 0)),
    ("a file-tool write to B's own path",
     lambda a, b, s, e: file_write(b / "f.py", b), (0, 1)),
    ("a file-tool write to a path no unit listed",
     lambda a, b, s, e: file_write(a / "unlisted.py", a), (0, 0)),
    ("a proven read-only call with no cwd", lambda a, b, s, e: shell("git status", None), (0, 0)),
    ("a later cd leaves the base unknown",
     lambda a, b, s, e: shell(f"touch x && cd {b} && touch y", a), (1, 1)),
    ("a relative file-tool write is placed by its cwd",
     lambda a, b, s, e: file_write("f.py", a), (1, 0)),
    ("a failed file-tool write is no write",
     lambda a, b, s, e: Call("write", str(a / "f.py"), str(a), "boom", True), (0, 0)),
    ("a NotebookEdit write to A's own path",
     lambda a, b, s, e: notebook_write(a / "f.py", a), (1, 0)),
    ("a blank command may have written anywhere", lambda a, b, s, e: shell("   ", a), (1, 1)),
]


@pytest.mark.parametrize(
    "name,build,expected", [pytest.param(*row, id=row[0]) for row in WRITE_ROWS],
)
def test_write_after_check_ownership(name, build, expected, tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    (sub,) = _dirs(tmp_path, "a/sub")
    (elsewhere,) = _dirs(tmp_path, "elsewhere")
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), build(repo_a, repo_b, sub, elsewhere)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c5", attempts, calls, repos)
    unit_a, unit_b = _row(payload, "impl-1"), _row(payload, "impl-2")
    assert (unit_a["writes_after"], unit_b["writes_after"]) == expected
    assert (unit_a["write_after_check"], unit_b["write_after_check"]) == (
        expected[0] > 0, expected[1] > 0,
    )
    assert code == (4 if any(expected) else 0)


def test_a_write_before_a_repositorys_last_check_is_not_a_write_after_it(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    # B's check ran after the write; A's did not.
    calls = [shell(CHECK, repo_a), shell("touch x", None), shell(CHECK, repo_b)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c5", attempts, calls, repos)
    assert _row(payload, "impl-1")["writes_after"] == 1
    assert _row(payload, "impl-2")["writes_after"] == 0


def test_nested_write_ownership_goes_to_the_longest_root(tmp_path, monkeypatch, capsys):
    repo, nested, attempts, repos = _nested(tmp_path)
    (deep,) = _dirs(tmp_path, "repo/vendor/x/sub")
    calls = [shell(CHECK, repo), shell(CHECK, nested), shell("touch x", deep)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c3", attempts, calls, repos)
    assert _row(payload, "impl-1")["writes_after"] == 0
    assert _row(payload, "impl-2")["writes_after"] == 1


def test_a_command_the_recorder_did_not_keep_is_a_possible_write_for_every_repository(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    long_command = "echo " + "x" * 3000
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), shell(long_command, repo_a)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c5", attempts, calls, repos)
    assert _row(payload, "impl-1")["writes_after"] == 1
    assert _row(payload, "impl-2")["writes_after"] == 1


def test_a_write_capable_call_is_listed_once_however_many_repositories_it_may_affect(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), shell("touch x", None)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c5", attempts, calls, repos)
    reasons = [row["reason"] for row in payload["not_counted"]]
    assert reasons == ["a Bash command that may write ran after the check"]


# --------------------------------------------------------------------------------------------
# check cycles, per (unit, repository)
# --------------------------------------------------------------------------------------------

CYCLE_ROWS = [
    ("write in A", lambda a, b, s, e: shell("touch x", a), (1, 0)),
    ("write in B", lambda a, b, s, e: shell("touch x", b), (0, 1)),
    ("write from a subdirectory of A", lambda a, b, s, e: shell("touch x", s), (1, 0)),
    ("literal absolute leading cd into B from A",
     lambda a, b, s, e: shell(f"cd {b} && touch x", a), (0, 1)),
    ("missing cwd", lambda a, b, s, e: shell("touch x", None), (1, 1)),
    ("relative cd leaves the base unknown",
     lambda a, b, s, e: shell("cd sub && touch x", a), (1, 1)),
    ("a cwd no declared root owns", lambda a, b, s, e: shell("touch x", e), (1, 1)),
    ("a file-tool write to A's own path",
     lambda a, b, s, e: file_write(a / "f.py", a), (1, 0)),
    ("a file-tool write to B's own path",
     lambda a, b, s, e: file_write(b / "f.py", b), (0, 1)),
    ("a proven read-only call", lambda a, b, s, e: shell("git status", None), (0, 0)),
    ("a later cd leaves the base unknown",
     lambda a, b, s, e: shell(f"touch x && cd {b} && touch y", a), (1, 1)),
    ("a relative file-tool write is placed by its cwd",
     lambda a, b, s, e: file_write("f.py", a), (1, 0)),
    ("a failed file-tool write is no write",
     lambda a, b, s, e: Call("write", str(a / "f.py"), str(a), "boom", True), (0, 0)),
    ("a NotebookEdit write to A's own path",
     lambda a, b, s, e: notebook_write(a / "f.py", a), (1, 0)),
    ("a blank command may have written anywhere", lambda a, b, s, e: shell("   ", a), (1, 1)),
]


@pytest.mark.parametrize(
    "name,build,expected", [pytest.param(*row, id=row[0]) for row in CYCLE_ROWS],
)
def test_check_cycles_count_the_writes_each_repository_owns(
    name, build, expected, tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    (sub,) = _dirs(tmp_path, "a/sub")
    (elsewhere,) = _dirs(tmp_path, "elsewhere")
    # a write, then each repository's check: a cycle for the repository the write belongs to
    calls = [build(repo_a, repo_b, sub, elsewhere), shell(CHECK, repo_a), shell(CHECK, repo_b)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c6", attempts, calls, repos)
    unit_a, unit_b = _row(payload, "impl-1"), _row(payload, "impl-2")
    assert (unit_a["cycles"], unit_b["cycles"]) == expected


def test_cycles_are_counted_per_repository_over_a_longer_history(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [
        file_write(repo_a / "f.py", repo_a), shell(CHECK, repo_a),
        file_write(repo_a / "f.py", repo_a), shell(CHECK, repo_a),
        shell(CHECK, repo_b),
        file_write(repo_b / "f.py", repo_b), shell(CHECK, repo_b),
    ]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c6", attempts, calls, repos)
    assert _row(payload, "impl-1")["cycles"] == 2
    assert _row(payload, "impl-2")["cycles"] == 1


# --------------------------------------------------------------------------------------------
# the text rendering names the repository at each level
# --------------------------------------------------------------------------------------------


def test_status_text_names_the_repository_on_each_unit_line(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c7", attempts, calls=[shell(CHECK, repo_a)], repos=repos,
    )
    _code, out, _err = run_cli(capsys, ["rescue", "status", str(scenario.run_dir), "--raw"])
    assert f"UNIT impl-1   repository {repo_a}   checks declared: 1" in out
    assert f"UNIT impl-2   repository {repo_b}   checks declared: 1" in out


def test_status_text_and_json_name_the_repository_through_the_levels_path_rule(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c7", attempts, calls=[shell(CHECK, repo_a)], repos=repos,
    )
    for level_flags in ([], ["--local"]):
        _code, out, _err = run_cli(
            capsys, ["rescue", "status", str(scenario.run_dir), *level_flags],
        )
        assert str(repo_a) not in out and str(repo_b) not in out
        assert "repository path-" in out
        _code, out, _err = run_cli(
            capsys, ["rescue", "status", str(scenario.run_dir), "--json", *level_flags],
        )
        assert str(repo_a) not in out and str(repo_b) not in out
        repos_named = [row["repo"] for row in json.loads(out)["units"]]
        assert all(name.startswith("path-") for name in repos_named)
        assert len(set(repos_named)) == 2


def test_a_unit_with_no_declared_repository_prints_the_line_as_before(
    tmp_path, monkeypatch, capsys,
):
    (other,) = _dirs(tmp_path, "elsewhere")
    attempts = [Att(1, steps=[write_step("t1", other / "f.py", other)])]
    scenario = build_scenario(tmp_path, monkeypatch, "5c7", attempts)
    _code, out, _err = run_cli(capsys, ["rescue", "status", str(scenario.run_dir), "--raw"])
    assert "UNIT impl-1   checks declared: 0" in out


# --------------------------------------------------------------------------------------------
# smaller rows
# --------------------------------------------------------------------------------------------


def test_an_exact_declared_command_beats_a_shorter_one_it_contains(tmp_path, monkeypatch, capsys):
    (repo,) = _dirs(tmp_path, "a")
    attempts = [Att(1, steps=[write_step("t1", repo / "f.py", repo)])]
    calls = [shell("make check", repo)]
    code, payload, _err = _status(
        tmp_path, monkeypatch, capsys, "5c8", attempts, calls, [(str(repo), ["make", "make check"])],
    )
    (row,) = payload["units"]
    assert row["word"] == "EXIT_0_OBSERVED"
    assert row["last_counted_run"]["command"] == "make check"
    assert payload["not_counted"] == []


def test_a_unit_with_no_counted_check_has_no_write_after_a_check(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell("touch x", repo_a), file_write(repo_a / "f.py", repo_a)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c8", attempts, calls, repos)
    for row in payload["units"]:
        assert (row["word"], row["writes_after"], row["cycles"]) == ("NOT_OBSERVED", 0, 0)
    assert code == 4


def test_a_declared_check_run_from_the_wrong_directory_is_also_a_possible_write(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    (elsewhere,) = _dirs(tmp_path, "elsewhere")
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), shell(CHECK, elsewhere)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c8", attempts, calls, repos)
    assert [row["writes_after"] for row in payload["units"]] == [1, 1]
    assert [row["reason"] for row in payload["not_counted"]] == ["other cwd"]
    assert code == 4


def test_a_sibling_whose_name_extends_a_declared_root_is_owned_by_nobody(
    tmp_path, monkeypatch, capsys,
):
    repo, repo2 = _dirs(tmp_path, "repo", "repo2")
    attempts = [Att(1, steps=[write_step("t1", repo2 / "f.py", repo2)])]
    code, payload, _err = _status(
        tmp_path, monkeypatch, capsys, "5c8", attempts, [shell(CHECK, repo)], [(str(repo), [CHECK])],
    )
    (row,) = payload["units"]
    assert row["repo"] is None and row["checks_declared"] == 0
    assert row["word"] == "NOT_OBSERVED"


def test_a_unit_with_no_declared_repository_never_borrows_another_units_check(
    tmp_path, monkeypatch, capsys,
):
    repo_a, other = _dirs(tmp_path, "a", "elsewhere")
    attempts = [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "f.py", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", other / "f.py", other)]),
    ]
    code, payload, _err = _status(
        tmp_path, monkeypatch, capsys, "5c8", attempts, [shell(CHECK, repo_a)],
        [(str(repo_a), [CHECK])],
    )
    assert _row(payload, "impl-1")["word"] == "EXIT_0_OBSERVED"
    assert _row(payload, "impl-2")["word"] == "NOT_OBSERVED"
    assert _row(payload, "impl-2")["last_counted_run"] is None


def test_a_file_tool_write_counts_only_for_the_repository_that_owns_the_path(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    attempts = [Att(1, steps=[
        write_step("t1", repo_a / "f.py", repo_a), write_step("t2", repo_b / "f.py", repo_b),
    ])]
    repos = [(str(repo_a), [CHECK]), (str(repo_b), [CHECK])]
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), file_write(repo_a / "f.py", repo_a)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c8", attempts, calls, repos)
    rows = {row["repo"]: row for row in payload["units"]}
    assert rows[str(repo_a)]["writes_after"] == 1
    assert rows[str(repo_b)]["writes_after"] == 0


def test_a_write_before_every_check_is_not_listed_as_a_write_after_a_check(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell("touch x", None), shell(CHECK, repo_a), shell(CHECK, repo_b)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c8", attempts, calls, repos)
    assert payload["not_counted"] == []
    assert [row["writes_after"] for row in payload["units"]] == [0, 0]
    assert [row["cycles"] for row in payload["units"]] == [1, 1]


def test_a_write_starts_one_cycle_only(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [file_write(repo_a / "f.py", repo_a), shell(CHECK, repo_a), shell(CHECK, repo_a)]
    code, payload, _err = _status(tmp_path, monkeypatch, capsys, "5c8", attempts, calls, repos)
    assert _row(payload, "impl-1")["cycles"] == 1


def test_a_run_with_no_parent_transcript_is_unknown_even_with_nothing_to_rescue(
    tmp_path, monkeypatch, capsys,
):
    from tests.n5b_helpers import Spec, build_workflow

    run_dir = build_workflow(
        tmp_path, monkeypatch, "wf_00000000-5cc", [Spec("v2:x", 1)], parent=None,
    )
    code, out, _err = run_cli(capsys, ["rescue", "status", str(run_dir), "--json"])
    payload = json.loads(out)
    assert payload["units"] == [] and payload["transcript_unknown"] is True
    assert code == 4
