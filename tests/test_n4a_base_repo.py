"""N4a coverage group 5: candidate base and repo (docs/DESIGN.md 4.13.1, C-10).

A base is a lexical candidate: only a leading absolute `cd DIR &&` replaces
the call cwd, and every other cd shape makes the later segments' base unknown.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands

CWD = "/tmp/ws"


def bases(command, cwd=CWD):
    return list(commands.segment_bases(commands.lex(command), cwd))


BASE_ROWS = [
    # no cd: the call cwd for every segment
    ("git commit -m x", CWD, [CWD]),
    ("git add . && git commit -m x", CWD, [CWD, CWD]),
    ("git commit -m x", None, [None]),
    ("git commit -m x", "rel/dir", [None]),
    ("git commit -m x", "", [None]),
    ("git commit -m x", "/tmp/ws/../ws/./sub/", ["/tmp/ws/sub"]),
    # a leading absolute cd replaces the base for the later segments
    ("cd /abs/repo && git commit -m x", "/tmp/other", ["/tmp/other", "/abs/repo"]),
    ("cd /abs/repo && git commit -m x", None, [None, "/abs/repo"]),
    ("cd /abs/repo && git add . && git commit -m x", CWD, [CWD, "/abs/repo", "/abs/repo"]),
    ("cd /a/../b/ && git commit -m x", CWD, [CWD, "/b"]),
    ("cd '/a b' && git commit -m x", CWD, [CWD, "/a b"]),
    # a relative operand gives an unknown base, even with a known cwd
    ("cd sub && git commit -m x", CWD, [CWD, None]),
    ("cd ./sub && git commit -m x", CWD, [CWD, None]),
    ("cd .. && git commit -m x", CWD, [CWD, None]),
    # forms the lexer lists: unknown
    ("cd - && git commit -m x", CWD, [CWD, None]),
    ("cd && git commit -m x", CWD, [CWD, None]),
    ("cd -- && git commit -m x", CWD, [CWD, None]),
    ("cd -- /abs && git commit -m x", CWD, [CWD, None]),
    ("cd -L /abs && git commit -m x", CWD, [CWD, None]),
    ("cd -P /abs && git commit -m x", CWD, [CWD, None]),
    ("cd /a /b && git commit -m x", CWD, [CWD, None]),
    ("cd '' && git commit -m x", CWD, [CWD, None]),
    ("cd /a >/dev/null && git commit -m x", CWD, [CWD, None]),
    # joined by something other than &&
    ("cd /abs ; git commit -m x", CWD, [CWD, None]),
    ("cd /abs || git commit -m x", CWD, [CWD, None]),
    ("cd /abs | git commit -m x", CWD, [CWD, None]),
    ("cd /abs & git commit -m x", CWD, [CWD, None]),
    ("cd /abs\ngit commit -m x", CWD, [CWD, None]),
    # a later cd, pushd, or popd: unknown for every later segment
    ("cd /a && cd /b && git commit -m x", CWD, [CWD, "/a", None]),
    ("git add . && cd /abs && git commit -m x", CWD, [CWD, CWD, None]),
    ("git add . && cd /abs && git add . && git commit -m x", CWD, [CWD, CWD, None, None]),
    ("pushd /abs && git commit -m x", CWD, [CWD, None]),
    ("popd && git commit -m x", CWD, [CWD, None]),
    ("git add . ; cd /abs ; git commit -m x", CWD, [CWD, CWD, None]),
    # opaque commands have no base at all
    ("cd \"$R\" && git commit -m x", CWD, [None, None]),
    ("cd ~/x && git commit -m x", CWD, [None, None]),
    ("cd /abs$X && git commit -m x", CWD, [None, None]),
    ("cd() { :; }; cd /abs && git commit -m x", CWD, [None, None, None, None]),
    ("echo x && cd /abs && git commit -m x $Y", CWD, [None, None, None]),
    # cd alone
    ("cd /abs", CWD, [CWD]),
]


@pytest.mark.parametrize("command,cwd,expected", BASE_ROWS)
def test_candidate_bases(command, cwd, expected):
    assert bases(command, cwd) == expected


def test_cdpath_cannot_change_an_absolute_operand():
    # CDPATH is invisible in the text and only ever applies to a relative
    # operand; the absolute form is the one the grammar trusts as a candidate.
    assert bases("cd /abs && git commit -m x") == [CWD, "/abs"]
    assert bases("cd abs && git commit -m x") == [CWD, None]


# --- git global options ----------------------------------------------------------


def prefix(command_words):
    return commands.git_prefix(command_words)


def test_git_prefix_accepts_only_the_listed_global_options():
    p = prefix(["git", "commit", "-m", "x"])
    assert (p.subcommand, p.args, p.c_dirs, p.pins_location) == ("commit", ("-m", "x"), (), False)
    p = prefix(["git", "-C", "/r", "commit"])
    assert (p.subcommand, p.c_dirs, p.pins_location) == ("commit", ("/r",), False)
    p = prefix(["git", "-C", "/a", "-C", "b", "commit", "-m", "x"])
    assert p.c_dirs == ("/a", "b")
    p = prefix(["git", "-c", "user.name=x", "commit"])
    assert (p.subcommand, p.c_dirs, p.pins_location) == ("commit", (), False)
    p = prefix(["git", "-C", "/r", "-c", "k=v", "-c", "a=b", "-C", "s", "commit"])
    assert (p.subcommand, p.c_dirs) == ("commit", ("/r", "s"))
    p = prefix(["git", "--git-dir=/x/.git", "commit"])
    assert (p.subcommand, p.pins_location) == ("commit", True)
    p = prefix(["git", "--work-tree=/x", "commit"])
    assert (p.subcommand, p.pins_location) == ("commit", True)
    p = prefix(["git", "-C", "", "commit"])
    assert p.c_dirs == ("",)
    p = prefix(["git", "status"])
    assert (p.subcommand, p.args) == ("status", ())


def test_git_prefix_rejects_everything_else():
    for words in (
        ["git"],
        ["git", "-C"],
        ["git", "-C", "/r"],
        ["git", "-c"],
        ["git", "--git-dir", "/x", "commit"],
        ["git", "--work-tree", "/x", "commit"],
        ["git", "--no-pager", "commit"],
        ["git", "-p", "commit"],
        ["git", "-P", "commit"],
        ["git", "--exec-path=/x", "commit"],
        ["git", "--namespace=x", "commit"],
        ["git", "-C/r", "commit"],
        ["git", "-cuser.name=x", "commit"],
        ["git", "--bare", "commit"],
        ["git", "--super-prefix=x", "commit"],
        ["notgit", "commit"],
        [],
    ):
        assert prefix(words) is None, words


GIT_REPO_ROWS = [
    ("/r", (), False, "/r"),
    (None, (), False, None),
    ("/r", ("/x",), False, "/x"),
    ("/r", ("a",), False, "/r/a"),
    ("/r", ("/a", "b"), False, "/a/b"),
    ("/r", ("a", "/b"), False, "/b"),
    ("/r", ("a", "b"), False, "/r/a/b"),
    ("/r", ("..",), False, "/"),
    ("/r/s", ("..",), False, "/r"),
    ("/r", ("",), False, "/r"),
    ("/r", ("/x/../y",), False, "/y"),
    (None, ("a",), False, None),
    (None, ("/a",), False, "/a"),
    (None, ("/a", "b"), False, "/a/b"),
    (None, ("a", "/b"), False, "/b"),
    (None, ("",), False, None),
    ("/r", (), True, None),
    ("/r", ("/x",), True, None),
    (None, (), True, None),
]


@pytest.mark.parametrize("base,c_dirs,pins,expected", GIT_REPO_ROWS)
def test_git_repo(base, c_dirs, pins, expected):
    p = commands.GitPrefix("commit", (), c_dirs, pins)
    assert commands.git_repo(base, p) == expected
