"""One bad line never crashes a scan (docs/DESIGN.md 4.1, 4.2, 4.8, 0.3.4).

A JSON line nested deeper than Python's parser can follow raises RecursionError, not a
JSONDecodeError. Wherever run data is parsed (a transcript, journal.jsonl, an agent's
meta.json, the parent transcript, the hook and watch readers) it is treated like any other
line that is not JSON: skipped, with a diagnostic, never a crash and never a silently
dropped run. `scan` carries on over the other runs and says how many lines and files it
skipped.
"""

from __future__ import annotations

import contextlib
import io
import json
import uuid
from pathlib import Path

import pytest

from agent_postmortem import cli, disclosure, reader
from agent_postmortem.cli import main

DEEP = "[" * 200000
AGENT = "a" + "d" * 16
WF = "wf_00000000-e0d"
# a run directory name that is not a run id: it must never be printed (DESIGN 5.1)
ODD_WF = "wf_ zzPrivate name"
UNREAD = (
    "could not be read (its data is nested too deeply to follow); nothing was judged"
)


def _uid(n: int) -> str:
    return str(uuid.UUID(int=0xD0E9 << 64 | n))


def _good(sid: str, second: int) -> list[str]:
    return [
        json.dumps({"type": "user", "timestamp": f"2026-09-01T10:00:{second:02d}.000Z",
                    "sessionId": sid, "cwd": "/tmp/deep", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "assistant", "timestamp": f"2026-09-01T10:00:{second + 1:02d}.000Z",
                    "sessionId": sid, "cwd": "/tmp/deep",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}),
    ]


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    (path / ".claude" / "projects" / "-tmp-deep").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return path


def _project(home):
    return home / ".claude" / "projects" / "-tmp-deep"


def _session(home, n: int, lines: list[str]):
    path = _project(home) / f"{_uid(n)}.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _workflow(home, *, journal_extra: str | None = None, meta_text: str | None = None,
              name: str = WF):
    session_dir = _project(home) / _uid(90)
    wf = session_dir / "subagents" / "workflows" / name
    wf.mkdir(parents=True)
    journal = [json.dumps({"type": "launched"}),
               json.dumps({"type": "started", "key": "k1", "label": "a", "phase": "p",
                           "agentId": AGENT})]
    if journal_extra is not None:
        journal.append(journal_extra)
    (wf / "journal.jsonl").write_text("".join(line + "\n" for line in journal), encoding="utf-8")
    (wf / f"agent-{AGENT}.jsonl").write_text(
        "".join(line + "\n" for line in _good(_uid(90), 10)), encoding="utf-8",
    )
    (wf / f"agent-{AGENT}.meta.json").write_text(
        meta_text if meta_text is not None else json.dumps({"agentType": "x"}), encoding="utf-8",
    )
    return wf


def _fail_loads(monkeypatch, only=None):
    """Make reading a run raise RecursionError past every parse site (a walker, say)."""
    real = reader._load_run

    def flaky(ref_or_path):
        path = getattr(ref_or_path, "path", None) or Path(ref_or_path)
        if only is None or path == only:
            raise RecursionError("maximum recursion depth exceeded")
        return real(ref_or_path)

    monkeypatch.setattr(reader, "_load_run", flaky)


def _cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(item) for item in argv])
    return code, out.getvalue(), err.getvalue()


def test_a_deep_transcript_line_is_a_skipped_line(home):
    good = _good(_uid(1), 0)
    path = _session(home, 1, [good[0], DEEP, good[1]])
    run = reader.load_run(path)
    assert f"{path.name}:2: nested too deeply to parse" in run.diagnostics
    assert run.skipped_lines == 1
    assert len(run.attempts) == 0 or run.attempts is not None  # it loaded


def test_a_deep_journal_line_is_a_skipped_line_and_the_run_is_incomplete(home):
    wf = _workflow(home, journal_extra=DEEP)
    run = reader.load_run(wf)
    assert "journal.jsonl:3: nested too deeply to parse" in run.diagnostics
    assert run.status == "INCOMPLETE"
    assert run.skipped_lines == 1


def test_a_deep_meta_json_is_a_skipped_file(home):
    wf = _workflow(home, meta_text=DEEP)
    run = reader.load_run(wf)
    assert f"agent-{AGENT}.meta.json: nested too deeply to parse" in run.diagnostics
    assert run.skipped_files == 1


