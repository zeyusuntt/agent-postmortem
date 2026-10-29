"""N4b-1 coverage group 8 (discovery): Run.discovery per source, and the
session launch reconciliation of docs/DESIGN.md 4.13.1 (C-14), every
reachable (tool, code) pair of the decision tables included
(.reviews/v0.3/N4-plan.md L13).

Fixtures are synthetic; every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_postmortem import reader
from agent_postmortem.models import Run
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _no_config,
    _parent_path,
    _user,
    _wf_dir,
)

SESSION = "00000000-0000-4000-8000-000000000000"
SLUG = "-tmp-proj"
T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
KEYS = ["journal", "parent", "transcripts", "meta"]
CLEAN = {"journal": [], "parent": [], "transcripts": [], "meta": []}
MISSING = object()


def _good_transcript(agent_id):
    return [_user("Do it.", T0, agent_id), _assistant_text("Done.", T0, agent_id)]


def _text(entries):
    return "".join(json.dumps(e) + "\n" for e in entries)


# --- a workflow run, with a defect injectable per source --------------------------------


def _workflow(tmp_path, wf_id, *, journal=None, transcript_prefix="", meta=None, parent=None):
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    agent_id = _aid(1)
    if journal is None:
        journal = _text([
            {"type": "started", "key": "v2:k", "agentId": agent_id, "label": "l", "phase": "P"},
            {"type": "result", "key": "v2:k", "agentId": agent_id, "result": {}},
        ])
    (run_dir / "journal.jsonl").write_text(journal, encoding="utf-8")
    (run_dir / f"agent-{agent_id}.jsonl").write_text(
        transcript_prefix + _text(_good_transcript(agent_id)), encoding="utf-8"
    )
    if meta is not None:
        (run_dir / f"agent-{agent_id}.meta.json").write_text(meta, encoding="utf-8")
    if parent is not None:
        path = _parent_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(parent, encoding="utf-8")
    return run_dir, agent_id


def test_a_clean_workflow_run_has_four_empty_sources(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow(tmp_path, "wf_00000000-c61")
    run = reader.load_run(run_dir)
    assert run.discovery == CLEAN
    assert list(run.discovery) == KEYS


def test_a_directly_built_run_has_a_clean_discovery():
    run = Run(
        run_id="wf_x", path=Path("."), project_slug="s", session_id=None, claude_version=None,
        journal=[], agents=[], attempts=[], started_at=None, ended_at=None, diagnostics=[],
    )
    assert run.discovery == CLEAN
    assert run.to_dict()["discovery"] == CLEAN


def test_each_run_gets_its_own_discovery_dict():
    def build():
        return Run(
            run_id="wf_x", path=Path("."), project_slug="s", session_id=None,
            claude_version=None, journal=[], agents=[], attempts=[], started_at=None,
            ended_at=None, diagnostics=[],
        )
    first, second = build(), build()
    first.discovery["journal"].append("x")
    assert second.discovery == CLEAN


def test_journal_source_missing_journal(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-c62")
    (run_dir / "journal.jsonl").unlink()
    run = reader.load_run(reader.RunRef(kind="workflow", path=run_dir, run_id=run_dir.name))
    assert run.discovery == {
        **CLEAN,
        "journal": ["journal.jsonl: missing"],
        "transcripts": [
            f"agent-{agent_id}.jsonl: transcript named by no journal started event"
        ],
    }


def test_journal_source_two_diagnostics_in_order(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    agent_id = _aid(1)
    journal = '[1]\n"x"\n' + _text([
        {"type": "started", "key": "v2:k", "agentId": agent_id, "label": "l", "phase": "P"},
        {"type": "result", "key": "v2:k", "agentId": agent_id, "result": {}},
    ])
    run_dir, _ = _workflow(tmp_path, "wf_00000000-c63", journal=journal)
    run = reader.load_run(run_dir)
    assert run.discovery == {
        **CLEAN,
        "journal": ["journal.jsonl:1: not a JSON object", "journal.jsonl:2: not a JSON object"],
    }


def test_journal_source_state_machine_diagnostic(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    agent_id = _aid(1)
    journal = _text([
        {"type": "result", "key": "v2:orphan", "agentId": agent_id, "result": {}},
        {"type": "started", "key": "v2:k", "agentId": agent_id, "label": "l", "phase": "P"},
        {"type": "result", "key": "v2:k", "agentId": agent_id, "result": {}},
    ])
    run_dir, _ = _workflow(tmp_path, "wf_00000000-c64", journal=journal)
    run = reader.load_run(run_dir)
    assert run.discovery == {
        **CLEAN,
        "journal": ["journal.jsonl: 'result' event for key@1 has no open attempt"],
    }


def test_journal_source_started_event_without_a_canonical_agent_id(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    journal = _text([
        {"type": "started", "key": "v2:k", "agentId": "short", "label": "l", "phase": "P"},
    ])
    run_dir, _ = _workflow(tmp_path, "wf_00000000-c65", journal=journal)
    run = reader.load_run(run_dir)
    assert run.discovery["journal"][0] == "journal.jsonl:1: started event has no canonical agentId"
    # the fixture's own agent-<aid>.jsonl is named by no started event (S1)
    assert run.discovery["transcripts"] == [
        f"agent-{_aid(1)}.jsonl: transcript named by no journal started event"
    ]
    assert run.discovery["meta"] == []


def test_parent_source(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow(tmp_path, "wf_00000000-c66", parent="not json\n")
    run = reader.load_run(run_dir)
    assert run.discovery == {
        **CLEAN, "parent": [f"{SESSION}.jsonl:1: undecodable parent transcript line"],
    }


def test_parent_source_two_diagnostics_in_order(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    parent = 'not json\n[1]\n{"type":"user","message":5}\n'
    run_dir, _ = _workflow(tmp_path, "wf_00000000-c66", parent=parent)
    run = reader.load_run(run_dir)
    assert run.discovery == {
        **CLEAN,
        "parent": [
            f"{SESSION}.jsonl:1: undecodable parent transcript line",
            # 0.3.4: a line that is not a JSON object is a diagnostic too (DESIGN 2)
            f"{SESSION}.jsonl:2: parent transcript line that is not a JSON object",
            f"{SESSION}.jsonl:3: malformed parent transcript entry",
            f"{SESSION}.jsonl:3: message is not an object (got int)",
        ],
    }


def test_meta_source_two_diagnostics_in_order(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    first, second = _aid(1), _aid(2)
    journal = _text([
        {"type": "started", "key": "v2:a", "agentId": first, "label": "l", "phase": "P"},
        {"type": "started", "key": "v2:b", "agentId": second, "label": "l", "phase": "P"},
    ])
    run_dir, _ = _workflow(tmp_path, "wf_00000000-c68", journal=journal, meta="[1]")
    (run_dir / f"agent-{second}.jsonl").write_text(_text(_good_transcript(second)), encoding="utf-8")
    (run_dir / f"agent-{second}.meta.json").write_text("[2]", encoding="utf-8")
    run = reader.load_run(run_dir)
    assert run.discovery == {
        **CLEAN,
        "meta": [
            f"agent-{first}.meta.json: not a JSON object",
            f"agent-{second}.meta.json: not a JSON object",
        ],
    }


def test_transcripts_source_two_diagnostics_in_order(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-c67", transcript_prefix="not json\n[1]\n")
    run = reader.load_run(run_dir)
    name = f"agent-{agent_id}.jsonl"
    assert run.discovery == {
        **CLEAN,
        "transcripts": [
            f"{name}:1: Expecting value: line 1 column 1 (char 0)",
            f"{name}:2: not a JSON object",
        ],
    }


def test_meta_source(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-c68", meta="[1]")
    run = reader.load_run(run_dir)
    assert run.discovery == {**CLEAN, "meta": [f"agent-{agent_id}.meta.json: not a JSON object"]}


def test_every_source_at_once_keeps_each_diagnostic_under_its_own_key(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    agent_id = _aid(1)
    journal = '[1]\n' + _text([
        {"type": "started", "key": "v2:k", "agentId": agent_id, "label": "l", "phase": "P"},
        {"type": "result", "key": "v2:k", "agentId": agent_id, "result": {}},
    ])
    run_dir, _ = _workflow(
        tmp_path, "wf_00000000-c69", journal=journal, transcript_prefix="not json\n",
        meta="[1]", parent="not json\n",
    )
    run = reader.load_run(run_dir)
    assert run.discovery == {
        "journal": ["journal.jsonl:1: not a JSON object"],
        "parent": [f"{SESSION}.jsonl:1: undecodable parent transcript line"],
        "transcripts": [f"agent-{agent_id}.jsonl:1: Expecting value: line 1 column 1 (char 0)"],
        "meta": [f"agent-{agent_id}.meta.json: not a JSON object"],
    }
    # the flat Run.diagnostics list keeps its v0.2 content and order
    assert run.diagnostics == (
        run.discovery["journal"] + run.discovery["meta"] + run.discovery["transcripts"]
        + run.discovery["parent"]
    )


def test_run_to_dict_carries_discovery(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-c6a", meta="[1]")
    run = reader.load_run(run_dir)
    assert run.to_dict()["discovery"] == {
        **CLEAN, "meta": [f"agent-{agent_id}.meta.json: not a JSON object"],
    }


# --- a session run ------------------------------------------------------------------------


def _entry_user(text="go"):
    return _user(text, T0)


def _launch(tool_id, name="Agent", *, line_blocks=()):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}},
                        *line_blocks],
            "usage": {},
        },
        "timestamp": "2026-09-01T12:00:01Z",
    }


def _agent_result(tool_id, *, entry=MISSING, block=MISSING):
    result_block = {"type": "tool_result", "tool_use_id": tool_id, "content": "done"}
    if block is not MISSING:
        result_block["agentId"] = block
    out = {
        "type": "user",
        "message": {"role": "user", "content": [result_block]},
        "timestamp": "2026-09-01T12:00:02Z",
    }
    if entry is not MISSING:
        out["toolUseResult"] = {"agentId": entry, "status": "completed"}
    return out


def _workflow_result(tool_id, run_id, ts="2026-09-01T12:00:02Z"):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "launched"}],
        },
        "toolUseResult": {"runId": run_id, "taskId": "tA", "status": "async_launched"},
        "timestamp": ts,
    }


def _session(tmp_path, entries, children=(), workflow_dirs=()):
    root = tmp_path / "root"
    main = root / SLUG / f"{SESSION}.jsonl"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(_text([_entry_user(), *entries]), encoding="utf-8")
    for child in children:
        path = root / SLUG / SESSION / "subagents" / f"agent-{child}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_text(_good_transcript(child)), encoding="utf-8")
    for wf in workflow_dirs:
        directory = root / SLUG / SESSION / "subagents" / "workflows" / wf
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "journal.jsonl").write_text("", encoding="utf-8")
    return main


def _diag(line, tool, code):
    return f"{SESSION}.jsonl:{line}: {tool} launch has no discovered child ({code})"


def _transcripts(main):
    return reader.load_session(main).discovery["transcripts"]


def _duplicate_id_diagnostic():
    """The reader's own v0.2 diagnostic for the duplicate launch id of lines 2 and 3 (4.2);
    it is a `transcripts` diagnostic that precedes the reconciliation's."""
    return f"{SESSION}.jsonl:3: duplicate tool_use_id toolu_1 at lines 2 and 3"


