"""N3b fix round 2, R2-1: every whole-value validator uses fullmatch, so a
final newline (or carriage return, NUL, embedded newline) never passes.
Checked directly per validator, and end to end through watch text, watch
JSONL (one physical record per event) and the forced hook fallback."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time

import pytest

from agent_postmortem import hooks_cli, markers, reader
from agent_postmortem.cli import main
from tests import test_watch as tw

GOOD = "wf_12345678-abc"
# NUL cannot be a file name, so it is unit-tested only.
BAD_UNIT = [GOOD + "\n", GOOD + "\r", GOOD + "\x00", "wf_12345678\n-abc", GOOD + "\n\n"]
BAD_FS = [GOOD + "\n", GOOD + "\r", "wf_12345678\n-abc"]


def _replacement(name: str) -> str:
    return "wf_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8] + "-x"


@pytest.mark.parametrize("name", [GOOD + "\r", GOOD + "\x00", "wf_12345678\n-abc", GOOD + "\n\n"] + [GOOD + "\n"])
def test_valid_workflow_run_id_rejects_control_suffixes(name):
    assert reader.valid_workflow_run_id(name) == _replacement(name)
    assert reader.valid_workflow_run_id(GOOD) == GOOD


def test_valid_agent_id_rejects_control_suffixes():
    good = "a" + "0" * 16
    for bad in (good + "\n", good + "\r", good + "\x00", "a0000000\n00000000"):
        diags: list[str] = []
        out = reader._valid_agent_id(bad, diags, "ctx")
        assert out.startswith("agent-") and diags
    assert reader._valid_agent_id(good, [], "ctx") == good


@pytest.mark.parametrize("name", [GOOD + "\r", GOOD + "\x00", "wf_12345678\n-abc", GOOD + "\n\n"] + [GOOD + "\n"])
def test_hook_run_id_extraction_rejects_control_suffixes(name):
    assert hooks_cli._extract_run_id({"runId": name}) is None
    assert hooks_cli._extract_run_id({"runId": GOOD}) == GOOD


def test_reported_commit_hex_rejects_a_final_newline():
    claims = reader._reported_claims({"commits": ["abcdef1", "abcdef1\n", "abcdef1\r"]})
    assert claims["commits"] == ["abcdef1"]


def test_exit_code_line_is_whole_value():
    ok = "Exit code 128\nfatal: not a git repository"
    assert markers._predicate_git_not_repo(ok, "x")
    for tail in ("\r", "\x00", " "):
        assert not markers._predicate_git_not_repo(
            "Exit code 128" + tail + "\nfatal: not a git repository", "x")


def test_session_discovery_ignores_a_uuid_stem_with_a_final_newline(tmp_path):
    uuid = "00000000-0000-4000-8000-000000000000"
    slug = tmp_path / "-proj"
    slug.mkdir()
    (slug / (uuid + "\n.jsonl")).write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    assert reader.discover_runs(tmp_path, frozenset({"session"})) == []


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
    return [lambda: tw._append_lines(journal, [
        tw._started("2026-02-01T00:10:00.000Z", "v2:watch-a", "aaa0000000000002a", "impl:a"),
    ])]


@pytest.mark.parametrize("name", [GOOD + "\r", "wf_12345678\n-abc"] + [GOOD + "\n"])
def test_watch_text_prints_the_replacement_on_one_line(name, monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / name
    tw._build_kill_scenario(run_dir)
    out, _err = _watch(monkeypatch, capsys, run_dir, _kill_actions(run_dir))
    assert f"rescue plan {_replacement(name)}" in out
    assert name not in out
    assert "\r" not in out
    assert "\n\n" not in out.rstrip("\n")


@pytest.mark.parametrize("name", [GOOD + "\r", "wf_12345678\n-abc"] + [GOOD + "\n"])
def test_watch_jsonl_is_one_physical_record_per_event(name, monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / name
    tw._build_kill_scenario(run_dir)
    out, _err = _watch(monkeypatch, capsys, run_dir, _kill_actions(run_dir), "--json")
    lines = out.splitlines()
    assert lines and all(line.startswith("{") for line in lines)
    # N8b K5.1: START is the first event; it and the KILL both name the replacement
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["event"] == "START"
    assert parsed[0]["run"] == _replacement(name)
    kill = next(event for event in parsed if event["event"] == "KILL")
    assert kill["run_id"] == _replacement(name)
    assert "\r" not in out


@pytest.mark.parametrize("name", [GOOD + "\r", "wf_12345678\n-abc"] + [GOOD + "\n"])
def test_forced_hook_fallback_prints_the_replacement(name, monkeypatch, capsys, tmp_path):
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
    assert name not in joined
    assert _replacement(name) in joined
