"""G-B, G5 (v0.3.1): `--here` and the caller's own workflows (docs/DESIGN.md 4.13.6).

When CLAUDE_CODE_SESSION_ID names exactly one session run whose transcript stem equals it AND
that session itself passes the `--here` predicate for the caller's directory, the workflow runs
under that session's directory join the here set for `run --here` (its `also` list) and for
`scan --here`, whatever their own working directories. Otherwise nothing changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import reader, report
from tests import n3b_helpers as h
from tests.n6_helpers import (
    SLUG_A,
    SLUG_B,
    aid,
    entry,
    session_path,
    uid,
    write_lines,
    write_workflow,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=10)
ENV = "CLAUDE_CODE_SESSION_ID"
WF_A = "wf_00000000-ea3"  # owned by session 1
WF_B = "wf_00000000-ea4"  # owned by session 2
WF_C = "wf_00000000-ea5"  # owned by session 3, which does not work here
ELSEWHERE = "/work/elsewhere/other"


@pytest.fixture
def world(tmp_path, monkeypatch):
    base = (tmp_path / "work").resolve()
    caller = base / "repo"
    caller.mkdir(parents=True)
    monkeypatch.chdir(caller)
    monkeypatch.setattr(report, "_now", lambda: NOW)
    root = tmp_path / "root"
    cwd = str(caller)

    def session(slug, n, where, start):
        path = session_path(root, slug, uid(n))
        write_lines(path, [
            entry("user", "work", start, cwd=where, session=uid(n)),
            entry("assistant", "done", start + timedelta(seconds=1), cwd=where, session=uid(n)),
        ])
        return path

    paths = {
        "s1": session(SLUG_A, 1, cwd, T0),
        "s2": session(SLUG_A, 2, cwd, T0 + timedelta(hours=1)),
        "s3": session(SLUG_B, 3, "/work/elsewhere/proj", T0 + timedelta(hours=2)),
    }
    write_workflow(root, SLUG_A, uid(1), WF_A, ((aid(1), ELSEWHERE, T0),))
    write_workflow(root, SLUG_A, uid(2), WF_B, ((aid(2), ELSEWHERE, T0),))
    write_workflow(root, SLUG_B, uid(3), WF_C, ((aid(3), ELSEWHERE, T0),))
    ids = {name: reader.load_run(path).run_id for name, path in paths.items()}
    return {"root": root, "cwd": cwd, "tmp": tmp_path, "ids": ids, "paths": paths,
            "session": session}


def _scan_ids(capsys, world, *flags) -> set[str]:
    code, out, err = h.cli(capsys, "scan", "--here", "--json", "--root", world["root"], *flags)
    assert code in (0, 3, 4), (code, err)
    return {row["run_id"] for row in json.loads(out)["rows"]}


def _run_here(capsys, world):
    code, out, err = h.cli(capsys, "run", "--here", "--json", "--root", world["root"])
    assert code in (0, 3, 4), (code, err)
    return json.loads(out)["run"]["run_id"], err


# --- scan --here -----------------------------------------------------------------------------


def test_g5_without_the_variable_only_runs_that_worked_here_match(world, capsys):
    ids = world["ids"]
    assert _scan_ids(capsys, world) == {ids["s1"], ids["s2"]}


@pytest.mark.parametrize("value", ["not-a-uuid", "", "  ", "../" + uid(1), uid(1)[:-1],
                                   uid(1).replace("-", ""), "agent-" + aid(1)])
def test_g5_a_malformed_variable_changes_nothing(world, capsys, monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    ids = world["ids"]
    assert _scan_ids(capsys, world) == {ids["s1"], ids["s2"]}


def test_g5_the_matching_session_brings_only_its_own_workflows(world, capsys, monkeypatch):
    ids = world["ids"]
    monkeypatch.setenv(ENV, uid(1))
    assert _scan_ids(capsys, world) == {ids["s1"], ids["s2"], WF_A}
    monkeypatch.setenv(ENV, uid(2))
    assert _scan_ids(capsys, world) == {ids["s1"], ids["s2"], WF_B}


def test_g5_the_variable_is_case_insensitive_like_the_session_id_hint(world, capsys, monkeypatch):
    monkeypatch.setenv(ENV, uid(1).upper())
    assert WF_A in _scan_ids(capsys, world)


def test_g5_a_session_that_does_not_work_here_brings_nothing(world, capsys, monkeypatch):
    ids = world["ids"]
    monkeypatch.setenv(ENV, uid(3))
    assert _scan_ids(capsys, world) == {ids["s1"], ids["s2"]}


def test_g5_a_variable_naming_no_session_changes_nothing(world, capsys, monkeypatch):
    ids = world["ids"]
    monkeypatch.setenv(ENV, uid(99))
    assert _scan_ids(capsys, world) == {ids["s1"], ids["s2"]}


def test_g5_a_session_that_is_not_unique_brings_nothing(world, capsys, monkeypatch):
    twin = world["session"](SLUG_B, 1, world["cwd"], T0 + timedelta(hours=3))
    assert twin.stem == world["paths"]["s1"].stem
    monkeypatch.setenv(ENV, uid(1))
    ids = _scan_ids(capsys, world)
    assert WF_A not in ids and WF_B not in ids and WF_C not in ids
    assert len(ids) == 3  # the two sessions called uid(1) and session 2


def test_g5_kind_workflow_finds_the_owned_workflow(world, capsys, monkeypatch):
    monkeypatch.setenv(ENV, uid(1))
    assert _scan_ids(capsys, world, "--kind", "workflow") == {WF_A}


def test_g5_kind_workflow_without_the_variable_matches_nothing(world, capsys):
    code, out, _err = h.cli(
        capsys, "scan", "--here", "--kind", "workflow", "--json", "--root", world["root"],
    )
    assert (code, out) == (2, "")


def test_g5_an_owned_workflow_whose_own_cwd_is_a_parent_is_not_listed_twice(
    world, capsys, monkeypatch,
):
    write_workflow(world["root"], SLUG_A, uid(1), "wf_00000000-ea6",
                   ((aid(6), world["cwd"], T0),))
    monkeypatch.setenv(ENV, uid(1))
    code, out, _err = h.cli(capsys, "scan", "--here", "--json", "--root", world["root"])
    rows = [row["run_id"] for row in json.loads(out)["rows"]]
    assert len(rows) == len(set(rows))
    assert "wf_00000000-ea6" in rows and WF_A in rows


# --- run --here ------------------------------------------------------------------------------


def test_g5_run_here_without_the_variable_lists_no_owned_workflow(world, capsys):
    picked, err = _run_here(capsys, world)
    assert WF_A not in err and WF_B not in err
    assert picked in set(world["ids"].values())


def test_g5_run_here_names_the_own_session_and_lists_its_workflows_in_also(
    world, capsys, monkeypatch,
):
    monkeypatch.setenv(ENV, uid(1))
    picked, err = _run_here(capsys, world)
    assert picked == world["ids"]["s1"]
    assert "3 runs worked here" in err
    assert "(your current session)" in err
    assert WF_A in err
    assert WF_B not in err and WF_C not in err


def test_g5_run_here_with_a_session_that_does_not_work_here_lists_nothing_extra(
    world, capsys, monkeypatch,
):
    monkeypatch.setenv(ENV, uid(3))
    _picked, err = _run_here(capsys, world)
    assert WF_C not in err and WF_A not in err and WF_B not in err
    assert "2 runs worked here" in err


def test_g5_the_resolver_returns_exactly_the_owned_workflow_directories(world):
    found = reader.caller_workflow_paths(world["root"], uid(1), world["cwd"])
    assert {path.name for path in found} == {WF_A}
    assert reader.caller_workflow_paths(world["root"], None, world["cwd"]) == frozenset()
    assert reader.caller_workflow_paths(world["root"], uid(3), world["cwd"]) == frozenset()
    assert reader.caller_workflow_paths(world["root"], uid(1), None) == frozenset()
