"""`scan` marks the caller's own runs (docs/DESIGN.md 4.7, 0.4).

Field report 2026-10-30, "no way to tell whose or which project's run it is" (all 4 testers, on a
machine with 18 sessions across several projects): NEEDS ATTENTION listed another project's
session without saying it was not the caller's. A triage row of the session named by
CLAUDE_CODE_SESSION_ID now ends in `(this session)` and a workflow it started in `(started by
this session)`, the same at every level like the rest of the triage text, which names no
identity. Each run's project is in `scan --all --local`; the default level names none (4.13.5).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from agent_postmortem.cli import main
from tests.test_field_v03 import FAKE_SLUG, FAKE_UUID, _wf_dir, _write_jsonl
from tests.test_share_label_case import _killed_after_silence

OTHER_SLUG = "-home-user-zz-other-project"
OTHER_UUID = f"{0xC4D4:08x}-0000-4000-8000-{0xC4D4:012x}"
MINE, THEIRS, WORKFLOW = "ab3a" + "0" * 12 + "1", "ab3a" + "0" * 12 + "2", "ab3a" + "0" * 12 + "3"


def _session(root, slug, uuid, agent, base):
    main_path = root / slug / f"{uuid}.jsonl"
    _write_jsonl(main_path, [{
        "type": "user", "message": {"role": "user", "content": "start"},
        "sessionId": uuid, "timestamp": base.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }])
    sub = root / slug / uuid / "subagents" / f"agent-{agent}.jsonl"
    _write_jsonl(sub, _killed_after_silence(agent, base))
    (sub.with_name(f"agent-{agent}.meta.json")).write_text(
        json.dumps({"agentType": "general-purpose", "description": "zz-task"}), encoding="utf-8")
    return main_path


def _machine(tmp_path):
    root = tmp_path / "projects"
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    _session(root, FAKE_SLUG, FAKE_UUID, MINE, base)
    _session(root, OTHER_SLUG, OTHER_UUID, THEIRS, base)
    run_dir = _wf_dir(root, "wf_00000000-b39")
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": WORKFLOW, "label": "zz-impl", "phase": "B"},
    ])
    _write_jsonl(run_dir / f"agent-{WORKFLOW}.jsonl", _killed_after_silence(WORKFLOW, base))
    old = (base + timedelta(seconds=500)).timestamp()
    for path in root.rglob("*"):
        os.utime(path, (old, old))
    return root


def _rows(out: str) -> dict[str, str]:
    return {line.split()[0]: line for line in out.splitlines()
            if line.startswith("  s_") or line.startswith("  wf_")}


def _scan(capsys, root, *flags):
    main(["scan", "--root", str(root), "--no-cache", *flags])
    return capsys.readouterr().out


def test_the_callers_own_session_and_its_workflow_are_marked(tmp_path, capsys, monkeypatch):
    root = _machine(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", FAKE_UUID)
    rows = _rows(_scan(capsys, root))
    assert len(rows) == 3, rows
    marked = {run_id: line for run_id, line in rows.items() if "(this session)" in line}
    started = {run_id: line for run_id, line in rows.items() if "(started by this session)" in line}
    assert len(marked) == 1 and next(iter(marked)).startswith("s_")
    assert list(started) == ["wf_00000000-b39"]
    other = [line for run_id, line in rows.items() if run_id not in marked and run_id not in started]
    assert len(other) == 1 and "session)" not in other[0]


def test_nothing_is_marked_without_a_caller_session(tmp_path, capsys, monkeypatch):
    root = _machine(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    out = _scan(capsys, root)
    assert "this session)" not in out


def test_the_full_list_at_local_names_each_runs_project_and_triage_stays_level_free(
    tmp_path, capsys, monkeypatch,
):
    root = _machine(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", FAKE_UUID)
    share = _scan(capsys, root)
    assert "zz-other-project" not in share and "project:" not in share
    raw = _scan(capsys, root, "--raw")
    assert raw.partition("\n")[2] == share  # the marker is the same at every level
    full = _scan(capsys, root, "--all", "--local")
    assert sum("zz-other-project" in line for line in full.splitlines()) == 1
