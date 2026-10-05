"""N5 fix 1, C1 and C2: `rescue status` write scan (docs/DESIGN.md 4.11, 4.13.11).

C1: a call whose text contains a declared check but that was not counted as a check
(wrong cwd, compound, backgrounded, no result, ambiguous id) is still a possible write; only a
call accepted as a counted check for some repository is skipped by the write scan.
C2: MultiEdit is a file tool: it is a write after a check, and a step of a check cycle, owned
by the repository that owns its target, exactly like Write.

The "repositories" are plain directories, as in test_n5c2_status_repos.py.
"""

from __future__ import annotations

import json

import pytest

from tests.n5c2_helpers import (
    CHECK,
    Att,
    build_scenario,
    file_write,
    multiedit_write,
    run_cli,
    shell,
    write_step,
)


def _dirs(tmp_path, *names):
    out = []
    for name in names:
        path = tmp_path / "w" / name
        path.mkdir(parents=True, exist_ok=True)
        out.append(path)
    return out


def _status(tmp_path, monkeypatch, capsys, suffix, attempts, calls, repos):
    scenario = build_scenario(tmp_path, monkeypatch, suffix, attempts, calls=calls, repos=repos)
    code, out, _err = run_cli(
        capsys, ["rescue", "status", str(scenario.run_dir), "--json", "--raw"],
    )
    return code, json.loads(out)


def _row(payload, agent: str):
    (row,) = [row for row in payload["units"] if row["agent"] == agent]
    return row


def _two_units(tmp_path):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    attempts = [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "f.py", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", repo_b / "f.py", repo_b)]),
    ]
    return repo_a, repo_b, attempts, [(str(repo_a), [CHECK]), (str(repo_b), [CHECK])]


BACKGROUND_RESULT = (
    "Command did not complete within its 30s timeout and was moved to the background (ID: 1)."
)

# --- C1 -----------------------------------------------------------------------------------------

C1_ROWS = [
    # name, builder(repo_a, repo_b, elsewhere) -> calls after both counted checks, (A, B) writes
    ("wrong cwd, no declared root owns it",
     lambda a, b, e: [shell(CHECK, e)], (1, 1)),
    ("wrong cwd, a subdirectory of A",
     lambda a, b, e: [shell(CHECK, a / "sub")], (1, 0)),
    ("compound command in A",
     lambda a, b, e: [shell(f"{CHECK} && touch x", a)], (1, 0)),
    ("compound command that cds into B first",
     lambda a, b, e: [shell(f"cd {b} && {CHECK}", a)], (0, 1)),
    ("backgrounded check in A",
     lambda a, b, e: [shell(CHECK, a, result=BACKGROUND_RESULT)], (1, 0)),
    ("check with no result in B",
     lambda a, b, e: [shell(CHECK, b, no_result=True)], (0, 1)),
    ("two calls sharing one tool id (ambiguous) in A",
     lambda a, b, e: [shell(CHECK, a, tool_id="dup"), shell(CHECK, a, tool_id="dup")], (2, 0)),
]


@pytest.mark.parametrize(
    "name,build,expected", [pytest.param(*row, id=row[0]) for row in C1_ROWS],
)
def test_a_rejected_check_match_after_the_last_check_is_a_possible_write(
    name, build, expected, tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    (elsewhere,) = _dirs(tmp_path, "elsewhere")
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), *build(repo_a, repo_b, elsewhere)]
    code, payload = _status(tmp_path, monkeypatch, capsys, "f1a", attempts, calls, repos)
    unit_a, unit_b = _row(payload, "impl-1"), _row(payload, "impl-2")
    assert (unit_a["writes_after"], unit_b["writes_after"]) == expected
    assert (unit_a["write_after_check"], unit_b["write_after_check"]) == (
        expected[0] > 0, expected[1] > 0,
    )
    assert code == 4


def test_an_accepted_check_is_still_no_write(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), shell(CHECK, repo_a)]
    code, payload = _status(tmp_path, monkeypatch, capsys, "f1b", attempts, calls, repos)
    assert [row["writes_after"] for row in payload["units"]] == [0, 0]
    assert payload["not_counted"] == []
    assert code == 0


def test_a_rejected_match_is_listed_once_under_not_counted(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    (elsewhere,) = _dirs(tmp_path, "elsewhere")
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), shell(CHECK, elsewhere)]
    _code, payload = _status(tmp_path, monkeypatch, capsys, "f1c", attempts, calls, repos)
    assert [row["reason"] for row in payload["not_counted"]] == ["other cwd"]


