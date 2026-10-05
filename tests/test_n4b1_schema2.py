"""N4b-1: the private reader facts never reach report.export_run() (schema 2,
docs/DESIGN.md 5.3b; .reviews/v0.3/N4-plan.md L1, L15). The byte goldens in
tests/test_schema2_goldens.py are the other half of this proof.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent_postmortem import detectors, reader, report
from agent_postmortem.redact import Redactor
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _assistant_tool,
    _no_config,
    _parent_path,
    _simple_completed_journal,
    _tool_result,
    _user,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)

PRIVATE_KEYS = {
    "journal_state", "result_ts", "discovery", "run_in_background", "multiedit_path",
    "classification", "risk_population_completeness",
    "risk_population_completeness_reasons", "_generations",
}
# N5a-1: `ledger` is public in schema 3 (docs/DESIGN.md 5.3b), exactly these keys.
PUBLIC_LEDGER_KEYS = {"completeness", "reasons", "entries"}


def _keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


def test_the_export_carries_none_of_the_private_reader_facts(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    wf_id = "wf_00000000-c41"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(1)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("go", base, agent_id),
        _assistant_tool("t1", "Bash", {"command": "git commit -m x", "run_in_background": False},
                        base, agent_id),
        _tool_result("t1", "[main abc1234] x", base, agent_id),
        _assistant_tool("t2", "MultiEdit", {"file_path": "/tmp/ws/a.py"}, base, agent_id),
        _tool_result("t2", "ok", base, agent_id),
        _assistant_tool("t3", "Monitor", {"command": "tail -f x"}, base, agent_id),
        _assistant_text("done", base, agent_id),
    ])
    _write_jsonl(_parent_path(root), [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
    ])
    run = reader.load_run(run_dir)
    # the private forms do carry the facts (so this test cannot pass vacuously)
    private = run.to_dict()
    assert "discovery" in private
    assert "journal_state" in private["attempts"][0]
    exported = report.export_run(run, detectors.run_detectors(run), Redactor(enabled=False))
    assert _keys(exported) & PRIVATE_KEYS == set()
    for attempt in exported["run"]["attempts"]:
        assert set(attempt["ledger"]) == PUBLIC_LEDGER_KEYS
    text = json.dumps(exported)
    for name in ("journal_state", "result_ts", "multiedit_path", "run_in_background"):
        assert name not in text


def test_the_design_names_the_new_private_facts_and_the_reconciliation_codes():
    from pathlib import Path

    flat = " ".join((Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md").read_text(
        encoding="utf-8").split())
    for phrase in (
        "journal_state: str | None",
        "result_ts: datetime | None",
        "discovery: dict[str, list[str]]",
        "multiedit_path: str | None",
        "run_in_background: object",
        "classification: CallClass | None",
        "launch has no discovered child (<code>)",
        "Agent, Task, Workflow",
    ):
        assert phrase in flat, phrase
    for code in (
        "malformed_launch", "ambiguous_tool_id", "no_result", "ambiguous_result",
        "non_string_id", "conflicting_ids", "no_id", "undiscovered",
    ):
        assert f"`{code}`" in flat, code
