"""N5c-1: the `observation` key and the REPO STATE block of `run --observe`
(plan S6; DESIGN 4.9, 5.3b).

A group exports exactly `group` (its index in the exported list), `repo`,
`refused`, `checked`, `reason`, `paths`, and `unobserved_paths`. `repo`, every
`paths` key, and every `unobserved_paths` element use the path namespace of the
level (`path-N` at share, the P19 spelling at local, the real absolute path at
raw); `reason` is a fixed phrase from the closed list; `repo_root`, `head`, and
`git_version` stay internal.
"""

from __future__ import annotations

import json
import re

import pytest

from agent_postmortem import cli
from tests.n5c1_helpers import Att, _aid, bash_step, build_run, init_repo, write_step

GROUP_KEYS = {"group", "repo", "refused", "checked", "reason", "paths", "unobserved_paths"}
PATH_KEYS = {"state", "reason", "later_write_observed", "attempts"}
CLOSED = {
    "refused", "historical", "not a git repository", "git unavailable", "guard refused",
    "outside the work tree",
}


def _two_repositories(tmp_path, monkeypatch, suffix="a70"):
    """Repository A is observable (complete ledger, one dirty file); repository B is
    refused (its attempt also ran a shell command). B's attempt also lands in the
    unknown group."""
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    (repo_a / "tracked.txt").write_text("changed a\n", encoding="utf-8")
    (repo_b / "tracked.txt").write_text("changed b\n", encoding="utf-8")
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[
            write_step("u1", repo_b / "tracked.txt", repo_b), bash_step("u2", "make", repo_b),
        ]),
    ])
    return run_dir, repo_a, repo_b


def _json(capsys, run_dir, *flags):
    cli.main(["run", str(run_dir), "--json", "--observe", *flags])
    return json.loads(capsys.readouterr().out)


def _ledger_view(payload):
    """The level's own spelling of each ledger path and repo, from the same payload."""
    first = payload["run"]["attempts"][0]["ledger"]["entries"][0]
    second = payload["run"]["attempts"][1]["ledger"]["entries"][0]
    return first["path"], first["repo"], second["path"], second["repo"]


def test_raw_pins_the_two_repository_objects_exactly(tmp_path, monkeypatch, capsys):
    run_dir, repo_a, repo_b = _two_repositories(tmp_path, monkeypatch)
    payload = _json(capsys, run_dir, "--raw")
    assert payload["observation"] == {"groups": [
        {
            "group": 0, "repo": str(repo_a), "refused": False, "checked": True, "reason": None,
            "paths": {str(repo_a / "tracked.txt"): {
                "state": "DIRTY_NOW", "reason": None, "later_write_observed": False,
                "attempts": [{"agent_id": _aid(1), "ordinal": 1}],
            }},
            "unobserved_paths": [],
        },
        {
            "group": 1, "repo": str(repo_b), "refused": True, "checked": False,
            "reason": "refused", "paths": {},
            "unobserved_paths": [str(repo_b / "tracked.txt")],
        },
        {
            "group": 2, "repo": None, "refused": True, "checked": False, "reason": "refused",
            "paths": {}, "unobserved_paths": [],
        },
    ]}


def test_share_pins_the_two_repository_objects_with_aliases_and_no_real_value(
    tmp_path, monkeypatch, capsys,
):
    run_dir, repo_a, repo_b = _two_repositories(tmp_path, monkeypatch)
    payload = _json(capsys, run_dir)
    a_path, a_repo, b_path, b_repo = _ledger_view(payload)
    alias_1 = payload["run"]["attempts"][0]["agent_id"]
    assert re.fullmatch(r"a:[0-9a-f]{12,}", alias_1)
    assert payload["observation"] == {"groups": [
        {
            "group": 0, "repo": a_repo, "refused": False, "checked": True, "reason": None,
            "paths": {a_path: {
                "state": "DIRTY_NOW", "reason": None, "later_write_observed": False,
                "attempts": [{"agent_id": alias_1, "ordinal": 1}],
            }},
            "unobserved_paths": [],
        },
        {
            "group": 1, "repo": b_repo, "refused": True, "checked": False,
            "reason": "refused", "paths": {}, "unobserved_paths": [b_path],
        },
        {
            "group": 2, "repo": None, "refused": True, "checked": False, "reason": "refused",
            "paths": {}, "unobserved_paths": [],
        },
    ]}
    assert re.fullmatch(r"path-\d+", a_repo) and re.fullmatch(r"path-\d+", b_repo)
    text = json.dumps(payload)
    for real in (str(repo_a), str(repo_b), "tracked.txt", _aid(1), _aid(2)):
        assert real not in text, real


