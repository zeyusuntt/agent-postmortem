"""Fix D (Codex round 2: A2 and A4 corrections), Amendment A5: a bad timestamp makes an
attempt unassignable (R2-1); a malformed result's own runId involves that run (R2-2); a
rejected launch is not proof that no run exists (R2-3); a result's digest does not depend
on the physical order of launch and result (R2-4). docs/DESIGN.md 4.13.1 and 4.13.3.

Fixtures are synthetic; every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import copy
import json

import pytest

from agent_postmortem import reader
from tests.n4b2_helpers import ledger
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
from tests.test_n4fixc_a4 import (
    RUN_A,
    RUN_B,
    RUN_C,
    RUN_D,
    _lines,
    _rejected_result,
    _sec,
)

FIX = "Fix D (Codex round 2: A2 and A4 corrections) RED: "


def _load_with_parent(tmp_path, monkeypatch, entries, run_id=RUN_A):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow(tmp_path, run_id, parent=_lines(entries))
    return reader.load_run(run_dir)


def _population_ids(run):
    return [attempt.agent_id for attempt in ledger.risk_population(run)]


# --- R2-1: an unparseable timestamp at or before the line that set first_ts ---------------


def _write_call(tool_id, ts):
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "id": tool_id, "name": "Write",
                "input": {"file_path": "/tmp/ws/risky", "content": "x"},
            }],
            "usage": {},
        },
        "cwd": "/tmp/ws",
    }
    if ts is not _OMIT:
        entry["timestamp"] = ts
    return entry


def _write_result(tool_id, ts):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
        },
        "timestamp": ts,
    }


_OMIT = object()
_GOOD = "2026-09-01T12:00:10Z"


def _ts_run(tmp_path, monkeypatch, transcript):
    """A completed workflow attempt whose transcript is `transcript`, in a run whose parent
    has one completed generation with its result at 12:00:01Z (Codex's input)."""
    _no_config(monkeypatch, tmp_path)
    parent = _lines([
        _workflow_launch("toolu_1", _sec(0)),
        _workflow_launch_result("toolu_1", _sec(1), RUN_A, "tA", status="completed"),
    ])
    run_dir, agent_id = _workflow(tmp_path, RUN_A, parent=parent)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", transcript)
    return reader.load_run(run_dir), agent_id


def _unassignable(run, agent_id):
    assert _population_ids(run) == [agent_id]
    assert run.risk_population_completeness == "partial"
    assert run.risk_population_completeness_reasons == ["unassignable_attempt"]


@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-45T99:99:99Z", 12345, ""])
def test_r2_1_a_bad_timestamp_before_first_ts_makes_the_attempt_unassignable(
    tmp_path, monkeypatch, bad
):
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", bad), _write_result("toolu_w", _GOOD),
    ])
    assert run.attempts[0].ledger.entries[0].effect == "observed"
    _unassignable(run, agent_id)


def test_r2_1_a_bad_timestamp_between_valid_lines_before_the_first_ts_line(
    tmp_path, monkeypatch
):
    # first_ts is the minimum event timestamp: line 3 (12:00:05). Line 2 is a bad
    # timestamp at an earlier line than the line that set it.
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", "2026-09-01T12:00:09Z"),
        _write_result("toolu_w", "not-a-time"),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "done"}], "usage": {}}, "timestamp": "2026-09-01T12:00:05Z"},
    ])
    _unassignable(run, agent_id)


def test_r2_1_a_bad_timestamp_after_the_first_ts_line_does_not_matter(tmp_path, monkeypatch):
    run, _ = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _GOOD), _write_result("toolu_w", "not-a-time"),
    ])
    assert _population_ids(run) == []
    assert run.risk_population_completeness == "complete"
    assert run.risk_population_completeness_reasons == []


def test_r2_1_an_absent_timestamp_before_first_ts_is_unplaceable_too(tmp_path, monkeypatch):
    # Fix E (R3-1): an omitted timestamp before the first valid one has the same placement
    # ambiguity as a malformed one, so the attempt is unassignable.
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _OMIT), _write_result("toolu_w", _GOOD),
    ])
    _unassignable(run, agent_id)


