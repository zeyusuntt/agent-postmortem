"""A local build says which commit it is (docs/DESIGN.md 8.1, 0.4).

Every build from the working branch reported the released version, so neither a session nor a
field-feedback entry could tell which code it ran. A wheel built from a git checkout that is not
its release tag now carries `agent_postmortem/_build_info.json` (branch, short commit, commit
date, whether tracked files were modified), written by the hatchling build hook in
`hatch_build.py`, and `agent-postmortem --version` prints `<version> (<branch> <commit>, <date>)`.
A release wheel (built from the sdist, or from the commit tagged v<version>) carries nothing and
prints the plain version. `hook capabilities` always reports the plain version: the plugin's
run.sh compares it with the plugin manifest and reinstalls the bundled CLI on a mismatch
(4.14.11), so a stamp must never reach it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agent_postmortem import __version__, cli

REPO = Path(__file__).resolve().parent.parent
STAMP = {"branch": "live", "commit": "7edc932", "date": "2026-10-30", "dirty": False}


def _hook_module():
    spec = importlib.util.spec_from_file_location("hatch_build_under_test", REPO / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


# --- the runtime text ------------------------------------------------------------------------


def test_the_version_text_names_branch_commit_and_date():
    assert cli._version_text(STAMP) == f"agent-postmortem {__version__} (live 7edc932, 2026-10-30)"


def test_a_build_with_modified_tracked_files_says_so():
    stamp = {**STAMP, "dirty": True}
    assert cli._version_text(stamp) == (
        f"agent-postmortem {__version__} (live 7edc932+dirty, 2026-10-30)"
    )


@pytest.mark.parametrize("stamp", [
    None, {}, {**STAMP, "commit": "xyz"}, {**STAMP, "date": "yesterday"},
    {**STAMP, "branch": "a b"}, {**STAMP, "dirty": "no"}, {**STAMP, "branch": "x" * 200},
    ["live"],
])
def test_a_missing_or_malformed_stamp_prints_the_plain_version(stamp):
    assert cli._version_text(stamp) == f"agent-postmortem {__version__}"


def test_the_version_flag_prints_the_installed_builds_stamp(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_build_info", lambda: STAMP)
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == (
        f"agent-postmortem {__version__} (live 7edc932, 2026-10-30)"
    )


def test_hook_capabilities_reports_the_plain_version_equal_to_the_plugin_manifest(
    monkeypatch, capsys,
):
    monkeypatch.setattr(cli, "_read_build_info", lambda: STAMP)
    manifest = json.loads((REPO / "plugin" / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    code = cli.main(["hook", "capabilities"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["version"] == manifest["version"] == __version__


# --- the build hook ----------------------------------------------------------------------------


def test_a_checkout_off_its_release_tag_is_stamped(tmp_path):
    repo = _repo(tmp_path)
    info = _hook_module().build_info(str(repo), "9.9.9")
    assert info == {
        "branch": "work", "commit": _git(repo, "rev-parse", "--short", "HEAD"),
        "date": _git(repo, "log", "-1", "--format=%cs"), "dirty": False,
    }


def test_a_modified_tracked_file_marks_the_stamp_dirty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "f.txt").write_text("two\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    assert _hook_module().build_info(str(repo), "9.9.9")["dirty"] is True


def test_the_commit_tagged_with_the_release_version_is_not_stamped(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "tag", "v9.9.9")
    assert _hook_module().build_info(str(repo), "9.9.9") is None
    assert _hook_module().build_info(str(repo), "9.9.8") is not None


def test_a_tree_that_is_not_a_git_checkout_is_not_stamped(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _hook_module().build_info(str(plain), "9.9.9") is None


# --- real builds ---------------------------------------------------------------------------------


def _wheel_stamp(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if "agent_postmortem/_build_info.json" not in names:
            return None
        return json.loads(archive.read("agent_postmortem/_build_info.json"))


def test_a_wheel_built_from_this_checkout_carries_its_commit_and_one_built_from_the_sdist_does_not(
    tmp_path,
):
    from_tree = tmp_path / "tree"
    via_sdist = tmp_path / "sdist"
    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(from_tree)], cwd=REPO, check=True,
                   capture_output=True, timeout=120)
    subprocess.run(["uv", "build", "-q", "-o", str(via_sdist)], cwd=REPO, check=True,
                   capture_output=True, timeout=120)
    stamp = _wheel_stamp(next(from_tree.glob("*.whl")))
    if f"v{__version__}" in _git(REPO, "tag", "--points-at", "HEAD").split():
        # the release commit itself (the release workflow runs this suite at its tag)
        assert stamp is None
    else:
        assert stamp is not None
        assert stamp["commit"] == _git(REPO, "rev-parse", "--short", "HEAD")
        assert cli._version_text(stamp).startswith(f"agent-postmortem {__version__} (")
    assert _wheel_stamp(next(via_sdist.glob("*.whl"))) is None
    assert sys.version_info >= (3, 11)
