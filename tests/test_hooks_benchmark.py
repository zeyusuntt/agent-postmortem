"""Performance budget for `agent-postmortem hook check` (docs/DESIGN.md 7.2).

Two scenarios, both against a synthetic session built in tmp_path:

- test_hook_check_meets_its_performance_budget: 20 already-`analyzed`
  finished workflow units (5 transcripts of 2000 lines each, in the shapes
  tests/fixtures/runs already use). p95 under 500 ms once nothing is new
  (docs/DESIGN.md 7.2's D1 stat-only fast path for an unchanged `analyzed`
  unit), then a freshly appended 10-agent unit analyzed under 2 s.
- test_hook_check_unchanged_running_units_meet_budget_without_reparsing:
  20 unchanged `running` workflow units (a journal with `started` but no
  `result`). docs/DESIGN.md 7.2's D7: fingerprint comparison runs first, so
  an unchanged running unit is never reparsed at all; asserted here via a
  counter on `hooks_cli._workflow_finished`, not merely inferred from
  timing.

Kept in the default run (@pytest.mark.benchmark is registered, not
skipped) so a regression is caught the same way any other test failure is.
"""

from __future__ import annotations

import io
import itertools
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem.cli import main

_FAKE_UUID = "00000000-0000-4000-8000-000000000000"
_CWD = "/home/user/project"

# docs/DESIGN.md 5.4 / tests/synthetic-ids.txt: every wf_* id a tracked
# fixture uses must be listed there. Reusing 21 of the ids already listed
# (rather than generating fresh ones) needs no addition to that file; a
# tmp_path tree is its own namespace, so reusing an id another fixture also
# uses causes no collision.
_WF_IDS = [
    "wf_00000000-001", "wf_00000000-730", "wf_00000000-740", "wf_00000000-750",
    "wf_00000000-760", "wf_00000000-770", "wf_00000000-777", "wf_00000000-780",
    "wf_00000000-820", "wf_00000000-790", "wf_00000000-800", "wf_00000000-810",
    "wf_00000000-900", "wf_00000000-930", "wf_00000000-940", "wf_00000000-999",
    "wf_a0000000-001", "wf_b0000000-002", "wf_c0000000-003", "wf_00000000-798",
    "wf_00000000-960",
]

_FIRST_LINE = (
    '{{"type":"user","timestamp":"{ts}","uuid":"{uuid}","parentUuid":null,'
    '"agentId":"{agent_id}","cwd":"{cwd}","sessionId":"{session_id}","version":"2.1.267",'
    '"message":{{"role":"user","content":"Implement the assigned task."}}}}'
)
_ASSISTANT_STEP = (
    '{{"type":"assistant","timestamp":"{ts}","uuid":"{uuid}","parentUuid":"{uuid}",'
    '"agentId":"{agent_id}","cwd":"{cwd}","sessionId":"{session_id}","version":"2.1.267",'
    '"message":{{"role":"assistant","content":[{{"type":"text","text":"Working on step {i}."}},'
    '{{"type":"tool_use","id":"toolu_{i}","name":"Bash","input":{{"command":"echo step-{i}"}}}}],'
    '"usage":{{"input_tokens":5,"output_tokens":3,"cache_read_input_tokens":0,'
    '"cache_creation_input_tokens":0}}}}}}'
)
_TOOL_RESULT_STEP = (
    '{{"type":"user","timestamp":"{ts}","uuid":"{uuid}","parentUuid":"{uuid}",'
    '"agentId":"{agent_id}","cwd":"{cwd}","sessionId":"{session_id}","version":"2.1.267",'
    '"message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"toolu_{i}",'
    '"content":"step-{i} done","is_error":false}}]}}}}'
)
_LAST_LINE = (
    '{{"type":"assistant","timestamp":"{ts}","uuid":"{uuid}","parentUuid":"{uuid}",'
    '"agentId":"{agent_id}","cwd":"{cwd}","sessionId":"{session_id}","version":"2.1.267",'
    '"message":{{"role":"assistant","content":[{{"type":"text","text":"Task complete."}}],'
    '"usage":{{"input_tokens":4,"output_tokens":6,"cache_read_input_tokens":0,'
    '"cache_creation_input_tokens":0}}}}}}'
)