def test_local_pins_the_two_repository_objects_with_no_absolute_path(
    tmp_path, monkeypatch, capsys,
):
    run_dir, repo_a, repo_b = _two_repositories(tmp_path, monkeypatch)
    payload = _json(capsys, run_dir, "--local")
    a_path, a_repo, b_path, b_repo = _ledger_view(payload)
    groups = payload["observation"]["groups"]
    assert [g["repo"] for g in groups] == [a_repo, b_repo, None]
    assert list(groups[0]["paths"]) == [a_path]
    assert groups[1]["unobserved_paths"] == [b_path]
    assert a_repo == "." and a_path == "tracked.txt"  # inside the observed repository
    assert re.fullmatch(r"path-\d+", b_repo)  # outside the repository and home
    text = json.dumps(payload["observation"])
    assert str(repo_a) not in text and str(repo_b) not in text


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_every_group_has_exactly_the_documented_keys_and_a_closed_reason(
    tmp_path, monkeypatch, capsys, flags,
):
    run_dir, _a, _b = _two_repositories(tmp_path, monkeypatch)
    groups = _json(capsys, run_dir, *flags)["observation"]["groups"]
    assert [g["group"] for g in groups] == [0, 1, 2]
    for group in groups:
        assert set(group) == GROUP_KEYS
        assert group["reason"] is None or group["reason"] in CLOSED
        for path_obs in group["paths"].values():
            assert set(path_obs) == PATH_KEYS
    assert "repo_root" not in json.dumps(groups) and "git_version" not in json.dumps(groups)


def test_the_observation_object_has_only_the_groups_key(tmp_path, monkeypatch, capsys):
    run_dir, _a, _b = _two_repositories(tmp_path, monkeypatch)
    assert set(_json(capsys, run_dir)["observation"]) == {"groups"}


def test_with_no_risk_population_the_observation_is_an_empty_group_list(
    tmp_path, monkeypatch, capsys,
):
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-a71", [Att(1, journal="completed")])
    assert _json(capsys, run_dir)["observation"] == {"groups": []}


def test_a_path_outside_the_toplevel_is_unobserved_and_never_invented_as_a_path_entry(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-a72", [
        Att(1, steps=[
            write_step("t1", repo / "tracked.txt", repo),
            write_step("t2", elsewhere / "o.txt", repo),
        ]),
    ])
    (group,) = _json(capsys, run_dir, "--raw")["observation"]["groups"]
    assert list(group["paths"]) == [str(repo / "tracked.txt")]
    assert group["unobserved_paths"] == [str(elsewhere / "o.txt")]
    assert group["checked"] is True and group["reason"] is None


# --- the REPO STATE text block --------------------------------------------------------------------------


def test_raw_repo_state_lists_each_group_with_its_paths(tmp_path, monkeypatch, capsys):
    run_dir, repo_a, repo_b = _two_repositories(tmp_path, monkeypatch)
    cli.main(["run", str(run_dir), "--raw", "--observe"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    start = lines.index("REPO STATE")
    block = lines[start:start + 9]
    assert block[:8] == [
        "REPO STATE",
        f"  repository {repo_a}  checked: yes",
        f"    {repo_a / 'tracked.txt'}  DIRTY_NOW  later write: no  attempts: 1",
        f"  repository {repo_b}  checked: no (its ledger is partial: incomplete evidence may "
        "hide more writes, so the tool does not run status for it)",
        f"    run yourself: git -C {repo_b} status --short",
        f"    {repo_b / 'tracked.txt'}  not observed",
        "  repository unknown  checked: no (refused)",
        "",
    ]


def test_share_repo_state_uses_aliases_only(tmp_path, monkeypatch, capsys):
    run_dir, repo_a, repo_b = _two_repositories(tmp_path, monkeypatch)
    cli.main(["run", str(run_dir), "--observe"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    block = lines[lines.index("REPO STATE"):]
    assert re.fullmatch(r"  repository path-\d+  checked: yes", block[1])
    assert re.fullmatch(r"    path-\d+  DIRTY_NOW  later write: no  attempts: 1", block[2])
    assert re.fullmatch(
        r"  repository path-\d+  checked: no \(its ledger is partial: incomplete evidence may "
        r"hide more writes, so the tool does not run status for it\)", block[3],
    )
    assert re.fullmatch(
        r"    run yourself: git status --short in path-\d+; --raw prints the exact command",
        block[4],
    )
    assert re.fullmatch(r"    path-\d+  not observed", block[5])
    assert block[6] == "  repository unknown  checked: no (refused)"
    for real in (str(repo_a), str(repo_b), "tracked.txt"):
        assert real not in out


def test_local_repo_state_uses_the_local_spelling(tmp_path, monkeypatch, capsys):
    run_dir, repo_a, repo_b = _two_repositories(tmp_path, monkeypatch)
    cli.main(["run", str(run_dir), "--local", "--observe"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    block = lines[lines.index("REPO STATE"):]
    assert block[1] == "  repository .  checked: yes"
    assert block[2] == "    tracked.txt  DIRTY_NOW  later write: no  attempts: 1"
    assert str(repo_a) not in out and str(repo_b) not in out


def test_without_observe_there_is_no_repo_state_and_no_observation_key(
    tmp_path, monkeypatch, capsys,
):
    run_dir, _a, _b = _two_repositories(tmp_path, monkeypatch)
    cli.main(["run", str(run_dir), "--raw"])
    assert "REPO STATE" not in capsys.readouterr().out
    cli.main(["run", str(run_dir), "--json"])
    assert "observation" not in json.loads(capsys.readouterr().out)
