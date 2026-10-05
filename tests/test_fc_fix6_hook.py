"""Fix round 6 (W6-1) of field milestone F: hooks/model_allocation_guard.mjs writes its failure
reason at the default share level, so it never echoes a path or a raw error message. The reason
is a fixed sentence plus the class of the failure from a closed list (docs/DESIGN.md 5.1).

The hook is Node; these tests spawn `node` with the same JSON payload Claude Code sends and are
skipped on a machine with no `node`. Every fixture is synthetic.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / "src" / "agent_postmortem" / "hooks" / "model_allocation_guard.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

PRIVATE = ("srv", "Client", "Secret", "deep", "missing", "notes", "Zqa")


def _hook(stdin: str) -> tuple[int, str]:
    result = subprocess.run(
        [NODE, str(HOOK)], input=stdin, capture_output=True, text=True, timeout=20,
    )
    return result.returncode, result.stdout


def _reason(stdin: str) -> str:
    code, out = _hook(stdin)
    assert code == 0, out
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
    return parsed["hookSpecificOutput"]["permissionDecisionReason"]


def _assert_no_private(reason: str, extra=()):
    for word in PRIVATE + tuple(extra):
        assert word not in reason, (word, reason)


# --- an unreadable scriptPath -----------------------------------------------------------------------------


def test_fc6_a_missing_script_path_with_a_space_names_no_path_component():
    payload = {"tool_name": "Workflow", "cwd": "/tmp",
               "tool_input": {"scriptPath": "/srv/Client Secret/deep/missing.mjs"}}
    reason = _reason(json.dumps(payload))
    _assert_no_private(reason)
    assert reason == (
        "model_allocation_guard: could not inspect this Workflow call (could not read "
        "scriptPath: ENOENT); denying rather than silently allowing an unchecked script. Fix "
        "the underlying issue and re-invoke Workflow."
    )


def test_fc6_a_relative_script_path_and_a_cwd_name_no_component(tmp_path):
    cwd = tmp_path / "Zqa Client"
    cwd.mkdir()
    payload = {"tool_name": "Workflow", "cwd": str(cwd),
               "tool_input": {"scriptPath": "deep/missing.mjs"}}
    reason = _reason(json.dumps(payload))
    _assert_no_private(reason, (str(tmp_path), tmp_path.name))
    assert "ENOENT" in reason


def test_fc6_a_directory_as_the_script_path_gives_the_class_only(tmp_path):
    folder = tmp_path / "Zqa Client Secret"
    folder.mkdir()
    reason = _reason(json.dumps({"tool_name": "Workflow", "tool_input": {"scriptPath": str(folder)}}))
    _assert_no_private(reason, (str(tmp_path), tmp_path.name))
    assert "EISDIR" in reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_fc6_an_unreadable_script_gives_the_class_only(tmp_path):
    script = tmp_path / "Zqa Client Secret.mjs"
    script.write_text("agent({})\n", encoding="utf-8")
    script.chmod(0)
    try:
        reason = _reason(json.dumps({"tool_name": "Workflow",
                                     "tool_input": {"scriptPath": str(script)}}))
    finally:
        script.chmod(0o600)
    _assert_no_private(reason, (str(tmp_path), tmp_path.name))
    assert "EACCES" in reason


def test_fc6_a_nul_in_the_script_path_gives_the_error_name_and_never_its_message():
    # a NUL byte makes Node throw ERR_INVALID_ARG_VALUE, a TypeError whose message quotes the path
    payload = {"tool_name": "Workflow", "cwd": "/tmp",
               "tool_input": {"scriptPath": "/srv/Client\u0000Secret/deep/missing.mjs"}}
    reason = _reason(json.dumps(payload))
    _assert_no_private(reason)
    assert "ERR_INVALID_ARG_VALUE" not in reason
    assert "could not read scriptPath: TypeError)" in reason, reason


# --- a payload that is not JSON ------------------------------------------------------------------------------


@pytest.mark.parametrize("stdin", [
    "/srv/Client Secret/deep/missing.mjs", '{"tool_input": {"scriptPath": "/srv/Client Secret/deep',
    "not json Zqa Client",
])
def test_fc6_a_payload_that_is_not_json_is_not_echoed(stdin):
    reason = _reason(stdin)
    _assert_no_private(reason)
    assert "not valid JSON (SyntaxError)" in reason, reason
    assert "Unexpected" not in reason


# --- the other reasons keep their contract ----------------------------------------------------------------------


def test_fc6_a_violation_still_names_only_line_numbers():
    script = "// Zqa Client Secret /srv/deep\nagent({prompt: 'x'})\n"
    reason = _reason(json.dumps({"tool_name": "Workflow", "tool_input": {"script": script}}))
    assert "1 agent() call(s) at line(s) 2" in reason, reason
    _assert_no_private(reason)


def test_fc6_the_source_never_interpolates_a_raw_error_message_or_a_path():
    source = HOOK.read_text(encoding="utf-8")
    assert not re.search(r"\.message\b", source), "a raw error message must not be echoed"
    assert "${ti.scriptPath}" not in source and "${input" not in source
    assert "errorClass" in source
