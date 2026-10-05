"""N6 (docs/DESIGN.md 4.8, 4.13.6; plan I7): `scan --here` is a filter applied before
rows, totals, the 7-day totals, NEEDS ATTENTION candidates, --all, and --json are
computed, and it composes with --kind, --since, and --limit."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests import n3b_helpers as h
from tests.n6_helpers import (
    BASE,
    HERE_TAIL,
    SLUG_A,
    SLUG_B,
    WF1,
    WF2,
    WF3,
    aid,
    uid,
    write_session,
    write_workflow,
)

OLD = BASE - timedelta(days=60)


@pytest.fixture
def world(tmp_path, monkeypatch):
    base = (tmp_path / "work").resolve()
    caller = base / "repo" / "sub"
    caller.mkdir(parents=True)
    monkeypatch.chdir(caller)
    return {"tmp": tmp_path, "base": base, "caller": caller}


def _populate(root, base, *, others: bool):
    """Matching runs (their cwd is the caller's directory or a parent of it) and, when
    `others`, runs that do not match."""
    repo = str(base / "repo")
    write_session(root, SLUG_A, uid(1), cwd=repo, start=BASE)
    write_workflow(
        root, SLUG_A, uid(2), WF1, ((aid(1), str(base), BASE + timedelta(hours=2)),),
    )
    write_session(root, SLUG_B, uid(3), cwd=repo, start=OLD)  # matches, but old
    if others:
        write_session(root, SLUG_A, uid(4), cwd=str(base / "elsewhere"), start=BASE)
        write_workflow(
            root, SLUG_B, uid(5), WF2, ((aid(2), str(base / "repo" / "sub" / "deeper"), BASE),),
        )
        write_workflow(root, SLUG_B, uid(6), WF3, ((aid(3), "/work/n6/none", BASE),))


@pytest.fixture
def roots(world):
    full, only = world["tmp"] / "full", world["tmp"] / "only"
    _populate(full, world["base"], others=True)
    _populate(only, world["base"], others=False)
    return full, only


@pytest.mark.parametrize("flags", [
    ["--json"], ["--all", "--json"], ["--all"], [], ["--local", "--all"], ["--raw", "--json"],
])
def test_scan_here_sees_only_matching_runs_in_every_view(flags, world, roots, capsys):
    full, only = roots
    here = h.cli(capsys, "scan", "--here", "--root", full, *flags)
    reference = h.cli(capsys, "scan", "--root", only, *flags)
    if "--raw" not in flags:
        assert here == reference
    else:
        # the raw payload names the root nowhere; rows and totals are what must agree
        assert json.loads(here[1])["rows"] == json.loads(reference[1])["rows"]
        assert json.loads(here[1])["totals"] == json.loads(reference[1])["totals"]
    assert here[0] == reference[0]


def test_scan_here_totals_count_only_the_matching_runs(world, roots, capsys):
    full, _only = roots
    payload = json.loads(h.cli(capsys, "scan", "--here", "--json", "--root", full)[1])
    assert len(payload["rows"]) == 3
    unfiltered = json.loads(h.cli(capsys, "scan", "--json", "--root", full)[1])
    assert len(unfiltered["rows"]) == 6
    assert payload["totals"] != unfiltered["totals"]


def test_scan_here_composes_with_kind_and_since_and_limit(world, roots, capsys):
    full, _only = roots
    kinds = {}
    for kind in ("session", "workflow"):
        payload = json.loads(
            h.cli(capsys, "scan", "--here", "--kind", kind, "--json", "--root", full)[1]
        )
        kinds[kind] = sorted(row["kind"] for row in payload["rows"])
    assert kinds == {"session": ["session", "session"], "workflow": ["workflow"]}
    since = (BASE - timedelta(days=1)).date().isoformat()
    payload = json.loads(
        h.cli(capsys, "scan", "--here", "--since", since, "--json", "--root", full)[1]
    )
    assert len(payload["rows"]) == 2
    limited = h.cli(capsys, "scan", "--here", "--limit", "1", "--root", full)
    assert limited[0] in (0, 3, 4)


def _neutral(root_span="path-1"):
    return (
        f"postmortem: no run under {root_span} that matches the requested filters "
        f"{HERE_TAIL}\n"
    )


def test_scan_here_with_no_match_is_the_neutral_exit_2(world, tmp_path, capsys):
    root = tmp_path / "nomatch"
    write_session(root, SLUG_A, uid(1), cwd=str(world["base"] / "elsewhere"), start=BASE)
    code, out, err = h.cli(capsys, "scan", "--here", "--root", root)
    assert (code, out, err) == (2, "", _neutral())
    code, out, err = h.cli(capsys, "scan", "--here", "--root", root, "--raw")
    assert (code, out, err) == (2, "", _neutral(str(root)))
    assert str(world["caller"]) not in err


def test_a_cwd_matching_session_excluded_by_kind_gives_the_same_neutral_message(
    world, tmp_path, capsys,
):
    root = tmp_path / "kind"
    write_session(root, SLUG_A, uid(1), cwd=str(world["base"] / "repo"), start=BASE)
    code, out, err = h.cli(capsys, "scan", "--here", "--kind", "workflow", "--root", root)
    assert (code, out, err) == (2, "", _neutral())


def test_a_cwd_matching_old_run_excluded_by_since_gives_the_same_neutral_message(
    world, tmp_path, capsys,
):
    root = tmp_path / "since"
    write_session(root, SLUG_A, uid(1), cwd=str(world["base"] / "repo"), start=OLD)
    code, out, err = h.cli(
        capsys, "scan", "--here", "--since", BASE.date().isoformat(), "--root", root,
    )
    assert (code, out, err) == (2, "", _neutral())