def test_a_session_run_reports_each_source(tmp_path):
    main = _session(tmp_path, [])
    child = _aid(3)
    root = tmp_path / "root"
    child_path = root / SLUG / SESSION / "subagents" / f"agent-{child}.jsonl"
    child_path.parent.mkdir(parents=True, exist_ok=True)
    child_path.write_text("not json\n" + _text(_good_transcript(child)), encoding="utf-8")
    child_path.with_name(f"agent-{child}.meta.json").write_text("[1]", encoding="utf-8")
    main.write_text("not json\n" + main.read_text(encoding="utf-8"), encoding="utf-8")
    run = reader.load_session(main)
    assert run.discovery == {
        "journal": [],
        "parent": [f"{SESSION}.jsonl:1: undecodable parent transcript line"],
        "transcripts": [
            f"{SESSION}.jsonl:1: Expecting value: line 1 column 1 (char 0)",
            f"agent-{child}.jsonl:1: Expecting value: line 1 column 1 (char 0)",
        ],
        "meta": [f"agent-{child}.meta.json: not a JSON object"],
    }


def test_a_malformed_subagent_file_name_is_a_transcripts_diagnostic(tmp_path):
    main = _session(tmp_path, [])
    path = tmp_path / "root" / SLUG / SESSION / "subagents" / "agent-x.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_text(_good_transcript("x")), encoding="utf-8")
    replacement = "agent-" + hashlib.sha256(b"x").hexdigest()[:8]
    assert reader.load_session(main).discovery == {
        **CLEAN,
        "transcripts": [
            f"session subagent: agent id did not match the expected format, "
            f"replaced with {replacement}"
        ],
    }


