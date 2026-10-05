"""One synthetic workflow run with distinctive planted values, built at run
time in a caller's tmp_path (N3b per-sink tests, spec "Per-sink tests").

Planted: an agent label, a phase title, a repository path (a real throwaway
git repository, so `run --observe` reports a path inside it), two raw agent
ids, a session UUID, a project directory name, and an excerpt marker inside a
transcript line. Every value is assembled at run time so this tracked source
never holds a contiguous private-looking literal (tests/test_privacy.py).

Not a test_*.py module: pytest does not collect it.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

WF_ID = "wf_00000000-b10"

LABEL = "zzLabel" + "Plant" + "7c1"
PHASE = "zzPhase" + "Plant" + "7c2"
EXCERPT = "zzExcerpt" + "Plant" + "7c3"
AGENT1 = "a" + "7e57ab1e" + "0000" + "0001"  # the killed attempt
AGENT2 = "a" + "7e57ab1e" + "0000" + "0002"  # the retry that completed
SESSION = str(uuid.UUID(int=0x7E57AB1E7E57AB1E7E57AB1E7E57AB1E))
SLUG = "-" + "Users" + "-zzplant-" + "zzProject" + "Plant" + "7c4"


@dataclass
class Planted:
    root: Path  # the projects root: pass as --root
    run_dir: Path
    repo: Path
    repo_file: Path  # a path inside the repository the killed attempt wrote
    slug: str = SLUG
    session: str = SESSION
    agent1: str = AGENT1
    agent2: str = AGENT2
    label: str = LABEL
    phase: str = PHASE
    excerpt: str = EXCERPT


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def _iso(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def build(
    tmp_path: Path, extra_edits: list[str] | None = None, observable: bool = False,
) -> Planted:
    """`observable=True` (N5c-1) is the variant whose repository group is observed:
    the killed attempt runs no shell command (a shell call makes its ledger partial
    and its group refused), so it has no `bash_timeout` evidence, and a parent
    transcript with the workflow's launch result places every attempt in its
    generation. The default keeps the shell call and its planted excerpt."""
    tmp_path = tmp_path.resolve()  # macOS: /var is a symlink to /private/var; git reports the latter
    repo = tmp_path / ("zzRepo" + "Plant" + "7c5")
    (repo / "src").mkdir(parents=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    repo_file = repo / "src" / "module.py"
    repo_file.write_text("original\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "initial"], repo)
    repo_file.write_text("changed\n", encoding="utf-8")

    root = tmp_path / "projects"
    run_dir = root / SLUG / SESSION / "subagents" / "workflows" / WF_ID
    run_dir.mkdir(parents=True)
    base = datetime.now(timezone.utc) - timedelta(minutes=20)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "launched"},
        {"type": "started", "key": "v2:plant0001", "agentId": AGENT1, "label": LABEL, "phase": PHASE},
        {"type": "started", "key": "v2:plant0001", "agentId": AGENT2, "label": LABEL, "phase": PHASE},
        {"type": "result", "key": "v2:plant0001", "agentId": AGENT2, "result": "done"},
    ])

    def common(agent: str, ts: float) -> dict:
        return {"agentId": agent, "sessionId": SESSION, "cwd": str(repo), "timestamp": _iso(base, ts)}

    _write_jsonl(run_dir / f"agent-{AGENT1}.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "Fix the module."}, **common(AGENT1, 0)},
        {"type": "assistant", "message": {"role": "assistant", "usage": {}, "content": [
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(repo_file)}},
        ]}, **common(AGENT1, 10)},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False},
        ]}, **common(AGENT1, 20)},
        *([] if observable else [
            {"type": "assistant", "message": {"role": "assistant", "usage": {}, "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}},
            ]}, **common(AGENT1, 30)},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                 "content": f"Exit code 143\nCommand timed out after 10m 0s {EXCERPT} {repo_file} {AGENT1}"},
            ]}, **common(AGENT1, 40)},
        ]),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"},
        ]}, **common(AGENT1, 400)},
    ])
    if extra_edits:
        # more Edit calls by the killed attempt, each one spelled as given
        path1 = run_dir / f"agent-{AGENT1}.jsonl"
        rows = [json.loads(line) for line in path1.read_text(encoding="utf-8").splitlines()]
        insert_at = 3  # before the Bash call, after the first Edit's result
        extra_rows = []
        for index, spelled in enumerate(extra_edits):
            tool_id = f"e{index}"
            extra_rows.append({"type": "assistant", "message": {"role": "assistant", "usage": {},
                "content": [{"type": "tool_use", "id": tool_id, "name": "Edit",
                             "input": {"file_path": spelled}}]}, **common(AGENT1, 21 + index)})
            extra_rows.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok", "is_error": False},
            ]}, **common(AGENT1, 21.5 + index)})
        rows[insert_at:insert_at] = extra_rows
        _write_jsonl(path1, rows)
    _write_jsonl(run_dir / f"agent-{AGENT2}.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "Retry the fix."}, **common(AGENT2, 410)},
        {"type": "assistant", "message": {"role": "assistant", "usage": {}, "content": [
            {"type": "text", "text": "Done."},
        ]}, **common(AGENT2, 420)},
    ])
    if observable:
        def launch_row(row_type: str, ts: float, content: list, **extra) -> dict:
            return {"type": row_type, "message": {"role": row_type, "usage": {}, "content": content},
                    "sessionId": SESSION, "timestamp": _iso(base, ts), **extra}

        notification = (
            "<task-notification>\n<task-id>tA</task-id>\n<tool-use-id>toolu_A</tool-use-id>\n"
            "<output-file>/tmp/o</output-file>\n<status>completed</status>\n</task-notification>"
        )
        _write_jsonl(root / SLUG / f"{SESSION}.jsonl", [
            launch_row("assistant", -6, [
                {"type": "tool_use", "id": "toolu_A", "name": "Workflow", "input": {}},
            ]),
            launch_row("user", -5, [
                {"type": "tool_result", "tool_use_id": "toolu_A", "content": "launched"},
            ], toolUseResult={"runId": WF_ID, "taskId": "tA", "status": "async_launched"}),
            {"type": "user", "message": {"role": "user", "content": notification},
             "origin": {"kind": "task-notification"}, "sessionId": SESSION,
             "timestamp": _iso(base, 430)},
        ])
    for agent in (AGENT1, AGENT2):
        (run_dir / f"agent-{agent}.meta.json").write_text(json.dumps({
            "agentType": "general-purpose", "description": LABEL, "workflowPhase": PHASE,
            "spawnDepth": 1, "requestShape": "text", "requestNonInteractive": "true",
        }), encoding="utf-8")
    return Planted(root=root, run_dir=run_dir, repo=repo, repo_file=repo_file)
