"""docs/DESIGN.md 4.9 rule 5: the filter-driver guard looks at where a driver
is configured, and `status` never runs a driver.

A clean or process filter driver defined only in the person's own system or
global git config (`git lfs install` writes Git LFS's drivers there) does not
stop `status`; it is switched off for that one call with `-c` arguments, so the
file is compared as stored and the driver never starts (a repository could
otherwise steer it: `.gitattributes` picks the driver, and the driver runs in
the work tree). A driver defined at a scope the repository controls (`local`,
`worktree`, or a file either of those includes) still stops `status`, and so
does any line the guard cannot read and any failed check. Both entry points are
covered: `observe_run` (one cwd for the whole run) and `observe_groups` (one
guarded sequence per repository).
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_postmortem import observe, reader
from agent_postmortem.models import Attempt, AttemptFacts, Run
from tests.n5c1_helpers import Att, build_run, git, init_repo, write_step

LFS_GLOBAL_CONFIG = (
    '[filter "lfs"]\n'
    "\tclean = git-lfs clean -- %f\n"
    "\tsmudge = git-lfs smudge -- %f\n"
    "\tprocess = git-lfs filter-process\n"
    "\trequired = true\n"
)


def _home_with_gitconfig(tmp_path: Path, monkeypatch, text: str) -> Path:
    """Point HOME at a fresh directory whose ~/.gitconfig is `text`: the
    global config git reads, for the test's own git calls and for observe's."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(text, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


def _plant_repo_filter(repo: Path, sentinel: Path, *, scope_args: list[str]) -> None:
    """A filter driver every file in `repo` goes through, configured with
    `git config <scope_args>`, whose clean command would create `sentinel`."""
    (repo / ".gitattributes").write_text("* filter=plant\n", encoding="utf-8")
    git(["config", *scope_args, "filter.plant.clean", f"touch {sentinel}; cat"], repo)


class GitSpy:
    """Records every argv `observe` hands to subprocess.run, then runs it for real."""

    def __init__(self, monkeypatch):
        self.calls: list[list[str]] = []
        real = subprocess.run

        def spy(argv, **kwargs):
            self.calls.append(list(argv))
            return real(argv, **kwargs)

        monkeypatch.setattr(observe.subprocess, "run", spy)

    def statuses(self):
        return [argv for argv in self.calls if "status" in argv]


def _single_cwd_run(repo: Path, written: Path) -> Run:
    """A run with one killed attempt that wrote `written` from `repo`, shaped
    like tests/test_observe.py's own builders."""
    facts = AttemptFacts(
        paths_written=[str(written)], in_flight=[], commits=[], cwds=[str(repo)],
        background_unfinished=0, last_tool=None, seconds_since_last_tool_at_end=None,
    )
    attempt = Attempt(
        agent_id="a0000000000000001", key="v2:k", label="agent", phase="phase",
        agent_type=None, meta={}, transcript_path=None, events=[], first_ts=None,
        last_ts=None, ordinal=1, outcome="killed", usage={}, facts=facts,
    )
    return Run(
        run_id="wf_00000000-e71", path=Path("/tmp/does-not-matter"),
        project_slug="-home-user-project", session_id=None, claude_version=None,
        journal=[], agents=[], attempts=[attempt], started_at=None, ended_at=None,
        diagnostics=[],
    )


def _grouped_run(tmp_path, monkeypatch, suffix: str, repo: Path):
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    return reader.load_run(run_dir)


NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


# --- observe_groups (per repository) -------------------------------------------


def test_a_global_git_lfs_filter_lets_the_group_be_checked(tmp_path, monkeypatch):
    _home_with_gitconfig(tmp_path, monkeypatch, LFS_GLOBAL_CONFIG)
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _grouped_run(tmp_path, monkeypatch, "e72", repo)
    spy = GitSpy(monkeypatch)

    (group,) = observe.observe_groups(run).groups

    assert (group.checked, group.reason) == (True, None)
    assert group.paths[str(repo / "tracked.txt")].state == observe.DIRTY_NOW
    assert len(spy.statuses()) == 1


def _global_driver_home(tmp_path, monkeypatch, sentinel: Path) -> None:
    """A global driver `mine` whose clean command would create `sentinel`."""
    _home_with_gitconfig(
        tmp_path, monkeypatch,
        f'[filter "mine"]\n\tclean = touch {sentinel}; cat\n\tsmudge = cat\n',
    )


