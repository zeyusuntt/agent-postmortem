"""N4b-1 coverage group 8 (reader facts): the private ToolCall fields
(run_in_background, a Monitor command, a MultiEdit path, the N4a
classification), Attempt.journal_state, and Generation.result_ts
(docs/DESIGN.md 4.1, 4.13.1, C-14; .reviews/v0.3/N4-plan.md L13, L15).

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import commands, reader
from agent_postmortem.models import Attempt, Generation
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _envelope,
    _kill,
    _no_config,
    _notification_entry,
    _parent_path,
    _simple_completed_journal,
    _user,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)

CWD = "/tmp/ws"


# --- builders -----------------------------------------------------------------------


def _use(tool_id, name, input_obj, cwd=CWD):
    return {
        "type": "assistant",
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}],
            "usage": {},
        },
    }


def _result(tool_id, text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _calls(tmp_path, entries):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return reader._read_transcript(path, [])[5]


def _one(tmp_path, name, input_obj, result_text=None, cwd=CWD):
    entries = [_use("t1", name, input_obj, cwd)]
    if result_text is not None:
        entries.append(_result("t1", result_text))
    (call,) = _calls(tmp_path, entries)
    return call


# --- ToolCall.run_in_background ---------------------------------------------------


@pytest.mark.parametrize("tool", ["Bash", "Monitor"])
@pytest.mark.parametrize(
    "input_extra,expected",
    [
        ({"run_in_background": True}, True),
        ({"run_in_background": False}, False),
        ({}, None),
        ({"run_in_background": "true"}, "true"),
        ({"run_in_background": 1}, 1),
        ({"run_in_background": None}, None),
    ],
)
def test_run_in_background_is_the_inputs_own_value(tmp_path, tool, input_extra, expected):
    call = _one(tmp_path, tool, {"command": "ls", **input_extra})
    assert call.run_in_background == expected
    assert type(call.run_in_background) is type(expected)


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "Read", "Agent"])
def test_run_in_background_is_none_for_every_other_tool(tmp_path, tool):
    call = _one(tmp_path, tool, {"file_path": "/tmp/f", "run_in_background": True})
    assert call.run_in_background is None


# --- Monitor's command ------------------------------------------------------------


def test_a_monitor_call_keeps_its_command(tmp_path):
    call = _one(tmp_path, "Monitor", {"command": "tail -f x.log"})
    assert call.command == "tail -f x.log"
    assert call.command_sha256 is None


def test_a_long_monitor_command_is_hashed_not_truncated(tmp_path):
    long_command = "echo " + "a" * 3000
    call = _one(tmp_path, "Monitor", {"command": long_command})
    assert call.command is None
    assert call.command_sha256 is not None and len(call.command_sha256) == 64


def test_a_monitor_call_with_a_non_string_command_has_none(tmp_path):
    call = _one(tmp_path, "Monitor", {"command": 5})
    assert (call.command, call.command_sha256) == (None, None)
    assert call.classification.row == 6  # an opaque command, never a crash


def test_other_tools_keep_no_command(tmp_path):
    for tool in ("Edit", "Agent", "Read"):
        call = _one(tmp_path, tool, {"command": "ls"})
        assert call.command is None
        assert call.run_in_background is None


# --- MultiEdit's path -------------------------------------------------------------


def test_multiedit_file_path_is_kept_apart_from_path(tmp_path):
    call = _one(tmp_path, "MultiEdit", {"file_path": "/tmp/ws/a.py", "edits": []}, "ok")
    assert call.multiedit_path == "/tmp/ws/a.py"
    assert call.path is None


def test_multiedit_path_boundaries(tmp_path):
    ok = "/" + "a" * 1023
    assert len(ok) == 1024
    assert _one(tmp_path, "MultiEdit", {"file_path": ok}).multiedit_path == ok
    assert _one(tmp_path, "MultiEdit", {"file_path": ok + "b"}).multiedit_path is None
    assert _one(tmp_path, "MultiEdit", {"file_path": 7}).multiedit_path is None
    assert _one(tmp_path, "MultiEdit", {}).multiedit_path is None


def test_other_tools_have_no_multiedit_path(tmp_path):
    for tool in ("Edit", "Write", "NotebookEdit", "Bash"):
        call = _one(tmp_path, tool, {"file_path": "/tmp/ws/a.py"})
        assert call.multiedit_path is None


def test_multiedit_never_reaches_the_schema_2_facts(tmp_path):
    calls = _calls(tmp_path, [
        _use("t1", "MultiEdit", {"file_path": "/tmp/ws/a.py"}),
        _result("t1", "ok"),
        _use("t2", "Edit", {"file_path": "/tmp/ws/b.py"}),
        _result("t2", "ok"),
        _use("t3", "MultiEdit", {"file_path": "/tmp/ws/c.py"}),
    ])
    assert [c.multiedit_path for c in calls] == ["/tmp/ws/a.py", None, "/tmp/ws/c.py"]
    facts = reader._build_attempt_facts(calls, "killed", None, Path("t.jsonl"), False)
    assert facts.paths_written == ["/tmp/ws/b.py"]
    assert facts.in_flight == [{"tool": "MultiEdit", "path": None, "line_no": 5}]


# --- the N4a classification, computed from the FULL command while streaming -----------


def test_the_classification_equals_the_classifier_on_the_full_command(tmp_path):
    cases = [
        ("Bash", {"command": "cat f"}),
        ("Bash", {"command": "make"}),
        ("Bash", {"command": "echo x > out"}),
        ("Bash", {"command": "git commit -m x"}),
        ("Bash", {"command": "cd /abs && git -C sub commit -m x"}),
        ("Bash", {"command": "codex exec x"}),
        ("Bash", {"command": "nohup make"}),
        ("Bash", {"command": "make &"}),
        ("Bash", {"command": "cat f", "run_in_background": True}),
        ("Bash", {"command": "echo $X"}),
        ("Bash", {"command": None}),
        ("Monitor", {"command": "tail -f x.log"}),
        ("Monitor", {"command": "touch f"}),
        ("Monitor", {"command": "codex exec x"}),
        ("Monitor", {"command": "git commit -m x"}),
        ("Agent", {"description": "d"}),
        ("Task", {"description": "d"}),
        ("Workflow", {}),
        ("SendMessage", {"to": "x"}),
    ]
    for name, input_obj in cases:
        call = _one(tmp_path, name, input_obj)
        expected = commands.classify_call(
            name, input_obj.get("command"),
            run_in_background=input_obj.get("run_in_background", False), cwd=CWD,
        )
        assert call.classification == expected, (name, input_obj)
        assert call.classification is not None, (name, input_obj)


def test_the_classification_rows_by_tool(tmp_path):
    assert _one(tmp_path, "Bash", {"command": "cat f"}).classification.row == 4
    assert _one(tmp_path, "Bash", {"command": "make"}).classification.row == 6
    assert _one(tmp_path, "Bash", {"command": "echo x > out"}).classification.row == 5
    assert _one(tmp_path, "Bash", {"command": "make &"}).classification.row == 2
    assert _one(tmp_path, "Bash", {"command": "codex exec x"}).classification.row == 3
    assert _one(tmp_path, "Monitor", {"command": "tail -f x.log"}).classification.row == 4
    assert _one(tmp_path, "Monitor", {"command": "touch f"}).classification.row == 6
    for tool in ("Agent", "Task", "Workflow", "SendMessage"):
        assert _one(tmp_path, tool, {}).classification.row == 3


@pytest.mark.parametrize(
    "tool", ["Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep", "mcp__x__y", "bash"]
)
def test_the_classification_is_none_outside_rows_2_to_6(tmp_path, tool):
    assert _one(tmp_path, tool, {"command": "rm -rf x", "file_path": "/tmp/f"}).classification is None


def test_the_classification_uses_the_input_run_in_background(tmp_path):
    call = _one(tmp_path, "Bash", {"command": "cat f", "run_in_background": True})
    assert call.classification.row == 2
    assert _one(tmp_path, "Bash", {"command": "cat f", "run_in_background": "true"}
                ).classification.row == 4


def test_the_classification_reads_the_full_command_not_the_stored_copy(tmp_path):
    pad = "a" * 3000
    call = _one(tmp_path, "Bash", {"command": f"echo {pad} && git commit -m x"})
    assert call.command is None and call.command_sha256 is not None
    assert call.classification.commit.is_commit
    background = _one(tmp_path, "Bash", {"command": f"echo {pad} &"})
    assert background.command is None
    assert background.classification.row == 2
    monitor = _one(tmp_path, "Monitor", {"command": f"echo {pad}"})
    assert monitor.command is None
    assert monitor.classification.row == 6 or monitor.classification.row == 4


def test_the_classification_carries_the_commit_segments_and_the_repo(tmp_path):
    call = _one(tmp_path, "Bash", {"command": "cd /abs && git commit -m x"})
    assert [s.repo for s in call.classification.commit.segments] == ["/abs"]
    assert call.classification.repo == "/abs"
    other = _one(tmp_path, "Bash", {"command": "git commit -m x"}, cwd="/tmp/other")
    assert [s.repo for s in other.classification.commit.segments] == ["/tmp/other"]


ANCHORED = "Command did not complete within its 120s timeout and was moved to the background"


def test_an_anchored_bash_backgrounded_hit_makes_the_call_background(tmp_path):
    call = _one(tmp_path, "Bash", {"command": "cat f"}, ANCHORED + "\nmore")
    assert call.classification.row == 2
    assert call.classification.basis == "background_process"


def test_a_payload_only_backgrounded_marker_does_not(tmp_path):
    call = _one(tmp_path, "Bash", {"command": "cat f"}, "first line\n" + ANCHORED)
    assert call.classification.row == 4


def test_a_monitor_result_never_makes_a_monitor_call_background(tmp_path):
    call = _one(tmp_path, "Monitor", {"command": "tail -f x"}, ANCHORED)
    assert call.classification.row == 4


def test_the_commit_ids_still_come_from_the_classification_segments(tmp_path):
    call = _one(tmp_path, "Bash", {"command": "git commit -m x"}, "[main abc1234] x")
    assert call.commit_ids == ("abc1234",)
    assert [s.repo for s in call.classification.commit.segments] == [CWD]
    monitor = _one(tmp_path, "Monitor", {"command": "git commit -m x"}, "[main abc1234] x")
    assert monitor.commit_ids == ()


# --- ToolCall.to_dict(): exact private form ------------------------------------------


def test_tool_call_to_dict_is_the_complete_private_form(tmp_path):
    call = _one(
        tmp_path, "Bash", {"command": "git commit -m x", "run_in_background": False},
        "[main abc1234] x",
    )
    assert call.to_dict() == {
        "tool_use_id": "t1",
        "tool_name": "Bash",
        "line_no": 1,
        "ts": None,
        "path": None,
        "command": "git commit -m x",
        "command_sha256": None,
        "cwd": CWD,
        "result_line": 2,
        "is_error": False,
        "backgrounded": False,
        "exit_code": None,
        "commit_ids": ["abc1234"],
        "ambiguous": False,
        "completion": "completed_ok",
        "run_in_background": False,
        "multiedit_path": None,
        "classification": {
            "row": 6,
            "effect": "possible",
            "basis": "opaque_command",
            "targets": [],
            "commit": [{"repo": CWD}],
            "repo": CWD,
        },
    }


def test_tool_call_to_dict_for_a_multiedit_and_a_tiny_write(tmp_path):
    multi = _one(tmp_path, "MultiEdit", {"file_path": "/tmp/ws/a.py"}, "ok")
    as_dict = multi.to_dict()
    assert as_dict["multiedit_path"] == "/tmp/ws/a.py"
    assert as_dict["classification"] is None
    assert as_dict["run_in_background"] is None
    tiny = _one(tmp_path, "Bash", {"command": "echo x > out"}, "")
    assert tiny.to_dict()["classification"] == {
        "row": 5,
        "effect": "claimed_target",
        "basis": "command_grammar",
        "targets": [
            {"path": "/tmp/ws/out", "repo": "/tmp/ws", "kind": "redirect", "truncated": False}
        ],
        "commit": [],
        "repo": CWD,
    }


# --- Attempt.journal_state ----------------------------------------------------------


def _wf_run(tmp_path, wf_id, journal, transcripts):
    """A workflow run directory; transcripts maps agent id -> entries (or None for none)."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(run_dir / "journal.jsonl", journal)
    base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    for agent_id, kind in transcripts.items():
        entries = [_user("Do it.", base, agent_id), _assistant_text("Done.", base, agent_id)]
        if kind == "killed":
            entries.append(_kill(base + timedelta(seconds=5), agent_id))
        _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", entries)
    return root, run_dir


