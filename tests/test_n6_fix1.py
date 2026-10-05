"""N6 fix round 1 (docs/DESIGN.md 4.8, 4.13.5, 4.13.6; .reviews/v0.3/N6-dispositions.md):
C1 `run --here` renders the run it chose with no second load, C2 a descriptor close
failure in the alias lookup is "unavailable", S-m2 a deleted caller directory,
S-m3 cwd on non-user lines, S-m4 an uppercase UUID is no match."""

from __future__ import annotations

import json
import os

import pytest

from agent_postmortem import disclosure, reader, state
from tests import n3b_helpers as h
from tests.n6_helpers import (
    BASE,
    EXPECTED_UNAVAILABLE,
    HERE_TAIL,
    SLUG_A,
    build_identity_root,
    entry,
    session_path,
    uid,
    write_lines,
    write_session,
)

# --- C1 ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    base = (tmp_path / "work").resolve()
    caller = base / "repo" / "sub"
    caller.mkdir(parents=True)
    monkeypatch.chdir(caller)
    root = tmp_path / "root"
    path = write_session(root, SLUG_A, uid(1), cwd=str(base / "repo"), start=BASE)
    return {"root": root, "path": path}


def _fail_second_load(monkeypatch, path):
    """The chosen run stops loading after its first load (it is removed or truncated
    between choosing and rendering)."""
    real = reader.load_run
    seen = []

    def flaky(arg):
        if str(getattr(arg, "path", arg)) == str(path):
            seen.append(arg)
            if len(seen) > 1:
                raise reader.NotARun(str(path))
        return real(arg)

    monkeypatch.setattr(reader, "load_run", flaky)
    return seen


@pytest.mark.parametrize("level", ["", "--local", "--raw"])
@pytest.mark.parametrize("mode", [(), ("--json",)])
def test_c1_run_here_renders_the_run_it_chose_even_if_a_second_load_would_fail(
    world, level, mode, monkeypatch, capsys,
):
    flags = [level] if level else []
    args = ("run", "--here", "--root", world["root"], *mode, *flags)
    expected = h.cli(capsys, *args)
    assert expected[0] in (0, 3, 4), expected
    seen = _fail_second_load(monkeypatch, world["path"])
    got = h.cli(capsys, *args)
    assert got == expected
    assert len(seen) == 1


@pytest.mark.parametrize("level", ["", "--local", "--raw"])
def test_c1_a_vanished_run_never_prints_a_traceback_a_path_or_none(
    world, level, monkeypatch, capsys,
):
    flags = [level] if level else []
    _fail_second_load(monkeypatch, world["path"])
    code, out, err = h.cli(capsys, "run", "--here", "--root", world["root"], *flags)
    assert "Traceback" not in err and "None" not in err + out
    assert str(world["path"]) not in err + out
    assert code in (0, 2, 3, 4)


# --- C2 ---------------------------------------------------------------------------


@pytest.fixture
def ident(tmp_path):
    return build_identity_root(tmp_path)


def _registered_alias(tmp_path, monkeypatch, ident):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-n6"))
    book = disclosure.register([("agent", ident.w2a)])
    assert book.available
    return book.alias("agent", ident.w2a)


def _failing_close(monkeypatch):
    opened: list[int] = []
    closed: list[int] = []
    real_open = state._open_verified_state_dir
    real_close = os.close

    def spy_open(path):
        fd = real_open(path)
        opened.append(fd)
        return fd

    def spy_close(fd):
        closed.append(fd)
        real_close(fd)
        if fd in opened:
            raise OSError("simulated close failure")

    monkeypatch.setattr(state, "_open_verified_state_dir", spy_open)
    monkeypatch.setattr(os, "close", spy_close)
    return opened, closed


def test_c2_a_close_failure_makes_the_lookup_unavailable(ident, tmp_path, monkeypatch):
    alias = _registered_alias(tmp_path, monkeypatch, ident)
    opened, closed = _failing_close(monkeypatch)
    assert disclosure.lookup_agent_alias(alias, {"ref": frozenset({ident.w2a})}) is None
    assert len(opened) == 1 and closed.count(opened[0]) == 1


