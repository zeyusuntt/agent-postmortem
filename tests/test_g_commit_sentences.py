"""G4 follow-up (v0.3.1, found by the main session's own `run --here` on a real session): every
sentence that states a commit count follows the COMMITS cell's rule (docs/DESIGN.md 4.7, 4.8,
4.12). The STORIES facts clause read `reported 0 commits` beside a `?` COMMITS cell; the watch
KILL line and the scan runtime-kill reason had the same exact 0.

A completed call that contained a commit command and reported no id makes a known count `N+`,
and an unknown one when no id is known at all.
"""

from __future__ import annotations

import pytest

from agent_postmortem import cli, markers
from agent_postmortem.cli import main
from tests.n4b2_helpers import result, session_run, use
from tests.n5b_helpers import Spec, build_workflow
from tests.n5c2_helpers import run_cli
from tests.test_watch import (
    _agent_form,
    _append_lines,
    _assistant_text,
    _assistant_tool_use,
    _Clock,
    _journal_line,
    _kill_sentinel,
    _meta,
    _patch_clock,
    _patch_scripted_sleep,
    _result,
    _started,
    _transcript_path,
    _user_text,
    _user_tool_result,
    _write_lines,
    _write_transcript,
)

X = "a" + "1" * 16
UNKNOWN = "commit count unknown (a commit command reported no id)"
DENIED = markers.PERMISSION_DENIED_AUTO_MODE + " Try something else."


def _call(command, result_text="", tool_id="t1"):
    return [use(tool_id, "Bash", {"command": command}), result(tool_id, result_text)]


def _denied(tool_id="d1"):
    # a permission denial gives the agent a minor finding, so it earns a STORIES entry
    return [use(tool_id, "Bash", {"command": "rm -rf build"}), result(tool_id, DENIED, True)]


def _write(tool_id="w1"):
    return [use(tool_id, "Write", {"file_path": "/tmp/ws/a.txt", "content": "x"}),
            result(tool_id, "ok")]


