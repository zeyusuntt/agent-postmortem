"""N3b fix B, S5: a pointer into an attempt without a canonical agent id is
omitted at share (P10, C-5): no `evidence-N` placeholder line stands in for
it. The reachable case is a session run whose subagent transcript file name
is not a canonical agent id (a workflow's journal id never opens a file for a
non-canonical id, S14)."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from agent_postmortem.cli import main

SESSION = str(uuid.UUID(int=0x7E57AB1E7E57AB1E7E57AB1E7E57AB1F))
SLUG = "-home-user-project"
WEIRD = "zzWeird" + "Plant" + "7d1"
EXCERPT = "zzExcerpt" + "Plant" + "7d2"


def _row(role: str, content, ts: str, agent: str | None = None, **extra) -> dict:
    row = {"type": role, "message": {"role": role, "content": content},
           "sessionId": SESSION, "cwd": "/tmp/zzrepo", "timestamp": ts}
    if agent:
        row["agentId"] = agent
    row["message"].update(extra)
    return row


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    main_path = root / SLUG / f"{SESSION}.jsonl"
    sub_dir = root / SLUG / SESSION / "subagents"
    sub_dir.mkdir(parents=True)
    _write(main_path, [_row("user", "hi", "2026-09-20T09:00:00.000Z")])
    _write(sub_dir / f"agent-{WEIRD}.jsonl", [
        _row("user", "Work.", "2026-09-20T09:00:01.000Z"),
        _row("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                            "input": {"command": "pytest -q"}}], "2026-09-20T09:00:02.000Z", usage={}),
        _row("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                       "content": f"Exit code 143\nCommand timed out after 10m 0s {EXCERPT}"}],
             "2026-09-20T09:10:02.000Z"),
        _row("user", [{"type": "text", "text": "[Request interrupted by user]"}],
             "2026-09-20T09:17:02.000Z"),
    ])
    return main_path


def _out(tmp_path, capsys, *flags):
    path = _build(tmp_path)
    capsys.readouterr()
    main(["run", str(path), *flags])
    return capsys.readouterr().out


def test_the_fixture_has_a_marker_finding_that_a_local_render_points_at(tmp_path, capsys):
    out = _out(tmp_path, capsys, "--local", "--verbose")
    assert f"agent-{WEIRD}.jsonl:" in out


@pytest.mark.parametrize("flags", [
    ["--verbose"],
    ["--verbose", "--evidence"],
    ["--bug-report"],
    [],
])
def test_share_has_no_evidence_placeholder_line_for_a_non_canonical_attempt(flags, tmp_path, capsys):
    out = _out(tmp_path, capsys, *flags)
    assert "mislabeled_kill" in out or flags == []
    assert not re.search(r"\bevidence-\d+\b", out), out
    assert WEIRD not in out and EXCERPT not in out
