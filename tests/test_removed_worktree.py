"""A run whose working directory was removed after it ended (a merged worktree, cleaned up) is
not a list of false alarms (docs/DESIGN.md 4.3, 4.7, 4.14.4, 0.4).

Field report 2026-10-30, "removing a merged worktree turns its reports into false alarms" (3 of
4 testers): each report file kept in the worktree read "absent now", WHAT TO DO asked to check
them, `status` printed a CLAIM, and WORK AT RISK told the person to run git in a directory that
no longer exists. A path inside an attempt's own working directory that is gone now cannot be
checked, so it is named as such (info), the `[look]` item leaves it out, `status` makes no CLAIM
of it, and a gone repository is named as gone instead of handed a git command.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem import cli
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _assistant_tool,
    _kill,
    _no_config,
    _parent_path,
    _tool_result,
    _user,
    _wf_dir,
    _write_jsonl,
)

AGENT = _aid(0xE35)


def _at_cwd(entry: dict, cwd: Path) -> dict:
    return {**entry, "cwd": str(cwd)}


def _completed_with_report(tmp_path: Path, worktree: Path, wf_id: str) -> Path:
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    report = worktree / "reports" / "task-report.md"
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": AGENT, "label": "impl", "phase": "B"},
        {"type": "result", "key": "v2:x", "agentId": AGENT,
         "result": {"ok": True, "report_path": str(report)}},
    ])
    _write_jsonl(run_dir / f"agent-{AGENT}.jsonl", [
        _at_cwd(_user("Write the report.", base, AGENT), worktree),
        _at_cwd(_assistant_tool("t1", "Bash", {"command": "ls"}, base + timedelta(seconds=1),
                                AGENT), worktree),
        _at_cwd(_tool_result("t1", "ok", base + timedelta(seconds=2), AGENT), worktree),
        _at_cwd(_assistant_text("Done.", base + timedelta(seconds=3), AGENT), worktree),
    ])
    _write_jsonl(_parent_path(root), [{
        "type": "user", "message": {"role": "user", "content": "s"}, "sessionId": "x",
        "timestamp": base.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }])
    return run_dir


def _run(capsys, run_dir: Path, *flags) -> str:
    cli.main(["run", str(run_dir), *flags])
    return capsys.readouterr().out


def test_a_report_in_a_removed_worktree_is_named_so_and_not_a_look_item(tmp_path, monkeypatch,
                                                                        capsys):
    _no_config(monkeypatch, tmp_path)
    run_dir = _completed_with_report(tmp_path, tmp_path / "gone-worktree", "wf_00000000-e35")
    payload = json.loads(_run(capsys, run_dir, "--json", "--raw"))
    found = [f for f in payload["findings"] if f["detector"] == "claim_contradiction"]
    assert len(found) == 1 and found[0]["severity"] == "info"
    assert "in a working directory that no longer exists" in found[0]["title"], found[0]["title"]
    assert found[0]["metrics"]["working_dir_gone"] is True
    text = _run(capsys, run_dir)
    assert "expected file absent now" not in text and "expected files absent now" not in text


def test_a_report_missing_from_a_worktree_that_still_exists_still_reads_absent(
    tmp_path, monkeypatch, capsys,
):
    _no_config(monkeypatch, tmp_path)
    kept = tmp_path / "kept-worktree"
    kept.mkdir()
    run_dir = _completed_with_report(tmp_path, kept, "wf_00000000-e36")
    payload = json.loads(_run(capsys, run_dir, "--json", "--raw"))
    found = [f for f in payload["findings"] if f["detector"] == "claim_contradiction"]
    assert len(found) == 1 and "is absent now" in found[0]["title"]
    assert "working_dir_gone" not in found[0]["metrics"]
    assert "1 expected file absent now" in _run(capsys, run_dir)


def test_work_at_risk_names_a_gone_repository_instead_of_a_git_command(tmp_path, monkeypatch,
                                                                       capsys):
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    run_dir = _wf_dir(root, "wf_00000000-e37")
    gone = tmp_path / "gone-worktree"
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": AGENT, "label": "impl", "phase": "B"},
    ])
    _write_jsonl(run_dir / f"agent-{AGENT}.jsonl", [
        _at_cwd(_user("Edit things.", base, AGENT), gone),
        _at_cwd(_assistant_tool("t1", "Bash", {"command": "make edit"},
                                base + timedelta(seconds=1), AGENT), gone),
        _at_cwd(_tool_result("t1", "ok", base + timedelta(seconds=2), AGENT), gone),
        _at_cwd(_kill(base + timedelta(seconds=9), AGENT), gone),
    ])
    for flags in ((), ("--local",)):
        text = _run(capsys, run_dir, *flags)
        risk = text.split("WORK AT RISK", 1)[1].split("\n\n", 1)[0]
        assert "no longer exists" in risk, risk
        assert "git status --short in path" not in risk and "git -C" not in risk, risk


def _session_with_output(tmp_path: Path, worktree: Path) -> Path:
    from live_helpers import FAKE_UUID, assistant_text, notification, user, write_jsonl
    from test_live_session import SLUG, T0, _launch, _sub_path
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    agent = AGENT
    _launch(main_path, agent, "toolu_m", T0)
    target = worktree / "notes.md"
    write_jsonl(_sub_path(main_path, agent), [
        {**user(agent, T0, f"write the notes to {target}"), "cwd": str(worktree)},
        {**assistant_text(agent, T0, "Done."), "cwd": str(worktree)},
    ])
    write_jsonl(main_path, [notification(T0, agent, "toolu_m", "completed", 'Agent "d" finished')],
                mode="a")
    return main_path


def test_status_makes_no_claim_of_an_output_in_a_removed_worktree(tmp_path, capsys):
    main_path = _session_with_output(tmp_path, tmp_path / "gone-worktree")
    assert cli.main(["status", str(main_path)]) == 0
    assert "CLAIM" not in capsys.readouterr().out


def test_status_still_claims_an_output_missing_from_a_worktree_that_exists(tmp_path, capsys):
    kept = tmp_path / "kept-worktree"
    kept.mkdir()
    main_path = _session_with_output(tmp_path, kept)
    assert cli.main(["status", str(main_path)]) == 0
    assert "CLAIM" in capsys.readouterr().out


def test_a_prompted_output_in_a_removed_worktree_is_named_so_too(tmp_path, monkeypatch, capsys):
    _no_config(monkeypatch, tmp_path)
    run_dir = _completed_with_report(tmp_path, tmp_path / "gone-worktree", "wf_00000000-e38")
    transcript = run_dir / f"agent-{AGENT}.jsonl"
    lines = transcript.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["message"]["content"] = f"Write the notes to {tmp_path / 'gone-worktree' / 'notes.md'}"
    transcript.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n", encoding="utf-8")
    payload = json.loads(_run(capsys, run_dir, "--json", "--raw"))
    found = [f for f in payload["findings"] if f["detector"] == "missing_artifact"]
    assert len(found) == 1, payload["findings"]
    assert "in a working directory that no longer exists" in found[0]["title"]
    assert found[0]["metrics"]["working_dir_gone"] is True
    assert "expected file" not in _run(capsys, run_dir)


def test_the_story_of_a_major_finding_names_the_removed_directory_too(tmp_path, monkeypatch,
                                                                      capsys):
    # batch 4 review: the story clause still said "a report_path that does not exist"
    _no_config(monkeypatch, tmp_path)
    run_dir = _completed_with_report(tmp_path, tmp_path / "gone-worktree", "wf_00000000-e35")
    journal = run_dir / "journal.jsonl"
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    rows[1]["result"]["ok"] = False  # a major clause, so the story prints by default
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    text = " ".join(_run(capsys, run_dir).split())  # the story wraps
    assert "report_path that does not exist" not in text, text
    assert "a report_path in a working directory that no longer exists" in text, text