def _story_line(tmp_path, capsys, entries):
    run = session_run(tmp_path, [{"type": "user", "message": {"role": "user", "content": "go"}}],
                      {X: entries})
    cli.main(["run", str(run.path), "--no-redact"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("STORIES"))
    return " ".join(" ".join(lines[start + 1:start + 4]).split())


# --- STORIES -----------------------------------------------------------------------------------


def test_story_with_no_known_id_says_the_count_is_unknown(tmp_path, capsys):
    story = _story_line(tmp_path, capsys, _write() + _call("git commit -q -m x", "", "c1")
                        + _denied())
    assert "Wrote 1" in story and f"paths, {UNKNOWN}." in story
    assert "reported 0 commits" not in story


def test_story_with_no_path_and_no_known_id_still_says_it(tmp_path, capsys):
    story = _story_line(tmp_path, capsys, _call("git commit -q -m x", "", "c1") + _denied())
    assert UNKNOWN in story


def test_story_with_one_id_and_an_idless_call_says_one_plus(tmp_path, capsys):
    entries = (_write() + _call("git commit -m x", "[main abc1234] x", "c1")
               + _call("git commit -q -m y", "", "c2") + _denied())
    story = _story_line(tmp_path, capsys, entries)
    assert "paths, reported 1+ commits." in story


def test_story_with_only_known_ids_is_unchanged(tmp_path, capsys):
    entries = _write() + _call("git commit -m x", "[main abc1234] x", "c1") + _denied()
    story = _story_line(tmp_path, capsys, entries)
    assert "paths, reported 1 commit." in story
    assert UNKNOWN not in story and "1+ commits" not in story


def test_story_with_no_commit_command_is_unchanged(tmp_path, capsys):
    story = _story_line(tmp_path, capsys, _write() + _denied())
    assert "paths, reported 0 commits." in story


# --- scan: the runtime-kill reason -------------------------------------------------------------


def _ws(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


def _killed_with(tmp_path, monkeypatch, capsys, suffix, tools):
    build_workflow(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Spec("v2:x", 1, journal="superseded", kill=True, silence=400, tools=[
            ("t1", "Write", {"file_path": str(_ws(tmp_path) / "a.txt")}, "ok", False), *tools,
        ]),
        Spec("v2:x", 2, journal="completed", start=500),
    ])
    _code, out, _err = run_cli(capsys, ["scan", "--root", str(tmp_path / "root")])
    return out


@pytest.fixture
def scan_out(tmp_path, monkeypatch, capsys):
    return lambda suffix, tools: _killed_with(tmp_path, monkeypatch, capsys, suffix, tools)


def test_scan_reason_with_no_known_id_says_commit_count_unknown(scan_out):
    out = scan_out("4b1", [("t2", "Bash", {"command": "git commit -q -m x"}, "", False)])
    assert "1 attempt killed by the runtime, 1+ paths named, commit count unknown" in out
    assert "0 commits" not in out


def test_scan_reason_with_one_id_and_an_idless_call_says_one_plus(scan_out):
    out = scan_out("4b2", [
        ("t2", "Bash", {"command": "git commit -m x"}, "[main abc1234] x", False),
        ("t3", "Bash", {"command": "git commit -q -m y"}, "", False),
    ])
    assert "killed by the runtime, 1+ paths named, 1+ commits" in out


def test_scan_reason_with_no_commit_command_is_unchanged(scan_out):
    out = scan_out("4b3", [])
    assert "1 attempt killed by the runtime, 1 path named, 0 commits" in out


# --- watch: the KILL line ----------------------------------------------------------------------


def _kill_run(run_dir, commit_result: str | None):
    """Attempt 1 edits one file, optionally runs a commit (result text `commit_result`), and is
    killed; attempt 2 is started and returns."""
    run_dir.mkdir(parents=True)
    agent1, agent2, key = "aaa0000000000001a", "aaa0000000000002a", "v2:watch-a"
    _write_lines(run_dir / "journal.jsonl", [
        _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
        _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:a"),
    ])
    _meta(run_dir, agent1)
    _meta(run_dir, agent2)
    lines = [
        _user_text("2026-02-01T00:00:00.000Z", "Implement the watch-a module."),
        _assistant_tool_use("2026-02-01T00:00:01.000Z", "toolu_1", "Edit",
                            {"file_path": "/home/user/project/a.py"}, text="Editing a."),
        _user_tool_result("2026-02-01T00:00:02.000Z", "toolu_1", "ok"),
    ]
    if commit_result is not None:
        lines += [
            _assistant_tool_use("2026-02-01T00:00:03.000Z", "toolu_2", "Bash",
                                {"command": "git commit -q -m x"}, text="Committing."),
            _user_tool_result("2026-02-01T00:00:04.000Z", "toolu_2", commit_result),
        ]
    lines += [_assistant_text("2026-02-01T00:00:05.000Z", "Next step."),
              _kill_sentinel("2026-02-01T00:07:01.000Z")]
    _write_transcript(_transcript_path(run_dir, agent1), lines)
    _write_transcript(_transcript_path(run_dir, agent2), [
        _user_text("2026-02-01T00:10:00.000Z", "Implement the watch-a module."),
        _assistant_text("2026-02-01T00:10:05.000Z", "Done."),
    ])
    return key, agent1, agent2


def _watch_kill_lines(monkeypatch, capsys, tmp_path, commit_result):
    run_dir = tmp_path / "wf_00000000-4b4"
    key, agent1, agent2 = _kill_run(run_dir, commit_result)
    journal = run_dir / "journal.jsonl"
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [
        lambda: _append_lines(journal, [_started("2026-02-01T00:10:00.000Z", key, agent2,
                                                 "impl:a")]),
        lambda: _append_lines(journal, [_result("2026-02-01T00:10:06.000Z", key, agent2)]),
    ], clock)
    main(["watch", str(run_dir), "--interval", "1"])
    lines = capsys.readouterr().out.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("KILL"))
    assert lines[index].startswith(f"KILL   {_agent_form(agent1)} attempt 1")
    return lines[index + 1]


def test_watch_kill_line_with_no_known_id_says_commit_count_unknown(monkeypatch, capsys, tmp_path):
    second = _watch_kill_lines(monkeypatch, capsys, tmp_path, "")
    # the shell call makes the path count a lower bound (4.13.8)
    assert second.startswith("       attempt 1 wrote 1+ paths, commit count unknown.  -> ")


def test_watch_kill_line_with_no_commit_command_counts_one_path(monkeypatch, capsys, tmp_path):
    second = _watch_kill_lines(monkeypatch, capsys, tmp_path, None)
    assert second.startswith("       attempt 1 wrote 1 path, reported 0 commits.  -> ")