def _started(key, agent_id):
    entry = {"type": "started", "agentId": agent_id, "label": "impl", "phase": "P"}
    if key is not None:
        entry["key"] = key
    return entry


def _states(run):
    return [(a.agent_id, a.outcome, a.journal_state) for a in run.attempts]


def test_journal_state_completed(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a = _aid(1)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c41", [
        _started("v2:k", a), {"type": "result", "key": "v2:k", "agentId": a, "result": {}},
    ], {a: "ok"})
    assert _states(reader.load_run(run_dir)) == [(a, "completed", "completed")]


def test_journal_state_failed(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a = _aid(2)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c42", [
        _started("v2:k", a), {"type": "failed", "key": "v2:k", "agentId": a},
    ], {a: "ok"})
    assert _states(reader.load_run(run_dir)) == [(a, "failed", "failed")]


def test_journal_state_open(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a = _aid(3)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c43", [_started("v2:k", a)], {a: "ok"})
    assert _states(reader.load_run(run_dir)) == [(a, "completed", "open")]


def test_journal_state_open_with_a_killed_transcript(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a = _aid(4)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c44", [_started("v2:k", a)], {a: "killed"})
    assert _states(reader.load_run(run_dir)) == [(a, "killed", "open")]


def test_journal_state_superseded_and_the_retry_that_completed(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a, b = _aid(5), _aid(6)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c45", [
        _started("v2:k", a), _started("v2:k", b),
        {"type": "result", "key": "v2:k", "agentId": b, "result": {}},
    ], {a: "ok", b: "ok"})
    assert _states(reader.load_run(run_dir)) == [
        (a, "superseded", "superseded"),
        (b, "completed", "completed"),
    ]


def test_journal_state_stays_superseded_when_the_outcome_is_killed(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a, b = _aid(7), _aid(8)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c46", [
        _started("v2:k", a), _started("v2:k", b),
    ], {a: "killed", b: "ok"})
    assert _states(reader.load_run(run_dir)) == [
        (a, "killed", "superseded"),
        (b, "completed", "open"),
    ]


