"""DESIGN.md 4.3 / 4.7: a run-wide tooling finding (bash_backgrounded,
bash_timeout, tool_json_error, git_cwd_mismatch, classifier_unavailable,
structured_output_failure)
carries an agent_id only when every hit belongs to that one agent. With hits
in two or more agents the agent_id is None and the finding backs the one run
story, never one agent's story. Synthetic in-memory runs only."""

from __future__ import annotations

import pytest

from agent_postmortem import markers, report
from agent_postmortem.detectors import (
    bash_backgrounded,
    bash_timeout,
    classifier_unavailable,
    git_cwd_mismatch,
    structured_output_failure,
    tool_json_error,
)
from agent_postmortem.redact import Redactor
from tests.test_detectors_tooling import _build_run

_BACKGROUNDED = (
    "Command did not complete within its 120s timeout and was moved to the background"
)
_JSON = (
    f"<tool_use_error>{markers.TOOL_INPUT_INVALID[0]}: "
    "Read was called with input that could not be parsed as JSON."
)
_CLASSIFIER = (
    f"claude-sonnet-5[1m] is {markers.CLASSIFIER_UNAVAILABLE[0]} (timed out), "
    "so auto mode cannot determine the safety of Bash right now."
)
_GIT = f"{markers.GIT_NOT_REPO[0]} (or any of the parent directories): .git"

CASES = {
    "bash_backgrounded": (
        bash_backgrounded, "wf_00000000-790",
        {"kind": "tool_result", "tool_name": "Bash", "text": _BACKGROUNDED},
    ),
    "bash_timeout": (
        bash_timeout, "wf_00000000-800",
        {"kind": "tool_result", "tool_name": "Bash", "text": "Exit code 143", "exit_code": 143},
    ),
    "tool_json_error": (
        tool_json_error, "wf_00000000-710",
        {"kind": "tool_result", "tool_name": "Read", "text": _JSON, "is_error": True},
    ),
    "classifier_unavailable": (
        classifier_unavailable, "wf_00000000-720",
        {"kind": "tool_result", "tool_name": "Bash", "text": _CLASSIFIER},
    ),
    "git_cwd_mismatch": (
        git_cwd_mismatch, "wf_00000000-730",
        {"kind": "tool_result", "tool_name": "Bash", "text": _GIT,
         "exit_code": 128, "is_error": True},
    ),
    "structured_output_failure": (
        structured_output_failure, "wf_00000000-810",
        {"kind": "tool_result", "tool_name": "StructuredOutput", "is_error": True,
         "text": "Output does not match required schema: root: must have required property 'x'"},
    ),
}
AGENT_A = "a9100000000000001"
AGENT_B = "a9100000000000002"


def _two_agent_run(run_id: str, row: dict):
    """One run whose two agents each have one hit."""
    first = _build_run(run_id, AGENT_A, [row])
    second = _build_run(run_id, AGENT_B, [row])
    for label, run in (("alpha", first), ("beta", second)):
        run.attempts[0].label = label
        run.agents[0].label = label
    first.attempts = first.attempts + second.attempts
    first.agents = first.agents + second.agents
    return first


@pytest.mark.parametrize("name", sorted(CASES))
def test_hits_in_one_agent_keep_that_agent(name):
    module, run_id, row = CASES[name]
    run = _build_run(run_id, AGENT_A, [row, row])
    (finding,) = module.DETECTOR.run(run)
    assert finding.agent_id == AGENT_A
    assert finding.metrics["count"] == 2


@pytest.mark.parametrize("name", sorted(CASES))
def test_hits_in_two_agents_have_no_agent_and_back_the_run_story(name):
    module, run_id, row = CASES[name]
    run = _two_agent_run(run_id, row)
    (finding,) = module.DETECTOR.run(run)
    assert finding.agent_id is None
    assert finding.metrics["count"] == 2

    records = report._story_records(run, [finding], False)
    assert [r["agent"] for r in records] == ["run"]
    assert records[0]["text"].rstrip(".") == finding.title

    text = report.render_run_text(run, [finding], Redactor(enabled=False))
    stories = text.split("STORIES", 1)[1].splitlines()[1:]
    story_lines = [line for line in stories if line.startswith("  ")]
    assert story_lines and story_lines[0].startswith("  run  ")
    assert not any(line.startswith(("  alpha", "  beta")) for line in story_lines)


def test_finding_id_differs_between_one_agent_and_many():
    module, run_id, row = CASES["bash_backgrounded"]
    one = module.DETECTOR.run(_build_run(run_id, AGENT_A, [row]))[0]
    many = module.DETECTOR.run(_two_agent_run(run_id, row))[0]
    assert one.finding_id != many.finding_id
