"""Shared synthetic builders for the N5c-1 tests (observation groups, WORK AT RISK).

Everything is synthetic: repositories are throwaway git repositories under
pytest's tmp_path, agent ids are `_aid(n)` ids, nothing is a real transcript.

`build_run` writes a real workflow run directory whose attempt transcripts carry
a `cwd` on every entry (the ledger's `repo` candidate comes from it), so the
reader, the ledger, and the repository grouping compute every input honestly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_field_v03 import (
    FAKE_SLUG,
    FAKE_UUID,
    _aid,
    _assistant_text,
    _assistant_tool,
    _envelope,
    _kill,
    _no_config,
    _notification_entry,
    _parent_path,
    _tool_result,
    _user,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)

__all__ = [
    "Att", "Step", "build_run", "git", "init_repo", "write_step", "bash_step", "BASE", "_aid",
    "_no_config", "FAKE_SLUG", "FAKE_UUID", "session_with_children",
]

BASE = datetime.now(timezone.utc) - timedelta(minutes=30)


def without_filter_neutralizers(argv: list[str]) -> list[str]:
    """`argv` with every exactly-formed `-c filter.<n>.clean= -c
    filter.<n>.process= -c filter.<n>.required=false` group removed. observe adds
    one such group per filter driver in the person's own system or global git
    config (docs/DESIGN.md 4.9 rule 5), and a machine or CI runner with Git LFS
    installed has one named `lfs`; every other argument stays, so a template
    check still fails on anything else."""
    kept: list[str] = []
    i = 0
    while i < len(argv):
        group = argv[i:i + 6]
        if len(group) == 6 and group[0::2] == ["-c", "-c", "-c"]:
            name = group[1][len("filter."):-len(".clean=")]
            if (
                group[1] == f"filter.{name}.clean=" and name
                and group[3] == f"filter.{name}.process="
                and group[5] == f"filter.{name}.required=false"
            ):
                i += 6
                continue
        kept.append(argv[i])
        i += 1
    return kept


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def init_repo(repo: Path, files: dict[str, str] | None = None) -> Path:
    """A fresh repository with one commit holding `files` (default tracked.txt)."""
    repo.mkdir(parents=True, exist_ok=True)
    git(["init", "-q"], repo)
    git(["config", "user.name", "Test"], repo)
    git(["config", "user.email", "test@example.com"], repo)
    for name, text in (files or {"tracked.txt": "original\n"}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "initial"], repo)
    return repo


@dataclass
class Step:
    """One tool call: (tool id, name, input, result text or None for no result, is_error, cwd)."""

    tool_id: str
    name: str
    input: dict
    result: str | None = "ok"
    is_error: bool = False
    cwd: str | None = None


def write_step(tool_id: str, path: Path | str, cwd: Path | str | None, *, result="ok") -> Step:
    return Step(tool_id, "Write", {"file_path": str(path)}, result, False,
                str(cwd) if cwd is not None else None)


def bash_step(tool_id: str, command: str, cwd: Path | str | None, *, result="") -> Step:
    return Step(tool_id, "Bash", {"command": command}, result, False,
                str(cwd) if cwd is not None else None)


@dataclass
class Att:
    """One attempt of a built workflow run."""

    number: int
    key: str = "v2:x"
    journal: str = "failed"  # completed | failed | open
    steps: list[Step] = field(default_factory=list)
    start: int = 10
    prompt: str = "Do the thing."


def _with_cwd(entry: dict, cwd: str | None) -> dict:
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


def build_run(
    tmp_path: Path, monkeypatch, wf_id: str, attempts: list[Att], *, parent: str | None = "completed",
    slug: str = FAKE_SLUG,
):
    """Write the run and return its directory."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id, slug=slug)
    journal: list[dict] = []
    for att in attempts:
        agent_id = _aid(att.number)
        journal.append({
            "type": "started", "key": att.key, "agentId": agent_id,
            "label": f"impl-{att.number}", "phase": "Implement",
        })
        if att.journal == "completed":
            journal.append({
                "type": "result", "key": att.key, "agentId": agent_id, "result": {"ok": True},
            })
        elif att.journal == "failed":
            journal.append({"type": "failed", "key": att.key, "agentId": agent_id})
        start = BASE + timedelta(seconds=att.start)
        events = [_user(att.prompt, start, agent_id)]
        step_time = 1
        for step in att.steps:
            events.append(_with_cwd(
                _assistant_tool(
                    step.tool_id, step.name, step.input, start + timedelta(seconds=step_time),
                    agent_id,
                ), step.cwd,
            ))
            if step.result is not None:
                events.append(_with_cwd(
                    _tool_result(
                        step.tool_id, step.result, start + timedelta(seconds=step_time + 1),
                        agent_id, is_error=step.is_error,
                    ), step.cwd,
                ))
            step_time += 2
        events.append(_assistant_text("done", start + timedelta(seconds=step_time), agent_id))
        _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _write_jsonl(run_dir / "journal.jsonl", journal)
    if parent is not None:
        lines = [
            _workflow_launch("toolu_A", BASE),
            _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        ]
        if parent != "running":
            lines.append(_notification_entry(
                BASE + timedelta(minutes=20), _envelope("tA", "toolu_A", parent),
            ))
        _write_jsonl(_parent_path(root, slug=slug), lines)
    _no_config(monkeypatch, tmp_path)
    return run_dir


def session_with_children(
    tmp_path: Path, children: list[tuple[int, list[Step], bool, bool]], *, main_entries=(),
):
    """A session transcript with subagents. Each child is (number, steps, killed, returned):
    `killed` ends its transcript in the kill sentinel; `returned` writes the Agent launch and
    a success result for it into the main transcript. Returns (main path, [agent ids])."""
    root = tmp_path / "sroot"
    main = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    lines = [{**_user("start", BASE), "sessionId": FAKE_UUID}]
    lines.extend(main_entries)
    ids = []
    for number, steps, killed, returned in children:
        agent_id = _aid(number)
        ids.append(agent_id)
        start = BASE + timedelta(seconds=20 * number)
        events = [_user("Work.", start, agent_id)]
        step_time = 1
        for step in steps:
            events.append(_with_cwd(
                _assistant_tool(step.tool_id, step.name, step.input,
                                start + timedelta(seconds=step_time), agent_id), step.cwd,
            ))
            if step.result is not None:
                events.append(_with_cwd(
                    _tool_result(step.tool_id, step.result,
                                 start + timedelta(seconds=step_time + 1), agent_id), step.cwd,
                ))
            step_time += 2
        if killed:
            events.append(_kill(start + timedelta(seconds=step_time), agent_id))
        else:
            events.append(_assistant_text("done", start + timedelta(seconds=step_time), agent_id))
        sub = root / FAKE_SLUG / FAKE_UUID / "subagents" / f"agent-{agent_id}.jsonl"
        _write_jsonl(sub, events)
        launch_id = f"toolu_launch{number}"
        if returned:
            lines.append({
                "type": "assistant", "timestamp": "2026-09-01T12:00:01Z",
                "message": {"role": "assistant", "usage": {}, "content": [
                    {"type": "tool_use", "id": launch_id, "name": "Agent", "input": {}}]},
            })
            lines.append({
                "type": "user", "timestamp": "2026-09-01T12:00:02Z",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": launch_id, "content": "done"}]},
                "toolUseResult": {"agentId": agent_id, "status": "completed"},
            })
    _write_jsonl(main, lines)
    return main, ids