def test_a_session_with_no_launch_is_clean(tmp_path):
    assert reader.load_session(_session(tmp_path, [])).discovery == CLEAN


# --- the launch reconciliation: clean rows -------------------------------------------------


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("how", ["entry", "block", "both"])
def test_an_agent_or_task_launch_reconciles_through_either_location(tmp_path, tool, how):
    child = _aid(4)
    kwargs = {}
    if how in ("entry", "both"):
        kwargs["entry"] = child
    if how in ("block", "both"):
        kwargs["block"] = child
    main = _session(
        tmp_path, [_launch("toolu_1", tool), _agent_result("toolu_1", **kwargs)], children=[child]
    )
    assert _transcripts(main) == []


def test_a_workflow_launch_reconciles_through_a_discovered_generation(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Workflow"), _workflow_result("toolu_1", "wf_00000000-c71")],
        workflow_dirs=["wf_00000000-c71"],
    )
    assert _transcripts(main) == []


def test_a_tool_use_shaped_block_in_a_user_entry_is_no_launch(tmp_path):
    user_entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_use", "id": "toolu_9", "name": "Agent", "input": {}}],
        },
        "timestamp": "2026-09-01T12:00:03Z",
    }
    assert _transcripts(_session(tmp_path, [user_entry])) == []


