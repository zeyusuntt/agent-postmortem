"""The first-run empty state (docs/DESIGN.md 4.8): a stranger who runs `scan` on a machine with
no Claude Code runs learns where the tool looked, what it reads, and what to run next.

The standard location `~/.claude/projects` is not private, so it prints home-relative at the
share and local levels; a root the person typed with `--root` stays `path-N` there, as every
typed path does (N3b P13). Exit code 2 ("no runs or not a run", 4.6) is unchanged, so a
script can still tell "nothing to judge" from "all clean".
"""

from __future__ import annotations

import json
import uuid

import pytest

from agent_postmortem.cli import main

HINT_READS = "agent-postmortem reads the transcripts Claude Code saves there"
HINT_DEMO = "agent-postmortem demo"
SESSION_UUID = str(uuid.UUID(int=0xE3B7E57A7E00000000000000000000E1))


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return path


def _cli(capsys, *argv):
    capsys.readouterr()
    code = main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _assert_guidance(err: str) -> None:
    assert HINT_READS in err
    assert "Workflow runs" in err and "subagents" in err
    assert HINT_DEMO in err
    assert "--root" in err and "CLAUDE_CONFIG_DIR" in err
    assert "30 days" in err


def _write_session(home, cwd: str) -> None:
    project = home / ".claude" / "projects" / "-tmp-elsewhere"
    project.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": "2026-09-01T10:00:00.000Z", "sessionId": SESSION_UUID,
         "cwd": cwd, "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": "2026-09-01T10:00:05.000Z", "sessionId": SESSION_UUID,
         "cwd": cwd, "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ]
    (project / f"{SESSION_UUID}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8",
    )


@pytest.mark.parametrize("level", [[], ["--local"]])
def test_scan_with_no_projects_directory_names_it_and_says_what_to_do(home, capsys, level):
    code, out, err = _cli(capsys, "scan", *level)
    assert code == 2
    assert out == ""
    assert err.splitlines()[0] == (
        "postmortem: no runs found under ~/.claude/projects (it does not exist yet)"
    )
    assert "path-1" not in err
    _assert_guidance(err)


@pytest.mark.parametrize("argv", [
    ["scan"], ["scan", "--all"], ["scan", "--json"], ["trend"], ["rules", "propose"],
])
def test_an_empty_projects_directory_gives_the_same_guidance(home, capsys, argv):
    (home / ".claude" / "projects").mkdir(parents=True)
    code, out, err = _cli(capsys, *argv)
    assert code == 2
    assert out == ""
    assert err.splitlines()[0] == "postmortem: no runs found under ~/.claude/projects"
    _assert_guidance(err)


def test_raw_names_the_absolute_standard_location(home, capsys):
    (home / ".claude" / "projects").mkdir(parents=True)
    code, _out, err = _cli(capsys, "scan", "--raw")
    assert code == 2
    assert f"postmortem: no runs found under {home / '.claude' / 'projects'}" in err
    _assert_guidance(err)


def test_a_typed_root_stays_path_n_at_share_but_still_gets_the_guidance(home, capsys, tmp_path):
    typed = tmp_path / "zzPrivate" / "projects"
    typed.mkdir(parents=True)
    code, _out, err = _cli(capsys, "scan", "--root", typed)
    assert code == 2
    assert err.splitlines()[0] == "postmortem: no runs found under path-1"
    assert "zzPrivate" not in err
    _assert_guidance(err)


@pytest.mark.parametrize("argv", [["run", "--here"], ["run", "wf_00000000-e01"]])
def test_run_with_no_runs_at_all_gives_the_empty_state(home, capsys, argv, tmp_path, monkeypatch):
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    code, out, err = _cli(capsys, *argv)
    assert code == 2
    assert out == ""
    assert err.splitlines()[0] == "postmortem: no runs found under ~/.claude/projects"
    _assert_guidance(err)


def test_run_here_with_runs_elsewhere_points_at_scan(home, capsys, tmp_path, monkeypatch):
    _write_session(home, "/tmp/elsewhere")
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    code, _out, err = _cli(capsys, "run", "--here")
    assert code == 2
    assert err.splitlines()[0] == (
        "postmortem: no run under ~/.claude/projects worked in this directory or a parent of it"
    )
    assert "agent-postmortem scan" in err


def test_scan_of_sessions_without_subagents_or_workflows_says_there_is_nothing_to_check(
    home, capsys,
):
    _write_session(home, "/tmp/elsewhere")
    code, out, _err = _cli(capsys, "scan")
    assert code == 0
    lines = out.splitlines()
    assert "  nothing needs attention" in lines
    hint = lines[lines.index("  nothing needs attention") + 1]
    # only what the scan observed: a launched subagent whose transcript is missing is not
    # "no subagent launched"
    assert hint == (
        "  this scan found no subagent transcript and no Workflow run, which is what "
        "agent-postmortem checks; agent-postmortem demo shows a sample report"
    )


def test_a_scan_limited_to_one_kind_gives_no_hint(home, capsys):
    _write_session(home, "/tmp/elsewhere")
    code, out, _err = _cli(capsys, "scan", "--kind", "session")
    assert code == 0
    assert "  nothing needs attention" in out.splitlines()
    assert "which is what agent-postmortem checks" not in out


def test_the_error_path_never_parses_transcripts(home, capsys, tmp_path, monkeypatch):
    """A deleted working directory needs no discovery to say "no run worked here"; the
    empty-state check must not add one (a malformed transcript there must not turn exit 2
    into a crash)."""
    project = home / ".claude" / "projects" / "-tmp-x"
    project.mkdir(parents=True)
    (project / f"{SESSION_UUID}.jsonl").write_text("[" * 200000 + "\n", encoding="utf-8")
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    code, out, err = _cli(capsys, "run", "--here")
    assert (code, out) == (2, "")
    assert err.splitlines()[0] == (
        "postmortem: no run under ~/.claude/projects worked in this directory or a parent of it"
    )


def test_watch_that_never_sees_a_journal_says_what_it_waits_for(home, capsys, monkeypatch, tmp_path):
    from tests.test_watch import _Clock, _patch_advancing_sleep, _patch_clock

    root = tmp_path / "root"
    root.mkdir()
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_advancing_sleep(monkeypatch, clock)
    code, _out, err = _cli(capsys, "watch", "wf_never-appears", "--root", root, "--interval", "10")
    assert code == 2
    assert "no journal appeared within 60 s" in err
    assert "a Workflow run while it is running" in err
    assert "agent-postmortem scan" in err


def test_wrong_type_entries_are_not_run_candidates(home, capsys, tmp_path, monkeypatch):
    """A file named like a workflow run directory, or a directory named like a session
    transcript, is not a run (discover_runs rejects both), so the empty state still shows."""
    projects = home / ".claude" / "projects"
    (projects / "p" / "s" / "subagents" / "workflows").mkdir(parents=True)
    (projects / "p" / "s" / "subagents" / "workflows" / "wf_00000000-e01").write_text("")
    (projects / "p" / f"{SESSION_UUID}.jsonl").mkdir()
    monkeypatch.chdir(tmp_path)
    for argv in (["run", "--here"], ["run", "wf_00000000-e01"]):
        code, _out, err = _cli(capsys, *argv)
        assert code == 2
        assert err.splitlines()[0] == "postmortem: no runs found under ~/.claude/projects"