def test_journal_state_is_none_for_a_workflow_attempt_with_no_key(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a = _aid(9)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c47", [_started(None, a)], {a: "ok"})
    assert _states(reader.load_run(run_dir)) == [(a, "completed", None)]


def test_journal_state_is_none_for_a_session_main_thread_and_subagent(tmp_path):
    root = tmp_path / "root"
    session = "00000000-0000-4000-8000-000000000000"
    main = root / "-tmp-proj" / f"{session}.jsonl"
    base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_jsonl(main, [_user("hi", base), _assistant_text("ok", base)])
    child = root / "-tmp-proj" / session / "subagents" / f"agent-{_aid(10)}.jsonl"
    _write_jsonl(child, [_user("c", base, _aid(10)), _assistant_text("d", base, _aid(10))])
    run = reader.load_session(main)
    assert run.kind == "session"
    assert run.main.journal_state is None
    assert [a.journal_state for a in run.attempts] == [None]


def test_attempt_to_dict_includes_journal_state(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    a = _aid(11)
    _root, run_dir = _wf_run(tmp_path, "wf_00000000-c48", [_started("v2:k", a)], {a: "ok"})
    run = reader.load_run(run_dir)
    assert run.attempts[0].to_dict()["journal_state"] == "open"


def test_a_directly_built_attempt_defaults_journal_state_to_none():
    attempt = Attempt(
        agent_id="a", key=None, label="l", phase="", agent_type=None, meta={},
        transcript_path=None, events=[], first_ts=None, last_ts=None, ordinal=1,
        outcome="unknown", usage={},
    )
    assert attempt.journal_state is None
    assert attempt.to_dict()["journal_state"] is None


# --- Generation.result_ts ------------------------------------------------------------

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _launch_result_with_ts(tool_id, ts, run_id, task_id):
    entry = _workflow_launch_result(tool_id, T0, run_id, task_id)
    entry["timestamp"] = ts
    return entry


def _index(tmp_path, entries):
    path = tmp_path / "parent.jsonl"
    _write_jsonl(path, entries)
    return reader.ParentIndex._scan(path), path


def test_result_ts_is_the_result_lines_parsed_timestamp(tmp_path):
    index, _ = _index(tmp_path, [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=7), "wf_00000000-c51", "tA"),
    ])
    (generation,) = index.generations
    assert generation.result_ts == T0 + timedelta(seconds=7)
    assert generation.result_ts.tzinfo is not None