def test_scan_carries_on_and_says_what_it_skipped(home):
    _session(home, 1, _good(_uid(1), 0))
    good = _good(_uid(2), 20)
    _session(home, 2, [good[0], DEEP, good[1]])
    _session(home, 3, [DEEP])  # no readable line: not a run, but not silently gone either
    code, out, err = _cli("scan", "--all")
    assert code in (0, 4), err
    bad_run = reader.discover_runs(_project(home).parent)
    ids = sorted(ref.run_id for ref in bad_run)
    assert len(ids) == 2
    skipped = [line for line in out.splitlines() if line.startswith("SKIPPED")]
    assert len(skipped) == 1
    assert "1 unreadable line in 1 run (" in skipped[0]
    assert "1 transcript file with no readable line" in skipped[0]
    assert "agent-postmortem run <run-id> --json lists each line under diagnostics" in skipped[0]
    code, out, _err = _cli("scan")
    assert any(line.startswith("SKIPPED") for line in out.splitlines())


def test_scan_carries_on_when_one_run_cannot_be_loaded(home, monkeypatch):
    _session(home, 1, _good(_uid(1), 0))
    _session(home, 2, _good(_uid(2), 20))
    _fail_loads(monkeypatch, only=reader.discover_runs(_project(home).parent)[0].path)
    code, out, _err = _cli("scan", "--all")
    skipped = [line for line in out.splitlines() if line.startswith("SKIPPED")]
    assert len(skipped) == 1 and "1 run that could not be read" in skipped[0]
    assert sum(1 for line in out.splitlines() if line.startswith("s_")) == 1
    assert code == 4


def test_the_empty_state_says_a_file_had_no_readable_line(home):
    _session(home, 3, [DEEP])
    code, out, err = _cli("scan")
    assert (code, out) == (2, "")
    lines = err.splitlines()
    assert lines[0] == "postmortem: no runs found under ~/.claude/projects"
    assert lines[1] == (
        "  1 transcript file there had no readable line (not JSON, or nested too deeply)"
    )


