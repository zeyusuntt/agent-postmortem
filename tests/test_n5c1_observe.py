"""N5c-1: per-repository observation (plan S5, S6; DESIGN 4.9, 4.13.1).

`observe.observe_groups(run, now=None)` partitions the risk population's ledger
paths with `ledger.group_by_repo` before any process starts. A refused group (a
contributing ledger is partial) and the `unknown` group start no git process at
all; a checked group runs the full 4.9 guarded sequence with only its own ledger
paths that lie inside the resolved toplevel. The result is a `RunObservation`:
an ordered list of group observations, named repositories in sorted candidate
order, then the `unknown` group last.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta

import pytest

from agent_postmortem import observe, reader
from tests.n5c1_helpers import (
    BASE,
    Att,
    bash_step,
    build_run,
    git,
    init_repo,
    without_filter_neutralizers,
    write_step,
)

CLOSED_REASONS = {
    "refused", "historical", "not a git repository", "git unavailable", "guard refused",
    "outside the work tree",
}


class GitSpy:
    """Records every argv `observe` hands to subprocess.run, then runs it for real."""

    def __init__(self, monkeypatch):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        real = subprocess.run

        def spy(argv, **kwargs):
            self.calls.append(list(argv))
            self.envs.append(dict(kwargs.get("env") or {}))
            return real(argv, **kwargs)

        monkeypatch.setattr(observe.subprocess, "run", spy)

    def statuses(self):
        return [argv for argv in self.calls if "status" in argv]


def _load(tmp_path, monkeypatch, suffix, atts, **kwargs):
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", atts, **kwargs)
    return reader.load_run(run_dir)


def test_a_complete_group_is_checked_with_its_own_paths_and_states(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    clean = repo / "clean.txt"
    clean.write_text("same\n", encoding="utf-8")
    git(["add", "clean.txt"], repo)
    git(["commit", "-q", "-m", "clean"], repo)
    gone = repo / "gone.txt"  # never created: MISSING_NOW
    steps = [
        write_step("t1", repo / "tracked.txt", repo),
        write_step("t2", clean, repo),
        write_step("t3", gone, repo),
    ]
    run = _load(tmp_path, monkeypatch, "a30", [Att(1, steps=steps)])
    result = observe.observe_groups(run)
    assert [group.repo for group in result.groups] == [str(repo)]
    (group,) = result.groups
    assert (group.refused, group.checked, group.reason) == (False, True, None)
    assert group.repo_root == str(repo)
    assert group.head and group.git_version
    assert {path: obs.state for path, obs in group.paths.items()} == {
        str(repo / "tracked.txt"): observe.DIRTY_NOW,
        str(clean): observe.CLEAN_NOW,
        str(gone): observe.MISSING_NOW,
    }
    assert group.unobserved_paths == []
    assert result.checked is True
    assert result.repo_root == str(repo)


def test_paths_carry_their_writing_attempts_and_the_later_write_flag(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    target = repo / "tracked.txt"
    first = Att(1, key="v2:k", journal="failed", steps=[write_step("t1", target, repo)])
    second = Att(2, key="v2:k", journal="failed", steps=[write_step("u1", target, repo)], start=60)
    run = _load(tmp_path, monkeypatch, "a31", [first, second])
    (group,) = observe.observe_groups(run).groups
    obs = group.paths[str(target)]
    assert [(a["agent_id"], a["ordinal"]) for a in obs.attempts] == [
        (run.attempts[0].agent_id, 1), (run.attempts[1].agent_id, 2),
    ]
    assert obs.later_write_observed is True


def test_a_refused_group_starts_no_git_process_at_all(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    steps = [write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make build", repo)]
    run = _load(tmp_path, monkeypatch, "a32", [Att(1, steps=steps)])
    spy = GitSpy(monkeypatch)
    run_git_calls = []
    monkeypatch.setattr(observe, "_run_git", lambda *a, **k: run_git_calls.append(a))
    result = observe.observe_groups(run)
    assert spy.calls == [] and run_git_calls == []
    refused = [group for group in result.groups if group.repo == str(repo)]
    assert len(refused) == 1
    (group,) = refused
    assert (group.refused, group.checked, group.reason) == (True, False, "refused")
    assert group.paths == {} and group.repo_root is None and group.head is None
    assert group.git_version is None
    assert group.unobserved_paths == [str(repo / "tracked.txt")]


def test_the_unknown_group_starts_no_git_process_and_comes_last(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    steps = [write_step("t1", repo / "tracked.txt", repo)]
    shell = Att(2, key="v2:s", steps=[bash_step("u1", "make build", None)], start=40)
    run = _load(tmp_path, monkeypatch, "a33", [Att(1, steps=steps), shell])
    spy = GitSpy(monkeypatch)
    result = observe.observe_groups(run)
    assert [group.repo for group in result.groups] == [str(repo), None]
    unknown = result.groups[-1]
    assert (unknown.refused, unknown.checked, unknown.reason) == (True, False, "refused")
    assert unknown.paths == {} and unknown.unobserved_paths == []
    # the named group is complete and was checked; only its commands ran
    assert all(str(repo) in argv or argv[1:] == ["--version"] for argv in spy.calls)


def test_an_empty_unknown_group_is_left_out(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    run = _load(tmp_path, monkeypatch, "a34", [Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)])])
    result = observe.observe_groups(run)
    assert [group.repo for group in result.groups] == [str(repo)]


def test_no_population_means_no_groups_and_no_process(tmp_path, monkeypatch):
    run = _load(tmp_path, monkeypatch, "a35", [Att(1, journal="completed")])
    spy = GitSpy(monkeypatch)
    result = observe.observe_groups(run)
    assert result.groups == [] and spy.calls == []
    assert result.checked is False and result.repo_root is None


def test_two_repositories_one_checked_one_refused_do_not_contaminate_each_other(
    tmp_path, monkeypatch,
):
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    (repo_a / "tracked.txt").write_text("changed a\n", encoding="utf-8")
    (repo_b / "tracked.txt").write_text("changed b\n", encoding="utf-8")
    clean_att = Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)])
    shell_att = Att(
        2, key="v2:b", start=40,
        steps=[write_step("u1", repo_b / "tracked.txt", repo_b), bash_step("u2", "make", repo_b)],
    )
    run = _load(tmp_path, monkeypatch, "a36", [clean_att, shell_att])
    spy = GitSpy(monkeypatch)
    result = observe.observe_groups(run)
    by_repo = {group.repo: group for group in result.groups}
    assert list(by_repo)[:2] == sorted([str(repo_a), str(repo_b)])
    assert (by_repo[str(repo_a)].checked, by_repo[str(repo_a)].refused) == (True, False)
    assert (by_repo[str(repo_b)].checked, by_repo[str(repo_b)].refused) == (False, True)
    assert by_repo[str(repo_a)].paths[str(repo_a / "tracked.txt")].state == observe.DIRTY_NOW
    assert by_repo[str(repo_b)].paths == {}
    assert by_repo[str(repo_b)].unobserved_paths == [str(repo_b / "tracked.txt")]
    (status,) = spy.statuses()
    assert status[1:3] == ["-C", str(repo_a)]
    assert status[-1] == "tracked.txt"
    assert not any(str(repo_b) in " ".join(argv) for argv in spy.calls)


def test_two_repositories_one_checked_one_whose_rev_parse_fails(tmp_path, monkeypatch):
    repo_a = init_repo(tmp_path / "repo-a")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n", encoding="utf-8")
    (repo_a / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _load(tmp_path, monkeypatch, "a37", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", plain / "f.txt", plain)]),
    ])
    spy = GitSpy(monkeypatch)
    result = observe.observe_groups(run)
    by_repo = {group.repo: group for group in result.groups}
    good, bad = by_repo[str(repo_a)], by_repo[str(plain)]
    assert (good.checked, good.reason) == (True, None)
    assert good.paths[str(repo_a / "tracked.txt")].state == observe.DIRTY_NOW
    assert (bad.checked, bad.refused, bad.reason) == (False, False, "not a git repository")
    assert bad.paths == {} and bad.unobserved_paths == [str(plain / "f.txt")]
    assert len(spy.statuses()) == 1
    assert all(str(repo_a) in argv or str(plain) in argv or argv[1:] == ["--version"]
               for argv in spy.calls)


def test_a_checked_group_runs_the_full_guarded_sequence_with_scrubbed_environment(
    tmp_path, monkeypatch,
):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _load(tmp_path, monkeypatch, "a38", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    spy = GitSpy(monkeypatch)
    observe.observe_groups(run)
    git_path = shutil.which("git")
    assert all(argv[0] == git_path for argv in spy.calls)
    assert [without_filter_neutralizers(argv[1:]) for argv in spy.calls] == [
        ["--version"],
        ["-C", str(repo), "rev-parse", "--show-toplevel"],
        ["-C", str(repo), "rev-parse", "HEAD"],
        ["-C", str(repo), "config", "--show-scope", "--get-regexp",
         r"^filter\..*\.(clean|process)$"],
        ["-C", str(repo), "--no-optional-locks", "-c", "core.fsmonitor=false", "status",
         "--porcelain=v2", "-z", "--no-renames", "--ignore-submodules=all", "--", "tracked.txt"],
    ]
    for env in spy.envs:
        assert set(env) <= {
            "PATH", "HOME", "GIT_OPTIONAL_LOCKS", "GIT_PAGER", "PAGER", "GIT_TERMINAL_PROMPT",
            "LC_ALL", "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH",
        }
        assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["LC_ALL"] == "C"


def test_the_version_check_runs_once_for_the_whole_run_not_per_group(tmp_path, monkeypatch):
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    run = _load(tmp_path, monkeypatch, "a39", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", repo_b / "tracked.txt", repo_b)]),
    ])
    spy = GitSpy(monkeypatch)
    result = observe.observe_groups(run)
    assert [argv[1:] for argv in spy.calls].count(["--version"]) == 1
    assert [group.checked for group in result.groups] == [True, True]
    assert len(spy.statuses()) == 2


def test_a_filter_driver_stops_status_and_names_a_guard(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    git(["config", "filter.x.clean", "cat"], repo)
    run = _load(tmp_path, monkeypatch, "a3a", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    spy = GitSpy(monkeypatch)
    (group,) = observe.observe_groups(run).groups
    assert spy.statuses() == []
    assert (group.checked, group.reason) == (False, "guard refused")
    assert group.paths == {} and group.unobserved_paths == [str(repo / "tracked.txt")]


def test_containment_passes_only_paths_inside_the_toplevel_to_status(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere" / "o.txt"
    outside.parent.mkdir()
    outside.write_text("x\n", encoding="utf-8")
    inside = repo / "tracked.txt"
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _load(tmp_path, monkeypatch, "a3b", [
        Att(1, steps=[write_step("t1", inside, repo), write_step("t2", outside, repo)]),
    ])
    spy = GitSpy(monkeypatch)
    (group,) = observe.observe_groups(run).groups
    (status,) = spy.statuses()
    assert status[status.index("--") + 1:] == ["tracked.txt"]
    assert (group.checked, group.reason) == (True, None)
    assert list(group.paths) == [str(inside)]
    assert group.unobserved_paths == [str(outside)]


def test_every_path_outside_the_toplevel_means_no_status_and_the_closed_reason(
    tmp_path, monkeypatch,
):
    repo = init_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere" / "o.txt"
    outside.parent.mkdir()
    outside.write_text("x\n", encoding="utf-8")
    run = _load(tmp_path, monkeypatch, "a3c", [
        Att(1, steps=[write_step("t1", outside, repo)]),
    ])
    spy = GitSpy(monkeypatch)
    (group,) = observe.observe_groups(run).groups
    assert spy.statuses() == []
    assert (group.checked, group.reason) == (False, "outside the work tree")
    assert group.paths == {} and group.unobserved_paths == [str(outside)]


@pytest.mark.parametrize("seconds,expected", [(71 * 3600, True), (72 * 3600, True), (72 * 3600 + 1, False)])
def test_a_run_older_than_72_hours_is_historical_with_no_process(
    tmp_path, monkeypatch, seconds, expected,
):
    repo = init_repo(tmp_path / "repo")
    run = _load(tmp_path, monkeypatch, "a3d", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    now = run.ended_at + timedelta(seconds=seconds)
    spy = GitSpy(monkeypatch)
    (group,) = observe.observe_groups(run, now=now).groups
    if expected:
        assert group.checked is True
    else:
        assert (group.checked, group.reason) == (False, "historical")
        assert spy.calls == []


def test_a_refused_group_keeps_its_refused_reason_in_a_historical_run(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    steps = [write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo)]
    run = _load(tmp_path, monkeypatch, "a3e", [Att(1, steps=steps)])
    spy = GitSpy(monkeypatch)
    result = observe.observe_groups(run, now=BASE + timedelta(days=30))
    assert {group.reason for group in result.groups} == {"refused"}
    assert spy.calls == []


def test_a_missing_candidate_directory_is_not_a_git_repository_with_no_process(
    tmp_path, monkeypatch,
):
    ghost = tmp_path / "vanished"
    run = _load(tmp_path, monkeypatch, "a3f", [
        Att(1, steps=[write_step("t1", ghost / "f.txt", ghost)]),
    ])
    spy = GitSpy(monkeypatch)
    (group,) = observe.observe_groups(run).groups
    assert (group.checked, group.reason) == (False, "not a git repository")
    assert spy.calls == [] and group.unobserved_paths == [str(ghost / "f.txt")]


def test_git_missing_from_path_is_git_unavailable(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    run = _load(tmp_path, monkeypatch, "a40", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    monkeypatch.setattr(observe.shutil, "which", lambda name: None)
    spy = GitSpy(monkeypatch)
    (group,) = observe.observe_groups(run).groups
    assert (group.checked, group.reason) == (False, "git unavailable")
    assert spy.calls == []


@pytest.mark.parametrize("version", ["git version 2.35.9\n", "nonsense\n"])
def test_an_old_or_unreadable_git_version_is_git_unavailable(tmp_path, monkeypatch, version):
    repo = init_repo(tmp_path / "repo")
    run = _load(tmp_path, monkeypatch, "a41", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])

    def fake(argv, **kwargs):
        assert argv[1:] == ["--version"], argv
        return subprocess.CompletedProcess(argv, 0, stdout=version, stderr="")

    monkeypatch.setattr(observe.subprocess, "run", fake)
    (group,) = observe.observe_groups(run).groups
    assert (group.checked, group.reason) == (False, "git unavailable")


def test_a_head_that_cannot_be_read_is_a_refused_guard(tmp_path, monkeypatch):
    repo = tmp_path / "empty"
    repo.mkdir()
    git(["init", "-q"], repo)  # no commit: rev-parse HEAD fails
    run = _load(tmp_path, monkeypatch, "a42", [
        Att(1, steps=[write_step("t1", repo / "new.txt", repo)]),
    ])
    (group,) = observe.observe_groups(run).groups
    assert (group.checked, group.reason) == (False, "guard refused")


def test_a_process_that_cannot_start_never_escapes_as_an_exception(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    run = _load(tmp_path, monkeypatch, "a43", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])

    def boom(argv, **kwargs):
        raise OSError("no process for you")

    monkeypatch.setattr(observe.subprocess, "run", boom)
    (group,) = observe.observe_groups(run).groups
    assert group.checked is False and group.reason in CLOSED_REASONS


def test_groups_are_partitioned_before_any_process_starts(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    run = _load(tmp_path, monkeypatch, "a44", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    order: list[str] = []
    real_group = observe.ledger.group_by_repo

    def tracked_group(run_):
        order.append("partition")
        return real_group(run_)

    monkeypatch.setattr(observe.ledger, "group_by_repo", tracked_group)
    real_run = subprocess.run

    def tracked_run(argv, **kwargs):
        order.append("process")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(observe.subprocess, "run", tracked_run)
    observe.observe_groups(run)
    assert order[0] == "partition" and "process" in order


def test_every_reason_is_in_the_closed_list(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    plain = tmp_path / "plain"
    plain.mkdir()
    run = _load(tmp_path, monkeypatch, "a45", [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", plain / "f", plain)]),
        Att(3, key="v2:c", start=80, steps=[bash_step("w1", "make", repo)]),
    ])
    for group in observe.observe_groups(run).groups:
        assert group.reason is None or group.reason in CLOSED_REASONS
        for obs in group.paths.values():
            assert obs.reason is None or obs.reason in CLOSED_REASONS


def test_the_environment_is_not_inherited_beyond_the_scrubbed_set(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "somewhere"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    run = _load(tmp_path, monkeypatch, "a46", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    spy = GitSpy(monkeypatch)
    (group,) = observe.observe_groups(run).groups
    assert group.checked is True
    assert all("GIT_DIR" not in env and "GIT_CONFIG_COUNT" not in env for env in spy.envs)
