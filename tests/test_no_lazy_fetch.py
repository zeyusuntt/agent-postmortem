"""docs/DESIGN.md 4.9 rule 4: git's environment carries `GIT_ALLOW_PROTOCOL=`
(no transport protocol allowed) and `GIT_NO_LAZY_FETCH=1`.

A repository configured as a partial clone fetches a missing object on demand,
and `git status` needs the HEAD tree. With a promisor remote whose URL uses the
`ext::` transport and `protocol.ext.allow=always` in its own config, that fetch
runs a command the repository chose. No filter driver is involved, so the
filter guard does not see it. With either variable set, git refuses the fetch:
`status` fails, the check is reported unchecked, and the command never runs.
"""
from __future__ import annotations

from pathlib import Path

from agent_postmortem import observe, reader
from tests.n5c1_helpers import Att, build_run, git, init_repo, write_step
from tests.test_filter_scope import NOW, _single_cwd_run


def _lazy_fetch_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository whose HEAD tree object is missing and whose promisor
    remote would run a script that creates the returned sentinel."""
    repo = init_repo(tmp_path / "repo")
    sentinel = tmp_path / "transport-ran"
    transport = tmp_path / "transport"
    transport.write_text(f'#!/bin/sh\nprintf x > "{sentinel}"\nexit 1\n', encoding="utf-8")
    transport.chmod(0o755)
    tree = git(["rev-parse", "HEAD^{tree}"], repo).stdout.strip()
    git(["config", "extensions.partialClone", "origin"], repo)
    git(["config", "remote.origin.promisor", "true"], repo)
    git(["config", "remote.origin.url", f"ext::{transport}"], repo)
    git(["config", "protocol.ext.allow", "always"], repo)
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).rename(tmp_path / "moved-tree-object")
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    return repo, sentinel


def test_observe_run_never_lets_status_fetch_through_a_repository_transport(tmp_path):
    repo, sentinel = _lazy_fetch_repo(tmp_path)

    result = observe.observe_run(_single_cwd_run(repo, repo / "tracked.txt"), now=NOW)

    assert not sentinel.exists()
    assert result.checked is False


def test_a_group_never_lets_status_fetch_through_a_repository_transport(tmp_path, monkeypatch):
    repo, sentinel = _lazy_fetch_repo(tmp_path)
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-e77", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])

    (group,) = observe.observe_groups(reader.load_run(run_dir)).groups

    assert not sentinel.exists()
    assert (group.checked, group.reason) == (False, "guard refused")


def test_build_env_forbids_every_transport_and_lazy_fetch(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = observe._build_env()
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_NO_LAZY_FETCH"] == "1"