def test_r2_1_no_valid_timestamp_at_all_is_unassignable(tmp_path, monkeypatch):
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", "not-a-time"), _write_result("toolu_w", "not-a-time"),
    ])
    _unassignable(run, agent_id)


def test_r2_1_the_timestamp_notes_stay_out_of_discovery(tmp_path, monkeypatch):
    run, _ = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", "not-a-time"), _write_result("toolu_w", _GOOD),
    ])
    assert run.discovery == {"journal": [], "parent": [], "transcripts": [], "meta": []}
    assert any("timestamp could not be parsed" in text for text in run.diagnostics)


# --- R2-2: a malformed result's own runId involves that run -----------------------------


def _malformed_naming(tool_id, ts, run_id):
    entry = _workflow_launch_result(tool_id, ts, run_id, "tA")
    del entry["toolUseResult"]["taskId"]
    return entry


def _valid_generation():
    return [
        _workflow_launch("toolu_1", _sec(-10)),
        _workflow_launch_result("toolu_1", _sec(-9), RUN_A, "tA", status="completed"),
    ]


def test_r2_2_a_malformed_result_naming_the_run_reaches_it(tmp_path, monkeypatch):
    entries = [
        *_valid_generation(),
        _workflow_launch("toolu_2", _sec(0)),
        _malformed_naming("toolu_2", _sec(1), RUN_A),
    ]
    run = _load_with_parent(tmp_path, monkeypatch, entries)
    assert run.discovery["parent"] == ["malformed launch result at line 4"]
    assert run.risk_population_completeness == "partial"
    assert run.risk_population_completeness_reasons == ["discovery_incomplete"]


def test_r2_2_a_malformed_result_without_a_status_names_the_run_too(tmp_path, monkeypatch):
    bad = _workflow_launch_result("toolu_2", _sec(1), RUN_A, "tA")
    del bad["toolUseResult"]["status"]
    entries = [*_valid_generation(), _workflow_launch("toolu_2", _sec(0)), bad]
    assert _load_with_parent(tmp_path, monkeypatch, entries).discovery["parent"] == [
        "malformed launch result at line 4"
    ]


def test_r2_2_a_malformed_result_naming_another_run_does_not_reach_this_one(
    tmp_path, monkeypatch
):
    entries = [
        *_valid_generation(),
        _workflow_launch("toolu_2", _sec(0)),
        _malformed_naming("toolu_2", _sec(1), RUN_B),
    ]
    assert _load_with_parent(tmp_path, monkeypatch, entries).discovery["parent"] == []


@pytest.mark.parametrize("value", [12345, None, ["wf"], {"a": 1}])
def test_r2_2_a_non_string_run_id_names_no_run(tmp_path, monkeypatch, value):
    bad = _malformed_naming("toolu_2", _sec(1), RUN_A)
    bad["toolUseResult"]["runId"] = value
    entries = [*_valid_generation(), _workflow_launch("toolu_2", _sec(0)), bad]
    assert _load_with_parent(tmp_path, monkeypatch, entries).discovery["parent"] == []


def test_r2_2_a_resume_of_one_run_with_a_result_naming_another_involves_both(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0), resume_from=RUN_A),
        _malformed_naming("toolu_1", _sec(1), RUN_B),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.discovery_diagnostics_for(RUN_A) == ["malformed launch result at line 2"]
    assert index.discovery_diagnostics_for(RUN_B) == ["malformed launch result at line 2"]
    assert index.discovery_diagnostics_for(RUN_C) == []
    assert [g.run_id for g in index.generations] == [RUN_A]  # only the resume's own


def test_r2_2_a_malformed_result_naming_a_run_builds_no_generation(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0)), _malformed_naming("toolu_1", _sec(1), RUN_A),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.generations == []
    assert index.discovery_diagnostics_for(RUN_A) == ["malformed launch result at line 2"]
    assert index.discovery_diagnostics_for(RUN_B) == []


def test_r2_2_a_result_less_initial_launch_still_reaches_no_run(tmp_path, monkeypatch):
    entries = [*_valid_generation(), _workflow_launch("toolu_2", _sec(0))]
    assert _load_with_parent(tmp_path, monkeypatch, entries).discovery["parent"] == []


# --- R2-3: a rejected launch is not proof that no run exists -----------------------------


