"""Shared, minimal on-disk workflow-run builder for tests/test_rules_propose.py
and tests/test_rules_review.py: a workflow run whose single attempt fires
git_cwd_mismatch (backing R-GIT-CWD), timestamped wherever the caller wants,
under a plain <root>/<slug>/<session>/subagents/workflows/<wf_id>/ layout
reader.discover_runs() already knows how to find. Not a test_*.py module
itself (mirrors tests/privacy_rules.py's own pattern), so pytest never
collects it directly.

Every wf_ id a caller passes in must already be listed in
tests/synthetic-ids.txt (docs/DESIGN.md 5.4, tests/test_privacy.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

GIT_NOT_REPO_TEXT = "fatal: not a git repository (or any of the parent directories): .git"
SLUG = "-home-user-project"
SESSION_UUID = "00000000-0000-4000-8000-000000000000"
CWD = "/home/user/project"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_id(n: int) -> str:
    """A conforming agent id (reader._AGENT_ID_RE: 'a' + 16 hex chars) -- a
    non-conforming one would force Run.status INCOMPLETE via a reader
    diagnostic and break every count below."""
    return "a" + format(n, "016x")


def build_git_cwd_mismatch_run(
    root: Path, wf_id: str, ts: datetime, *, agent_n: int = 1,
) -> Path:
    """A minimal workflow run under root/SLUG/SESSION_UUID: one attempt, one
    Bash call ('git status') that ran outside a git repository -- fires
    git_cwd_mismatch (rule_id R-GIT-CWD, docs/DESIGN.md 4.3), a plain minor
    detector needing nothing more than one tool_use/tool_result pair.
    Run.started_at comes from the earliest transcript event timestamp
    (docs/DESIGN.md 4.2), so `ts` is what actually places this run on a day.
    """
    aid = agent_id(agent_n)
    wf_dir = root / SLUG / SESSION_UUID / "subagents" / "workflows" / wf_id
    wf_dir.mkdir(parents=True, exist_ok=True)

    journal = [
        {"type": "started", "key": f"v2:{wf_id}", "agentId": aid, "label": "impl", "phase": "Implement"},
    ]
    (wf_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in journal) + "\n", encoding="utf-8"
    )
    (wf_dir / f"agent-{aid}.meta.json").write_text(
        json.dumps({
            "agentType": "general-purpose", "description": "impl", "workflowPhase": "Implement",
            "spawnDepth": "1", "requestShape": "single", "requestNonInteractive": "true",
        }),
        encoding="utf-8",
    )

    tool_id = "toolu_01"
    events = [
        {
            "type": "user", "message": {"role": "user", "content": "run git status"},
            "agentId": aid, "sessionId": SESSION_UUID, "version": "2.1.267",
            "cwd": CWD, "timestamp": _iso(ts),
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "git status"}}
                ],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
            "agentId": aid, "sessionId": SESSION_UUID, "version": "2.1.267",
            "cwd": CWD, "timestamp": _iso(ts),
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result", "tool_use_id": tool_id,
                        "content": GIT_NOT_REPO_TEXT, "is_error": True,
                    }
                ],
            },
            "agentId": aid, "sessionId": SESSION_UUID, "version": "2.1.267",
            "cwd": CWD, "timestamp": _iso(ts),
        },
    ]
    (wf_dir / f"agent-{aid}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return wf_dir


def build_clean_run(root: Path, wf_id: str, ts: datetime, *, agent_n: int = 1) -> Path:
    """A minimal workflow run exactly like build_git_cwd_mismatch_run's own
    shape, except its one Bash call succeeds ordinarily inside a git
    repository -- it counts as a run (a denominator) without ever firing
    git_cwd_mismatch (a numerator), for a before/after rate below 100%."""
    aid = agent_id(agent_n)
    wf_dir = root / SLUG / SESSION_UUID / "subagents" / "workflows" / wf_id
    wf_dir.mkdir(parents=True, exist_ok=True)

    journal = [
        {"type": "started", "key": f"v2:{wf_id}", "agentId": aid, "label": "impl", "phase": "Implement"},
        {"type": "result", "key": f"v2:{wf_id}", "agentId": aid, "result": {"ok": True}},
    ]
    (wf_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in journal) + "\n", encoding="utf-8"
    )
    (wf_dir / f"agent-{aid}.meta.json").write_text(
        json.dumps({
            "agentType": "general-purpose", "description": "impl", "workflowPhase": "Implement",
            "spawnDepth": "1", "requestShape": "single", "requestNonInteractive": "true",
        }),
        encoding="utf-8",
    )

    tool_id = "toolu_01"
    events = [
        {
            "type": "user", "message": {"role": "user", "content": "run git status"},
            "agentId": aid, "sessionId": SESSION_UUID, "version": "2.1.267",
            "cwd": CWD, "timestamp": _iso(ts),
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "git status"}}
                ],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
            "agentId": aid, "sessionId": SESSION_UUID, "version": "2.1.267",
            "cwd": CWD, "timestamp": _iso(ts),
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result", "tool_use_id": tool_id,
                        "content": "On branch main\nnothing to commit, working tree clean",
                        "is_error": False,
                    }
                ],
            },
            "agentId": aid, "sessionId": SESSION_UUID, "version": "2.1.267",
            "cwd": CWD, "timestamp": _iso(ts),
        },
    ]
    (wf_dir / f"agent-{aid}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return wf_dir
