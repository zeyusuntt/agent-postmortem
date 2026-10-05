"""N3b P19 (D-m, D-n): the one path function every sink uses at local.

Make the path absolute against its documented base, os.path.normpath it (no
symlink resolution), then: inside the repository (os.path.commonpath, a
ValueError means outside) -> repo-relative; inside home but outside the
repository -> ~/.../<basename>; outside both -> path-N. Never absolute. With
no repository root every path counts as outside the repository.
"""

from __future__ import annotations

import os

import pytest

from agent_postmortem import disclosure
from agent_postmortem.redact import Pseudonyms


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    sibling = tmp_path / "repo2"
    outside = tmp_path / "elsewhere"
    for directory in (home / "notes", repo / "src", sibling, outside):
        directory.mkdir(parents=True)
    return {
        "home": home, "repo": repo, "sibling": sibling, "outside": outside,
        "tmp": tmp_path,
    }


def _call(tree, value, *, repo=True, base=None, names=None):
    names = names or Pseudonyms()
    return disclosure.local_path(
        value,
        repo_root=str(tree["repo"]) if repo else None,
        home=tree["home"],
        base=base,
        outside=names.path,
    )


def test_inside_the_repository_is_repo_relative(tree):
    assert _call(tree, str(tree["repo"] / "src" / "a.py")) == "src/a.py"


def test_the_repository_root_itself_is_dot(tree):
    assert _call(tree, str(tree["repo"])) == "."
    assert _call(tree, str(tree["repo"]) + "/") == "."


def test_dotdot_that_stays_inside_is_repo_relative(tree):
    assert _call(tree, f"{tree['repo']}/src/../b.py") == "b.py"
    assert _call(tree, f"{tree['repo']}/../repo/c.py") == "c.py"


def test_dotdot_that_escapes_is_outside(tree):
    value = f"{tree['repo']}/src/../../elsewhere/x.py"
    assert _call(tree, value) == "path-1"


def test_a_sibling_directory_sharing_the_prefix_is_outside(tree):
    value = str(tree["sibling"] / "x.py")
    assert str(tree["sibling"]).startswith(str(tree["repo"]))
    assert _call(tree, value) == "path-1"


def test_a_relative_path_resolves_against_its_base(tree):
    assert _call(tree, "src/a.py", base=str(tree["repo"])) == "src/a.py"
    assert _call(tree, "../elsewhere/x.py", base=str(tree["repo"])) == "path-1"


def test_a_relative_path_without_a_base_is_outside(tree):
    assert _call(tree, "src/a.py") == "path-1"


def test_a_relative_path_with_a_relative_base_is_outside(tree):
    assert _call(tree, "src/a.py", base="some/where") == "path-1"


def test_inside_home_outside_the_repository_is_structural(tree):
    assert _call(tree, str(tree["home"] / "notes" / "todo.md")) == "~/.../todo.md"
    assert _call(tree, str(tree["home"] / "deep" / "er" / "todo.md")) == "~/.../todo.md"


def test_outside_home_and_repository_is_path_n_and_numbers_stay_stable(tree):
    names = Pseudonyms()
    first = _call(tree, str(tree["outside"] / "x.py"), names=names)
    second = _call(tree, str(tree["tmp"] / "other" / "y.py"), names=names)
    again = _call(tree, str(tree["outside"] / "x.py"), names=names)
    assert (first, second, again) == ("path-1", "path-2", "path-1")


def test_the_repository_wins_over_home_when_the_repository_is_inside_home(tree, tmp_path):
    inner = tree["home"] / "work" / "proj"
    (inner / "pkg").mkdir(parents=True)
    result = disclosure.local_path(
        str(inner / "pkg" / "m.py"),
        repo_root=str(inner), home=tree["home"], base=None, outside=Pseudonyms().path,
    )
    assert result == "pkg/m.py"


def test_without_a_repository_root_everything_is_outside_the_repository(tree):
    value = str(tree["repo"] / "src" / "a.py")
    assert _call(tree, value, repo=False) == "path-1"
    # home still collapses structurally: that rule does not need a repository.
    assert _call(tree, str(tree["home"] / "n" / "x.md"), repo=False) == "~/.../x.md"


def test_a_relative_repository_root_is_treated_as_no_repository(tree):
    result = disclosure.local_path(
        str(tree["repo"] / "src" / "a.py"),
        repo_root="repo", home=tree["home"], base=None, outside=Pseudonyms().path,
    )
    assert result == "path-1"


def test_a_symlink_spelling_is_not_resolved(tree):
    link_to_repo = tree["tmp"] / "link"
    os.symlink(tree["repo"], link_to_repo)
    # Spelled outside the repository: outside, although it points inside.
    assert _call(tree, str(link_to_repo / "src" / "a.py")) == "path-1"
    # Spelled inside the repository: inside, although it points outside.
    inner_link = tree["repo"] / "ln"
    os.symlink(tree["outside"], inner_link)
    assert _call(tree, str(inner_link / "x.py")) == "ln/x.py"


@pytest.mark.parametrize("value", ["", None, 5, "bad\x00path"])
def test_a_value_that_is_not_a_usable_path_is_never_returned_absolute(tree, value):
    result = _call(tree, value)
    assert not str(result).startswith("/")


def test_the_result_is_never_absolute_for_any_spelling(tree):
    for value in (
        "/etc/passwd", "/", "//double//slash", str(tree["repo"].parent),
        str(tree["tmp"] / ".." / ".." / "etc"), "~/x/y", "./x",
    ):
        assert not _call(tree, value).startswith("/"), value