def _ts(seconds: int) -> str:
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _transcript_text(agent_id: str, session_id: str, n_lines: int) -> str:
    """n_lines of realistic-shaped transcript content (the same first-line /
    tool_use+tool_result / final-text shapes tests/fixtures/runs uses),
    built with plain string formatting rather than json.dumps per line --
    this file's own construction cost is not part of the budget under test,
    but a 200,000-line build should still be quick to keep the suite fast."""
    parts = [_FIRST_LINE.format(ts=_ts(0), uuid=_FAKE_UUID, agent_id=agent_id, cwd=_CWD, session_id=session_id)]
    remaining = n_lines - 2
    i = 0
    while remaining > 0:
        parts.append(_ASSISTANT_STEP.format(
            ts=_ts(i + 1), uuid=_FAKE_UUID, agent_id=agent_id, cwd=_CWD, session_id=session_id, i=i,
        ))
        remaining -= 1
        if remaining <= 0:
            break
        parts.append(_TOOL_RESULT_STEP.format(
            ts=_ts(i + 1), uuid=_FAKE_UUID, agent_id=agent_id, cwd=_CWD, session_id=session_id, i=i,
        ))
        remaining -= 1
        i += 1
    parts.append(_LAST_LINE.format(ts=_ts(i + 2), uuid=_FAKE_UUID, agent_id=agent_id, cwd=_CWD, session_id=session_id))
    return "\n".join(parts[:n_lines]) + "\n"


def _build_finished_workflow_unit(
    workflows_dir: Path, run_id: str, n_agents: int, n_lines_each: int,
    session_id: str, id_counter: "itertools.count",
) -> None:
    unit_dir = workflows_dir / run_id
    unit_dir.mkdir(parents=True)
    journal_lines = ['{"type":"launched","timestamp":"2026-03-01T00:00:00Z"}']
    for k in range(n_agents):
        agent_id = "a" + format(next(id_counter), "016x")
        key = f"v2:agent-{k}"
        journal_lines.append(json.dumps({
            "type": "started", "timestamp": _ts(k), "key": key,
            "agentId": agent_id, "label": f"impl:{k}", "phase": "Implement",
        }))
        journal_lines.append(json.dumps({
            "type": "result", "timestamp": _ts(1000 + k), "agentId": agent_id,
            "key": key, "outcome": "completed",
        }))
        (unit_dir / f"agent-{agent_id}.jsonl").write_text(
            _transcript_text(agent_id, session_id, n_lines_each), encoding="utf-8",
        )
        (unit_dir / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({
                "agentType": "general-purpose", "description": f"Implement part {k}.",
                "workflowPhase": "Implement", "spawnDepth": "1",
                "requestShape": "single", "requestNonInteractive": "true",
            }),
            encoding="utf-8",
        )
    (unit_dir / "journal.jsonl").write_text("\n".join(journal_lines) + "\n", encoding="utf-8")


def _build_running_workflow_unit(workflows_dir: Path, run_id: str, agent_id: str) -> None:
    """A workflow unit that never finishes: a `started` with no matching
    `result` (docs/DESIGN.md 7.2's `_workflow_finished` == False forever).
    Minimal content is deliberate -- this unit must never actually be
    reparsed once its fingerprint stops changing, so its size must not be
    what keeps it inside the budget."""
    unit_dir = workflows_dir / run_id
    unit_dir.mkdir(parents=True)
    journal_lines = [
        '{"type":"launched","timestamp":"2026-03-01T00:00:00Z"}',
        json.dumps({
            "type": "started", "timestamp": _ts(0), "key": "v2:agent-0",
            "agentId": agent_id, "label": "impl:0", "phase": "Implement",
        }),
    ]
    (unit_dir / "journal.jsonl").write_text("\n".join(journal_lines) + "\n", encoding="utf-8")


def _run_hook_check(monkeypatch, capsys, payload: dict) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code = main(["hook", "check"])
    capsys.readouterr()  # drain stdout so it never pollutes pytest's own output
    return code


