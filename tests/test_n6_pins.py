"""N6 pins (plan rows R2 prefix, R5 watch, R6): behavior that holds before and after
N6 and that the new dispatch must not disturb. None of these is a RED test."""

from __future__ import annotations

import json

import pytest

from agent_postmortem import cli, disclosure, reader, watch
from tests import n3b_helpers as h
from tests.n6_helpers import (
    BASE,
    PREFIX_ADVICE,
    SLUG_A,
    WF1,
    WF2,
    WF3,
    aid,
    build_identity_root,
    transcript_rows,
    uid,
    write_lines,
)


@pytest.fixture
def ident(tmp_path):
    return build_identity_root(tmp_path)


@pytest.mark.parametrize("level", ["", "--local", "--raw"])
def test_r2_a_prefix_ambiguity_keeps_its_longer_prefix_advice(ident, level, capsys):
    flags = [level] if level else []
    code, out, err = h.cli(capsys, "run", "wf_00000000-6a", "--root", ident.root, *flags)
    assert (code, out) == (1, "")
    ids = f"{WF1}, {WF2}, {WF3}"
    if level == "--raw":
        assert err == (
            f"postmortem: 'wf_00000000-6a' matches more than one run under {ident.root}: {ids}\n"
        )
    else:
        assert err == (
            f"postmortem: 'value-1' matches more than one run under path-1: {ids} "
            f"({PREFIX_ADVICE})\n"
        )


def _workflow_like_dir(base, name):
    run_dir = base / name
    agent = aid(990)
    write_lines(run_dir / "journal.jsonl", [
        {"type": "started", "key": "k", "agentId": agent, "label": "l", "phase": "p"},
        {"type": "result", "key": "k", "agentId": agent, "result": {"ok": True}},
    ])
    write_lines(run_dir / f"agent-{agent}.jsonl", transcript_rows("/work/n6/x", BASE, agent=agent))
    return run_dir


@pytest.mark.parametrize("form", ["uuid", "agent stem", "alias"])
def test_r6_an_existing_path_wins_over_a_special_form_name(form, ident, tmp_path, monkeypatch, capsys):
    if form == "uuid":
        name, would_be = ident.s1, reader.load_run(ident.paths["s1"]).run_id
    elif form == "agent stem":
        name, would_be = f"agent-{ident.w2a}", WF2
    else:
        book = disclosure.register([("agent", ident.w2a)])
        name, would_be = f"a:{book.alias('agent', ident.w2a)}", WF2
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _workflow_like_dir(cwd, name)
    monkeypatch.chdir(cwd)
    code, out, _err = h.cli(capsys, "run", name, "--json", "--root", ident.root)
    payload = json.loads(out)["run"]
    assert payload["run_id"] != would_be
    assert payload["run_id"].startswith("wf_") and payload["run_id"].endswith("-x")


@pytest.mark.parametrize("arg", [
    "a:" + "ABCDEF012345", "a:" + "abcdef01234", "a" + "6e6" + "000000000000f",
    "AGENT-a6e6000000000000f", "agent-A6E6000000000000F",
])
def test_not_new_forms_take_the_prefix_rule_and_end_in_exit_2(arg, ident, capsys):
    code, out, err = h.cli(capsys, "run", arg, "--root", ident.root)
    assert (code, out) == (2, "")
    assert err.startswith("postmortem: no run matches ")


class _Clock:
    def __init__(self):
        self.now = 0.0


def test_r5_watch_with_an_alias_or_a_stem_behaves_as_before(ident, monkeypatch, capsys):
    clock = _Clock()
    monkeypatch.setattr(watch, "_now", lambda: clock.now)
    monkeypatch.setattr(watch, "_sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    book = disclosure.register([("agent", ident.w2a)])
    for arg in (f"a:{book.alias('agent', ident.w2a)}", f"agent-{ident.w2a}", ident.s1):
        clock.now = 0.0
        code = cli.main(["watch", arg, "--root", str(ident.root), "--interval", "30"])
        captured = capsys.readouterr()
        assert code == 2
        assert "no journal appeared within 60 s" in captured.err
        assert captured.out == ""


@pytest.mark.parametrize("argv", [
    ["rescue", "plan", "x", "--here"], ["rescue", "status", "x", "--here"],
    ["rules", "--run", "x", "--here"], ["watch", "x", "--here"], ["trend", "--here"],
])
def test_here_is_only_on_run_and_scan(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 1
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_run_cwd_set_never_reaches_json_or_text(ident, tmp_path, capsys):
    planted = "/work/n6/zz-entry-cwd-only"
    path = tmp_path / "solo" / SLUG_A / f"{uid(80)}.jsonl"
    write_lines(path, transcript_rows(planted, BASE, session=uid(80)))
    for flags in ([], ["--json"], ["--raw"], ["--raw", "--json"], ["--local", "--json"]):
        code, out, err = h.cli(capsys, "run", path, *flags)
        assert planted not in out + err
        assert "entry_cwds" not in out + err