def test_result_ts_is_not_the_launch_lines_timestamp(tmp_path):
    index, _ = _index(tmp_path, [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(minutes=3), "wf_00000000-c51", "tA"),
    ])
    (generation,) = index.generations
    assert generation.result_ts != T0
    assert generation.result_ts == T0 + timedelta(minutes=3)


def test_result_ts_is_none_when_the_result_line_has_no_timestamp(tmp_path):
    result = _workflow_launch_result("toolu_A", T0, "wf_00000000-c51", "tA")
    del result["timestamp"]
    index, _ = _index(tmp_path, [_workflow_launch("toolu_A", T0), result])
    (generation,) = index.generations
    assert generation.result_line == 2
    assert generation.result_ts is None


@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-45T99:99:99Z", 12345, "", None, ["x"]])
def test_result_ts_is_none_when_the_timestamp_does_not_parse(tmp_path, bad):
    result = _workflow_launch_result("toolu_A", T0, "wf_00000000-c51", "tA")
    result["timestamp"] = bad
    index, _ = _index(tmp_path, [_workflow_launch("toolu_A", T0), result])
    (generation,) = index.generations
    assert generation.result_ts is None


def test_result_ts_for_a_result_less_resume_is_none(tmp_path):
    index, _ = _index(tmp_path, [_workflow_launch("toolu_A", T0, resume_from="wf_00000000-c52")])
    (generation,) = index.generations
    assert generation.result_line is None
    assert generation.result_ts is None


