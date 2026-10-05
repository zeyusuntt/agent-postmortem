"""N6 (docs/DESIGN.md 4.8, 4.13.6; plan I8): `run --here` picks the newest run whose
cwd set contains the caller's directory or a parent of it, then behaves exactly as
`run <that run's path>`."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agent_postmortem import observe, reader
from tests import n3b_helpers as h
from tests.n6_helpers import (
    BASE,
    HERE_TAIL,
    NO_BOTH,
    NO_NEITHER,
    SLUG_A,
    SLUG_B,
    WF_DUP,
    aid,
    uid,
    write_session,
    write_workflow,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    base = (tmp_path / "work").resolve()
    caller = base / "repo" / "sub"
    caller.mkdir(parents=True)
    monkeypatch.chdir(caller)
    return {"root": tmp_path / "root", "cwd": str(base / "repo"), "caller": caller}


def _run_id(path) -> str:
    return reader.load_run(path).run_id


def _picked(capsys, root, *flags) -> str:
    code, out, _err = h.cli(capsys, "run", "--here", "--json", "--root", root, *flags)
    assert code in (0, 3, 4), (code, _err)
    return json.loads(out)["run"]["run_id"]


def test_run_here_picks_the_newest_by_started_at(world, capsys):
    older = write_session(world["root"], SLUG_A, uid(1), cwd=world["cwd"], start=BASE)
    newer = write_session(
        world["root"], SLUG_A, uid(2), cwd=world["cwd"], start=BASE + timedelta(hours=1),
    )
    write_session(world["root"], SLUG_A, uid(3), cwd=world["cwd"] + "-other", start=BASE + timedelta(days=1))
    assert _picked(capsys, world["root"]) == _run_id(newer)
    assert _run_id(older) != _run_id(newer)


def test_a_dated_run_beats_an_undated_one(world, capsys):
    dated = write_session(world["root"], SLUG_A, uid(1), cwd=world["cwd"], start=BASE)
    # a workflow whose transcript lines carry no timestamp, and the largest run id
    write_workflow(
        world["root"], SLUG_A, uid(2), "wf_00000000-6af", ((aid(1), world["cwd"], None),),
    )
    undated = reader.load_run(
        world["root"] / SLUG_A / uid(2) / "subagents" / "workflows" / "wf_00000000-6af"
    )
    assert undated.started_at is None
    assert _picked(capsys, world["root"]) == _run_id(dated)


def test_two_undated_runs_break_the_tie_by_run_id(world, capsys):
    for n, wf in ((1, "wf_00000000-6a9"), (2, "wf_00000000-6a8")):  # path order disagrees
        write_workflow(world["root"], SLUG_A, uid(n), wf, ((aid(n), world["cwd"], None),))
    assert _picked(capsys, world["root"]) == "wf_00000000-6a9"


def test_equal_start_times_break_by_run_id(world, capsys):
    for n, wf in ((1, "wf_00000000-6a9"), (2, "wf_00000000-6a8")):  # path order disagrees
        write_workflow(world["root"], SLUG_A, uid(n), wf, ((aid(n), world["cwd"], BASE),))
    assert _picked(capsys, world["root"]) == "wf_00000000-6a9"


def test_the_same_workflow_run_id_in_two_projects_still_picks_the_exact_ref(world, capsys):
    one = write_workflow(
        world["root"], SLUG_A, uid(1), WF_DUP, ((aid(1), world["cwd"], BASE),),
    )
    two = write_workflow(
        world["root"], SLUG_B, uid(2), WF_DUP,
        ((aid(2), world["cwd"], BASE), (aid(3), world["cwd"], BASE)),
    )
    got = h.cli(capsys, "run", "--here", "--json", "--root", world["root"])
    # FB1: two runs matched, so stderr carries the one "worked here" line; code and stdout
    # are exactly `run <path>`'s.
    assert got[:2] == h.cli(capsys, "run", str(two), "--json", "--root", world["root"])[:2]
    assert got[2].startswith("postmortem: 2 runs worked here; showing ")
    assert got[:2] != h.cli(capsys, "run", str(one), "--json", "--root", world["root"])[:2]
    assert json.loads(got[1])["run"]["counts"]["attempts"] == 2


ORACLE_FLAGS = {
    "text": [],
    "json": ["--json"],
    "local text": ["--local"],
    "local json": ["--local", "--json"],
    "raw text": ["--raw"],
    "raw json": ["--raw", "--json"],
    "bug report": ["--bug-report"],
    "observe": ["--observe"],
    "verbose evidence": ["--verbose", "--evidence"],
}


@pytest.mark.parametrize("name", sorted(ORACLE_FLAGS))
def test_run_here_equals_run_of_the_selected_path(name, world, monkeypatch, capsys):
    monkeypatch.setattr(observe.shutil, "which", lambda _name: None)  # git mocked away
    write_session(world["root"], SLUG_A, uid(1), cwd=world["cwd"], start=BASE)
    chosen = write_session(
        world["root"], SLUG_A, uid(2), cwd=world["cwd"], start=BASE + timedelta(hours=1),
        children=((aid(7), world["cwd"], BASE + timedelta(hours=1)),),
    )
    flags = ORACLE_FLAGS[name]
    here = h.cli(capsys, "run", "--here", "--root", world["root"], *flags)
    by_path = h.cli(capsys, "run", str(chosen), "--root", world["root"], *flags)
    # FB1: two runs matched, so `--here` adds the one "worked here" stderr line; the exit
    # code and stdout are exactly `run <path>`'s, and its stderr is that line and nothing else.
    assert here[:2] == by_path[:2]
    assert by_path[2] == ""
    assert here[2].startswith("postmortem: 2 runs worked here; showing ")
    assert here[2].count("\n") == 1
    assert here[1] != ""


@pytest.mark.parametrize("level", ["", "--local", "--raw"])
def test_both_or_neither_of_run_id_and_here_is_a_usage_error(level, world, capsys):
    flags = [level] if level else []
    code, out, err = h.cli(capsys, "run", "wf_00000000-6a1", "--here", "--root", world["root"], *flags)
    assert (code, out, err) == (1, "", NO_BOTH)
    code, out, err = h.cli(capsys, "run", "--root", world["root"], *flags)
    assert (code, out, err) == (1, "", NO_NEITHER)


def test_run_here_with_no_match_is_exit_2_and_never_prints_the_cwd(world, capsys):
    write_session(world["root"], SLUG_A, uid(1), cwd=world["cwd"] + "-elsewhere", start=BASE)
    code, out, err = h.cli(capsys, "run", "--here", "--root", world["root"])
    assert (code, out) == (2, "")
    assert err == f"postmortem: no run under path-1 {HERE_TAIL}\n"
    assert str(world["caller"]) not in err and world["cwd"] not in err
