"""Shared synthetic builders for the N5b tests (severity inputs, rows, the
verdict). Everything is synthetic: paths are under /tmp, the ids are the
registered synthetic ones, nothing is a real transcript.

Two kinds of builder:

- `build_workflow` writes a real workflow run directory (journal, agent
  transcripts, a parent transcript with a launch and an optional terminal
  notification) so the reader, the ledger, and the lifecycle compute every
  input honestly.
- `model_*` build bare dataclasses for the input functions of `severity`,
  where the whole point is to set one field and nothing else.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem.models import Attempt, Ledger, LedgerEntry, Lifecycle, LogicalAgent, Run
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
    "Spec", "build_workflow", "build_session", "model_attempt", "model_workflow_run", "model_session_run",
    "ledger_of", "entry_of", "severity", "BASE", "_no_config", "_aid",
]


class _Lazy:
    """The severity module, looked up on first use (RED: it does not exist yet)."""

    def __getattr__(self, name):
        return getattr(importlib.import_module("agent_postmortem.severity"), name)


severity = _Lazy()

BASE = datetime.now(timezone.utc) - timedelta(minutes=30)


@dataclass
class Spec:
    """One attempt of a built workflow run."""

    key: str
    number: int  # distinct per attempt in one run; becomes the agent id
    journal: str = "completed"  # completed | failed | open | superseded (just not closed)
    kill: bool = False  # the transcript ends in the kill sentinel
    silence: int = 10  # seconds between the last real event and the kill sentinel
    tools: list = field(default_factory=list)  # (tool_id, name, input, result_text, is_error)
    last_text: str = "done"
    result: dict = field(default_factory=lambda: {"ok": True})
    start: int = 10  # seconds after BASE
    prompt: str = "Do the thing."


def build_workflow(
    tmp_path: Path, monkeypatch, wf_id: str, specs: list[Spec], *, parent: str | None = "completed",
    slug: str = FAKE_SLUG,
):
    """Write the run and return its directory. `parent` is None (no parent
    transcript at all), a lifecycle word for the launch's notification
    (`completed`, `failed`, `stopped`), or `running` (a launch with no
    notification)."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id, slug=slug)
    journal: list[dict] = []
    for spec in specs:
        agent_id = _aid(spec.number)
        journal.append({
            "type": "started", "key": spec.key, "agentId": agent_id,
            "label": f"impl-{spec.number}", "phase": "Implement",
        })
        if spec.journal == "completed":
            journal.append({
                "type": "result", "key": spec.key, "agentId": agent_id, "result": spec.result,
            })
        elif spec.journal == "failed":
            journal.append({"type": "failed", "key": spec.key, "agentId": agent_id})
        start = BASE + timedelta(seconds=spec.start)
        events = [_user(spec.prompt, start, agent_id)]
        step = 1
        for tool_id, name, input_obj, result_text, is_error in spec.tools:
            events.append(_assistant_tool(
                tool_id, name, input_obj, start + timedelta(seconds=step), agent_id,
            ))
            events.append(_tool_result(
                tool_id, result_text, start + timedelta(seconds=step + 1), agent_id,
                is_error=is_error,
            ))
            step += 2
        events.append(_assistant_text(spec.last_text, start + timedelta(seconds=step), agent_id))
        if spec.kill:
            events.append(_kill(start + timedelta(seconds=step + spec.silence), agent_id))
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


def build_session(tmp_path: Path, subagent_events, *, session: str = FAKE_UUID):
    """A session transcript with one subagent; `subagent_events(agent_id)` returns
    that subagent's transcript lines. Returns (main transcript path, agent id)."""
    root = tmp_path / "sroot"
    main = root / FAKE_SLUG / f"{session}.jsonl"
    agent_id = _aid(60)
    _write_jsonl(main, [_user("start", BASE)])
    sub = root / FAKE_SLUG / session / "subagents" / f"agent-{agent_id}.jsonl"
    _write_jsonl(sub, subagent_events(agent_id))
    sub.with_suffix(".meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "impl:s"}), encoding="utf-8",
    )
    return main, agent_id


# --- bare dataclasses -----------------------------------------------------------------


def entry_of(effect: str, path: str | None = "/tmp/ws/a.txt") -> LedgerEntry:
    basis = {
        "observed": "tool_result", "claimed_target": "command_grammar",
        "attempted": "tool_result", "possible": "opaque_command",
    }[effect]
    return LedgerEntry(effect, basis, "success", path, "/tmp/ws", "agent-x.jsonl:3")


def ledger_of(*effects: str, completeness: str = "complete") -> Ledger:
    reasons = [] if completeness == "complete" else ["shell_context_unverified"]
    return Ledger([entry_of(effect) for effect in effects], completeness, reasons)


def model_attempt(
    number: int, key: str | None = "v2:x", ordinal: int = 1, outcome: str = "completed",
    journal_state: str | None = None, ledger: Ledger | None = None, **extra,
) -> Attempt:
    return Attempt(
        agent_id=_aid(number), key=key, label=f"impl-{number}", phase="Implement",
        agent_type=None, meta={}, transcript_path=None, events=[], first_ts=None, last_ts=None,
        ordinal=ordinal, outcome=outcome, usage={}, journal_state=journal_state, ledger=ledger,
        **extra,
    )


def _lifecycle(terminal: str | None) -> Lifecycle | None:
    if terminal is None:
        return None
    return Lifecycle(
        terminal=terminal, source=None if terminal == "none" else "notification",
        activity="stale", generations=1, diagnostics=[],
    )


def model_workflow_run(
    attempts: list[Attempt], *, terminal: str | None = "completed", status: str = "COMPLETE",
    completeness: str | None = "complete",
) -> Run:
    by_key: dict[str, list[Attempt]] = {}
    for attempt in attempts:
        if attempt.key is not None:
            by_key.setdefault(attempt.key, []).append(attempt)
    agents = [
        LogicalAgent(key=key, label=group[0].label, phase="Implement", attempts=group)
        for key, group in by_key.items()
    ]
    run = Run(
        run_id="wf_00000000-b00", kind="workflow", path=Path("/tmp/n5b-model"),
        project_slug="-home-user-project", session_id=None, claude_version=None, journal=[],
        agents=agents, attempts=attempts, started_at=None, ended_at=None, diagnostics=[],
        status=status, lifecycle=_lifecycle(terminal),
    )
    run.risk_population_completeness = completeness
    return run


def model_session_run(
    attempts: list[Attempt], *, main: Attempt | None = None, status: str = "COMPLETE",
    completeness: str | None = "complete",
) -> Run:
    run = Run(
        run_id="00000000-0000-4000-8000-000000000000", kind="session",
        path=Path("/tmp/n5b-model-session.jsonl"), project_slug="-home-user-project",
        session_id=None, claude_version=None, journal=[], main=main, agents=[],
        attempts=attempts, started_at=None, ended_at=None, diagnostics=[], status=status,
        finished=None,
    )
    run.risk_population_completeness = completeness
    return run