def test_run_rescue_hook_and_watch_never_crash_on_a_deep_line(home, monkeypatch, capsys):
    wf = _workflow(home, journal_extra=DEEP)
    good = _good(_uid(4), 30)
    session = _session(home, 4, [good[0], DEEP, good[1]])
    for argv in (["run", wf], ["run", session], ["rescue", "plan", wf], ["rescue", "status", wf]):
        code, _out, err = _cli(*argv)
        assert "RecursionError" not in err, argv
        assert code in (0, 2, 3, 4), (argv, code, err)
    payload = json.dumps({"session_id": _uid(90), "transcript_path": str(_project(home) / f"{_uid(90)}.jsonl"),
                          "cwd": "/tmp/deep", "hook_event_name": "Stop"})
    (_project(home) / f"{_uid(90)}.jsonl").write_text("\n".join(_good(_uid(90), 40) + [DEEP]) + "\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code, _out, err = _cli("hook", "check")
    assert code == 0 and "RecursionError" not in err
    from tests.test_watch import _Clock, _patch_advancing_sleep, _patch_clock

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_advancing_sleep(monkeypatch, clock)
    code, _out, err = _cli("watch", wf, "--interval", "10", "--max-hours", "0.1")
    assert "RecursionError" not in err


def test_scan_names_a_run_it_cannot_read_by_its_safe_id(home, monkeypatch):
    _session(home, 1, _good(_uid(1), 0))
    wf = _workflow(home, name=ODD_WF)
    _fail_loads(monkeypatch, only=wf)
    safe = reader.valid_workflow_run_id(ODD_WF)
    code, out, err = _cli("scan", "--all")
    assert code == 4
    skipped = [line for line in out.splitlines() if line.startswith("SKIPPED")]
    assert len(skipped) == 1 and f"1 run that could not be read ({safe})" in skipped[0]
    assert "zzPrivate" not in out + err
    # --json keeps stdout a JSON document; what it skipped goes to stderr
    code, out, err = _cli("scan", "--json")
    assert code == 4
    json.loads(out)
    assert any(
        line.startswith("SKIPPED") and f"({safe})" in line for line in err.splitlines()
    )
    assert "zzPrivate" not in out + err


def test_scan_where_no_run_can_be_read_is_incomplete_not_empty(home, monkeypatch):
    wf = _workflow(home, name=ODD_WF)
    _fail_loads(monkeypatch, only=wf)
    safe = reader.valid_workflow_run_id(ODD_WF)
    code, out, err = _cli("scan")
    assert (code, out) == (4, "")
    assert f"  1 run there could not be read ({safe})" in err.splitlines()
    assert "zzPrivate" not in err


@pytest.mark.parametrize("argv", [
    ["rules"], ["rules", "--json"], ["rules", "propose"], ["rules", "propose", "--json"],
    ["rules", "review"], ["rules", "review", "--json"], ["trend"], ["trend", "--json"],
])
def test_fleet_commands_say_which_runs_they_left_out(home, monkeypatch, argv):
    _session(home, 1, _good(_uid(1), 0))
    wf = _workflow(home, name=ODD_WF)
    _fail_loads(monkeypatch, only=wf)
    monkeypatch.setattr(cli, "_adopted_entries", lambda cfg: [
        {"rule_id": "R-GIT-CWD", "target": "t.md", "applied_at": "2026-08-15"},
    ])
    safe = reader.valid_workflow_run_id(ODD_WF)
    code, out, err = _cli(*argv)
    assert "RecursionError" not in err and "unexpected" not in err, err
    assert f"postmortem: 1 run could not be read and was left out ({safe})" in err.splitlines()
    assert "zzPrivate" not in out + err
    if "--json" in argv:
        json.loads(out)


def test_here_says_which_runs_it_left_out(home, monkeypatch, tmp_path):
    _session(home, 1, _good(_uid(1), 0))
    wf = _workflow(home, name=ODD_WF)
    _fail_loads(monkeypatch, only=wf)
    here = tmp_path / "deep"
    here.mkdir()
    monkeypatch.chdir(here)
    safe = reader.valid_workflow_run_id(ODD_WF)
    code, _out, err = _cli("run", "--here")
    assert "unexpected" not in err
    assert f"postmortem: 1 run could not be read and was left out ({safe})" in err.splitlines()


def test_an_explicit_run_that_cannot_be_read_says_so_and_is_incomplete(home, monkeypatch):
    wf = _workflow(home, name=ODD_WF)
    session = _session(home, 4, _good(_uid(4), 30))
    session_id = next(
        ref.run_id for ref in reader.discover_runs(_project(home).parent) if ref.path == session
    )
    _fail_loads(monkeypatch)
    safe = reader.valid_workflow_run_id(ODD_WF)
    for argv, shown in (
        (["run", wf], safe), (["run", "--json", wf], safe), (["run", session], session_id),
        (["rules", "--run", wf], safe), (["rules", "propose", "--run", wf], safe),
        (["rescue", "plan", wf], safe), (["rescue", "status", wf], safe),
    ):
        code, out, err = _cli(*argv)
        assert code == 4, (argv, code, err)
        assert err.splitlines()[-1] == f"postmortem: {shown} {UNREAD}", argv
        assert "unexpected" not in err and "zzPrivate" not in out + err, argv


def test_a_deep_parent_line_is_counted_for_each_run_that_reads_it(home):
    wf = _workflow(home)
    good = _good(_uid(90), 0)
    parent = _project(home) / f"{_uid(90)}.jsonl"
    parent.write_text(good[0] + "\n" + DEEP + "\n" + good[1] + "\n", encoding="utf-8")
    run = reader.load_run(wf)
    assert run.skipped_lines == 1
    assert (
        f"{parent.name}:2: undecodable parent transcript line: nested too deeply to parse"
        in run.diagnostics
    )
    # the session reads the same file as its own transcript: one line, counted once
    assert reader.load_run(parent).skipped_lines == 1


@pytest.mark.parametrize("tail", [b'{"type":', b'{"type": "\xc3'])
def test_an_unfinished_last_transcript_line_is_not_counted_as_skipped(home, tail):
    good = _good(_uid(5), 0)
    path = _project(home) / f"{_uid(5)}.jsonl"
    path.write_bytes("".join(line + "\n" for line in good).encode("utf-8") + tail)
    run = reader.load_run(path)
    assert run.skipped_lines == 0
    assert any(d.startswith(f"{path.name}:3: ") for d in run.diagnostics)


def test_an_unfinished_last_journal_line_is_not_counted_as_skipped(home):
    wf = _workflow(home)
    with (wf / "journal.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"type":')
    run = reader.load_run(wf)
    assert run.skipped_lines == 0
    assert any(d.startswith("journal.jsonl:3: ") for d in run.diagnostics)


def test_the_skipped_line_points_at_a_command_that_names_each_line(home):
    good = _good(_uid(2), 20)
    _session(home, 2, [good[0], DEEP, good[1]])
    _code, out, _err = _cli("scan", "--all")
    skipped = next(line for line in out.splitlines() if line.startswith("SKIPPED"))
    assert skipped.endswith(
        "; agent-postmortem run <run-id> --json lists each line under diagnostics"
    )
    run_id = reader.discover_runs(_project(home).parent)[0].run_id
    _code, out, _err = _cli("run", run_id, "--json")
    diagnostics = json.loads(out)["run"]["diagnostics"]
    assert any(d.endswith(":2: nested too deeply to parse") for d in diagnostics)


def test_watch_says_a_finished_run_it_cannot_read_could_not_be_read(home, monkeypatch, tmp_path):
    from tests.test_watch import (
        _assistant_text,
        _Clock,
        _meta,
        _patch_clock,
        _result,
        _started,
        _transcript_path,
        _user_text,
        _write_lines,
        _write_transcript,
    )

    run_dir = tmp_path / "wf_00000000-fad"
    run_dir.mkdir(parents=True)
    key, agent = "v2:only", "aaa0000000000001a"
    _write_lines(run_dir / "journal.jsonl", [
        _started("2026-02-01T00:00:00.000Z", key, agent, "impl"),
        _result("2026-02-01T00:00:30.000Z", key, agent),
    ])
    _meta(run_dir, agent)
    _write_transcript(_transcript_path(run_dir, agent), [
        _user_text("2026-02-01T00:00:00.000Z", "Only."),
        _assistant_text("2026-02-01T00:00:29.000Z", "Done."),
    ])
    _patch_clock(monkeypatch, _Clock())
    _fail_loads(monkeypatch)
    code, _out, err = _cli("watch", run_dir, "--interval", "1", "--done-grace", "0")
    assert code == 4
    assert err.splitlines()[-1] == f"postmortem: wf_00000000-fad {UNREAD}"


def test_an_agent_id_or_alias_whose_only_run_cannot_be_read_says_so(home, monkeypatch):
    """The cheap candidate check found the agent there; a run that cannot be read cannot
    refute it, so the command loads it and says it could not be read."""
    _workflow(home)
    monkeypatch.setattr(disclosure, "lookup_agent_alias", lambda alias, ids_by_ref: {
        ref: ids for ref, ids in ids_by_ref.items() if AGENT in ids
    })
    _fail_loads(monkeypatch)
    alias = "a:" + "0" * 12
    for argv in (["run", f"agent-{AGENT}"], ["run", alias], ["rules", "--run", alias]):
        code, _out, err = _cli(*argv)
        assert code == 4, (argv, code, err)
        assert err.splitlines()[-1] == f"postmortem: {WF} {UNREAD}", argv


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_scan_here_with_only_unreadable_runs_names_them(home, monkeypatch, tmp_path, json_flag):
    wf = _workflow(home, name=ODD_WF)
    _fail_loads(monkeypatch, only=wf)
    here = tmp_path / "deep"
    here.mkdir()
    monkeypatch.chdir(here)
    safe = reader.valid_workflow_run_id(ODD_WF)
    code, out, err = _cli("scan", "--here", *json_flag)
    assert (code, out) == (4, "")
    assert f"postmortem: 1 run could not be read and was left out ({safe})" in err.splitlines()
    assert "zzPrivate" not in err


@pytest.mark.parametrize("odd", ["null", "[]"])
def test_a_parent_line_that_is_not_an_object_is_counted(home, odd):
    wf = _workflow(home)
    good = _good(_uid(90), 0)
    parent = _project(home) / f"{_uid(90)}.jsonl"
    parent.write_text(good[0] + "\n" + odd + "\n" + good[1] + "\n", encoding="utf-8")
    run = reader.load_run(wf)
    assert run.skipped_lines == 1
    assert f"{parent.name}:2: parent transcript line that is not a JSON object" in run.diagnostics
    assert reader.load_run(parent).skipped_lines == 1


def test_a_workflow_directory_named_like_a_transcript_gets_a_workflow_id(home, monkeypatch):
    name = "wf_ zzPrivate.jsonl"
    wf = _workflow(home, name=name)
    _fail_loads(monkeypatch)
    code, _out, err = _cli("run", wf)
    assert code == 4
    assert err.splitlines()[-1] == f"postmortem: {reader.valid_workflow_run_id(name)} {UNREAD}"
