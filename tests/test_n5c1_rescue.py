"""N5c-1: rescue switches from the single-repository `observe_run` to the groups
(plan S5, S6; DESIGN 4.9, 4.11, 5.3b). `rescue plan` and `rescue status` start no
git process for a refused or unknown group; `rescue plan --json` carries a
top-level `groups` array (the export objects of `run --observe`, the unknown group
left out when it names no path), and each `paths[]` row names its group by index
with `observation` null when its group was refused or not checked.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta

from agent_postmortem import cli, observe, reader
from tests.n5c1_helpers import Att, bash_step, build_run, init_repo, write_step


class GitSpy:
    def __init__(self, monkeypatch):
        self.calls: list[list[str]] = []
        real = subprocess.run

        def spy(argv, **kwargs):
            self.calls.append(list(argv))
            return real(argv, **kwargs)

        monkeypatch.setattr(observe.subprocess, "run", spy)

    def statuses(self):
        return [argv for argv in self.calls if "status" in argv]


def _plan(capsys, run_dir, *flags):
    code = cli.main(["rescue", "plan", str(run_dir), "--json", *flags])
    return code, json.loads(capsys.readouterr().out)


def _one_repo(tmp_path, monkeypatch, suffix, *, with_shell=False):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    steps = [write_step("t1", repo / "tracked.txt", repo)]
    if with_shell:
        steps.append(bash_step("t2", "make", repo))
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [Att(1, steps=steps)])
    return run_dir, repo


def test_a_checked_group_gives_its_rows_an_observation_and_the_group_index(
    tmp_path, monkeypatch, capsys,
):
    run_dir, repo = _one_repo(tmp_path, monkeypatch, "a80")
    code, payload = _plan(capsys, run_dir, "--raw")
    assert [g["repo"] for g in payload["groups"]] == [str(repo)]
    assert payload["groups"][0]["checked"] is True
    (unit,) = payload["units"]
    (row,) = unit["paths"]
    assert row["group"] == 0
    assert row["observation"] == {"state": "DIRTY_NOW", "reason": None}
    assert row["path"] == "tracked.txt"
    assert code == 0


def test_a_refused_group_starts_no_git_and_its_rows_have_a_null_observation(
    tmp_path, monkeypatch, capsys,
):
    run_dir, repo = _one_repo(tmp_path, monkeypatch, "a81", with_shell=True)
    spy = GitSpy(monkeypatch)
    code, payload = _plan(capsys, run_dir, "--raw")
    assert spy.calls == []
    assert [(g["repo"], g["refused"], g["checked"], g["reason"]) for g in payload["groups"]] == [
        (str(repo), True, False, "refused"),
    ]
    (row,) = payload["units"][0]["paths"]
    assert row["group"] == 0 and row["observation"] is None
    assert code == 4  # a refused group's paths make the UNKNOWN fact true


def test_the_text_plan_shows_the_refusal_as_the_state_reason(tmp_path, monkeypatch, capsys):
    run_dir, repo = _one_repo(tmp_path, monkeypatch, "a82", with_shell=True)
    spy = GitSpy(monkeypatch)
    code = cli.main(["rescue", "plan", str(run_dir), "--raw"])
    out = capsys.readouterr().out
    assert spy.calls == [] and code == 4
    assert "UNKNOWN (refused)" in out


def test_the_unknown_group_is_left_out_of_the_plan_when_it_names_no_path(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-a83", [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:b", start=40, steps=[bash_step("u1", "make", None)]),
    ])
    _code, payload = _plan(capsys, run_dir, "--raw")
    assert [g["repo"] for g in payload["groups"]] == [str(repo)]
    cli.main(["run", str(run_dir), "--json", "--observe", "--raw"])
    run_payload = json.loads(capsys.readouterr().out)
    assert [g["repo"] for g in run_payload["observation"]["groups"]] == [str(repo), None]


def test_two_repositories_each_row_names_its_own_group(tmp_path, monkeypatch, capsys):
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    (repo_a / "tracked.txt").write_text("changed a\n", encoding="utf-8")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-a84", [
        Att(1, key="v2:a", steps=[write_step("t1", repo_a / "tracked.txt", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[
            write_step("u1", repo_b / "tracked.txt", repo_b), bash_step("u2", "make", repo_b),
        ]),
    ])
    spy = GitSpy(monkeypatch)
    code, payload = _plan(capsys, run_dir, "--raw")
    assert [g["repo"] for g in payload["groups"]][:2] == [str(repo_a), str(repo_b)]
    by_agent = {unit["agent"]: unit for unit in payload["units"]}
    rows = [row for unit in by_agent.values() for row in unit["paths"]]
    observations = sorted(
        (row["group"], row["observation"] is not None) for row in rows
    )
    assert observations == [(0, True), (1, False)]
    assert len(spy.statuses()) == 1 and str(repo_b) not in " ".join(
        " ".join(argv) for argv in spy.calls
    )
    assert code == 4


def test_a_historical_run_gives_null_observations_and_the_historical_reason(
    tmp_path, monkeypatch, capsys,
):
    from agent_postmortem import report
    run_dir, _repo = _one_repo(tmp_path, monkeypatch, "a85")
    run = reader.load_run(run_dir)
    monkeypatch.setattr(report, "_now", lambda: run.ended_at + timedelta(days=9))
    real_observe = observe.observe_groups
    monkeypatch.setattr(
        observe, "observe_groups",
        lambda run_, now=None: real_observe(run_, now=run.ended_at + timedelta(days=9)),
    )
    spy = GitSpy(monkeypatch)
    code, payload = _plan(capsys, run_dir, "--raw")
    assert spy.calls == []
    assert payload["groups"][0]["reason"] == "historical"
    assert payload["units"][0]["paths"][0]["observation"] is None
    assert code == 4


def test_rescue_status_starts_no_git_for_a_refused_group(tmp_path, monkeypatch, capsys):
    run_dir, _repo = _one_repo(tmp_path, monkeypatch, "a86", with_shell=True)
    spy = GitSpy(monkeypatch)
    code = cli.main(["rescue", "status", str(run_dir), "--json", "--raw"])
    payload = json.loads(capsys.readouterr().out)
    assert spy.calls == []
    assert payload["schema"] == 3
    assert code in (0, 3, 4)


def test_rescue_status_still_observes_a_checked_group(tmp_path, monkeypatch, capsys):
    run_dir, _repo = _one_repo(tmp_path, monkeypatch, "a87")
    spy = GitSpy(monkeypatch)
    cli.main(["rescue", "status", str(run_dir), "--json", "--raw"])
    capsys.readouterr()
    assert len(spy.statuses()) == 1


def test_the_share_plan_groups_use_aliases(tmp_path, monkeypatch, capsys):
    run_dir, repo = _one_repo(tmp_path, monkeypatch, "a88")
    _code, payload = _plan(capsys, run_dir)
    text = json.dumps(payload)
    assert str(repo) not in text
    group = payload["groups"][0]
    assert group["repo"].startswith("path-")
    assert set(group) == {"group", "repo", "refused", "checked", "reason", "paths",
                          "unobserved_paths"}
