"""N5 fix 1, m1, m3, m6, m7, m8 (docs/DESIGN.md 4.7, 4.11): the small text fixes.

m1: the share manual command says `--raw` prints the exact command.
m3: every group that is not checked, or that holds unobserved paths, prints its manual command
    when the run was observed.
m6: the rescue module docstring names the function the module really calls.
m7: the /postmortem skill describes WORK AT RISK as a block, not one line.
m8: "1 path dirty now".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_postmortem import cli, rescue
from tests.n5c1_helpers import Att, Step, bash_step, build_run, init_repo, write_step

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugin"


def _block(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("WORK AT RISK"))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip():
            break
        block.append(line)
    return block


def _run_block(capsys, target, *flags):
    cli.main(["run", str(target), *flags])
    return _block(capsys.readouterr().out)


def _workflow(tmp_path, monkeypatch, suffix, atts):
    return build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", atts)


def _commit_step(repo):
    return Step("t9", "Bash", {"command": "git commit -m x"}, "[main 1234567] x", False, str(repo))


# --- m1 ----------------------------------------------------------------------------------------


def test_the_share_manual_command_says_raw_prints_the_exact_command(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "f61", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), _commit_step(repo)]),
    ])
    block = _run_block(capsys, run_dir)
    (line,) = [line for line in block if re.match(r"  run yourself: git status --short in path", line)]
    assert re.fullmatch(
        r"  run yourself: git status --short in path-\d+; --raw prints the exact command", line,
    )
    assert not any("runnable" in line for line in block)


# --- m3 ----------------------------------------------------------------------------------------


def test_an_unchecked_group_prints_its_manual_command_with_observe(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    run_dir = _workflow(tmp_path, monkeypatch, "f62", [
        Att(1, steps=[write_step("t1", plain / "f.txt", plain)]),
    ])
    block = _run_block(capsys, run_dir, "--raw", "--observe")
    assert "  repo state not checked (not a git repository)" in block
    assert block[-1] == f"  run yourself: git -C {plain} status --short"


def test_an_unchecked_group_at_share_names_only_its_alias(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    run_dir = _workflow(tmp_path, monkeypatch, "f63", [
        Att(1, steps=[write_step("t1", plain / "f.txt", plain)]),
    ])
    block = _run_block(capsys, run_dir, "--observe")
    assert re.fullmatch(
        r"  run yourself: git status --short in path-\d+; --raw prints the exact command",
        block[-1],
    )
    assert str(plain) not in "\n".join(block)


def test_a_group_with_an_unobserved_path_prints_its_manual_command(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere" / "x.txt"
    run_dir = _workflow(tmp_path, monkeypatch, "f64", [
        Att(1, steps=[
            write_step("t1", repo / "tracked.txt", repo),
            Step("t2", "MultiEdit", {"file_path": str(outside), "edits": []}, "ok", False,
                 str(repo)),
        ]),
    ])
    block = _run_block(capsys, run_dir, "--raw", "--observe")
    assert block[-1] == f"  run yourself: git -C {repo} status --short"


def test_a_fully_checked_group_prints_no_manual_command(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "f65", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    block = _run_block(capsys, run_dir, "--raw", "--observe")
    assert not any("run yourself" in line for line in block)


def test_without_observe_an_unrefused_group_prints_no_manual_command(
    tmp_path, monkeypatch, capsys,
):
    plain = tmp_path / "plain"
    plain.mkdir()
    run_dir = _workflow(tmp_path, monkeypatch, "f66", [
        Att(1, steps=[write_step("t1", plain / "f.txt", plain)]),
    ])
    block = _run_block(capsys, run_dir, "--raw")
    assert not any("run yourself" in line for line in block)


def test_a_refused_group_prints_its_manual_command_once_even_with_observe(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "f67", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo)]),
    ])
    block = _run_block(capsys, run_dir, "--raw", "--observe")
    assert [line for line in block if line == f"  run yourself: git -C {repo} status --short"] == [
        f"  run yourself: git -C {repo} status --short"
    ]


# --- m6 ----------------------------------------------------------------------------------------


def test_the_rescue_module_docstring_names_the_function_it_calls():
    doc = rescue.__doc__ or ""
    assert "observe_run" not in doc
    assert "observe_groups" in doc


# --- m7 ----------------------------------------------------------------------------------------


def test_the_postmortem_skill_describes_work_at_risk_as_a_block():
    text = (PLUGIN_ROOT / "skills" / "postmortem" / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "`WORK AT RISK` line" not in flat
    assert "`WORK AT RISK` block" in flat
    assert "indented" in flat and "run yourself" in flat
    assert "its first line is not the `none:` or `unknown:` form" in flat
    assert "/rescue" in text


# --- m8 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("dirty,expected", [
    pytest.param(0, "0 paths"),
    pytest.param(1, "1 path"),
    pytest.param(2, "2 paths"),
])
def test_the_dirty_count_uses_the_singular_for_one(dirty, expected, tmp_path, monkeypatch, capsys):
    names = {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    repo = init_repo(tmp_path / "repo", names)
    for name in list(names)[:dirty]:
        (repo / name).write_text("changed\n", encoding="utf-8")
    run_dir = _workflow(tmp_path, monkeypatch, "f68", [
        Att(1, steps=[
            write_step("t1", repo / "a.txt", repo), write_step("t2", repo / "b.txt", repo),
            write_step("t3", repo / "c.txt", repo),
        ]),
    ])
    block = _run_block(capsys, run_dir, "--raw", "--observe")
    assert f"  {expected} dirty now, 0 missing, {3 - dirty} clean" in block
