"""Fix B, S3: a file-tool ledger path is the lexical normalization of the input
path (docs/DESIGN.md 4.13.1 Paths (D7); .reviews/v0.3/N4-dispositions.md S3).

cwd is /tmp/ws unless a row says otherwise. Every row asserts the whole entry.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands
from tests.n4b2_helpers import CWD, NAME, one_call
from tests.test_n4b2_rows import entries_of, entry, reasons_of

FIELD = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
         "NotebookEdit": "notebook_path"}

NORMALIZED = [
    ("/tmp/ws/../x/./y", "/tmp/x/y"),
    ("/tmp/ws//dup", "/tmp/ws/dup"),
    ("//tmp/ws/a.py", "/tmp/ws/a.py"),
    ("///tmp/ws/a.py", "/tmp/ws/a.py"),
    ("/tmp/ws/dir/", "/tmp/ws/dir"),
    ("rel/file.py", "/tmp/ws/rel/file.py"),
    ("./rel/../file.py", "/tmp/ws/file.py"),
    ("../up.py", "/tmp/up.py"),
]


@pytest.mark.parametrize("tool", sorted(FIELD))
@pytest.mark.parametrize("given,expected", NORMALIZED)
def test_a_file_tool_path_is_normalized(tmp_path, tool, given, expected):
    call = one_call(tmp_path, tool, {FIELD[tool]: given}, "ok")
    assert entries_of(call) == [
        entry("observed", "tool_result", "success", expected, CWD, f"{NAME}:2")
    ]
    assert reasons_of(call) == frozenset()


def test_an_errored_relative_path_is_attempted_at_the_joined_path(tmp_path):
    call = one_call(tmp_path, "Edit", {"file_path": "a/b.py"}, "boom", is_error=True)
    assert entries_of(call) == [
        entry("attempted", "tool_result", "error", "/tmp/ws/a/b.py", CWD, f"{NAME}:2")
    ]


def test_an_already_clean_absolute_path_is_unchanged(tmp_path):
    call = one_call(tmp_path, "Write", {"file_path": "/tmp/ws/a.py"}, "ok")
    assert [e["path"] for e in entries_of(call)] == ["/tmp/ws/a.py"]


@pytest.mark.parametrize("tool", sorted(FIELD))
def test_a_relative_path_with_no_call_cwd_is_null_with_a_shape_reason(tmp_path, tool):
    call = one_call(tmp_path, tool, {FIELD[tool]: "rel/file.py"}, "ok", cwd=None)
    assert entries_of(call) == [
        entry("observed", "tool_result", "success", None, None, f"{NAME}:2")
    ]
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


def test_a_relative_path_with_a_relative_call_cwd_is_null_with_a_shape_reason(tmp_path):
    call = one_call(tmp_path, "Edit", {"file_path": "rel/file.py"}, "ok", cwd="sub/dir")
    assert [e["path"] for e in entries_of(call)] == [None]
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


@pytest.mark.parametrize("tool", sorted(FIELD))
def test_an_empty_path_is_null_with_a_shape_reason(tmp_path, tool):
    call = one_call(tmp_path, tool, {FIELD[tool]: ""}, "ok")
    assert entries_of(call) == [
        entry("observed", "tool_result", "success", None, CWD, f"{NAME}:2")
    ]
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


def test_an_empty_path_with_no_cwd_is_null_with_a_shape_reason(tmp_path):
    call = one_call(tmp_path, "Write", {"file_path": ""}, "ok", cwd=None)
    assert [e["path"] for e in entries_of(call)] == [None]
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


def test_a_missing_field_stays_null_with_a_shape_reason(tmp_path):
    call = one_call(tmp_path, "Write", {}, "ok")
    assert [e["path"] for e in entries_of(call)] == [None]
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


def test_a_joined_path_over_1024_characters_is_null_and_truncated(tmp_path):
    cwd = "/tmp/" + "d" * 1000
    call = one_call(tmp_path, "Edit", {"file_path": "f" * 100}, "ok", cwd=cwd)
    assert [e["path"] for e in entries_of(call)] == [None]
    assert reasons_of(call) == frozenset({"truncated_field"})


def test_a_normalized_path_of_exactly_1024_characters_is_kept(tmp_path):
    cwd = "/tmp/" + "d" * 1000
    name = "f" * (1024 - len(cwd) - 1)
    call = one_call(tmp_path, "Edit", {"file_path": name}, "ok", cwd=cwd)
    (only,) = entries_of(call)
    assert only["path"] == f"{cwd}/{name}" and len(only["path"]) == 1024
    assert reasons_of(call) == frozenset()


def test_a_raw_path_over_1024_characters_stays_null_and_truncated(tmp_path):
    call = one_call(tmp_path, "Edit", {"file_path": "/tmp/" + "a" * 1100}, "ok")
    assert [e["path"] for e in entries_of(call)] == [None]
    assert reasons_of(call) == frozenset({"truncated_field"})


def test_a_raw_path_over_1024_that_normalizes_shorter_is_still_truncated(tmp_path):
    raw = "/tmp/ws/" + "x/../" * 220 + "a.py"
    assert len(raw) > 1024
    call = one_call(tmp_path, "Edit", {"file_path": raw}, "ok")
    assert [e["path"] for e in entries_of(call)] == [None]
    assert reasons_of(call) == frozenset({"truncated_field"})


# --- the shell side spells a leading `//` the same way ----------------------------------


def test_a_redirect_target_with_a_leading_double_slash_collapses():
    lexed = commands.lex("echo x > //tmp/ws/out")
    match = commands.match_write_grammar(lexed, "/tmp/ws")
    assert [t.path for t in match.targets] == ["/tmp/ws/out"]


def test_a_double_slash_call_cwd_collapses_for_the_repo():
    lexed = commands.lex("echo x > out")
    match = commands.match_write_grammar(lexed, "//tmp/ws")
    assert [(t.path, t.repo) for t in match.targets] == [("/tmp/ws/out", "/tmp/ws")]


def test_a_double_slash_dash_c_collapses_for_the_commit_repo():
    facts = commands.commit_facts("git -C //tmp/b commit -m x", "/tmp/ws")
    assert [seg.repo for seg in facts.segments] == ["/tmp/b"]