@pytest.mark.benchmark
def test_hook_check_meets_its_performance_budget(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    root = tmp_path / "root"
    slug_dir = root / "-home-user-project"
    session_id = _FAKE_UUID
    session_dir = slug_dir / session_id
    workflows_dir = session_dir / "subagents" / "workflows"
    (session_dir / "subagents").mkdir(parents=True)
    main_transcript = slug_dir / f"{session_id}.jsonl"
    main_transcript.write_text(
        _FIRST_LINE.format(ts=_ts(0), uuid=_FAKE_UUID, agent_id=session_id, cwd=_CWD, session_id=session_id) + "\n",
        encoding="utf-8",
    )

    id_counter = itertools.count(1)
    for run_id in _WF_IDS[:20]:
        _build_finished_workflow_unit(workflows_dir, run_id, 5, 2000, session_id, id_counter)

    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(main_transcript),
        "session_id": session_id,
        "stop_hook_active": False,
    }

    # First check parses and analyzes all 20 units; not part of the budget.
    assert _run_hook_check(monkeypatch, capsys, payload) == 0

    # docs/DESIGN.md 7.2: p95 under 500 ms once nothing has changed.
    samples = []
    for _ in range(10):
        start = time.perf_counter()
        assert _run_hook_check(monkeypatch, capsys, payload) == 0
        samples.append(time.perf_counter() - start)
    samples.sort()
    p95 = samples[math.ceil(0.95 * len(samples)) - 1]
    assert p95 < 0.5, f"stat-only p95 {p95:.3f}s over the 500ms budget: {samples}"

    # docs/DESIGN.md 7.2: one newly finished 10-agent unit analyzed under 2s.
    _build_finished_workflow_unit(workflows_dir, _WF_IDS[20], 10, 2000, session_id, id_counter)
    start = time.perf_counter()
    assert _run_hook_check(monkeypatch, capsys, payload) == 0
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"new-unit analysis took {elapsed:.3f}s, over the 2s budget"


@pytest.mark.benchmark
def test_hook_check_unchanged_running_units_meet_budget_without_reparsing(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 7.2's D7: fingerprint comparison runs first, so an
    unchanged `running` unit is never reparsed -- checked here via a
    counter on hooks_cli._workflow_finished, not only via timing."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    root = tmp_path / "root"
    slug_dir = root / "-home-user-project"
    session_id = _FAKE_UUID
    session_dir = slug_dir / session_id
    workflows_dir = session_dir / "subagents" / "workflows"
    (session_dir / "subagents").mkdir(parents=True)
    main_transcript = slug_dir / f"{session_id}.jsonl"
    main_transcript.write_text(
        _FIRST_LINE.format(ts=_ts(0), uuid=_FAKE_UUID, agent_id=session_id, cwd=_CWD, session_id=session_id) + "\n",
        encoding="utf-8",
    )

    for i, run_id in enumerate(_WF_IDS[:20]):
        agent_id = "a" + format(i + 1, "016x")
        _build_running_workflow_unit(workflows_dir, run_id, agent_id)

    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(main_transcript),
        "session_id": session_id,
        "stop_hook_active": False,
    }

    # First check inspects every unit fresh (no previous fingerprint to
    # compare against); not part of the no-reparse assertion below.
    assert _run_hook_check(monkeypatch, capsys, payload) == 0

    from agent_postmortem import hooks_cli

    calls = {"count": 0}
    original = hooks_cli._workflow_finished

    def _counting_workflow_finished(journal_path):
        calls["count"] += 1
        return original(journal_path)

    monkeypatch.setattr(hooks_cli, "_workflow_finished", _counting_workflow_finished)

    # Exactly 2 more checks: docs/DESIGN.md 7.2's unknown threshold is 3
    # unchanged checks, and crossing it forces one real parse -- this
    # measurement must stay under that threshold to test the fast path at
    # all (stable_checks goes 0 -> 1 -> 2, never reaching 3).
    samples = []
    for _ in range(2):
        start = time.perf_counter()
        assert _run_hook_check(monkeypatch, capsys, payload) == 0
        samples.append(time.perf_counter() - start)
    p95 = max(samples)
    assert p95 < 0.5, f"unchanged-running p95 {p95:.3f}s over the 500ms budget: {samples}"
    assert calls["count"] == 0, (
        f"_workflow_finished was called {calls['count']} times for an unchanged fingerprint"
    )
