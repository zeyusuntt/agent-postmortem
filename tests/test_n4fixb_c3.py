"""Fix B, C3: one `possible` entry per targetless shell call. `repo` is the single
candidate when every commit segment yields exactly one distinct candidate, else
null (docs/DESIGN.md 4.13.1; .reviews/v0.3/N4-dispositions.md C3)."""

from __future__ import annotations

from tests.n4b2_helpers import CWD, NAME, one_call
from tests.test_n4b2_rows import entries_of


def shell(tmp_path, command, **kwargs):
    return one_call(tmp_path, "Bash", {"command": command}, "ok", **kwargs)


def test_two_commit_repositories_give_one_entry_with_a_null_repo(tmp_path):
    call = shell(tmp_path, "git -C /tmp/b commit -m x && git -C /tmp/a commit -m y")
    assert entries_of(call) == [
        {"effect": "possible", "basis": "opaque_command", "completion": "success", "path": None,
         "repo": None, "line": f"{NAME}:2"}
    ]


def test_a_known_and_an_unknown_commit_repository_give_one_null_repo_entry(tmp_path):
    call = shell(tmp_path, "git -C /tmp/b commit -m x && git --git-dir=/x commit -m y")
    assert [e["repo"] for e in entries_of(call)] == [None]
    assert len(entries_of(call)) == 1


def test_an_unknown_then_a_known_commit_repository_give_one_null_repo_entry(tmp_path):
    call = shell(tmp_path, "git --git-dir=/x commit -m y && git -C /tmp/b commit -m x")
    assert [e["repo"] for e in entries_of(call)] == [None]


def test_three_commit_repositories_give_one_entry(tmp_path):
    call = shell(
        tmp_path,
        "git -C /tmp/a commit -m 1 && git -C /tmp/b commit -m 2 ; git -C /tmp/c commit -m 3",
    )
    assert [e["repo"] for e in entries_of(call)] == [None]


def test_a_cd_then_a_commit_elsewhere_and_a_commit_here_give_one_null_entry(tmp_path):
    call = shell(tmp_path, "git commit -m 1 ; git -C /tmp/b commit -m 2")
    assert [e["repo"] for e in entries_of(call)] == [None]


def test_the_same_repository_twice_is_still_one_entry_at_that_repository(tmp_path):
    call = shell(tmp_path, "git -C /tmp/b commit -m x && git -C /tmp/b commit -m y")
    assert [e["repo"] for e in entries_of(call)] == ["/tmp/b"]


def test_one_commit_segment_keeps_its_candidate(tmp_path):
    call = shell(tmp_path, "cd /tmp/other && git commit -m x")
    assert [e["repo"] for e in entries_of(call)] == ["/tmp/other"]


def test_the_same_candidate_reached_two_ways_is_one_distinct_candidate(tmp_path):
    call = shell(tmp_path, "git commit -m x && git -C /tmp/ws commit -m y")
    assert [e["repo"] for e in entries_of(call)] == [CWD]


def test_a_call_with_no_commit_segment_keeps_the_calls_candidate(tmp_path):
    call = shell(tmp_path, "git -C /tmp/b status && make")
    assert [e["repo"] for e in entries_of(call)] == [CWD]


def test_a_single_commit_with_an_unknown_repository_is_null(tmp_path):
    call = shell(tmp_path, "git --git-dir=/x commit -m y")
    assert [e["repo"] for e in entries_of(call)] == [None]
