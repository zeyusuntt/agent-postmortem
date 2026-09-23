"""Tests for hooks/model_allocation_guard.mjs: the PreToolUse hook backing
R-MODEL-ALLOCATION. It is Node, not Python (there is no third-party runtime
dependency in agent_postmortem itself; this hook is a separate installable
artifact), so these tests spawn `node` and feed it the same JSON stdin
payload Claude Code would. Skipped entirely on a machine with no `node` on
PATH, matching the "no assumptions about this machine" rule for anything
user-facing. See docs/review-codex.md's two major findings on this file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "agent_postmortem" / "hooks" / "model_allocation_guard.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def _run_hook(payload: dict) -> tuple[int, dict | None]:
    result = subprocess.run(
        [NODE, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    stdout = result.stdout.strip()
    parsed = json.loads(stdout) if stdout else None
    return result.returncode, parsed


def _deny_reason(parsed: dict) -> str:
    return parsed["hookSpecificOutput"]["permissionDecisionReason"]


def _workflow_payload(script: str) -> dict:
    return {"tool_name": "Workflow", "tool_input": {"script": script}}


# --- confirmed non-Workflow and nothing-to-check exit clean -----------------


def test_a_non_workflow_tool_call_is_not_inspected():
    code, parsed = _run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert code == 0
    assert parsed is None


def test_a_named_workflow_with_no_script_text_has_nothing_to_check():
    code, parsed = _run_hook({"tool_name": "Workflow", "tool_input": {"name": "my-workflow"}})
    assert code == 0
    assert parsed is None


# --- fail closed when the call cannot be inspected --------------------------


def test_a_payload_that_is_not_valid_json_denies_with_a_diagnostic():
    result = subprocess.run(
        [NODE, str(HOOK_PATH)], input="not json", capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "not valid JSON" in _deny_reason(parsed)


def test_an_unreadable_scriptpath_denies_with_a_diagnostic(tmp_path):
    missing = tmp_path / "does-not-exist.mjs"
    code, parsed = _run_hook({"tool_name": "Workflow", "tool_input": {"scriptPath": str(missing)}})
    assert code == 0
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "could not read scriptPath" in _deny_reason(parsed)


# --- concrete top-level opts.model / opts.agentType --------------------------


def test_a_call_with_no_model_or_agenttype_is_a_violation():
    code, parsed = _run_hook(_workflow_payload("agent(doStuff)"))
    assert code == 0
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "line(s) 1" in _deny_reason(parsed)


def test_a_concrete_top_level_model_exempts_the_call():
    code, parsed = _run_hook(_workflow_payload('agent(doStuff, { model: "opus" })'))
    assert code == 0
    assert parsed is None


def test_a_concrete_top_level_agenttype_exempts_the_call():
    code, parsed = _run_hook(_workflow_payload('agent(doStuff, { agentType: "reviewer" })'))
    assert code == 0
    assert parsed is None


def test_model_set_to_the_literal_undefined_is_still_a_violation():
    code, parsed = _run_hook(_workflow_payload("agent(doStuff, { model: undefined })"))
    assert code == 0
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_model_property_nested_inside_another_object_does_not_exempt_the_call():
    """A model mentioned deep in some unrelated nested option (or response
    data reflected back into the call) must not count as this call setting
    its own model."""
    code, parsed = _run_hook(_workflow_payload('agent(doStuff, { onError: { model: "x" } })'))
    assert code == 0
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_model_ok_comment_still_exempts_a_call_with_neither_option():
    code, parsed = _run_hook(_workflow_payload("agent(doStuff) // model-ok: inherits session model\n"))
    assert code == 0
    assert parsed is None