def _resume_launch(tool_id, resume):
    launch = _launch(tool_id, "Workflow")
    launch["message"]["content"][0]["input"] = {"resumeFromRunId": resume}
    return launch


def test_r2_3_a_rejected_resume_launch_reaches_the_run_it_names(tmp_path, monkeypatch):
    entries = [
        _workflow_launch("toolu_1", _sec(0), resume_from=RUN_A), _rejected_result("toolu_1", _sec(1)),
    ]
    run = _load_with_parent(tmp_path, monkeypatch, entries)
    assert run.discovery["parent"] == ["rejected launch result at line 2"]
    assert run.risk_population_completeness == "partial"


def test_r2_3_a_rejected_resume_launch_in_the_parent_index(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_1", _sec(0), resume_from=RUN_A), _rejected_result("toolu_1", _sec(1)),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.generations == []
    assert index.discovery_diagnostics_for(RUN_A) == ["rejected launch result at line 2"]
    assert index.discovery_diagnostics_for(RUN_B) == []


def test_r2_3_a_rejected_initial_launch_involves_no_run(tmp_path, monkeypatch):
    entries = [_workflow_launch("toolu_1", _sec(0)), _rejected_result("toolu_1", _sec(1))]
    assert _load_with_parent(tmp_path, monkeypatch, entries).discovery["parent"] == []


def test_r2_3_a_rejected_resume_launch_of_another_run_does_not_reach_this_one(
    tmp_path, monkeypatch
):
    entries = [
        _workflow_launch("toolu_1", _sec(0), resume_from=RUN_B), _rejected_result("toolu_1", _sec(1)),
    ]
    assert _load_with_parent(tmp_path, monkeypatch, entries).discovery["parent"] == []


def test_r2_3_a_rejected_resume_launch_whose_directory_exists_reconciles(tmp_path):
    main = _session(
        tmp_path, [_resume_launch("toolu_1", RUN_C), _rejected_result("toolu_1", _sec(2))],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == []


def test_r2_3_a_rejected_resume_launch_with_no_directory_is_undiscovered(tmp_path):
    main = _session(
        tmp_path, [_resume_launch("toolu_1", RUN_C), _rejected_result("toolu_1", _sec(2))],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r2_3_a_rejected_resume_launch_naming_a_missing_directory_beside_another(tmp_path):
    main = _session(
        tmp_path, [_resume_launch("toolu_1", RUN_C), _rejected_result("toolu_1", _sec(2))],
        workflow_dirs=[RUN_D],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


@pytest.mark.parametrize("name", ["../x", "wf_00000000-c7", "not-a-run", ""])
def test_r2_3_a_rejected_resume_launch_naming_a_non_run_id_is_undiscovered(tmp_path, name):
    main = _session(
        tmp_path, [_resume_launch("toolu_1", name), _rejected_result("toolu_1", _sec(2))],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r2_3_a_rejected_initial_launch_beside_an_orphan_directory_is_undiscovered(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2))],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r2_3_a_rejected_initial_launch_stays_undiscovered_when_every_directory_is_associated(
    tmp_path,
):
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2)),
         _launch("toolu_2", "Workflow"),
         _workflow_result("toolu_2", RUN_C, ts="2026-09-01T12:00:03Z")],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r2_3_one_orphan_directory_among_associated_ones_is_undiscovered(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2)),
         _launch("toolu_2", "Workflow"),
         _workflow_result("toolu_2", RUN_C, ts="2026-09-01T12:00:03Z")],
        workflow_dirs=[RUN_C, RUN_D],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r2_3_a_rejected_initial_launch_with_no_directory_at_all_is_undiscovered(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2))])
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


# --- R2-4: the result digest does not depend on physical order ---------------------------


def test_r2_4_identical_results_before_the_launch_are_one_result_in_the_parent_index(tmp_path):
    result = _workflow_launch_result("toolu_3", _sec(1), RUN_A, "tA")
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [result, copy.deepcopy(result), _workflow_launch("toolu_3", _sec(0))])
    index = reader.ParentIndex._scan(parent)
    assert index.diagnostics == []
    assert [(g.run_id, g.result_line) for g in index.generations] == [(RUN_A, 1)]
    assert index.ambiguous_launches == []