def test_result_ts_for_a_malformed_result_keeps_the_lines_timestamp(tmp_path):
    bad = _workflow_launch_result("toolu_A", T0 + timedelta(seconds=9), "wf_00000000-c52", "tA")
    del bad["toolUseResult"]["runId"]
    index, _ = _index(tmp_path, [
        _workflow_launch("toolu_A", T0, resume_from="wf_00000000-c52"), bad,
    ])
    (generation,) = index.generations
    assert generation.terminal_source == "incomplete_generation"
    assert generation.result_line == 2
    assert generation.result_ts == T0 + timedelta(seconds=9)


def test_each_generation_has_its_own_result_ts(tmp_path):
    index, _ = _index(tmp_path, [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=1), "wf_00000000-c53", "tA"),
        _workflow_launch("toolu_B", T0 + timedelta(minutes=5), resume_from="wf_00000000-c53"),
        _workflow_launch_result("toolu_B", T0 + timedelta(minutes=6), "wf_00000000-c53", "tB"),
    ])
    assert [g.result_ts for g in index.generations] == [
        T0 + timedelta(seconds=1), T0 + timedelta(minutes=6),
    ]


def test_result_ts_comes_from_the_snapshot_not_a_second_parent_read(tmp_path):
    index, path = _index(tmp_path, [
        _workflow_launch("toolu_A", T0),
        _launch_result_with_ts("toolu_A", "2026-09-01T12:00:07Z", "wf_00000000-c51", "tA"),
    ])
    assert index.generations[0].result_ts == T0 + timedelta(seconds=7)
    # the parent is refreshed on disk after the snapshot (same size, new timestamp)
    text = path.read_text(encoding="utf-8").replace("12:00:07Z", "12:09:59Z")
    path.write_text(text, encoding="utf-8")
    index._rebuild_generations()
    assert index.generations[0].result_ts == T0 + timedelta(seconds=7)


def test_the_lifecycle_snapshot_holds_the_generations_it_was_built_from(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    wf_id = "wf_00000000-c54"
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    a = _aid(12)
    _simple_completed_journal(run_dir, a)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    _write_jsonl(run_dir / f"agent-{a}.jsonl", [_user("go", base, a), _assistant_text("ok", base, a)])
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
    ])
    run = reader.load_run(run_dir)
    (snapshot,) = run.lifecycle._generations
    assert snapshot.result_ts == (base + timedelta(seconds=1)).replace(microsecond=0)
    # a later refresh of the shared parent index adds a generation; the snapshot is unchanged
    with parent.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_workflow_launch("toolu_B", base + timedelta(minutes=1))) + "\n")
        handle.write(json.dumps(
            _workflow_launch_result("toolu_B", base + timedelta(minutes=2), wf_id, "tB")
        ) + "\n")
    refreshed = reader.ParentIndex.for_path(parent)
    assert len(refreshed.generations_for(wf_id)) == 2
    assert len(run.lifecycle._generations) == 1
    assert "_generations" not in run.lifecycle.to_dict()


