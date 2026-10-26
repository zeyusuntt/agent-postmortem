"""Tests for observe.py: read-only repo observation of paths written by a
killed/failed/superseded attempt (docs/DESIGN.md 4.9, unit M2b).

Every git repository these tests use is a throwaway one built under
pytest's tmp_path with the real system git via subprocess -- never a
checked-in fixture, and never the project's own repository. Run/Attempt/
AttemptFacts objects are built directly in memory for the pure
git-behavior tests (observe_run only ever reads facts.paths_written,
facts.in_flight, facts.cwds, outcome, agent_id, ordinal, and run.ended_at,
so a full transcript is unnecessary machinery there); the CLI end-to-end
tests build a real workflow run directory at test time instead, the same
approach tests/test_attempt_facts.py uses for its over-2048-char-command
case, so no checked-in fixture ever needs a real absolute path.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, observe
from agent_postmortem.models import Attempt, AttemptFacts, Run
from tests.n5c1_helpers import without_filter_neutralizers

OBSERVE_PATH = Path(observe.__file__)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- in-memory Run/Attempt/AttemptFacts builders ---------------------------


def _facts(
    paths_written=(),
    in_flight=(),
    cwds=(),
) -> AttemptFacts:
    return AttemptFacts(
        paths_written=list(paths_written),
        in_flight=list(in_flight),
        commits=[],
        cwds=list(cwds),
        background_unfinished=0,
        last_tool=None,
        seconds_since_last_tool_at_end=None,
    )


def _attempt(
    *,
    agent_id: str,
    key: str | None,
    ordinal: int,
    outcome: str,
    facts: AttemptFacts | None,
) -> Attempt:
    return Attempt(
        agent_id=agent_id,
        key=key,
        label="agent",
        phase="phase",
        agent_type=None,
        meta={},
        transcript_path=None,
        events=[],
        first_ts=None,
        last_ts=None,
        ordinal=ordinal,
        outcome=outcome,
        usage={},
        facts=facts,
    )


def _run(attempts: list[Attempt], *, ended_at: datetime | None = None) -> Run:
    return Run(
        run_id="R1",
        path=Path("/tmp/does-not-matter"),
        project_slug="-home-user-project",
        session_id=None,
        claude_version=None,
        journal=[],
        agents=[],
        attempts=attempts,
        started_at=None,
        ended_at=ended_at,
        diagnostics=[],
    )


def _killed_attempt(cwd: str, *, agent_id="a0000000000000001", key="v2:k", ordinal=1,
                     paths_written=(), in_flight=(), outcome="killed") -> Attempt:
    return _attempt(
        agent_id=agent_id, key=key, ordinal=ordinal, outcome=outcome,
        facts=_facts(paths_written=paths_written, in_flight=in_flight, cwds=[cwd]),
    )


# --- throwaway git repo builder ---------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(repo: Path) -> Path:
    """A fresh repo at `repo` with one commit holding tracked.txt."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "tracked.txt"], repo)
    _git(["commit", "-q", "-m", "initial"], repo)
    return repo


# --- rule 1: staleness -------------------------------------------------------


def test_run_ended_more_than_72h_ago_is_not_checked_and_starts_no_process(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    run = _run(
        [_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])],
        ended_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    result = observe.observe_run(run, now=datetime(2020, 1, 5, tzinfo=timezone.utc))
    assert result.checked is False
    assert result.reason == "historical: run older than 72 hours"
    assert result.paths == {}


def test_run_ended_under_72h_ago_is_checked(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    run = _run(
        [_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])],
        ended_at=datetime(2020, 1, 1, 10, tzinfo=timezone.utc),
    )
    result = observe.observe_run(run, now=datetime(2020, 1, 1, 20, tzinfo=timezone.utc))
    assert result.checked is True


