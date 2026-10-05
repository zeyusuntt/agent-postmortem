"""Part FB1 (field milestone F, T3): `run --here` prefers the caller's own session
(docs/DESIGN.md 4.8, 4.13.6; .reviews/v0.3/F-plan.md FB1).

The field case: several sessions worked in one directory, `run --here` showed the one with
the latest START, which was a short, idle session, not the one the caller was working in.
The rule, one normative tuple in 4.13.6: among the runs `--here` matches, (1) the session
run whose transcript stem equals `CLAUDE_CODE_SESSION_ID` (a canonical UUID), else (2) the
most recent activity (`ended_at` or else `started_at`, then run id, then path). When more
than one run matched, one stderr line names the choice; stdout is `run <selected path>`.

Every test controls the variable with monkeypatch (tests/conftest.py deletes it first).
Fixtures are synthetic: session ids are built at run time, the cwd is under tmp_path.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import reader, report
from tests import n3b_helpers as h
from tests.n6_helpers import (
    SLUG_A,
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


@pytest.fixture
def world(tmp_path, monkeypatch):
    base = (tmp_path / "work").resolve()
    caller = base / "repo"
    caller.mkdir(parents=True)
    monkeypatch.chdir(caller)
    monkeypatch.setattr(report, "_now", lambda: NOW)
    return {"root": tmp_path / "root", "cwd": str(caller), "tmp": tmp_path}


def _session(world, n, start, end=None, *, slug=SLUG_A, cwd=None):
    """A session whose transcript runs from `start` to `end` (default: one second)."""
    cwd = cwd if cwd is not None else world["cwd"]
    end = end if end is not None else start + timedelta(seconds=1)
    path = session_path(world["root"], slug, uid(n))
    write_lines(path, [
        entry("user", "work on it", start, cwd=cwd, session=uid(n)),
        entry("assistant", "done", end, cwd=cwd, session=uid(n)),
    ])
    return path


def _id(path) -> str:
    return reader.load_run(path).run_id


def _picked(capsys, world, *flags):
    code, out, err = h.cli(capsys, "run", "--here", "--json", "--root", world["root"], *flags)
    assert code in (0, 3, 4), (code, err)
    return json.loads(out)["run"]["run_id"], err


# --- the selection tuple -------------------------------------------------------------------


def test_two_sessions_the_variable_names_the_older_one(world, capsys, monkeypatch):
    older = _session(world, 1, T0)
    newer = _session(world, 2, T0 + timedelta(hours=1))
    monkeypatch.setenv(ENV, uid(1))
    picked, _err = _picked(capsys, world)
    assert picked == _id(older)
    assert _id(older) != _id(newer)


def test_without_the_variable_the_most_recently_active_run_is_shown(world, capsys):
    # the late-starting session is idle; the early-starting one worked until much later
    busy = _session(world, 1, T0, T0 + timedelta(hours=3))
    _session(world, 2, T0 + timedelta(hours=1), T0 + timedelta(hours=1, seconds=1))
    picked, _err = _picked(capsys, world)
    assert picked == _id(busy)


def test_activity_is_ended_at_else_started_at(world, capsys):
    # an undated-end run cannot exist for a session; a workflow with no timestamps has no
    # activity at all and loses to any dated run
    dated = _session(world, 1, T0)
    write_workflow(
        world["root"], SLUG_A, uid(2), "wf_00000000-6af", ((aid(1), world["cwd"], None),),
    )
    picked, _err = _picked(capsys, world)
    assert picked == _id(dated)


def test_equal_activity_breaks_by_run_id_then_path(world, capsys):
    for n, wf in ((1, "wf_00000000-6a9"), (2, "wf_00000000-6a8")):  # path order disagrees
        write_workflow(world["root"], SLUG_A, uid(n), wf, ((aid(n), world["cwd"], T0),))
    picked, _err = _picked(capsys, world)
    assert picked == "wf_00000000-6a9"


def test_a_fork_pair_resolves_to_the_caller(world, capsys, monkeypatch):
    parent = _session(world, 1, T0, T0 + timedelta(hours=1))
    fork = session_path(world["root"], SLUG_A, uid(2))
    write_lines(fork, [
        # the copied prefix names the parent (FA4), then the fork's own entries
        entry("user", "copied", T0, cwd=world["cwd"], session=uid(1)),
        entry("assistant", "copied", T0 + timedelta(minutes=1), cwd=world["cwd"], session=uid(1)),
        entry("user", "own", T0 + timedelta(hours=2), cwd=world["cwd"], session=uid(2)),
        entry("assistant", "own", T0 + timedelta(hours=2, seconds=1), cwd=world["cwd"],
              session=uid(2)),
    ])
    monkeypatch.setenv(ENV, uid(1))
    assert _picked(capsys, world)[0] == _id(parent)
    monkeypatch.setenv(ENV, uid(2))
    assert _picked(capsys, world)[0] == _id(fork)


@pytest.mark.parametrize("value", [
    "not-a-uuid", "", "   ", uid(1)[:-1], uid(1) + "0", uid(1).replace("-", ""),
    "agent-" + aid(1), "../" + uid(1),
])
def test_a_variable_that_is_not_a_canonical_uuid_falls_back(world, capsys, monkeypatch, value):
    busy = _session(world, 1, T0, T0 + timedelta(hours=3))
    _session(world, 2, T0 + timedelta(hours=1))
    monkeypatch.setenv(ENV, value)
    assert _picked(capsys, world)[0] == _id(busy)


def test_a_variable_that_names_no_matching_run_falls_back(world, capsys, monkeypatch):
    busy = _session(world, 1, T0, T0 + timedelta(hours=3))
    _session(world, 2, T0 + timedelta(hours=1))
    # a canonical uuid, but no run under the root has it
    monkeypatch.setenv(ENV, uid(99))
    assert _picked(capsys, world)[0] == _id(busy)
    # a session that exists but worked in another directory does not match `--here`
    _session(world, 3, T0 - timedelta(hours=1), cwd=world["cwd"] + "-other")
    monkeypatch.setenv(ENV, uid(3))
    assert _picked(capsys, world)[0] == _id(busy)


def test_the_variable_is_compared_in_lowercase(world, capsys, monkeypatch):
    older = _session(world, 1, T0)
    _session(world, 2, T0 + timedelta(hours=1))
    monkeypatch.setenv(ENV, uid(1).upper())
    assert _picked(capsys, world)[0] == _id(older)


def test_a_workflow_run_is_never_the_callers_session(world, capsys, monkeypatch):
    session = _session(world, 1, T0)
    # a workflow run directory whose parent session is the caller's: still not "your session"
    write_workflow(
        world["root"], SLUG_A, uid(5), "wf_00000000-6ab",
        ((aid(2), world["cwd"], T0 + timedelta(hours=1)),),
    )
    monkeypatch.setenv(ENV, uid(5))
    picked, err = _picked(capsys, world)
    assert picked == "wf_00000000-6ab"  # the most recent activity, and by activity alone
    assert "(most recent activity)" in err
    assert _id(session) not in picked


def test_the_stem_is_compared_in_lowercase_on_both_sides(world, capsys, monkeypatch):
    # a transcript whose file name is upper-case hex (the reader accepts it) and a lower-case
    # variable name the same session
    lower = uid(1)
    upper_path = session_path(world["root"], SLUG_A, lower).with_name(lower.upper() + ".jsonl")
    write_lines(upper_path, [
        entry("user", "work on it", T0, cwd=world["cwd"], session=lower),
        entry("assistant", "done", T0 + timedelta(seconds=1), cwd=world["cwd"], session=lower),
    ])
    _session(world, 2, T0 + timedelta(hours=1), T0 + timedelta(hours=1, seconds=1))
    monkeypatch.setenv(ENV, lower)
    assert _picked(capsys, world)[0] == _id(upper_path)


def test_activity_is_the_end_else_the_start(tmp_path):
    import dataclasses

    from agent_postmortem import cli

    run = reader.load_run(_fixture_session(tmp_path))
    assert cli._activity_of(run) == run.ended_at != run.started_at
    assert cli._activity_of(dataclasses.replace(run, ended_at=None)) == run.started_at
    assert cli._activity_of(dataclasses.replace(run, ended_at=None, started_at=None)) is None


def _fixture_session(tmp_path):
    path = session_path(tmp_path / "activity-root", SLUG_A, uid(1))
    write_lines(path, [
        entry("user", "a", T0, cwd="/tmp/fb", session=uid(1)),
        entry("assistant", "b", T0 + timedelta(minutes=5), cwd="/tmp/fb", session=uid(1)),
    ])
    return path


# --- stdout is `run <selected path>` -------------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_stdout_is_identical_to_run_of_the_selected_path(world, capsys, monkeypatch, flags):
    older = _session(world, 1, T0)
    _session(world, 2, T0 + timedelta(hours=1))
    monkeypatch.setenv(ENV, uid(1))
    code_here, out_here, _err = h.cli(capsys, "run", "--here", "--root", world["root"], *flags)
    code_path, out_path, _err2 = h.cli(capsys, "run", str(older), "--root", world["root"], *flags)
    assert (code_here, out_here) == (code_path, out_path)


# --- the stderr line -----------------------------------------------------------------------

LINE = re.compile(
    r"postmortem: (\d+) runs worked here; showing (\S+) \((your current session|"
    r"most recent activity)\); also: (.*)\n"
)


def _three(world):
    a = _session(world, 1, T0 + timedelta(hours=1), T0 + timedelta(hours=2))  # 9d ago
    b = _session(world, 2, T0, T0 + timedelta(days=3))  # 7d ago
    c = _session(world, 3, T0 + timedelta(days=9, hours=21), T0 + timedelta(days=9, hours=23))  # 1h
    return a, b, c


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_the_stderr_line_without_the_variable(world, capsys, flags):
    a, b, c = _three(world)
    code, _out, err = h.cli(capsys, "run", "--here", "--root", world["root"], *flags)
    assert code in (0, 3, 4)
    assert err == (
        f"postmortem: 3 runs worked here; showing {_id(c)} (most recent activity); "
        f"also: {_id(b)} (7d ago), {_id(a)} (9d ago)\n"
    ), err


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_the_stderr_line_with_the_variable(world, capsys, monkeypatch, flags):
    a, b, c = _three(world)
    monkeypatch.setenv(ENV, uid(1))
    code, _out, err = h.cli(capsys, "run", "--here", "--root", world["root"], *flags)
    assert code in (0, 3, 4)
    assert err == (
        f"postmortem: 3 runs worked here; showing {_id(a)} (your current session); "
        f"also: {_id(c)} (1h ago), {_id(b)} (7d ago)\n"
    ), err


def test_the_line_lists_at_most_three_others(world, capsys):
    paths = [_session(world, n, T0 + timedelta(hours=n)) for n in range(1, 7)]
    ids = [_id(path) for path in paths]
    _code, _out, err = h.cli(capsys, "run", "--here", "--root", world["root"])
    match = LINE.fullmatch(err)
    assert match, err
    assert match.group(1) == "6" and match.group(2) == ids[5]
    listed = [part.split(" (")[0] for part in match.group(4).split(", ")]
    assert listed == [ids[4], ids[3], ids[2]]


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_the_line_names_no_directory_path_or_session_uuid(world, capsys, flags):
    _three(world)
    _code, _out, err = h.cli(capsys, "run", "--here", "--root", world["root"], *flags)
    assert LINE.fullmatch(err), err
    for planted in (str(world["tmp"]), world["cwd"], "/work", "/repo", SLUG_A, uid(1), uid(2),
                    uid(3), uid(1)[-4:]):
        assert planted not in err, planted


def test_an_activity_free_run_is_listed_with_unknown_age(world, capsys):
    dated = _session(world, 1, T0)
    write_workflow(
        world["root"], SLUG_A, uid(2), "wf_00000000-6ac", ((aid(1), world["cwd"], None),),
    )
    _code, _out, err = h.cli(capsys, "run", "--here", "--root", world["root"])
    assert err == (
        f"postmortem: 2 runs worked here; showing {_id(dated)} (most recent activity); "
        "also: wf_00000000-6ac (unknown)\n"
    )


def test_one_matching_run_prints_no_line(world, capsys, monkeypatch):
    only = _session(world, 1, T0)
    _session(world, 2, T0 + timedelta(hours=1), cwd=world["cwd"] + "-other")
    monkeypatch.setenv(ENV, uid(1))
    picked, err = _picked(capsys, world)
    assert picked == _id(only)
    assert err == ""


def test_no_match_still_exits_2_with_the_existing_message(world, capsys):
    _session(world, 1, T0, cwd=world["cwd"] + "-other")
    code, out, err = h.cli(capsys, "run", "--here", "--root", world["root"])
    assert code == 2 and out == ""
    assert err.startswith("postmortem: no run under ")
    assert "worked here" not in err


# --- the contract --------------------------------------------------------------------------


def test_design_states_one_tuple_and_4_8_refers_to_it():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md").read_text("utf-8")
    start = text.index("### 4.13.6 Identity")
    end = text.index("### 4.13.7")
    section = text[start:end]
    assert "CLAUDE_CODE_SESSION_ID" in section
    assert "most recent activity" in section
    assert "runs worked here" in section
    four_eight = text[text.index("### 4.8 CLI"):text.index("### 4.9")]
    assert "CLAUDE_CODE_SESSION_ID" not in four_eight  # one place states the tuple
    assert "4.13.6 states" in four_eight
    # the old rule is gone from both
    assert "picks the\n  newest such run" not in section