def test_r2_4_identical_results_on_both_sides_of_the_launch_are_one_result(tmp_path):
    result = _workflow_launch_result("toolu_3", _sec(1), RUN_A, "tA")
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [result, _workflow_launch("toolu_3", _sec(0)), copy.deepcopy(result)])
    index = reader.ParentIndex._scan(parent)
    assert index.diagnostics == []
    assert [(g.run_id, g.result_line) for g in index.generations] == [(RUN_A, 1)]


def test_r2_4_differing_results_before_the_launch_stay_ambiguous(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch_result("toolu_3", _sec(1), RUN_A, "tA"),
        _workflow_launch_result("toolu_3", _sec(1), RUN_A, "tB"),
        _workflow_launch("toolu_3", _sec(0)),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.diagnostics == ["ambiguous launch 'toolu_3': lines 1, 2, 3"]


def test_r2_4_identical_workflow_results_before_the_launch_reconcile_in_the_session(tmp_path):
    result = _workflow_result("toolu_1", RUN_C)
    main = _session(
        tmp_path, [result, copy.deepcopy(result), _launch("toolu_1", "Workflow")],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == []


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_r2_4_identical_agent_results_before_the_launch_reconcile_in_the_session(
    tmp_path, tool
):
    child = _aid(5)
    result = _agent_result("toolu_1", entry=child)
    main = _session(
        tmp_path, [result, copy.deepcopy(result), _launch("toolu_1", tool)], children=[child]
    )
    assert _transcripts(main) == []


def test_r2_4_differing_agent_results_before_the_launch_stay_ambiguous(tmp_path):
    child = _aid(5)
    main = _session(
        tmp_path,
        [_agent_result("toolu_1", entry=child), _agent_result("toolu_1", entry=child, block=child),
         _launch("toolu_1", "Agent")],
        children=[child],
    )
    assert _transcripts(main) == [_diag(4, "Agent", "ambiguous_result")]


# --- close variants pinned by the Fix D mutation proof -----------------------------------


def test_r2_1_the_first_bad_timestamp_line_counts_not_the_last(tmp_path, monkeypatch):
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", "not-a-time"),
        _write_result("toolu_w", _GOOD),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "done"}], "usage": {}}, "timestamp": "not-a-time"},
    ])
    _unassignable(run, agent_id)


def test_r2_1_the_earliest_line_carrying_first_ts_is_the_line_that_set_it(
    tmp_path, monkeypatch
):
    # Lines 1 and 3 both carry first_ts; the bad timestamp of line 2 comes after the
    # earliest of them, so it does not matter.
    run, _ = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _GOOD), _write_result("toolu_w", "not-a-time"),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "done"}], "usage": {}}, "timestamp": _GOOD},
    ])
    assert _population_ids(run) == []
    assert run.risk_population_completeness == "complete"


def test_r2_3_an_unlistable_workflows_directory_leaves_a_rejected_initial_launch_open(
    tmp_path, monkeypatch
):
    from pathlib import Path

    main = _session(
        tmp_path, [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2))],
        workflow_dirs=[RUN_C],
    )
    real = Path.iterdir

    def fake(self):
        if self.name == "workflows":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", fake)
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r2_3_a_stray_file_in_the_workflows_directory_is_not_a_run_directory(tmp_path):
    main = _session(
        tmp_path, [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2))],
        workflow_dirs=[RUN_C, RUN_D],
    )
    workflows = main.parent / main.stem / "subagents" / "workflows"
    (workflows / RUN_D / "journal.jsonl").unlink()
    (workflows / RUN_D).rmdir()
    (workflows / RUN_D).write_text("a file, not a run directory", encoding="utf-8")
    # RUN_C is still an orphan directory; with it associated, the stray file does not matter.
    main.write_text(
        main.read_text(encoding="utf-8")
        + json.dumps(_launch("toolu_2", "Workflow")) + "\n"
        + json.dumps(_workflow_result("toolu_2", RUN_C, ts="2026-09-01T12:00:03Z")) + "\n",
        encoding="utf-8",
    )
    # Fix E (R3-2): the initial rejected launch is undiscovered whatever the directories hold.
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]
