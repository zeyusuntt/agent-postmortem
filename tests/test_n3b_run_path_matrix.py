"""N3b P19 through `run --local --observe` on a synthetic repository under
tmp_path (D-m, D-n): every path spelling a killed attempt wrote renders per
the one path function in REPO STATE, never absolute.
"""

from __future__ import annotations

import os

import pytest

from agent_postmortem.cli import main
from tests import n3b_planted as plant


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    (home / "notes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    base = tmp_path.resolve()
    sibling = base / "zzRepoPlant7c5-sibling"  # shares the repo's name as a prefix
    sibling.mkdir()
    return {"base": base, "home": home, "sibling": sibling}


def _build(tmp_path, world):
    repo = (tmp_path / ("zzRepo" + "Plant" + "7c5")).resolve()
    sibling = world["sibling"]
    edits = [
        f"{repo}/src/../src/dotdot.py",                 # '..' that stays inside
        f"{sibling}/other.py",                          # sibling sharing the prefix
        "src/relative.py",                               # relative to the run's cwd
        f"{repo}",                                       # the repository root itself
        "/srv/elsewhere/tool.py",                        # outside home and repository
        f"{world['home']}/notes/todo.md",                # inside home, outside the repo
        f"{world['base']}/linkToRepo/src/viaLink.py",    # a symlink spelling
    ]
    planted = plant.build(tmp_path, extra_edits=edits, observable=True)
    os.symlink(planted.repo, world["base"] / "linkToRepo")
    return planted


def _repo_state(out: str) -> dict[str, str]:
    lines = out.split("REPO STATE\n", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    rows = {}
    for line in lines:
        display, _, rest = line.strip().partition("  ")
        rows[display] = rest
    return rows


def test_local_repo_state_follows_the_path_function_for_every_spelling(
    tmp_path, world, capsys,
):
    planted = _build(tmp_path, world)
    capsys.readouterr()
    main(["run", str(planted.run_dir), "--local", "--observe"])
    out = capsys.readouterr().out
    rows = _repo_state(out)
    displays = list(rows)

    assert "src/module.py" in rows            # the plain inside path
    assert "src/dotdot.py" in rows            # '..' spelling that stays inside
    assert "src/relative.py" in rows          # relative path, resolved against cwd
    assert "." in rows                        # the repository root itself
    assert "~/.../todo.md" in rows            # inside home, outside the repository
    outside = sorted(d for d in displays if d.startswith("path-"))
    assert len(outside) == 3                  # sibling prefix, /srv/elsewhere, symlink spelling
    # no spelling printed absolute, and no tmp directory leaked
    assert all(not d.startswith("/") for d in displays)
    assert str(tmp_path) not in out
    assert str(tmp_path.resolve()) not in out
    assert "zzRepo" not in out
    assert "sibling" not in out


def test_share_repo_state_is_path_n_for_every_spelling(tmp_path, world, capsys):
    planted = _build(tmp_path, world)
    capsys.readouterr()
    main(["run", str(planted.run_dir), "--observe"])
    out = capsys.readouterr().out
    displays = list(_repo_state(out))
    assert displays and all(d.startswith("path-") for d in displays)


def test_raw_repo_state_keeps_the_keys_observe_produced(tmp_path, world, capsys):
    planted = _build(tmp_path, world)
    capsys.readouterr()
    main(["run", str(planted.run_dir), "--raw", "--observe"])
    out = capsys.readouterr().out
    displays = list(_repo_state(out))
    # N5c-1: a group's paths are keyed by the absolute ledger path, so raw shows
    # the real absolute path of every one (observed or not).
    assert str(planted.repo_file) in displays
    assert all(d.startswith("/") for d in displays)
