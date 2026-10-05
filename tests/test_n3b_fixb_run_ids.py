"""N3b fix B, C2: a workflow directory name is never printed as a run id
unless it passes the reader's validator. `watch` (direct path and discovered
directory, text and JSONL) and the hook's analysis-error fallback print the
same replacement `run` uses (`wf_<8 hex of sha256>-x`)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from pathlib import Path

import pytest

from agent_postmortem import hooks_cli, reader
from agent_postmortem.cli import main
from tests import test_watch as tw

BAD_NAMES = [
    "wf_CLIENT_SECRET",
    "wf_has space",
    "wf_00000000-0000-4000-8000-000000000000",
    "wf_-Users-zzbob-proj",
]
GOOD_NAME = "wf_00000000-fb1"


def _replacement(name: str) -> str:
    return "wf_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8] + "-x"


def test_the_reader_validator_is_public_and_replaces_a_bad_name():
    assert reader.valid_workflow_run_id(GOOD_NAME) == GOOD_NAME
    for name in BAD_NAMES:
        out = reader.valid_workflow_run_id(name)
        assert out == _replacement(name)
        assert reader.valid_workflow_run_id(name, []) == out


def _watch(monkeypatch, capsys, target, actions, *extra):
    clock = tw._Clock()
    tw._patch_clock(monkeypatch, clock)
    tw._patch_scripted_sleep(monkeypatch, actions, clock)
    capsys.readouterr()
    main(["watch", str(target), "--interval", "1", *extra])
    captured = capsys.readouterr()
    return captured.out, captured.err


def _kill_actions(run_dir):
    journal = run_dir / "journal.jsonl"
    # agent ids of tw._build_kill_scenario
    return [lambda: tw._append_lines(journal, [
        tw._started("2026-02-01T00:10:00.000Z", "v2:watch-a", "aaa0000000000002a", "impl:a"),
    ])]


@pytest.mark.parametrize("name", BAD_NAMES)
@pytest.mark.parametrize("json_mode", [False, True])
def test_watch_direct_path_prints_the_replacement_not_the_directory_name(
    name, json_mode, monkeypatch, capsys, tmp_path,
):
    run_dir = tmp_path / name
    tw._build_kill_scenario(run_dir)
    out, err = _watch(monkeypatch, capsys, run_dir, _kill_actions(run_dir),
                      *(["--json"] if json_mode else []))
    assert "KILL" in out
    assert name not in out and name not in err
    assert _replacement(name) in out
    if json_mode:
        # N8b K5.1: START is the first event now; the KILL follows it. Both name the run by
        # the replacement, never by the directory name.
        parsed = [json.loads(line) for line in out.splitlines()]
        assert parsed[0]["event"] == "START"
        assert parsed[0]["run"] == _replacement(name)
        kill = next(event for event in parsed if event["event"] == "KILL")
        assert kill["run_id"] == _replacement(name)


@pytest.mark.parametrize("name", [n for n in BAD_NAMES if " " not in n])
@pytest.mark.parametrize("json_mode", [False, True])
def test_watch_discovered_directory_prints_the_replacement(
    name, json_mode, monkeypatch, capsys, tmp_path,
):
    root = tmp_path / "root"
    run_dir = root / "-proj" / "sess" / "subagents" / "workflows" / name
    tw._build_kill_scenario(run_dir)
    out, err = _watch(monkeypatch, capsys, name, _kill_actions(run_dir), "--root", str(root),
                      *(["--json"] if json_mode else []))
    assert "KILL" in out
    assert name not in out and name not in err
    assert _replacement(name) in out


def test_watch_a_conforming_name_is_unchanged(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / GOOD_NAME
    tw._build_kill_scenario(run_dir)
    out, _err = _watch(monkeypatch, capsys, run_dir, _kill_actions(run_dir))
    assert f"rescue plan {GOOD_NAME}" in out


# --- the hook's analysis-error fallback ---------------------------------------


def test_the_hook_fallback_id_is_never_the_directory_name():
    for name in BAD_NAMES:
        got = hooks_cli._fallback_run_id("workflow", Path("/x") / name)
        assert got == _replacement(name), name
    assert hooks_cli._fallback_run_id("workflow", Path("/x") / GOOD_NAME) == GOOD_NAME


@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_forced_hook_analysis_error_prints_the_replacement(name, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    session_id = "00000000-0000-4000-8000-000000000000"
    session_dir = tmp_path / "-home-user-project" / session_id
    wf_dir = session_dir / "subagents" / "workflows" / name
    wf_dir.mkdir(parents=True)
    transcript = session_dir.parent / f"{session_id}.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    tw._write_lines(wf_dir / "journal.jsonl", [
        tw._journal_line(type="launched", timestamp="2026-01-01T00:00:00.000Z"),
        tw._started("2026-01-01T00:00:00.000Z", "v2:k", "aaa0000000000001a", "impl:a"),
    ])
    old = time.time() - 11 * 60
    for p in (wf_dir / "journal.jsonl", wf_dir):
        os.utime(p, (old, old))

    def boom(path):
        raise RuntimeError("forced")

    monkeypatch.setattr(hooks_cli, "_analyze_finished_workflow", boom)
    payload = {"hook_event_name": "Stop", "transcript_path": str(transcript),
               "session_id": session_id, "stop_hook_active": False}
    outputs = []
    for _ in range(5):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        capsys.readouterr()
        main(["hook", "check"])
        outputs.append(capsys.readouterr().out)
    joined = "\n".join(outputs)
    assert "unit could not be analyzed" in joined or _replacement(name) in joined
    assert name not in joined
    assert _replacement(name) in joined