def test_generation_to_dict_is_exact_with_result_ts(tmp_path):
    index, _ = _index(tmp_path, [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=7), "wf_00000000-c51", "tA"),
        _notification_entry(T0 + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
    ])
    (generation,) = index.generations
    assert generation.to_dict() == {
        "launch_line": 1,
        "tool_use_id": "toolu_A",
        "resume_from": None,
        "result_line": 2,
        "run_id": "wf_00000000-c51",
        "task_id": "tA",
        "launch_status": "async_launched",
        "terminal": "completed",
        "terminal_source": "notification",
        "terminal_line": 3,
        "diagnostics": [],
        "result_ts": "2026-09-01T12:00:07+00:00",
        "launch_ts": "2026-09-01T12:00:00+00:00",
    }


def test_generation_to_dict_result_ts_null(tmp_path):
    index, _ = _index(tmp_path, [_workflow_launch("toolu_A", T0, resume_from="wf_00000000-c52")])
    assert index.generations[0].to_dict()["result_ts"] is None


def test_a_directly_built_generation_defaults_result_ts_to_none():
    generation = Generation(
        launch_line=1, tool_use_id="t", resume_from=None, result_line=None, run_id="wf_x",
        task_id=None, launch_status=None, terminal=None, terminal_source=None,
        terminal_line=None, diagnostics=[],
    )
    assert generation.result_ts is None


def _lifecycle_run(tmp_path, monkeypatch, wf_id, journal_done, parent_entries, agent_n):
    """A workflow run whose parent holds `parent_entries` (callables of the run id)."""
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(agent_n)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    if journal_done:
        _simple_completed_journal(run_dir, agent_id)
    else:
        _write_jsonl(run_dir / "journal.jsonl", [_started("v2:x", agent_id)])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("go", base, agent_id), _assistant_text("ok", base, agent_id),
    ])
    parent = _parent_path(root)
    if parent_entries:
        _write_jsonl(parent, [entry(wf_id, base) for entry in parent_entries])
    return reader.load_run(run_dir)


def _launch_entries(tool_id):
    return [
        lambda wf, base: _workflow_launch(tool_id, base),
        lambda wf, base: _workflow_launch_result(tool_id, base + timedelta(seconds=1), wf, "tA"),
    ]


@pytest.mark.parametrize(
    "terminal,journal_done,extra",
    [
        ("completed", False, "completed"),  # a notification decides
        ("journal_resolved", True, None),
        ("none", False, None),
    ],
)
def test_every_lifecycle_branch_keeps_its_snapshot_generations(
    tmp_path, monkeypatch, terminal, journal_done, extra
):
    entries = _launch_entries("toolu_A")
    if extra is not None:
        entries.append(
            lambda wf, base: _notification_entry(
                base + timedelta(minutes=1), _envelope("tA", "toolu_A", extra)
            )
        )
    run = _lifecycle_run(tmp_path, monkeypatch, "wf_00000000-c49", journal_done, entries, 13)
    assert run.lifecycle.terminal == terminal
    assert [g.tool_use_id for g in run.lifecycle._generations] == ["toolu_A"]


def test_the_ambiguous_launch_branch_keeps_its_snapshot_generations(tmp_path, monkeypatch):
    entries = _launch_entries("toolu_A") + [
        lambda wf, base: _workflow_launch("toolu_B", base + timedelta(seconds=5)),
        lambda wf, base: _workflow_launch("toolu_B", base + timedelta(seconds=6)),
        lambda wf, base: _workflow_launch_result("toolu_B", base + timedelta(seconds=7), wf, "tB"),
    ]
    run = _lifecycle_run(tmp_path, monkeypatch, "wf_00000000-c49", True, entries, 14)
    assert run.lifecycle.source == "ambiguous_launch"
    assert [g.tool_use_id for g in run.lifecycle._generations] == ["toolu_A"]


def test_the_parent_unavailable_branch_keeps_its_snapshot_generations(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    wf_id = "wf_00000000-c49"
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(15)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [_user("go", base, agent_id)])
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
    ])
    with parent.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    run = reader.load_run(run_dir)
    assert run.lifecycle.source == "parent_unavailable"
    assert [g.tool_use_id for g in run.lifecycle._generations] == ["toolu_A"]