def test_c2_a_close_failure_is_exit_2_through_the_cli(ident, tmp_path, monkeypatch, capsys):
    alias = _registered_alias(tmp_path, monkeypatch, ident)
    opened, closed = _failing_close(monkeypatch)
    code, out, err = h.cli(capsys, "run", f"a:{alias}", "--root", ident.root)
    assert (code, out) == (2, "")
    assert err == (
        "postmortem: stable aliases unavailable on this machine, so 'value-1' "
        f"cannot be resolved ({EXPECTED_UNAVAILABLE})\n"
    )
    assert len(opened) == 1 and closed.count(opened[0]) == 1


# --- S-m2: a deleted caller directory ---------------------------------------------


@pytest.fixture
def deleted_cwd(tmp_path, monkeypatch):
    base = (tmp_path / "work").resolve()
    gone = base / "gone"
    gone.mkdir(parents=True)
    root = tmp_path / "root"
    # a run that works in the deleted directory's path and in a sibling, so a lookup that
    # treats an unreadable caller as "match everything" or "newest overall" shows up
    write_session(root, SLUG_A, uid(1), cwd=str(gone), start=BASE)
    write_session(root, SLUG_A, uid(2), cwd=str(base / "other"), start=BASE)
    monkeypatch.chdir(gone)
    gone.rmdir()
    return root


def test_s_m2_run_here_from_a_deleted_directory_is_the_neutral_exit_2(deleted_cwd, capsys):
    code, out, err = h.cli(capsys, "run", "--here", "--root", deleted_cwd)
    assert (code, out, err) == (2, "", f"postmortem: no run under path-1 {HERE_TAIL}\n")


def test_s_m2_scan_here_from_a_deleted_directory_is_the_neutral_exit_2(deleted_cwd, capsys):
    code, out, err = h.cli(capsys, "scan", "--here", "--root", deleted_cwd)
    assert (code, out, err) == (
        2, "",
        f"postmortem: no run under path-1 that matches the requested filters {HERE_TAIL}\n",
    )


# --- S-m3: cwd on lines that are not user lines -----------------------------------

_CWD_ONLY_ON = {
    "system line": lambda cwd: [{"type": "system", "cwd": cwd, "timestamp": "2026-01-05T12:00:02Z"}],
    "assistant line": lambda cwd: [entry("assistant", "x", BASE, cwd=cwd)],
    "summary line": lambda cwd: [{"type": "summary", "cwd": cwd}],
}


@pytest.mark.parametrize("name", sorted(_CWD_ONLY_ON))
def test_s_m3_a_cwd_that_sits_only_on_one_non_user_line_counts(
    name, tmp_path, monkeypatch, capsys,
):
    base = (tmp_path / "work").resolve()
    (base / "repo").mkdir(parents=True)
    root = tmp_path / "root"
    session = uid(5)
    rows = [
        entry("user", "work on it", BASE, session=session),
        *_CWD_ONLY_ON[name](str(base / "repo")),
    ]
    path = session_path(root, SLUG_A, session)
    write_lines(path, rows)
    run = reader.load_run(path)
    assert reader.run_cwd_set(run) == {str(base / "repo")}
    monkeypatch.chdir(base / "repo")
    code, out, _err = h.cli(capsys, "run", "--here", "--json", "--root", root)
    assert code in (0, 3, 4) and json.loads(out)["run"]["run_id"] == run.run_id
    code, out, _err = h.cli(capsys, "scan", "--here", "--json", "--root", root)
    assert code in (0, 3, 4) and len(json.loads(out)["rows"]) == 1


# --- S-m4: an uppercase UUID is not a match (a pin of today's behavior) -----------


def test_s_m4_an_uppercase_uuid_matches_nothing_and_the_lowercase_form_resolves(
    tmp_path, capsys,
):
    root = tmp_path / "root"
    session = uid(7)
    write_session(root, SLUG_A, session, cwd="/work/n6/up", start=BASE)
    code, _out, _err = h.cli(capsys, "run", session, "--json", "--root", root)
    assert code in (0, 3, 4)
    code, out, err = h.cli(capsys, "run", session.upper(), "--root", root)
    assert (code, out) == (2, "")
    assert err.startswith("postmortem: no run matches 'uuid:0007' under path-1")