def test_a_rejected_match_before_the_last_check_is_no_write_after_it(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    (elsewhere,) = _dirs(tmp_path, "elsewhere")
    calls = [shell(CHECK, elsewhere), shell(CHECK, repo_a), shell(CHECK, repo_b)]
    code, payload = _status(tmp_path, monkeypatch, capsys, "f1d", attempts, calls, repos)
    assert [row["writes_after"] for row in payload["units"]] == [0, 0]
    assert code == 0


def test_a_rejected_match_between_two_checks_makes_a_cycle(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [
        shell(CHECK, repo_a), shell(f"{CHECK} && touch x", repo_a), shell(CHECK, repo_a),
        shell(CHECK, repo_b),
    ]
    _code, payload = _status(tmp_path, monkeypatch, capsys, "f1e", attempts, calls, repos)
    assert (_row(payload, "impl-1")["cycles"], _row(payload, "impl-2")["cycles"]) == (1, 0)


# --- C2 -----------------------------------------------------------------------------------------

C2_ROWS = [
    # name, builder(repo_a, repo_b) -> calls after both counted checks, (A, B) writes
    ("MultiEdit of A's path, absolute", lambda a, b: [multiedit_write(a / "f.py", a)], (1, 0)),
    ("MultiEdit of A's path, relative", lambda a, b: [multiedit_write("f.py", a)], (1, 0)),
    ("MultiEdit of B's path from A's directory",
     lambda a, b: [multiedit_write(b / "f.py", a)], (0, 1)),
    ("MultiEdit of a path no unit listed", lambda a, b: [multiedit_write(a / "u.py", a)], (0, 0)),
    ("a failed MultiEdit is no write",
     lambda a, b: [multiedit_write(a / "f.py", a, error=True, result="boom")], (0, 0)),
    ("an ambiguous MultiEdit result (one tool id twice) is not a completed write",
     lambda a, b: [multiedit_write(a / "f.py", a, tool_id="dup"),
                   multiedit_write(a / "f.py", a, tool_id="dup")], (0, 0)),
]

@pytest.mark.parametrize(
    "name,build,expected", [pytest.param(*row, id=row[0]) for row in C2_ROWS],
)
def test_multiedit_after_the_last_check_is_a_write_for_the_owning_repository(
    name, build, expected, tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [shell(CHECK, repo_a), shell(CHECK, repo_b), *build(repo_a, repo_b)]
    code, payload = _status(tmp_path, monkeypatch, capsys, "f2a", attempts, calls, repos)
    unit_a, unit_b = _row(payload, "impl-1"), _row(payload, "impl-2")
    assert (unit_a["writes_after"], unit_b["writes_after"]) == expected
    # A duplicated tool id also makes the parent transcript unknown (exit 4 on its own).
    assert code == (4 if any(expected) or payload["transcript_unknown"] else 0)


def test_multiedit_goes_to_the_longest_nested_root(tmp_path, monkeypatch, capsys):
    repo, nested, _sub = _dirs(tmp_path, "repo", "repo/vendor/x", "repo/vendor/x/sub")
    (src,) = _dirs(tmp_path, "repo/src")
    attempts = [
        Att(1, key="v2:o", steps=[write_step("t1", src / "g.py", repo)]),
        Att(2, key="v2:n", start=40, steps=[write_step("u1", nested / "f.py", nested)]),
    ]
    repos = [(str(repo), [CHECK]), (str(nested), [CHECK])]
    calls = [
        shell(CHECK, repo), shell(CHECK, nested), multiedit_write(nested / "f.py", nested),
    ]
    code, payload = _status(tmp_path, monkeypatch, capsys, "f2b", attempts, calls, repos)
    assert _row(payload, "impl-1")["writes_after"] == 0
    assert _row(payload, "impl-2")["writes_after"] == 1
    assert code == 4


def test_multiedit_before_the_check_is_no_write_after_it(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [multiedit_write(repo_a / "f.py", repo_a), shell(CHECK, repo_a), shell(CHECK, repo_b)]
    code, payload = _status(tmp_path, monkeypatch, capsys, "f2c", attempts, calls, repos)
    assert [row["writes_after"] for row in payload["units"]] == [0, 0]
    assert code == 0


def test_multiedit_between_two_checks_makes_a_cycle_like_write(tmp_path, monkeypatch, capsys):
    repo_a, repo_b, attempts, repos = _two_units(tmp_path)
    calls = [
        shell(CHECK, repo_a), multiedit_write(repo_a / "f.py", repo_a), shell(CHECK, repo_a),
        file_write(repo_b / "f.py", repo_b), shell(CHECK, repo_b),
    ]
    _code, payload = _status(tmp_path, monkeypatch, capsys, "f2d", attempts, calls, repos)
    assert (_row(payload, "impl-1")["cycles"], _row(payload, "impl-2")["cycles"]) == (1, 1)


def test_a_unit_whose_only_writes_are_multiedit_has_a_row_for_its_repository(
    tmp_path, monkeypatch, capsys,
):
    from tests.n5c1_helpers import Step

    repo_a, _repo_b = _dirs(tmp_path, "a", "b")
    attempts = [Att(1, key="v2:a", steps=[
        Step("t1", "MultiEdit", {"file_path": str(repo_a / "f.py"), "edits": []}, "ok", False,
             str(repo_a)),
    ])]
    calls = [
        shell(CHECK, repo_a), multiedit_write(repo_a / "f.py", repo_a),
    ]
    code, payload = _status(
        tmp_path, monkeypatch, capsys, "f2e", attempts, calls, [(str(repo_a), [CHECK])],
    )
    (row,) = payload["units"]
    assert (row["repo"], row["word"], row["writes_after"]) == (
        str(repo_a), "EXIT_0_OBSERVED", 1,
    )
    assert code == 4