def test_send_message_and_other_tools_are_not_launches(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1", "SendMessage"), _launch("toolu_2", "Bash")])
    assert _transcripts(main) == []


# --- the launch reconciliation: one row per reachable (tool, code) pair -----------------------


def _agent_case(code, tool):
    """(entries, children, line of the launch) for one Agent/Task code."""
    ghost = _aid(9)  # names no subagent transcript file
    if code == "malformed_launch":
        block = _launch("ignored", tool)
        block["message"]["content"][0]["id"] = 5
        return [block], [], 2
    if code == "ambiguous_tool_id":
        return [_launch("toolu_1", tool), _launch("toolu_1", tool), _agent_result("toolu_1")], [], 2
    if code == "no_result":
        return [_launch("toolu_1", tool)], [], 2
    if code == "ambiguous_result":
        return [
            _launch("toolu_1", tool),
            _agent_result("toolu_1", entry=ghost), _agent_result("toolu_1", entry=_aid(8)),
        ], [], 2
    if code == "non_string_id":
        return [_launch("toolu_1", tool), _agent_result("toolu_1", entry=7)], [], 2
    if code == "conflicting_ids":
        return [
            _launch("toolu_1", tool), _agent_result("toolu_1", entry=_aid(8), block=ghost),
        ], [], 2
    if code == "no_id":
        return [_launch("toolu_1", tool), _agent_result("toolu_1")], [], 2
    if code == "undiscovered":
        return [_launch("toolu_1", tool), _agent_result("toolu_1", entry=ghost)], [], 2
    raise AssertionError(code)


AGENT_CODES = [
    "malformed_launch", "ambiguous_tool_id", "no_result", "ambiguous_result", "non_string_id",
    "conflicting_ids", "no_id", "undiscovered",
]


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("code", AGENT_CODES)
def test_every_agent_and_task_code_alone(tmp_path, tool, code):
    entries, children, line = _agent_case(code, tool)
    got = _transcripts(_session(tmp_path, entries, children=children))
    expected = [_diag(line, tool, code)]
    if code == "ambiguous_tool_id":
        expected = [_duplicate_id_diagnostic(), _diag(2, tool, code), _diag(3, tool, code)]
    assert got == expected


def test_an_agent_launch_diagnostic_makes_no_other_source_dirty(tmp_path):
    entries, children, _ = _agent_case("no_id", "Agent")
    run = reader.load_session(_session(tmp_path, entries, children=children))
    assert run.discovery["journal"] == []
    assert run.discovery["parent"] == []
    assert run.discovery["meta"] == []
    assert run.status == "COMPLETE"
    assert run.diagnostics == []


def _workflow_case(code):
    if code == "malformed_launch":
        block = _launch("ignored", "Workflow")
        block["message"]["content"][0]["id"] = None
        return [block], []
    if code == "ambiguous_tool_id":
        return [
            _launch("toolu_1", "Workflow"), _launch("toolu_1", "Workflow"),
            _workflow_result("toolu_1", "wf_00000000-c72"),
        ], ["wf_00000000-c72"]
    if code == "no_result":
        return [_launch("toolu_1", "Workflow")], []
    if code == "ambiguous_result":
        return [
            _launch("toolu_1", "Workflow"),
            _workflow_result("toolu_1", "wf_00000000-c72"),
            _workflow_result("toolu_1", "wf_00000000-c73"),
        ], ["wf_00000000-c72"]
    if code == "undiscovered":
        return [
            _launch("toolu_1", "Workflow"), _workflow_result("toolu_1", "wf_00000000-c73"),
        ], []
    raise AssertionError(code)


@pytest.mark.parametrize(
    "code", ["malformed_launch", "ambiguous_tool_id", "no_result", "ambiguous_result", "undiscovered"]
)
def test_every_workflow_code_alone(tmp_path, code):
    entries, dirs = _workflow_case(code)
    got = _transcripts(_session(tmp_path, entries, workflow_dirs=dirs))
    expected = [_diag(2, "Workflow", code)]
    if code == "ambiguous_tool_id":
        expected = [
            _duplicate_id_diagnostic(), _diag(2, "Workflow", code), _diag(3, "Workflow", code),
        ]
    assert got == expected


def test_a_workflow_launch_with_no_generation_is_undiscovered(tmp_path):
    bad = _workflow_result("toolu_1", "wf_00000000-c71")
    del bad["toolUseResult"]["taskId"]
    main = _session(
        tmp_path, [_launch("toolu_1", "Workflow"), bad], workflow_dirs=["wf_00000000-c71"]
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_a_workflow_run_id_is_never_a_path_outside_the_workflows_directory(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Workflow"), _workflow_result("toolu_1", "../../../..")],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_a_workflow_result_without_an_agent_id_is_not_no_id(tmp_path):
    # a Workflow result with no agentId anywhere is not `no_id`: it reconciles through a Generation
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Workflow"), _workflow_result("toolu_1", "wf_00000000-c71")],
        workflow_dirs=["wf_00000000-c71"],
    )
    assert _transcripts(main) == []


# --- mixed-defect witnesses (the first row that applies wins) ------------------------------------


def test_a_non_string_launch_id_with_no_matched_result_is_malformed(tmp_path):
    block = _launch("ignored", "Agent")
    block["message"]["content"][0]["id"] = 5
    assert _transcripts(_session(tmp_path, [block])) == [_diag(2, "Agent", "malformed_launch")]


def test_a_duplicate_launch_id_with_no_result_is_ambiguous_not_no_result(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1", "Agent"), _launch("toolu_1", "Agent")])
    assert _transcripts(main) == [
        _duplicate_id_diagnostic(),
        _diag(2, "Agent", "ambiguous_tool_id"), _diag(3, "Agent", "ambiguous_tool_id"),
    ]


@pytest.mark.parametrize("tool", ["Agent", "Task", "Workflow"])
def test_a_duplicate_launch_id_with_two_results_is_ambiguous_tool_id(tmp_path, tool):
    if tool == "Workflow":
        results = [
            _workflow_result("toolu_1", "wf_00000000-c71"),
            _workflow_result("toolu_1", "wf_00000000-c71"),
        ]
    else:
        results = [_agent_result("toolu_1"), _agent_result("toolu_1")]
    main = _session(
        tmp_path, [_launch("toolu_1", tool), _launch("toolu_1", tool), *results],
        workflow_dirs=["wf_00000000-c71"],
    )
    assert _transcripts(main) == [
        _duplicate_id_diagnostic(),
        _diag(2, tool, "ambiguous_tool_id"), _diag(3, tool, "ambiguous_tool_id"),
    ]


def test_two_results_of_which_one_is_valid_is_ambiguous_result(tmp_path):
    child = _aid(5)
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Agent"), _agent_result("toolu_1", entry=child),
         _agent_result("toolu_1")],
        children=[child],
    )
    assert _transcripts(main) == [_diag(2, "Agent", "ambiguous_result")]


def test_a_non_string_location_beside_a_valid_id_naming_no_file_is_non_string_id(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Agent"), _agent_result("toolu_1", entry=7, block=_aid(9))],
    )
    assert _transcripts(main) == [_diag(2, "Agent", "non_string_id")]


def test_two_different_ids_neither_naming_a_file_is_conflicting_ids(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Agent"), _agent_result("toolu_1", entry=_aid(8), block=_aid(9))],
    )
    assert _transcripts(main) == [_diag(2, "Agent", "conflicting_ids")]


def test_a_result_with_neither_location_is_no_id_not_undiscovered(tmp_path):
    for tool in ("Agent", "Task"):
        main = _session(tmp_path / tool, [_launch("toolu_1", tool), _agent_result("toolu_1")])
        assert _transcripts(main) == [_diag(2, tool, "no_id")]


def test_diagnostics_follow_tool_use_order_across_tools(tmp_path):
    child = _aid(6)
    main = _session(
        tmp_path,
        [
            _launch("toolu_1", "Task"),
            _launch("toolu_2", "Agent"), _agent_result("toolu_2", entry=child),
            _launch("toolu_3", "Workflow"),
            _agent_result("toolu_1"),
        ],
        children=[child],
    )
    assert _transcripts(main) == [
        _diag(2, "Task", "no_id"),
        _diag(5, "Workflow", "no_result"),
    ]


def test_a_launch_inside_a_multi_block_assistant_entry_is_reconciled_by_its_own_id(tmp_path):
    child = _aid(7)
    first = _launch("toolu_1", "Agent", line_blocks=[
        {"type": "tool_use", "id": "toolu_2", "name": "Task", "input": {}},
    ])
    main = _session(
        tmp_path, [first, _agent_result("toolu_1", entry=child)], children=[child]
    )
    assert _transcripts(main) == [_diag(2, "Task", "no_result")]


def test_a_named_but_missing_child_makes_discovery_not_clean_and_nothing_else(tmp_path):
    child = _aid(2)
    main = _session(
        tmp_path, [_launch("toolu_1", "Agent"), _agent_result("toolu_1", entry=child)],
        children=[child],
    )
    (tmp_path / "root" / SLUG / SESSION / "subagents" / f"agent-{child}.jsonl").unlink()
    run = reader.load_session(main)
    assert run.discovery["transcripts"] == [_diag(2, "Agent", "undiscovered")]
    assert run.attempts == []
    assert run.status == "COMPLETE"


def test_a_view_of_named_subagents_reconciles_nothing(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1", "Agent")])
    assert reader.load_session_view(main, []).discovery == CLEAN


def test_a_workflow_launch_reconciles_only_through_its_own_generation(tmp_path):
    main = _session(
        tmp_path,
        [
            _launch("toolu_1", "Workflow"), _workflow_result("toolu_1", "wf_00000000-c71"),
            _launch("toolu_2", "Workflow"), _workflow_result("toolu_2", "wf_00000000-c73"),
        ],
        workflow_dirs=["wf_00000000-c71"],
    )
    assert _transcripts(main) == [_diag(4, "Workflow", "undiscovered")]


def test_diagnostics_are_in_tool_use_order_not_text_order(tmp_path):
    filler = [_assistant_text("x", T0) for _ in range(8)]
    main = _session(
        tmp_path, [_launch("toolu_1", "Agent"), *filler, _launch("toolu_2", "Task")]
    )
    assert _transcripts(main) == [_diag(2, "Agent", "no_result"), _diag(11, "Task", "no_result")]
