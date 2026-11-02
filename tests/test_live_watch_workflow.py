"""docs/DESIGN.md 4.14.2 R1 and R2 in `watch <RUN_ID>`: a journal `result` whose own claims
contradict it (ok false, failed tests, failure counts) or that names an absent report_path prints
CLAIM in place of OK. Same scripted-clock harness as tests/test_watch.py."""

from __future__ import annotations

import json

from test_watch import (
    _agent_form,
    _assistant_text,
    _Clock,
    _meta,
    _patch_advancing_sleep,
    _patch_clock,
    _started,
    _transcript_path,
    _user_text,
    _write_lines,
    _write_transcript,
)

from agent_postmortem.cli import main

SESSION_ID = "00000000-0000-4000-8000-000000000000"
AGENT = "a" + "1" * 16
KEY = "v2:" + "0" * 64


def _run(tmp_path, result: dict):
    run_dir = (tmp_path / "-home-user-project" / SESSION_ID / "subagents" / "workflows"
               / "wf_00000000-751")
    run_dir.mkdir(parents=True)
    _meta(run_dir, AGENT)
    _write_transcript(_transcript_path(run_dir, AGENT), [
        _user_text("2026-02-01T00:00:00.000Z", "Do the task."),
        _assistant_text("2026-02-01T00:00:29.000Z", "Done."),
    ])
    _write_lines(run_dir / "journal.jsonl", [
        _started("2026-02-01T00:00:00.000Z", KEY, AGENT, "task"),
        json.dumps({"type": "result", "timestamp": "2026-02-01T00:00:30.000Z", "key": KEY,
                    "agentId": AGENT, "result": result}),
    ])
    return run_dir


def _watch(tmp_path, monkeypatch, capsys, result: dict, *extra):
    run_dir = _run(tmp_path, result)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_advancing_sleep(monkeypatch, clock)
    code = main(["watch", str(run_dir), "--interval", "1", "--done-grace", "0", *extra])
    return code, capsys.readouterr().out.splitlines()


def test_ok_false_prints_claim_in_place_of_ok(tmp_path, monkeypatch, capsys):
    _code, lines = _watch(tmp_path, monkeypatch, capsys, {"ok": False, "tests_failed": 2})
    agent = _agent_form(AGENT)
    assert not any(line.startswith("OK ") for line in lines)
    index = next(i for i, line in enumerate(lines) if line.startswith("CLAIM"))
    assert lines[index] == f"CLAIM  {agent} attempt 1 returned, but its own result reports:"
    assert lines[index + 1] == "       2 failed tests; ok is false."
    assert lines[index + 2] == "       -> agent-postmortem run wf_00000000-751"
    assert all(len(line) <= 78 for line in lines[index:index + 3])


def test_a_report_path_absent_at_return_is_a_claim(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "reports" / "review.md"
    _code, lines = _watch(tmp_path, monkeypatch, capsys,
                          {"status": "done", "report_path": str(missing)})
    joined = "\n".join(lines)
    assert "CLAIM" in joined
    assert "its report_path does not exist" in joined
    assert str(missing) not in joined


def test_a_report_path_that_exists_is_ok(tmp_path, monkeypatch, capsys):
    present = tmp_path / "reports" / "review.md"
    present.parent.mkdir(parents=True)
    present.write_text("ok", encoding="utf-8")
    _code, lines = _watch(tmp_path, monkeypatch, capsys,
                          {"status": "done", "report_path": str(present)})
    assert any(line.startswith("OK     ") for line in lines)
    assert not any(line.startswith("CLAIM") for line in lines)


def test_a_clean_result_is_still_ok(tmp_path, monkeypatch, capsys):
    _code, lines = _watch(tmp_path, monkeypatch, capsys, {"ok": True})
    assert lines[1] == f"OK     {_agent_form(AGENT)} attempt 1 returned a result."


def test_claim_in_json_carries_the_clause_ids(tmp_path, monkeypatch, capsys):
    _code, lines = _watch(tmp_path, monkeypatch, capsys, {"ok": False}, "--json")
    events = [json.loads(line) for line in lines]
    claim = next(e for e in events if e["event"] == "CLAIM")
    assert claim["clauses"] == ["ok_false"]
    assert claim["attempt"] == 1 and claim["run_id"] == "wf_00000000-751"
    assert not any(e["event"] == "OK" for e in events)
