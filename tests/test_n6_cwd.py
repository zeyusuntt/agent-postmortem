"""N6 (docs/DESIGN.md 4.13.6, 4.13.11 "N6"; plan I5, I6): the run's cwd set and the
lexical containment `--here` uses, at function level and through `scan --here` and
`run --here`. Every containment row of the plan's acceptance checklist is a named
row here."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_postmortem import reader
from tests import n3b_helpers as h
from tests.n6_helpers import (
    BASE,
    SLUG_A,
    aid,
    entry,
    session_path,
    transcript_rows,
    uid,
    write_lines,
    write_workflow,
)

_UNSET = object()


@pytest.fixture
def base(tmp_path):
    (tmp_path / "work").mkdir()
    return (tmp_path / "work").resolve()


def _fill(template, base):
    if isinstance(template, str):
        return template.replace("{b}", str(base))
    return template


def _session(tmp_path, base, main_cwd, *, extra_main=(), children=(), n=1):
    """A loaded session whose main transcript carries `main_cwd` (`_UNSET` leaves the
    key out) plus any `extra_main` raw lines, and `children` as (cwd) values."""
    root = tmp_path / "root"
    session = uid(100 + n)
    path = session_path(root, SLUG_A, session)
    rows = transcript_rows(... if main_cwd is _UNSET else _fill(main_cwd, base), BASE,
                           session=session)
    write_lines(path, [*rows, *extra_main])
    for index, child_cwd in enumerate(children):
        child = aid(500 + index)
        write_lines(
            root / SLUG_A / session / "subagents" / f"agent-{child}.jsonl",
            transcript_rows(_fill(child_cwd, base), BASE, session=session, agent=child),
        )
    return root, reader.load_run(path)


# --- function level and CLI level over one table ------------------------------------

# name: (run cwd, caller, expected, can the CLI caller be this directory)
CONTAINMENT = {
    "equal": ("{b}/repo", "{b}/repo", True, True),
    "inside": ("{b}/repo", "{b}/repo/src", True, True),
    "deep inside": ("{b}/repo", "{b}/repo/a/b/c/d", True, True),
    "sibling with a shared prefix": ("{b}/repo", "{b}/repo2", False, True),
    "caller above the run cwd": ("{b}/repo", "{b}", False, True),
    "dotdot in the run cwd": ("{b}/other/../repo", "{b}/repo/src", True, True),
    "dotdot in the caller": ("{b}/repo", "{b}/repo/src/..", True, False),
    "a dotdot caller that leaves the run cwd": ("{b}/repo", "{b}/repo/../x", False, False),
    "trailing slash on the run cwd": ("{b}/repo/", "{b}/repo", True, True),
    "trailing slash on the caller": ("{b}/repo", "{b}/repo/src/", True, False),
    "a run cwd of the filesystem root matches any caller": ("/", "{b}/repo", True, True),
}


@pytest.mark.parametrize("name", sorted(CONTAINMENT))
def test_containment_at_function_level(name, tmp_path, base):
    run_cwd, caller, expected, _cli = CONTAINMENT[name]
    _root, run = _session(tmp_path, base, run_cwd)
    assert reader.run_matches_cwd(run, _fill(caller, base)) is expected


@pytest.mark.parametrize("name", sorted(n for n, row in CONTAINMENT.items() if row[3]))
def test_containment_through_scan_here_and_run_here(name, tmp_path, base, monkeypatch, capsys):
    run_cwd, caller, expected, _cli = CONTAINMENT[name]
    root, run = _session(tmp_path, base, run_cwd)
    caller_dir = Path(os.path.normpath(_fill(caller, base)))
    caller_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(caller_dir)
    scan = h.cli(capsys, "scan", "--here", "--json", "--root", root)
    here = h.cli(capsys, "run", "--here", "--json", "--root", root)
    if expected:
        assert scan[0] in (0, 3, 4) and [r["run_id"] for r in json.loads(scan[1])["rows"]] == [
            run.run_id,
        ]
        assert here[0] in (0, 3, 4) and json.loads(here[1])["run"]["run_id"] == run.run_id
    else:
        assert (scan[0], scan[1]) == (2, "")
        assert (here[0], here[1]) == (2, "")


# --- relative values, the literal tilde, and values that are ignored ------------------


def test_a_relative_cwd_value_is_made_absolute_against_the_process_cwd(
    tmp_path, base, monkeypatch,
):
    (base / "rel" / "dir").mkdir(parents=True)
    (base / "elsewhere").mkdir()
    _root, run = _session(tmp_path, base, "rel/dir")
    monkeypatch.chdir(base)
    assert reader.run_matches_cwd(run, str(base / "rel" / "dir")) is True
    monkeypatch.chdir(base / "elsewhere")
    assert reader.run_matches_cwd(run, str(base / "rel" / "dir")) is False


def test_a_relative_dot_matches_the_process_cwd_through_the_command_line(
    tmp_path, base, monkeypatch, capsys,
):
    root, run = _session(tmp_path, base, ".")
    monkeypatch.chdir(base)
    code, out, _err = h.cli(capsys, "scan", "--here", "--json", "--root", root)
    assert code in (0, 3, 4) and len(json.loads(out)["rows"]) == 1


def test_a_literal_tilde_is_a_relative_name_never_the_home_directory(
    tmp_path, base, monkeypatch, capsys,
):
    home = base / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (base / "~").mkdir()
    _root, run = _session(tmp_path, base, "~")
    monkeypatch.chdir(base / "home")
    assert reader.run_matches_cwd(run, str(home)) is False
    monkeypatch.chdir(base)
    assert reader.run_matches_cwd(run, str(base / "~")) is True
    root = tmp_path / "root"
    monkeypatch.chdir(home)
    code, out, _err = h.cli(capsys, "scan", "--here", "--json", "--root", root)
    assert (code, out) == (2, "")


@pytest.mark.parametrize("name, value", [
    ("an empty string", ""), ("a number", 5), ("a list", ["/x"]), ("null", None),
])
def test_a_cwd_value_that_is_not_a_usable_string_contributes_nothing(
    name, value, tmp_path, base, monkeypatch, capsys,
):
    root, run = _session(tmp_path, base, value)
    assert reader.run_cwd_set(run) == frozenset()
    assert reader.run_matches_cwd(run, str(base)) is False
    assert reader.run_matches_cwd(run, "/") is False
    monkeypatch.chdir(base)
    code, out, _err = h.cli(capsys, "scan", "--here", "--json", "--root", root)
    assert (code, out) == (2, "")


def test_a_run_with_no_cwd_never_matches(tmp_path, base, monkeypatch, capsys):
    root, run = _session(tmp_path, base, _UNSET)
    assert reader.run_cwd_set(run) == frozenset()
    assert reader.run_matches_cwd(run, str(base)) is False
    monkeypatch.chdir(base)
    assert h.cli(capsys, "scan", "--here", "--json", "--root", root)[:2] == (2, "")
    assert h.cli(capsys, "run", "--here", "--json", "--root", root)[:2] == (2, "")


# --- which transcripts count ----------------------------------------------------------


def test_a_child_attempts_cwd_counts(tmp_path, base, monkeypatch, capsys):
    root, run = _session(tmp_path, base, _UNSET, children=("{b}/child",))
    assert reader.run_cwd_set(run) == {str(base / "child")}
    assert reader.run_matches_cwd(run, str(base / "child" / "x")) is True
    (base / "child").mkdir()
    monkeypatch.chdir(base / "child")
    code, out, _err = h.cli(capsys, "scan", "--here", "--json", "--root", root)
    assert code in (0, 3, 4) and len(json.loads(out)["rows"]) == 1


def test_a_workflows_parent_session_cwd_and_the_journal_never_count(
    tmp_path, base, monkeypatch, capsys,
):
    root = tmp_path / "root"
    parent, journal = str(base / "parent"), str(base / "journal")
    run_dir = write_workflow(
        root, SLUG_A, uid(60), "wf_00000000-6a7",
        ((aid(61), ..., BASE),), parent_cwd=parent, journal_extra={"cwd": journal},
    )
    run = reader.load_run(run_dir)
    assert run.kind == "workflow" and reader.run_cwd_set(run) == frozenset()
    for caller in (parent, journal, str(base / "parent" / "x")):
        assert reader.run_matches_cwd(run, caller) is False
    (base / "parent").mkdir()
    monkeypatch.chdir(base / "parent")
    code, out, _err = h.cli(capsys, "scan", "--here", "--kind", "workflow", "--json",
                            "--root", root)
    assert (code, out) == (2, "")


# --- lines that carry nothing ---------------------------------------------------------


def test_an_unparsable_line_a_non_object_line_and_a_nested_cwd_contribute_nothing(
    tmp_path, base,
):
    good = str(base / "good")
    extra = [
        '{"type": "user", "cwd": "' + str(base / "bad"),  # truncated JSON
        '["' + str(base / "listed") + '"]',
        json.dumps({"type": "user", "message": {"cwd": str(base / "nested")}}),
        json.dumps({"type": "user", "toolUseResult": {"cwd": str(base / "result")}}),
    ]
    _root, run = _session(tmp_path, base, good, extra_main=extra)
    assert reader.run_cwd_set(run) == {good}


def test_entry_cwds_are_first_seen_ordered_deduplicated_and_internal(tmp_path, base):
    a, b = str(base / "a"), str(base / "b")
    extra = [entry("user", "x", BASE, cwd=b), entry("user", "y", BASE, cwd=a)]
    _root, run = _session(tmp_path, base, a, extra_main=extra)
    assert run.main.entry_cwds == (a, b)
    assert "entry_cwds" not in run.main.to_dict()


def test_a_transcript_that_becomes_unreadable_midway_contributes_no_cwd(tmp_path, base):
    root = tmp_path / "root"
    session = uid(120)
    path = session_path(root, SLUG_A, session)
    write_lines(path, transcript_rows(str(base / "kept"), BASE, session=session))
    child = aid(520)
    child_path = root / SLUG_A / session / "subagents" / f"agent-{child}.jsonl"
    # more than one read buffer of valid lines, so some are parsed before the bad byte
    padding = [entry("user", "x" * 900, BASE, cwd=str(base / "lost")) for _ in range(20)]
    write_lines(
        child_path,
        [*transcript_rows(str(base / "lost"), BASE, session=session, agent=child), *padding],
    )
    with child_path.open("ab") as handle:
        handle.write(b"\xff\xfe\n")
    run = reader.load_run(path)
    assert reader.run_cwd_set(run) == {str(base / "kept")}