def _routed_repo(tmp_path) -> Path:
    """A repository whose attributes route every file through `mine`, with
    tracked.txt changed to the same length so git must compare content."""
    repo = init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("* filter=mine\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    return repo


def test_a_global_driver_the_repository_routes_to_never_runs_and_the_group_is_checked(
    tmp_path, monkeypatch,
):
    sentinel = tmp_path / "global-driver-ran"
    _global_driver_home(tmp_path, monkeypatch, sentinel)
    repo = _routed_repo(tmp_path)
    run = _grouped_run(tmp_path, monkeypatch, "e73", repo)

    (group,) = observe.observe_groups(run).groups

    assert (group.checked, group.reason) == (True, None)
    assert group.paths[str(repo / "tracked.txt")].state == observe.DIRTY_NOW
    assert not sentinel.exists()


def test_observe_run_never_runs_a_global_driver_the_repository_routes_to(
    tmp_path, monkeypatch,
):
    sentinel = tmp_path / "global-driver-ran"
    _global_driver_home(tmp_path, monkeypatch, sentinel)
    repo = _routed_repo(tmp_path)

    result = observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    assert result.checked is True
    assert result.paths["tracked.txt"].state == observe.DIRTY_NOW
    assert not sentinel.exists()


def test_status_switches_off_each_person_driver_right_after_fsmonitor(
    tmp_path, monkeypatch,
):
    _home_with_gitconfig(tmp_path, monkeypatch, LFS_GLOBAL_CONFIG)
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    spy = GitSpy(monkeypatch)

    observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    (status,) = spy.statuses()
    head = status[1:6]
    assert head == ["-C", str(repo), "--no-optional-locks", "-c", "core.fsmonitor=false"]
    switches = status[6:status.index("status")]
    assert len(switches) % 6 == 0
    groups = [switches[i:i + 6] for i in range(0, len(switches), 6)]
    assert [
        "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=",
        "-c", "filter.lfs.required=false",
    ] in groups


def test_a_local_filter_beside_a_global_one_still_refuses_and_never_runs(
    tmp_path, monkeypatch,
):
    _home_with_gitconfig(tmp_path, monkeypatch, LFS_GLOBAL_CONFIG)
    repo = init_repo(tmp_path / "repo")
    sentinel = tmp_path / "planted-filter-ran"
    _plant_repo_filter(repo, sentinel, scope_args=["--local"])
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _grouped_run(tmp_path, monkeypatch, "e74", repo)
    spy = GitSpy(monkeypatch)

    (group,) = observe.observe_groups(run).groups

    assert (group.checked, group.reason) == (False, "guard refused")
    assert spy.statuses() == []
    assert not sentinel.exists()


def test_a_filter_in_a_file_the_local_config_includes_refuses(tmp_path, monkeypatch):
    _home_with_gitconfig(tmp_path, monkeypatch, "")
    repo = init_repo(tmp_path / "repo")
    sentinel = tmp_path / "planted-filter-ran"
    included = tmp_path / "included.gitconfig"
    included.write_text(
        f'[filter "plant"]\n\tclean = touch {sentinel}; cat\n', encoding="utf-8",
    )
    (repo / ".gitattributes").write_text("* filter=plant\n", encoding="utf-8")
    git(["config", "--local", "include.path", str(included)], repo)
    run = _grouped_run(tmp_path, monkeypatch, "e75", repo)
    spy = GitSpy(monkeypatch)

    (group,) = observe.observe_groups(run).groups

    assert (group.checked, group.reason) == (False, "guard refused")
    assert spy.statuses() == []
    assert not sentinel.exists()


def test_a_worktree_scope_filter_refuses(tmp_path, monkeypatch):
    _home_with_gitconfig(tmp_path, monkeypatch, "")
    repo = init_repo(tmp_path / "repo")
    git(["config", "extensions.worktreeConfig", "true"], repo)
    sentinel = tmp_path / "planted-filter-ran"
    _plant_repo_filter(repo, sentinel, scope_args=["--worktree"])
    run = _grouped_run(tmp_path, monkeypatch, "e76", repo)
    spy = GitSpy(monkeypatch)

    (group,) = observe.observe_groups(run).groups

    assert (group.checked, group.reason) == (False, "guard refused")
    assert spy.statuses() == []
    assert not sentinel.exists()


# --- observe_run (one cwd for the whole run) ----------------------------------


def test_observe_run_checks_a_repository_when_only_a_global_lfs_filter_exists(
    tmp_path, monkeypatch,
):
    _home_with_gitconfig(tmp_path, monkeypatch, LFS_GLOBAL_CONFIG)
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    assert result.checked is True
    assert result.paths["tracked.txt"].state == observe.DIRTY_NOW


def test_observe_run_still_refuses_a_local_filter_and_never_runs_it(tmp_path, monkeypatch):
    _home_with_gitconfig(tmp_path, monkeypatch, LFS_GLOBAL_CONFIG)
    repo = init_repo(tmp_path / "repo")
    sentinel = tmp_path / "planted-filter-ran"
    _plant_repo_filter(repo, sentinel, scope_args=["--local"])
    spy = GitSpy(monkeypatch)

    result = observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    assert result.checked is False
    assert "filter" in result.reason
    assert spy.statuses() == []
    assert not sentinel.exists()


def test_the_guard_asks_git_for_each_drivers_scope(tmp_path, monkeypatch):
    _home_with_gitconfig(tmp_path, monkeypatch, LFS_GLOBAL_CONFIG)
    repo = init_repo(tmp_path / "repo")
    spy = GitSpy(monkeypatch)

    observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    config_calls = [argv[1:] for argv in spy.calls if "config" in argv]
    assert config_calls == [[
        "-C", str(repo), "config", "--show-scope", "--get-regexp",
        r"^filter\..*\.(clean|process)$",
    ]]


# --- a failed or unreadable check refuses, at both entry points ---------------


def _done(code: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr="")


FAILED_CHECKS = {
    "exit 128": _done(128, ""),
    "no process": None,
    "exit 0 with no output": _done(0, ""),
    "a line without a tab": _done(0, "global filter.x.clean cat\n"),
    "a trusted scope on a line that is not a filter key": _done(0, "global\tgarbage\n"),
    "a driver name outside the accepted characters": _done(
        0, "global\tfilter.my name.clean cat\n",
    ),
    "a continuation line after a trusted one": _done(
        0, "global\tfilter.x.clean cat\nglobal\tgarbage\n",
    ),
    "an empty continuation line of a multi-line value": _done(
        0, "global\tfilter.x.clean cat\n\n",
    ),
}


def _config_answers(monkeypatch, outcome) -> list[list[str]]:
    """Make the guard's `config` call return `outcome`; every other git call
    runs for real. Returns the list the `status` calls are recorded in."""
    real = observe._run_git
    statuses: list[list[str]] = []

    def fake(git_path, args, env):
        if "config" in args:
            return outcome
        if "status" in args:
            statuses.append(list(args))
        return real(git_path, args, env)

    monkeypatch.setattr(observe, "_run_git", fake)
    return statuses


@pytest.mark.parametrize("case", sorted(FAILED_CHECKS))
def test_observe_run_refuses_on_a_failed_or_unreadable_check(tmp_path, monkeypatch, case):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    statuses = _config_answers(monkeypatch, FAILED_CHECKS[case])

    result = observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    assert result.checked is False
    assert statuses == []


@pytest.mark.parametrize("case", sorted(FAILED_CHECKS))
def test_a_group_refuses_on_a_failed_or_unreadable_check(tmp_path, monkeypatch, case):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _grouped_run(tmp_path, monkeypatch, "e78", repo)
    statuses = _config_answers(monkeypatch, FAILED_CHECKS[case])

    (group,) = observe.observe_groups(run).groups

    assert (group.checked, group.reason) == (False, "guard refused")
    assert statuses == []


ACCEPTED_LINES = {
    "a valueless key": "global\tfilter.x.clean\n",
    "a value holding a Unicode line separator": "global\tfilter.x.clean cat\u2028note\n",
    "a system driver": "system\tfilter.x.process cat\n",
}


@pytest.mark.parametrize("case", sorted(ACCEPTED_LINES))
def test_a_person_driver_line_git_can_print_is_switched_off_not_refused(
    tmp_path, monkeypatch, case,
):
    """git prints one record per line; only `\\n` ends a record, so a value
    holding another Unicode line break is still one line."""
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    statuses = _config_answers(monkeypatch, _done(0, ACCEPTED_LINES[case]))

    result = observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    assert result.checked is True
    (status,) = statuses
    assert "filter.x.clean=" in status


@pytest.mark.parametrize("case", sorted(ACCEPTED_LINES))
def test_a_group_switches_off_a_person_driver_line_git_can_print(tmp_path, monkeypatch, case):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _grouped_run(tmp_path, monkeypatch, "e79", repo)
    statuses = _config_answers(monkeypatch, _done(0, ACCEPTED_LINES[case]))

    (group,) = observe.observe_groups(run).groups

    assert (group.checked, group.reason) == (True, None)
    (status,) = statuses
    assert "filter.x.clean=" in status
