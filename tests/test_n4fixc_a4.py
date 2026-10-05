"""Fix C (N4 fix M-N4-3, Amendment A4): parent discovery diagnostics are scoped to the
run they involve; a rejected Workflow launch reconciles; byte-identical duplicate results
are one result (docs/DESIGN.md 4.13.1, 4.13.3).

Fixtures are synthetic; every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import reader
from tests.test_field_v03 import (
    _aid,
    _no_config,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)
from tests.test_n4b1_discovery import (
    _agent_result,
    _diag,
    _launch,
    _session,
    _transcripts,
    _workflow,
    _workflow_result,
)

BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
RUN_A = "wf_00000000-c61"
RUN_B = "wf_00000000-c62"
RUN_C = "wf_00000000-c72"
RUN_D = "wf_00000000-c73"


def _sec(n):
    return BASE + timedelta(seconds=n)


def _lines(entries):
    return "".join(json.dumps(entry) + "\n" for entry in entries)


def _parent_discovery(tmp_path, monkeypatch, entries, run_id=RUN_A):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow(tmp_path, run_id, parent=_lines(entries))
    return reader.load_run(run_dir).discovery["parent"]


def _rejected_result(tool_id, ts, text="The Workflow tool could not start", is_error=True):
    entry = _workflow_launch_result(tool_id, ts, RUN_A, "tA")
    entry["message"]["content"][0]["content"] = text
    if is_error:
        entry["message"]["content"][0]["is_error"] = True
    entry["toolUseResult"] = text
    return entry


# --- (a) a parent diagnostic reaches only the run it involves ---------------------------


def test_a_result_less_launch_with_no_resume_reaches_no_run(tmp_path, monkeypatch):
    entries = [_workflow_launch("toolu_1", _sec(0))]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == []


def test_a_malformed_launch_result_with_no_resume_reaches_no_run(tmp_path, monkeypatch):
    bad = _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA")
    del bad["toolUseResult"]["taskId"]
    del bad["toolUseResult"]["runId"]
    entries = [_workflow_launch("toolu_1", _sec(0)), bad]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == []


def test_an_ambiguous_launch_of_another_run_is_not_in_this_runs_parent_discovery(
    tmp_path, monkeypatch
):
    entries = [
        _workflow_launch("toolu_1", _sec(0)),
        _workflow_launch_result("toolu_1", _sec(1), RUN_C, "t1"),
        _workflow_launch_result("toolu_1", _sec(2), RUN_C, "t2"),
    ]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == []


def test_an_ambiguous_launch_whose_result_names_the_run_reaches_it(tmp_path, monkeypatch):
    entries = [
        _workflow_launch("toolu_1", _sec(0)),
        _workflow_launch_result("toolu_1", _sec(1), RUN_A, "t1"),
        _workflow_launch_result("toolu_1", _sec(2), RUN_A, "t2"),
    ]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == [
        "ambiguous launch 'toolu_1': lines 1, 2, 3"
    ]


def test_an_ambiguous_launch_that_resumes_the_run_reaches_it(tmp_path, monkeypatch):
    entries = [
        _workflow_launch("toolu_1", _sec(0), resume_from=RUN_A),
        _workflow_launch("toolu_1", _sec(1)),
    ]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == [
        "ambiguous launch 'toolu_1': lines 1, 2"
    ]


def test_a_result_less_resume_launch_reaches_the_run_it_names(tmp_path, monkeypatch):
    entries = [_workflow_launch("toolu_1", _sec(0), resume_from=RUN_A)]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == [
        "launch without result at line 1"
    ]


def test_a_malformed_resume_result_reaches_the_run_it_names(tmp_path, monkeypatch):
    bad = _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA")
    del bad["toolUseResult"]["status"]
    entries = [_workflow_launch("toolu_1", _sec(0), resume_from=RUN_A), bad]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == [
        "malformed launch result at line 2"
    ]


def test_a_result_less_resume_launch_does_not_reach_another_run(tmp_path, monkeypatch):
    entries = [_workflow_launch("toolu_1", _sec(0), resume_from=RUN_B)]
    assert _parent_discovery(tmp_path, monkeypatch, entries) == []


def test_a_parent_file_diagnostic_still_reaches_every_run(tmp_path, monkeypatch):
    assert _parent_discovery(tmp_path, monkeypatch, []) == []
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow(tmp_path, RUN_A, parent="not json\n")
    assert reader.load_run(run_dir).discovery["parent"] == [
        "00000000-0000-4000-8000-000000000000.jsonl:1: undecodable parent transcript line"
    ]


# --- (b) a rejected Workflow launch ----------------------------------------------------------


def test_a_rejected_launch_is_no_parent_diagnostic(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0)), _rejected_result("toolu_1", _sec(1)),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.diagnostics == []
    assert index.generations == []


def test_a_rejected_resume_launch_builds_no_incomplete_generation(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0), resume_from=RUN_A),
        _rejected_result("toolu_1", _sec(1)),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.generations == []
    assert index.diagnostics == ["rejected launch result at line 2"]


def test_a_string_result_without_is_error_is_still_malformed(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0)),
        _rejected_result("toolu_1", _sec(1), is_error=False),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.diagnostics == ["malformed launch result at line 2"]


def test_an_is_error_result_with_a_dict_toolUseResult_is_still_malformed(tmp_path):
    bad = _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA")
    del bad["toolUseResult"]["taskId"]
    bad["message"]["content"][0]["is_error"] = True
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [_workflow_launch("toolu_1", _sec(0)), bad])
    assert reader.ParentIndex._scan(parent).diagnostics == ["malformed launch result at line 2"]


def test_an_initial_rejected_workflow_launch_is_undiscovered_in_the_session_table(tmp_path):
    main = _session(tmp_path, [
        _launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2)),
    ])
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_rejected_precedes_undiscovered(tmp_path):
    # Fix E (R3-2): an initial rejected launch is undiscovered; the rejected row precedes the
    # generic "no discovered child" row only for a resume launch (see test_n4fixd_a5.py).
    result = _rejected_result("toolu_1", _sec(2))
    result["toolUseResult"] = "x"
    main = _session(tmp_path, [_launch("toolu_1", "Workflow"), result])
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_a_workflow_result_with_a_string_but_no_is_error_is_undiscovered(tmp_path):
    main = _session(tmp_path, [
        _launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2), is_error=False),
    ])
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_a_rejected_workflow_with_a_second_differing_result_is_ambiguous(tmp_path):
    main = _session(tmp_path, [
        _launch("toolu_1", "Workflow"),
        _rejected_result("toolu_1", _sec(2)),
        _rejected_result("toolu_1", _sec(3), text="a different refusal"),
    ])
    assert _transcripts(main) == [_diag(2, "Workflow", "ambiguous_result")]


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_an_errored_agent_or_task_call_keeps_its_table(tmp_path, tool):
    result = _agent_result("toolu_1")
    result["message"]["content"][0]["is_error"] = True
    result["toolUseResult"] = "boom"
    main = _session(tmp_path, [_launch("toolu_1", tool), result])
    assert _transcripts(main) == [_diag(2, tool, "no_id")]


# --- (c) byte-identical duplicate results are one result -----------------------------------


def test_identical_duplicate_results_are_one_generation(tmp_path):
    result = _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA")
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0)), result, copy.deepcopy(result),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.diagnostics == []
    assert [(g.run_id, g.result_line) for g in index.generations] == [(RUN_A, 2)]
    assert index.ambiguous_launches == []


def test_differing_duplicate_results_stay_ambiguous(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0)),
        _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA"),
        _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tB"),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.diagnostics == ["ambiguous launch 'toolu_1': lines 1, 2, 3"]


def test_a_differing_block_flag_makes_duplicate_results_differ(tmp_path):
    first = _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA")
    second = copy.deepcopy(first)
    second["message"]["content"][0]["is_error"] = False
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [_workflow_launch("toolu_1", _sec(0)), first, second])
    assert reader.ParentIndex._scan(parent).diagnostics == [
        "ambiguous launch 'toolu_1': lines 1, 2, 3"
    ]


def test_identical_duplicate_launches_stay_ambiguous(tmp_path):
    parent = tmp_path / "parent.jsonl"
    launch = _workflow_launch("toolu_1", _sec(0))
    _write_jsonl(parent, [
        launch, copy.deepcopy(launch), _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA"),
    ])
    assert reader.ParentIndex._scan(parent).diagnostics == [
        "ambiguous launch 'toolu_1': lines 1, 2, 3"
    ]


def test_identical_duplicate_workflow_results_reconcile_in_the_session_table(tmp_path):
    result = _workflow_result("toolu_1", RUN_C)
    main = _session(
        tmp_path, [_launch("toolu_1", "Workflow"), result, copy.deepcopy(result)],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == []


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_identical_duplicate_agent_results_reconcile_in_the_session_table(tmp_path, tool):
    child = _aid(5)
    result = _agent_result("toolu_1", entry=child)
    main = _session(
        tmp_path, [_launch("toolu_1", tool), result, copy.deepcopy(result)], children=[child]
    )
    assert _transcripts(main) == []


def test_differing_duplicate_agent_results_stay_ambiguous_result(tmp_path):
    child = _aid(5)
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Agent"), _agent_result("toolu_1", entry=child),
         _agent_result("toolu_1", entry=child, block=child)],
        children=[child],
    )
    assert _transcripts(main) == [_diag(2, "Agent", "ambiguous_result")]


def test_identical_duplicate_results_without_a_launch_are_harmless(tmp_path):
    result = _agent_result("toolu_9", entry=_aid(5))
    main = _session(tmp_path, [result, copy.deepcopy(result)])
    assert _transcripts(main) == []


# --- scope of the discovery list for a session, and the lines an ambiguous launch names ------


def test_a_session_run_gets_no_launch_diagnostic_in_its_parent_discovery(tmp_path):
    main = _session(tmp_path, [
        _launch("toolu_1", "Workflow"), _launch("toolu_2", "Workflow"),
        _workflow_result("toolu_2", RUN_C, ts="2026-09-01T12:00:03Z"),
        copy.deepcopy(_workflow_result("toolu_2", RUN_D)),
    ], workflow_dirs=[RUN_C])
    run = reader.load_session(main)
    assert run.discovery["parent"] == []
    assert _diag(2, "Workflow", "no_result") in run.discovery["transcripts"]


def test_an_ambiguous_launch_names_the_line_of_every_result_including_repeats(tmp_path):
    parent = tmp_path / "parent.jsonl"
    first = _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA")
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0)), first, copy.deepcopy(first),
        _workflow_launch_result("toolu_1", _sec(3), RUN_A, "tB"),
    ])
    assert reader.ParentIndex._scan(parent).diagnostics == [
        "ambiguous launch 'toolu_1': lines 1, 2, 3, 4"
    ]


def test_an_unreadable_parent_still_reaches_the_run_parent_discovery(tmp_path, monkeypatch):
    from pathlib import Path

    from tests.test_n2_lifecycle import _permission_denied_open_for

    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow(
        tmp_path, RUN_A, parent=_lines([_workflow_launch("toolu_1", _sec(0))]),
    )
    parent = tmp_path / "root" / "-home-user-project" / (
        "00000000-0000-4000-8000-000000000000.jsonl"
    )
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))
    run = reader.load_run(run_dir)
    assert run.discovery["parent"] == [f"{parent.name}: parent transcript unreadable"]


def test_a_session_whose_id_a_launch_resumes_still_gets_no_launch_diagnostic(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1", "Workflow")])
    session_id = reader.load_session(main).run_id
    launch = _launch("toolu_1", "Workflow")
    launch["message"]["content"][0]["input"] = {"resumeFromRunId": session_id}
    main = _session(tmp_path, [launch])
    run = reader.load_session(main)
    assert run.run_id == session_id
    assert run.discovery["parent"] == []


@pytest.mark.parametrize("flag", [1, "true", "True", [True]])
def test_only_a_true_is_error_makes_a_rejected_launch(tmp_path, flag):
    parent = tmp_path / "parent.jsonl"
    result = _rejected_result("toolu_1", _sec(1))
    result["message"]["content"][0]["is_error"] = flag
    _write_jsonl(parent, [_workflow_launch("toolu_1", _sec(0)), result])
    assert reader.ParentIndex._scan(parent).diagnostics == ["malformed launch result at line 2"]
