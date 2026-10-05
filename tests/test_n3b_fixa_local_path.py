"""N3b fix A, S11: `local_path` spellings. A leading `//` is collapsed to `/`
before the containment rule, a `scheme:` prefix (`file://`) counts as
outside, and `~/` is expanded against the context's home."""

from __future__ import annotations

import pytest

from agent_postmortem import disclosure
from agent_postmortem.redact import Pseudonyms


def _call(value, *, repo="/srv/work/repo", home="/srv/people/tester", base="/srv/work/repo"):
    names = Pseudonyms()
    return disclosure.local_path(
        value, repo_root=repo, home=home, base=base, outside=names.path,
    )


def test_a_leading_double_slash_is_one_slash():
    assert _call("//srv/work/repo/src/x.py") == "src/x.py"


def test_a_leading_double_slash_on_the_root_is_dot():
    assert _call("//srv/work/repo") == "."


def test_a_leading_double_slash_inside_home():
    assert _call("//srv/people/tester/notes/todo.md") == "~/.../todo.md"


def test_a_triple_slash_is_left_to_normpath():
    assert _call("///srv/work/repo/src/x.py") == "src/x.py"


@pytest.mark.parametrize("value", [
    "file:///srv/work/repo/x.py",
    "file://srv/work/repo/x.py",
    "https://example.org/a/b",
    "s3:bucket/key",
])
def test_a_scheme_prefix_is_outside(value):
    assert _call(value) == "path-1"


def test_a_tilde_slash_path_is_expanded_against_home():
    assert _call("~/client/notes.md") == "~/.../notes.md"


def test_a_bare_tilde_is_home():
    assert _call("~", repo=None) == "~"


def test_a_tilde_slash_path_into_the_repository_is_repo_relative():
    assert _call("~/work/x.py", repo="/srv/people/tester/work", home="/srv/people/tester") == "x.py"


def test_a_tilde_with_no_home_is_outside():
    assert _call("~/x/y.md", home=None) == "path-1"


def test_a_user_tilde_is_not_expanded():
    assert _call("~other/x.md") == "~other/x.md"
