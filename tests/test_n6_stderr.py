"""N6 (docs/DESIGN.md 4.13.5, 4.13.6; plan I4, row R7): the stderr matrix. Every new
error and every reused one, at share, local, and raw, through run, rescue plan,
rescue status, and rules --run. At share and local the structural message and its
spans are present and no planted root, caller directory, session UUID, agent id, or
alias is; at raw the typed input and the root are present and the caller's
directory is still absent from the --here messages."""

from __future__ import annotations

import pytest

from agent_postmortem import disclosure
from tests import n3b_helpers as h
from tests.n6_helpers import (
    EXACT_ADVICE,
    EXPECTED_RUN,
    EXPECTED_UNAVAILABLE,
    HERE_TAIL,
    NO_BOTH,
    NO_NEITHER,
    WF1,
    WF3,
    aid,
    build_identity_root,
    uid,
)

COMMANDS = {
    "run": ["run"],
    "rescue plan": ["rescue", "plan"],
    "rescue status": ["rescue", "status"],
    "rules --run": ["rules", "--run"],
}
LEVELS = {"share": [], "local": ["--local"], "raw": ["--raw"]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    caller = tmp_path / "zzN6CallerDir"
    caller.mkdir()
    monkeypatch.chdir(caller)
    ident = build_identity_root(tmp_path / "zzN6RootDir")
    return ident, caller


def _register(*ids):
    book = disclosure.register([("agent", agent_id) for agent_id in ids])
    assert book.available
    return {agent_id: book.alias("agent", agent_id) for agent_id in ids}


def _planted(ident, caller, *extra):
    values = [str(ident.root), str(caller), "zzN6", ident.s1, ident.s2, ident.w1a, ident.w2a,
              ident.s1a, *extra]
    return values


def _assert_nothing_planted(err, ident, caller, *extra):
    for value in _planted(ident, caller, *extra):
        assert value not in err, value
    assert "Traceback" not in err


def _case(kind, ident, monkeypatch, tmp_path):
    """(argument, exit code, share/local message, raw message) for one kind. The share
    message is for `shown_arg`; `{root}` is the raw root."""
    if kind == "uuid no match":
        arg = uid(777)
        return arg, 2, (
            f"postmortem: no run matches 'uuid:{arg[-4:]}' under path-1 ({EXPECTED_RUN})\n"
        ), f"postmortem: no run matches '{arg}' under {ident.root}\n"
    if kind == "agent stem no match":
        arg = f"agent-{aid(777)}"
        return arg, 2, (
            f"postmortem: no run matches 'value-1' under path-1 ({EXPECTED_RUN})\n"
        ), f"postmortem: no run matches '{arg}' under {ident.root}\n"
    if kind == "alias no match":
        _register(ident.w2a)
        arg = "a:" + "0" * 12
        return arg, 2, (
            f"postmortem: no run matches 'value-1' under path-1 ({EXPECTED_RUN})\n"
        ), f"postmortem: no run matches '{arg}' under {ident.root}\n"
    if kind == "alias state unavailable":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "no-such-state"))
        arg = "a:" + "1" * 12
        tail = f"cannot be resolved ({EXPECTED_UNAVAILABLE})\n"
        return arg, 2, (
            f"postmortem: stable aliases unavailable on this machine, so 'value-1' {tail}"
        ), f"postmortem: stable aliases unavailable on this machine, so '{arg}' {tail}"
    ids = {
        "uuid": [r for r in (_run_ids(ident, "s2a", "s2b"))],
        "stem": [WF1, WF3],
        "alias": [WF1, WF3],
    }
    if kind == "ambiguous session uuid":
        arg, shown, listed = ident.s2, f"uuid:{ident.s2[-4:]}", ids["uuid"]
    elif kind == "ambiguous agent stem":
        arg, shown, listed = f"agent-{ident.w1a}", "value-1", ids["stem"]
    else:
        arg, shown, listed = f"a:{_register(ident.w1a)[ident.w1a]}", "value-1", ids["alias"]
    joined = ", ".join(listed)
    return arg, 1, (
        f"postmortem: '{shown}' matches more than one run under path-1: {joined} ({EXACT_ADVICE})\n"
    ), f"postmortem: '{arg}' matches more than one run under {ident.root}: {joined}\n"


def _run_ids(ident, *keys):
    from agent_postmortem import reader

    wanted = [ident.paths[key] for key in keys]
    return [ref.run_id for ref in reader.discover_runs(ident.root) if ref.path in wanted]


KINDS = [
    "uuid no match", "agent stem no match", "alias no match", "alias state unavailable",
    "ambiguous session uuid", "ambiguous agent stem", "ambiguous alias",
]


def _matrix():
    """Every (kind, command, level)."""
    for kind in KINDS:
        for command in sorted(COMMANDS):
            for level in sorted(LEVELS):
                yield pytest.param(kind, command, level, id=f"{kind}-{command}-{level}")


@pytest.mark.parametrize("kind, command, level", list(_matrix()))
def test_r7_run_resolution_errors_per_level(kind, command, level, world, monkeypatch, tmp_path, capsys):
    ident, caller = world
    arg, code, shared, raw = _case(kind, ident, monkeypatch, tmp_path)
    got = h.cli(capsys, *COMMANDS[command], arg, "--root", ident.root, *LEVELS[level])
    assert got[0] == code and got[1] == ""
    if level == "raw":
        assert got[2] == raw
        assert str(caller) not in got[2]
    else:
        assert got[2] == shared
        _assert_nothing_planted(got[2], ident, caller)
        assert arg not in got[2]


# --- --here messages -----------------------------------------------------------------


def _here_args(kind, ident):
    if kind == "run both":
        return ["run", "wf_00000000-6a1", "--here", "--root", ident.root], 1
    if kind == "run neither":
        return ["run", "--root", ident.root], 1
    if kind == "run here no match":
        return ["run", "--here", "--root", ident.root], 2
    return ["scan", "--here", "--root", ident.root], 2


@pytest.mark.parametrize("level", sorted(LEVELS))
@pytest.mark.parametrize("kind", ["run both", "run neither", "run here no match", "scan here no match"])
def test_r7_here_messages_per_level(kind, level, world, capsys):
    ident, caller = world
    args, code = _here_args(kind, ident)
    got = h.cli(capsys, *args, *LEVELS[level])
    root = str(ident.root) if level == "raw" else "path-1"
    expected = {
        "run both": NO_BOTH,
        "run neither": NO_NEITHER,
        "run here no match": (
            f"postmortem: no run under {root} {HERE_TAIL}\n"
            "  agent-postmortem scan lists every run it found; agent-postmortem run <run-id> diagnoses one.\n"
        ),
        "scan here no match": (
            f"postmortem: no run under {root} that matches the requested filters {HERE_TAIL}\n"
        ),
    }[kind]
    assert got == (code, "", expected)
    assert str(caller) not in got[2] and "zzN6Caller" not in got[2]
    if level != "raw":
        _assert_nothing_planted(got[2], ident, caller)