def test_run_with_no_ended_at_is_never_stale(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    run = _run(
        [_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])],
        ended_at=None,
    )
    result = observe.observe_run(run, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True


# --- rule 2: candidates ------------------------------------------------------


def test_no_candidate_paths_is_not_checked(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    run = _run([_killed_attempt(str(repo), paths_written=[], in_flight=[])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is False
    assert result.reason == "no paths written by a killed or failed attempt"


def test_only_a_completed_attempts_paths_are_never_candidates(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    completed = _attempt(
        agent_id="a0000000000000002", key="v2:c", ordinal=1, outcome="completed",
        facts=_facts(paths_written=[str(repo / "tracked.txt")], cwds=[str(repo)]),
    )
    run = _run([completed])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is False
    assert result.reason == "no paths written by a killed or failed attempt"


@pytest.mark.parametrize("outcome", ["killed", "failed", "superseded"])
def test_each_candidate_outcome_is_observed(tmp_path, outcome):
    repo = _init_repo(tmp_path / "repo")
    attempt = _killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")], outcome=outcome)
    run = _run([attempt])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True


# --- rule 3: repo directory ---------------------------------------------------


def test_cwd_not_found_is_not_checked():
    run = _run([_killed_attempt("/no/such/directory/at/all", paths_written=["/no/such/directory/at/all/x.txt"])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is False
    assert result.reason == "cwd not found"


def test_most_common_cwd_wins_ties_broken_by_first_seen(tmp_path):
    repo_a = _init_repo(tmp_path / "repo_a")
    repo_b = _init_repo(tmp_path / "repo_b")
    # repo_a appears on two attempts, repo_b on one -- repo_a must win.
    attempt1 = _killed_attempt(
        str(repo_a), agent_id="a0000000000000001", key="v2:k1", ordinal=1,
        paths_written=[str(repo_a / "tracked.txt")],
    )
    attempt2 = _killed_attempt(
        str(repo_a), agent_id="a0000000000000002", key="v2:k2", ordinal=1,
        paths_written=[str(repo_a / "tracked.txt")],
    )
    attempt3 = _killed_attempt(
        str(repo_b), agent_id="a0000000000000003", key="v2:k3", ordinal=1,
        paths_written=[str(repo_b / "tracked.txt")],
    )
    run = _run([attempt1, attempt2, attempt3])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True
    assert result.repo_root == str(repo_a.resolve())


# --- rule 7: git status states -----------------------------------------------


def test_tracked_modified_unstaged_file_is_dirty_now(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True
    assert result.paths["tracked.txt"].state == observe.DIRTY_NOW


def test_staged_file_is_dirty_now(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    _git(["add", "tracked.txt"], repo)
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["tracked.txt"].state == observe.DIRTY_NOW


def test_untracked_file_is_dirty_now(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "new.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["new.txt"].state == observe.DIRTY_NOW


def test_clean_file_is_clean_now(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["tracked.txt"].state == observe.CLEAN_NOW


def test_missing_file_is_missing_now(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "never-existed.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["never-existed.txt"].state == observe.MISSING_NOW


def test_parse_porcelain_v2_handles_every_record_type_including_rename():
    # Built by hand rather than produced by real git (--no-renames means git
    # itself never emits a type '2' record), so the parser's handling of the
    # rename shape (two NUL-separated paths per record) is still exercised
    # (docs/DESIGN.md 4.9 rule 7).
    raw = "\0".join([
        "1 .M N... 100644 100644 100644 " + "0" * 40 + " " + "0" * 40 + " modified.txt",
        "2 R. N... 100644 100644 100644 " + "0" * 40 + " " + "0" * 40 + " R100 new-name.txt",
        "old-name.txt",
        "? untracked.txt",
        "u UU N... 100644 100644 100644 100644 " + "0" * 40 + " " + "0" * 40 + " " + "0" * 40 + " conflict.txt",
    ]) + "\0"
    dirty = observe._parse_porcelain_v2(raw)
    assert dirty == {"modified.txt", "new-name.txt", "untracked.txt", "conflict.txt"}


# --- rule 6: containment ------------------------------------------------------


def test_path_outside_the_work_tree_is_unknown_and_not_passed_to_git(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere" / "file.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("x\n", encoding="utf-8")
    run = _run([_killed_attempt(str(repo), paths_written=[str(outside)])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True
    observation = result.paths[str(outside)]
    assert observation.state == observe.UNKNOWN
    assert observation.reason == "outside the work tree"


def test_relative_dotdot_escape_is_unknown_and_not_passed_to_git(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    escaping = "../escape.txt"
    run = _run([_killed_attempt(str(repo), paths_written=[escaping])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True
    observation = result.paths[escaping]
    assert observation.state == observe.UNKNOWN
    assert observation.reason == "outside the work tree"


# --- rule 5: guards, in order --------------------------------------------------


def test_git_not_on_path_is_not_checked(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(observe.shutil, "which", lambda name: None)
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is False
    assert "git" in result.reason
    assert "PATH" in result.reason


def _write_fake_git(fakebin: Path, script_body: str) -> None:
    fakebin.mkdir(parents=True, exist_ok=True)
    fake_git = fakebin / "git"
    fake_git.write_text(f"#!{sys.executable}\n{script_body}", encoding="utf-8")
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_git_older_than_2_36_is_not_checked(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    fakebin = tmp_path / "fakebin"
    _write_fake_git(fakebin, "print('git version 2.30.0')\n")
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is False
    assert "2.36" in result.reason or "old" in result.reason
    assert result.git_version == "git version 2.30.0"


def test_unparsable_git_version_is_not_checked(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    fakebin = tmp_path / "fakebin"
    _write_fake_git(fakebin, "print('nonsense output')\n")
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is False


def test_filter_driver_configured_is_not_checked_and_status_never_runs(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _git(["config", "filter.foo.clean", "cat"], repo)
    logfile = tmp_path / "argv.log"
    real_git = shutil.which("git")
    fakebin = tmp_path / "fakebin"
    _write_fake_git(
        fakebin,
        (
            "import json, sys, subprocess\n"
            f"with open({str(logfile)!r}, 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + chr(10))\n"
            f"result = subprocess.run([{real_git!r}] + sys.argv[1:], capture_output=True)\n"
            "sys.stdout.buffer.write(result.stdout)\n"
            "sys.stderr.buffer.write(result.stderr)\n"
            "sys.exit(result.returncode)\n"
        ),
    )
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")

    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))

    assert result.checked is False
    assert "filter" in result.reason
    recorded = [json.loads(line) for line in logfile.read_text().splitlines()]
    assert not any("status" in argv for argv in recorded)


def test_directory_that_is_not_a_git_repository_is_not_checked(tmp_path):
    plain_dir = tmp_path / "not_a_repo"
    plain_dir.mkdir()
    run = _run([_killed_attempt(str(plain_dir), paths_written=[str(plain_dir / "x.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is False
    assert "repo" in result.reason


# --- rule 4: exact argument lists and exact environment -----------------------


_SELECTED_ENV_VARS = (
    "PATH", "HOME", "GIT_OPTIONAL_LOCKS", "GIT_PAGER", "PAGER",
    "GIT_TERMINAL_PROMPT", "LC_ALL", "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH",
)

# The fake git records argv plus only this *selected* set of variables
# (never a full os.environ dump): a spawned interpreter's own runtime start-up
# can inject environment variables of its own that were never part of what
# observe.py actually passed (on macOS, for example, a Python child process
# frequently gains a __CF_USER_TEXT_ENCODING entry from CoreFoundation that
# has nothing to do with the parent's env= argument). Selected-variable
# recording sidesteps that noise; test_build_env_returns_exactly_the_required_
# variables below is what proves observe.py's own env dict has no extras.
_FAKE_GIT_PROXY = """\
import json, os, subprocess, sys

selected = {{name: os.environ.get(name) for name in {selected!r}}}
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": selected}}) + chr(10))

result = subprocess.run([{real!r}] + sys.argv[1:], env=os.environ.copy(), capture_output=True)
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
"""


def test_build_env_returns_exactly_the_required_variables(monkeypatch):
    """The dict observe._build_env() constructs is exactly what is later
    handed to subprocess.run's env= parameter (docs/DESIGN.md 4.9 rule 4):
    testing this pure function directly is what proves 'contains exactly
    the listed variables', since reading a spawned child's own os.environ
    cannot be trusted not to have gained extra entries from its own
    runtime start-up (see _FAKE_GIT_PROXY's docstring above)."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/user")
    env = observe._build_env()
    assert set(env) == set(_SELECTED_ENV_VARS)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/user"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_PAGER"] == "cat"
    assert env["PAGER"] == "cat"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["LC_ALL"] == "C"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_NO_LAZY_FETCH"] == "1"


def test_build_env_omits_home_when_the_parent_process_has_none(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("HOME", raising=False)
    env = observe._build_env()
    assert "HOME" not in env
    assert set(env) == set(_SELECTED_ENV_VARS) - {"HOME"}


def test_recorded_argv_matches_the_five_templates_and_selected_env_vars_are_exact(
    tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    logfile = tmp_path / "argv.log"
    real_git = shutil.which("git")
    fakebin = tmp_path / "fakebin"
    _write_fake_git(
        fakebin,
        _FAKE_GIT_PROXY.format(selected=list(_SELECTED_ENV_VARS), log=str(logfile), real=real_git),
    )
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")

    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True

    records = [json.loads(line) for line in logfile.read_text().splitlines()]
    assert records, "the fake git was never invoked"

    templates_seen = set()
    for record in records:
        argv = without_filter_neutralizers(record["argv"])
        if argv == ["--version"]:
            templates_seen.add("version")
        elif argv == ["-C", str(repo), "rev-parse", "--show-toplevel"]:
            templates_seen.add("toplevel")
        elif argv == ["-C", str(repo), "rev-parse", "HEAD"]:
            templates_seen.add("head")
        elif argv == [
            "-C", str(repo), "config", "--show-scope", "--get-regexp",
            r"^filter\..*\.(clean|process)$",
        ]:
            templates_seen.add("config")
        elif argv[:8] == [
            "-C", str(repo), "--no-optional-locks", "-c", "core.fsmonitor=false",
            "status", "--porcelain=v2", "-z",
        ]:
            assert argv[8:11] == ["--no-renames", "--ignore-submodules=all", "--"]
            assert argv[11:] == ["tracked.txt"]
            templates_seen.add("status")
        else:
            raise AssertionError(f"argv does not match any of the five templates: {argv}")

        env = record["env"]
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert env["GIT_PAGER"] == "cat"
        assert env["PAGER"] == "cat"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["LC_ALL"] == "C"
        assert env["PATH"] is not None
        assert str(fakebin) in env["PATH"]

    assert templates_seen == {"version", "toplevel", "head", "config", "status"}


def test_git_executable_is_resolved_once_and_the_absolute_path_is_used_in_every_argv(
    tmp_path, monkeypatch,
):
    """docs/DESIGN.md 4.9 rule 4 (release finding F1): observe_run resolves
    git with shutil.which("git") exactly once and passes that same absolute
    path as argv[0] on every subsequent call -- never the bare string "git".

    This patches subprocess.run directly instead of shelling out to a real
    or fake git binary: a shebang script's own sys.argv[0] is always the
    on-disk path the kernel resolved to run it, regardless of what string
    the parent process's argv[0] actually was (verified experimentally), so
    a spawned fake git can never prove which literal argv[0] observe.py
    passed. Only a direct spy on subprocess.run can."""
    repo = _init_repo(tmp_path / "repo")
    resolved = "/resolved/fake/git"
    which_calls: list[str] = []
    run_calls: list[list[str]] = []

    def fake_which(name):
        which_calls.append(name)
        return resolved

    def fake_run(argv, **kwargs):
        run_calls.append(list(argv))
        cmd = argv[1:]
        if cmd == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if cmd[-2:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{repo}\n", stderr="")
        if cmd[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{'a' * 40}\n", stderr="")
        if "config" in cmd:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        if "status" in cmd:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(observe.shutil, "which", fake_which)
    monkeypatch.setattr(observe.subprocess, "run", fake_run)

    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True

    assert which_calls.count("git") == 1, which_calls
    assert run_calls, "subprocess.run was never called"
    assert all(argv[0] == resolved for argv in run_calls), run_calls
    assert all(argv[0] != "git" for argv in run_calls)


def test_filter_check_is_the_call_immediately_before_status_with_nothing_between(
    tmp_path, monkeypatch,
):
    """docs/DESIGN.md 4.9 (release finding F1): the clean/process-filter
    guard must be the last subprocess call before `status`, with no other
    git invocation between them -- this is what closes (almost all of) the
    race window a repository-controlled filter driver could otherwise
    exploit between the guard and the command it guards."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    logfile = tmp_path / "argv-order.log"
    real_git = shutil.which("git")
    fakebin = tmp_path / "fakebin"
    _write_fake_git(
        fakebin,
        _FAKE_GIT_PROXY.format(selected=list(_SELECTED_ENV_VARS), log=str(logfile), real=real_git),
    )
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")

    run = _run([_killed_attempt(str(repo), paths_written=[str(repo / "tracked.txt")])])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True

    records = [json.loads(line) for line in logfile.read_text().splitlines()]
    order = []
    for record in records:
        argv = without_filter_neutralizers(record["argv"])
        if argv == ["--version"]:
            order.append("version")
        elif argv[:3] == ["-C", str(repo), "config"]:
            order.append("config")
        elif argv[:8] == [
            "-C", str(repo), "--no-optional-locks", "-c", "core.fsmonitor=false",
            "status", "--porcelain=v2", "-z",
        ]:
            order.append("status")
        elif argv[:3] == ["-C", str(repo), "rev-parse"]:
            order.append("rev-parse")
    assert order[-2:] == ["config", "status"], order


# --- rule 9: no writes ---------------------------------------------------------


def test_observe_run_makes_no_write_to_the_repository(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "staged.txt").write_text("s\n", encoding="utf-8")
    _git(["add", "staged.txt"], repo)
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")

    index_path = repo / ".git" / "index"
    head_path = repo / ".git" / "HEAD"
    index_before = index_path.read_bytes()
    index_mtime_before = index_path.stat().st_mtime_ns
    head_before = head_path.read_bytes()
    stash_before = _git(["stash", "list"], repo).stdout
    tree_before = {
        p: p.read_bytes() for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }

    run = _run([_killed_attempt(
        str(repo),
        paths_written=[str(repo / "tracked.txt"), str(repo / "staged.txt")],
        in_flight=[{"tool": "Write", "path": str(repo / "untracked.txt"), "line_no": 1}],
    )])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.checked is True

    assert index_path.read_bytes() == index_before
    assert index_path.stat().st_mtime_ns == index_mtime_before
    assert head_path.read_bytes() == head_before
    assert _git(["stash", "list"], repo).stdout == stash_before
    tree_after = {
        p: p.read_bytes() for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }
    assert tree_after == tree_before
    assert not (repo / ".git" / "index.lock").exists()


# --- rule 8: later_write_observed ----------------------------------------------


def test_later_write_observed_true_when_a_higher_ordinal_attempt_of_the_same_key_wrote_it(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    path = str(repo / "tracked.txt")
    attempt1 = _killed_attempt(
        str(repo), agent_id="a0000000000000001", key="v2:same", ordinal=1,
        paths_written=[path], outcome="killed",
    )
    attempt2 = _attempt(
        agent_id="a0000000000000002", key="v2:same", ordinal=2, outcome="completed",
        facts=_facts(paths_written=[path], cwds=[str(repo)]),
    )
    run = _run([attempt1, attempt2])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["tracked.txt"].later_write_observed is True
    assert result.paths["tracked.txt"].attempts == [{"agent_id": "a0000000000000001", "ordinal": 1}]


def test_later_write_observed_false_when_no_later_attempt_touches_the_path(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    path = str(repo / "tracked.txt")
    attempt1 = _killed_attempt(
        str(repo), agent_id="a0000000000000001", key="v2:solo", ordinal=1, paths_written=[path],
    )
    run = _run([attempt1])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["tracked.txt"].later_write_observed is False


def test_two_attempts_of_different_keys_writing_the_same_path_is_not_a_later_write(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    path = str(repo / "tracked.txt")
    attempt1 = _killed_attempt(
        str(repo), agent_id="a0000000000000001", key="v2:one", ordinal=1, paths_written=[path],
    )
    attempt2 = _killed_attempt(
        str(repo), agent_id="a0000000000000002", key="v2:two", ordinal=1, paths_written=[path],
    )
    run = _run([attempt1, attempt2])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["tracked.txt"].later_write_observed is False
    # Both attempts wrote it, so both are recorded, in first-seen order.
    assert result.paths["tracked.txt"].attempts == [
        {"agent_id": "a0000000000000001", "ordinal": 1},
        {"agent_id": "a0000000000000002", "ordinal": 1},
    ]


def test_in_flight_path_also_counts_for_later_write_observed(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    path = str(repo / "tracked.txt")
    attempt1 = _killed_attempt(
        str(repo), agent_id="a0000000000000001", key="v2:same", ordinal=1, paths_written=[path],
    )
    attempt2 = _attempt(
        agent_id="a0000000000000002", key="v2:same", ordinal=2, outcome="killed",
        facts=_facts(
            in_flight=[{"tool": "Edit", "path": path, "line_no": 5}], cwds=[str(repo)],
        ),
    )
    run = _run([attempt1, attempt2])
    result = observe.observe_run(run, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert result.paths["tracked.txt"].later_write_observed is True


# --- source-level: only the five permitted git subcommands ever appear --------


def test_source_never_contains_a_forbidden_git_subcommand_literal():
    """Every argv this module ever runs is built as a Python list literal
    (docs/DESIGN.md 4.9 rule 4): `["--version"]`, `["-C", cwd, "rev-parse",
    ...]`, and so on. Scanning only the string elements of list literals
    (rather than every string constant in the file) is deliberate: a
    literal such as errors="replace" (an encoding error-handler name, not a
    git subcommand) or a docstring must never produce a false positive
    here."""
    source = OBSERVE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed_subcommands = {"--version", "rev-parse", "config", "status"}
    forbidden_subcommands = {
        "log", "diff", "add", "commit", "reset", "checkout", "restore",
        "stash", "clean", "fetch", "pull", "push", "merge", "rebase",
        "cat-file", "show", "branch", "tag", "clone", "gc", "prune",
        "reflog", "worktree", "submodule", "apply", "am", "cherry-pick",
        "revert", "mv", "rm", "diff-tree", "ls-files", "ls-tree", "blame",
        "grep", "bisect", "filter-branch", "notes", "replace", "init",
        "remote", "fsck", "gitk", "difftool", "mergetool",
    }
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    literals.add(elt.value)
    hit = literals & forbidden_subcommands
    assert not hit, f"observe.py contains a forbidden git subcommand literal: {hit}"
    assert allowed_subcommands <= literals


def test_source_only_calls_subprocess_run_never_shell(tmp_path):
    source = OBSERVE_PATH.read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system(" not in source
    assert "subprocess.Popen" not in source or "subprocess.run" in source


# --- CLI wiring: --observe on `run` --------------------------------------------


def _write_observe_fixture_run(tmp_path: Path, repo: Path, dirty_path: Path, missing_path: Path) -> Path:
    """A minimal workflow run directory whose sole attempt failed and wrote
    (via Edit tool calls, completed_ok) two paths inside `repo`: one that
    ends up dirty on disk, one that is missing entirely. Built at test time,
    the same approach tests/test_attempt_facts.py uses for its long-command
    case, so no checked-in fixture ever needs a real absolute tmp_path."""
    run_dir = (
        tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
        / "subagents" / "workflows" / "wf_00000000-0b2"
    )
    run_dir.mkdir(parents=True)
    agent_id = "af0b0200000000001"
    (run_dir / "journal.jsonl").write_text(
        '{"type":"started","key":"v2:observe","agentId":"' + agent_id + '",'
        '"label":"impl:observe","phase":"Implement"}\n'
        '{"type":"failed","key":"v2:observe","agentId":"' + agent_id + '"}\n',
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    ts0 = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts1 = (now + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts2 = (now + timedelta(seconds=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts3 = (now + timedelta(seconds=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Edit two files."},
            "agentId": agent_id,
            "cwd": str(repo),
            "timestamp": ts0,
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_dirty", "name": "Edit",
                     "input": {"file_path": str(dirty_path)}},
                    {"type": "tool_use", "id": "toolu_missing", "name": "Edit",
                     "input": {"file_path": str(missing_path)}},
                ],
                "usage": {},
            },
            "agentId": agent_id,
            "cwd": str(repo),
            "timestamp": ts1,
        }),
        json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_dirty", "content": "ok",
                     "is_error": False},
                    {"type": "tool_result", "tool_use_id": "toolu_missing", "content": "ok",
                     "is_error": False},
                ],
            },
            "agentId": agent_id,
            "cwd": str(repo),
            "timestamp": ts2,
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "API Error: Server error mid-response"}],
                "usage": {},
            },
            "agentId": agent_id,
            "cwd": str(repo),
            "timestamp": ts3,
        }),
    ]
    (run_dir / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # N5c-1: the groups are observed from the ledger, and an attempt the tool
    # cannot place in time is never assumed safe, so the run needs the parent
    # transcript whose launch result places the attempt in its generation.
    from tests.test_field_v03 import (
        _envelope,
        _notification_entry,
        _parent_path,
        _workflow_launch,
        _workflow_launch_result,
        _write_jsonl,
    )

    _write_jsonl(_parent_path(tmp_path), [
        _workflow_launch("toolu_A", now - timedelta(seconds=30)),
        _workflow_launch_result(
            "toolu_A", now - timedelta(seconds=29), "wf_00000000-0b2", "tA",
        ),
        _notification_entry(now + timedelta(seconds=60), _envelope("tA", "toolu_A", "completed")),
    ])
    return run_dir


@pytest.fixture
def observe_cli_run(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    dirty_path = repo / "tracked.txt"
    dirty_path.write_text("changed\n", encoding="utf-8")
    missing_path = repo / "never-existed.txt"
    run_dir = _write_observe_fixture_run(tmp_path, repo, dirty_path, missing_path)
    return run_dir, repo


def test_run_with_observe_flag_adds_repo_state_section_in_text(observe_cli_run, capsys):
    run_dir, repo = observe_cli_run
    # unit M3a: FINDINGS is verbose-only now (STORIES replaces it by
    # default), so this also passes --verbose to keep proving REPO STATE's
    # own position relative to it.
    code = cli.main(["run", str(run_dir), "--observe", "--verbose"])
    out = capsys.readouterr().out
    assert code in (0, 3, 4)
    assert "REPO STATE" in out
    agent_table_index = out.index("AGENT")
    findings_index = out.index("FINDINGS")
    repo_state_index = out.index("REPO STATE")
    assert agent_table_index < repo_state_index < findings_index
    assert "path-1" in out
    assert "path-2" in out
    assert "DIRTY_NOW" in out
    assert "MISSING_NOW" in out
    assert str(repo) not in out


def test_run_with_observe_and_no_redact_shows_real_repo_relative_paths(observe_cli_run, capsys):
    run_dir, repo = observe_cli_run
    cli.main(["run", str(run_dir), "--observe", "--no-redact"])
    out = capsys.readouterr().out
    assert "tracked.txt" in out
    assert "never-existed.txt" in out


def test_run_with_observe_flag_adds_observation_key_in_json(observe_cli_run, capsys):
    run_dir, repo = observe_cli_run
    cli.main(["run", str(run_dir), "--observe", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "observation" in payload
    # N5c-1: the observation is {groups: [...]}, one group per repository.
    (group,) = payload["observation"]["groups"]
    assert group["checked"] is True
    states = {entry["state"] for entry in group["paths"].values()}
    assert observe.DIRTY_NOW in states
    assert observe.MISSING_NOW in states
    for key in group["paths"]:
        assert key.startswith("path-")


def test_run_with_observe_and_no_redact_json_has_real_paths(observe_cli_run, capsys):
    run_dir, repo = observe_cli_run
    cli.main(["run", str(run_dir), "--observe", "--json", "--no-redact"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    # N5c-1: a group's paths are keyed by the absolute ledger path at raw.
    (group,) = payload["observation"]["groups"]
    assert str(repo / "tracked.txt") in group["paths"]
    assert str(repo / "never-existed.txt") in group["paths"]


def test_run_without_observe_flag_never_mentions_repo_state(observe_cli_run, capsys):
    run_dir, _repo = observe_cli_run
    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    assert "REPO STATE" not in out


def test_run_without_observe_flag_json_has_no_observation_key(observe_cli_run, capsys):
    run_dir, _repo = observe_cli_run
    cli.main(["run", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "observation" not in payload


# docs/DESIGN.md 4.9 rule 10: without --observe, text and JSON stay exactly
# what they were before this unit -- proven here on the packaged demo run,
# not just the fixture this file builds itself.
def test_demo_run_without_observe_flag_is_unaffected(capsys):
    from importlib import resources

    demo_ref = resources.files("agent_postmortem") / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
    with resources.as_file(demo_ref) as demo_path:
        cli.main(["run", str(demo_path)])
    text_out = capsys.readouterr().out
    assert "REPO STATE" not in text_out

    with resources.as_file(demo_ref) as demo_path:
        cli.main(["run", str(demo_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "observation" not in payload
