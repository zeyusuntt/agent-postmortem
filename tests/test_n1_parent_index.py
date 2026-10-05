"""N1 acceptance tests for the parent index's launch/result association,
notifications, and incremental cache (docs/DESIGN.md 4.13.3).

Every test here works directly against a standalone parent transcript file
(never a full run directory) via reader.ParentIndex._scan/for_path, since
association and generation-building are parent-transcript-only concerns.
Builders are imported from tests.test_field_v03 rather than copied
(CLAUDE.md). Every wf_ id literal here is registered in
tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import reader
from tests.test_field_v03 import (
    _assistant_tool,
    _envelope,
    _kill,
    _notification_entry,
    _queue_notification_entry,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)
from tests.test_n2_lifecycle import _permission_denied_open_for, _raise_once_then_call

BASE = datetime.now(timezone.utc) - timedelta(minutes=10)
RUN_X = "wf_00000000-ba1"
RUN_Y = "wf_00000000-ba2"


def _gen(index, tool_use_id: str):
    hits = [g for g in index.generations if g.tool_use_id == tool_use_id]
    assert len(hits) == 1, (tool_use_id, index.generations)
    return hits[0]


# --- launches and results, association (4.13.3's N1 acceptance list) -----


def test_single_launch_complete_generation_all_eleven_fields(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])
    index = reader.ParentIndex._scan(parent)
    assert len(index.generations) == 1
    gen = index.generations[0]
    assert gen.launch_line == 1
    assert gen.tool_use_id == "toolu_A"
    assert gen.resume_from is None
    assert gen.result_line == 2
    assert gen.run_id == RUN_X
    assert gen.task_id == "tA"
    assert gen.launch_status == "async_launched"
    assert gen.terminal is None
    assert gen.terminal_source is None
    assert gen.terminal_line is None
    assert gen.diagnostics == []


def test_resume_generation_carries_resume_from(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _workflow_launch("toolu_B", BASE + timedelta(minutes=1), resume_from=RUN_X),
        _workflow_launch_result("toolu_B", BASE + timedelta(minutes=1, seconds=1), RUN_X, "tB"),
    ])
    index = reader.ParentIndex._scan(parent)
    assert len(index.generations) == 2
    gen_b = _gen(index, "toolu_B")
    assert gen_b.resume_from == RUN_X
    assert gen_b.run_id == RUN_X
    assert gen_b.launch_line == 3
    assert gen_b.result_line == 4


def test_result_less_resume_is_incomplete_generation(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_B", BASE, resume_from=RUN_X),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_B")
    assert gen.launch_line == 1
    assert gen.resume_from == RUN_X
    assert gen.result_line is None
    assert gen.run_id == RUN_X
    assert gen.task_id is None
    assert gen.launch_status is None
    assert gen.terminal == "unknown"
    assert gen.terminal_source == "incomplete_generation"
    assert gen.terminal_line is None
    assert gen.diagnostics == ["launch without result at line 1"]


def test_malformed_result_of_resume_is_incomplete_generation(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_B", BASE, resume_from=RUN_X),
        {
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_B", "content": "launched"},
            ]},
            # malformed: no taskId, no status.
            "toolUseResult": {"runId": RUN_X},
            "timestamp": (BASE + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_B")
    assert gen.result_line == 2
    assert gen.run_id == RUN_X
    assert gen.task_id is None
    assert gen.terminal == "unknown"
    assert gen.terminal_source == "incomplete_generation"
    assert gen.diagnostics == ["malformed launch result at line 2"]


def test_result_less_initial_launch_yields_no_generation_but_a_diagnostic(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [_workflow_launch("toolu_A", BASE)])
    index = reader.ParentIndex._scan(parent)
    assert index.generations == []
    assert "launch without result at line 1" in index.diagnostics


def test_malformed_result_with_no_resume_yields_no_generation_but_a_diagnostic(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        {
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_A", "content": "launched"},
            ]},
            "toolUseResult": "not-a-dict",
            "timestamp": (BASE + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.generations == []
    assert "malformed launch result at line 2" in index.diagnostics


def test_result_names_another_run_id_is_still_a_generation_of_that_run(tmp_path):
    """docs/DESIGN.md 4.13.3's association rules 2/3 collapse globally: a
    well-formed result is always a complete generation of whatever run its
    own runId names -- never merely "not X's" with no generation at all."""
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_Y, "tA"),
    ])
    index = reader.ParentIndex._scan(parent)
    assert [g.run_id for g in index.generations if g.run_id == RUN_X] == []
    matches = [g for g in index.generations if g.run_id == RUN_Y]
    assert len(matches) == 1


def test_duplicate_launch_ids_yield_zero_generations_and_an_exact_diagnostic(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch("toolu_A", BASE + timedelta(seconds=1)),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=2), RUN_X, "tA"),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.generations == []
    assert index.diagnostics == ["ambiguous launch 'toolu_A': lines 1, 2, 3"]


def test_duplicate_results_yield_zero_generations_and_an_exact_diagnostic(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=2), RUN_X, "tA2"),
    ])
    index = reader.ParentIndex._scan(parent)
    assert index.generations == []
    assert index.diagnostics == ["ambiguous launch 'toolu_A': lines 1, 2, 3"]


def test_unrecognized_launch_status_diagnostic_matches_the_contract_literal_string(tmp_path):
    """docs/DESIGN.md 4.13.3's Launch status paragraph gives the diagnostic
    for this case as the literal quoted string "unrecognized launch status",
    with no line number -- unlike the "... at line N" diagnostics the same
    section gives for a malformed or missing result. The line itself is not
    lost: it is still carried on terminal_line."""
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result(
            "toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA", status="weird_status",
        ),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "unknown"
    assert gen.terminal_source == "unrecognized_status"
    assert gen.terminal_line == 2
    assert gen.diagnostics == ["unrecognized launch status"]


# --- status vocabulary (4.13.3, N1N2 review finding 6) ----------------------
#
# docs/DESIGN.md 4.13.3's Status vocabulary paragraph: for notifications AND
# synchronous launch results alike, 'completed' -> 'completed', 'failed' ->
# 'failed', 'killed'/'stopped'/'cancelled' -> 'stopped'. Every scenario above
# that reaches a 'completed'/'failed'/'stopped' terminal does so through an
# async launch plus a separate notification; none of them ever sets a
# synchronous (non-async_launched) status directly on the launch result
# itself, so mutation M20 (omit synchronous launch-result terminal evidence
# entirely) and mutation M18 (map 'killed' to 'failed' instead of 'stopped')
# went uncaught. These two parametrized tests exercise the full vocabulary
# on both sources and assert every Generation field.


@pytest.mark.parametrize(
    ("raw_status", "mapped"),
    [
        ("completed", "completed"),
        ("failed", "failed"),
        ("killed", "stopped"),
        ("stopped", "stopped"),
        ("cancelled", "stopped"),
    ],
)
def test_synchronous_launch_result_status_vocabulary(tmp_path, raw_status, mapped):
    """A synchronous launch result (status anything but 'async_launched') is
    terminal evidence on its own, source 'launch_result' (4.13.3's Launch
    status paragraph) -- no notification needed at all."""
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result(
            "toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA", status=raw_status,
        ),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.launch_line == 1
    assert gen.tool_use_id == "toolu_A"
    assert gen.resume_from is None
    assert gen.result_line == 2
    assert gen.run_id == RUN_X
    assert gen.task_id == "tA"
    assert gen.launch_status == raw_status
    assert gen.terminal == mapped
    assert gen.terminal_source == "launch_result"
    assert gen.terminal_line == 2
    assert gen.diagnostics == []


@pytest.mark.parametrize(
    ("raw_status", "mapped"),
    [
        ("completed", "completed"),
        ("failed", "failed"),
        ("killed", "stopped"),
        ("stopped", "stopped"),
        ("cancelled", "stopped"),
    ],
)
def test_notification_status_vocabulary(tmp_path, raw_status, mapped):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", raw_status)),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.launch_line == 1
    assert gen.tool_use_id == "toolu_A"
    assert gen.resume_from is None
    assert gen.result_line == 2
    assert gen.run_id == RUN_X
    assert gen.task_id == "tA"
    assert gen.launch_status == "async_launched"
    assert gen.terminal == mapped
    assert gen.terminal_source == "notification"
    assert gen.terminal_line == 3
    assert gen.diagnostics == []


# --- notifications (4.13.3) ------------------------------------------------


def test_notification_user_form_accepted(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "completed"
    assert gen.terminal_source == "notification"
    assert gen.terminal_line == 3


def test_notification_both_forms_deduplicate_to_the_earlier_line(tmp_path):
    envelope_text = _envelope("tA", "toolu_A", "completed")
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _queue_notification_entry(BASE + timedelta(minutes=1), envelope_text),
        _notification_entry(BASE + timedelta(minutes=1, seconds=5), envelope_text),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "completed"
    assert gen.terminal_source == "notification"
    assert gen.terminal_line == 3  # the earlier (queue) line, not the later duplicate


def test_notification_mismatched_task_id_is_ignored(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _notification_entry(
            BASE + timedelta(minutes=1), _envelope("t-other", "toolu_A", "completed"),
        ),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal is None
    assert gen.terminal_source is None


def test_notification_mismatched_tool_use_id_is_ignored(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _notification_entry(
            BASE + timedelta(minutes=1), _envelope("tA", "toolu-other", "completed"),
        ),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal is None
    assert gen.terminal_source is None


def test_notification_user_without_origin_before_first_origin_line_counts(tmp_path):
    """docs/DESIGN.md 4.13.3: a user entry without `origin` counts only when
    its own physical line number is lower than the first line in the
    transcript that carries origin."""
    envelope_text = _envelope("tA", "toolu_A", "completed")
    origin_bearing = _notification_entry(
        BASE + timedelta(minutes=2), _envelope("unrelated", "toolu_z", "failed"),
    )
    no_origin_notification = {
        "type": "user",
        "message": {"role": "user", "content": envelope_text},
        "timestamp": (BASE + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        no_origin_notification,  # line 3, before any origin-bearing line
        origin_bearing,  # line 4, the first line carrying 'origin'
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "completed"
    assert gen.terminal_source == "notification"
    assert gen.terminal_line == 3


def test_notification_user_without_origin_after_first_origin_line_is_ignored(tmp_path):
    envelope_text = _envelope("tA", "toolu_A", "completed")
    origin_bearing = _notification_entry(
        BASE + timedelta(minutes=1), _envelope("unrelated", "toolu_z", "failed"),
    )
    no_origin_notification = {
        "type": "user",
        "message": {"role": "user", "content": envelope_text},
        "timestamp": (BASE + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        origin_bearing,  # line 3, the first line carrying 'origin'
        no_origin_notification,  # line 4, after it -- must not count
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal is None
    assert gen.terminal_source is None


def test_task_stop_then_later_notification_resolves_to_the_notification(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _assistant_tool("stop1", "TaskStop", {"task_id": "tA"}, BASE + timedelta(minutes=1)),
        _notification_entry(BASE + timedelta(minutes=2), _envelope("tA", "toolu_A", "completed")),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "completed"
    assert gen.terminal_source == "notification"
    assert gen.terminal_line == 4


# --- TaskStop task_id / shell_id matching (4.13.3, N1N2 review finding 6) ---
#
# "Terminal evidence per generation": a TaskStop tool_use block whose input
# task_id OR shell_id equals the generation's own task_id counts. Every
# TaskStop fixture above (and the existing attribute_interrupt tests in
# tests/test_attribution.py, which build _TaskStop directly) only ever sets
# task_id, so mutation M19 (stop matching by shell_id) went uncaught here.
# A TaskStop alone (no later notification) is itself the generation's only
# terminal evidence -- the single-value case of _combine_terminal_evidence --
# so these two tests pin shell_id matching without needing a notification.


def test_task_stop_alone_matches_generation_by_task_id(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _assistant_tool("stop1", "TaskStop", {"task_id": "tA"}, BASE + timedelta(minutes=1)),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "stopped"
    assert gen.terminal_source == "task_stop"
    assert gen.terminal_line == 3
    assert gen.diagnostics == []


def test_task_stop_alone_matches_generation_by_shell_id(tmp_path):
    """4.13.3: 'a TaskStop tool_use block whose input task_id (or shell_id)
    equals the generation's task_id' -- shell_id alone (no task_id at all in
    the TaskStop's own input) must match just as well."""
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _assistant_tool("stop1", "TaskStop", {"shell_id": "tA"}, BASE + timedelta(minutes=1)),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "stopped"
    assert gen.terminal_source == "task_stop"
    assert gen.terminal_line == 3
    assert gen.diagnostics == []


def test_two_conflicting_notifications_mark_unknown_conflict(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
        _notification_entry(BASE + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed")),
    ])
    index = reader.ParentIndex._scan(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "unknown"
    assert gen.terminal_source == "conflict"
    assert gen.terminal_line == 4
    assert gen.diagnostics == ["conflicting terminal evidence at line 4"]


# --- incremental cache (docs/DESIGN.md 4.13.3) -----------------------------


def test_counters_start_at_zero_on_a_fresh_index(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [_workflow_launch("toolu_A", BASE)])
    index = reader.ParentIndex(parent, [], [], [])
    assert index.lines_parsed == 0
    assert index.rebuilds == 0


def test_lines_parsed_counts_a_complete_blank_line(tmp_path):
    parent = tmp_path / "parent.jsonl"
    # One complete blank line, then one complete valid JSON line: both are
    # complete physical lines (each ends in its own newline), so the
    # contract's "one increment per complete line parsed" counts both.
    parent.write_text("\n" + json.dumps(_workflow_launch("toolu_A", BASE)) + "\n", encoding="utf-8")
    index = reader.ParentIndex._scan(parent)
    assert index.lines_parsed == 2


def test_cache_suffix_only_append_parses_only_the_new_lines(tmp_path):
    parent = tmp_path / f"parent-{id(tmp_path)}.jsonl"
    _write_jsonl(parent, [_workflow_launch("toolu_A", BASE)])
    index = reader.ParentIndex.for_path(parent)
    assert index.lines_parsed == 1
    assert index.rebuilds == 0

    with parent.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(_workflow_launch_result(
                "toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA",
            )) + "\n"
        )
    index2 = reader.ParentIndex.for_path(parent)
    assert index2 is index
    assert index2.lines_parsed == 2
    assert index2.rebuilds == 0
    assert len(index2.generations) == 1


def test_cache_partial_final_line_not_parsed_until_its_newline_arrives(tmp_path):
    parent = tmp_path / "parent.jsonl"
    line1 = json.dumps(_workflow_launch("toolu_A", BASE))
    parent.write_text(line1 + "\n", encoding="utf-8")
    index = reader.ParentIndex.for_path(parent)
    assert index.lines_parsed == 1

    line2 = json.dumps(
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA")
    )
    with parent.open("a", encoding="utf-8") as handle:
        handle.write(line2)  # no trailing newline yet
    index2 = reader.ParentIndex.for_path(parent)
    assert index2.lines_parsed == 1
    assert index2.generations == []

    with parent.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    index3 = reader.ParentIndex.for_path(parent)
    assert index3.lines_parsed == 2
    assert len(index3.generations) == 1


def test_cache_truncation_rebuilds(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])
    index = reader.ParentIndex.for_path(parent)
    assert len(index.generations) == 1

    parent.write_text("", encoding="utf-8")
    index2 = reader.ParentIndex.for_path(parent)
    assert index2 is index
    assert index2.generations == []
    assert index2.rebuilds == 1


def test_cache_replacement_new_inode_rebuilds(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])
    index = reader.ParentIndex.for_path(parent)
    assert len(index.generations) == 1

    replacement = tmp_path / "parent.jsonl.new"
    _write_jsonl(replacement, [
        _workflow_launch("toolu_B", BASE),
        _workflow_launch_result("toolu_B", BASE + timedelta(seconds=1), RUN_Y, "tB"),
    ])
    replacement.replace(parent)  # same path, new inode
    index2 = reader.ParentIndex.for_path(parent)
    assert index2 is index
    assert index2.rebuilds == 1
    assert [g.tool_use_id for g in index2.generations] == ["toolu_B"]


def test_cache_rewrite_inside_prefix_then_append_rebuilds(tmp_path):
    """A rewrite that changes earlier bytes without shrinking the file (and
    without changing its inode) is caught only by the prefix hash check,
    never by size or inode alone."""
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])
    index = reader.ParentIndex.for_path(parent)
    assert len(index.generations) == 1

    first_line = json.dumps(_workflow_launch("toolu_A", BASE))
    second_line = json.dumps(
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA")
    )
    original_size = len(first_line.encode("utf-8")) + 1 + len(second_line.encode("utf-8")) + 1

    rewritten_first_line = json.dumps(_workflow_launch("toolu_C", BASE))
    pad = max(0, len(first_line) - len(rewritten_first_line))
    rewritten_first_line = rewritten_first_line[:-1] + " " * pad + "}" if pad else rewritten_first_line
    new_text = rewritten_first_line + "\n" + second_line + "\n"
    assert len(new_text.encode("utf-8")) >= original_size, "fixture bug: rewrite must not shrink"
    parent.write_text(new_text, encoding="utf-8")

    index2 = reader.ParentIndex.for_path(parent)
    assert index2 is index
    assert index2.rebuilds == 1
    # toolu_A's own launch is gone from the rewritten prefix; only the
    # (now orphaned) result for it remains, which is a parent diagnostic,
    # never a stale generation.
    assert all(g.tool_use_id != "toolu_A" for g in index2.generations)


def test_cache_same_size_rewrite_rebuilds(tmp_path):
    parent = tmp_path / "parent.jsonl"

    def _lines(status: str, pad: int) -> list[dict]:
        return [
            _workflow_launch("toolu_A", BASE),
            _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
            _notification_entry(
                BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", status, pad=pad),
            ),
        ]

    failed_len = len(_envelope("tA", "toolu_A", "failed"))
    completed_len = len(_envelope("tA", "toolu_A", "completed"))
    pad = max(0, completed_len - failed_len)

    _write_jsonl(parent, _lines("failed", pad))
    stat1 = parent.stat()
    index = reader.ParentIndex.for_path(parent)
    gen = _gen(index, "toolu_A")
    assert gen.terminal == "failed"

    _write_jsonl(parent, _lines("completed", 0))
    assert parent.stat().st_size == stat1.st_size, "fixture bug: rewrite must keep the same size"
    os.utime(parent, (stat1.st_atime, stat1.st_mtime))

    index2 = reader.ParentIndex.for_path(parent)
    assert index2 is index
    assert index2.rebuilds == 1
    gen2 = _gen(index2, "toolu_A")
    assert gen2.terminal == "completed"


# --- N1N2 fix review finding 1: a reset-then-read-failure must not --------
# discard what the reset wiped (docs/DESIGN.md 4.13.3: a read failure
# "anywhere in this cache's own rebuild/rehash/parse steps ... flips [inspec-
# tion_complete] False and leaves the index's prior state otherwise
# untouched, exactly like the existing 'file missing or unreadable at
# stat()' branch already did"). _reset_state() ran inside the same try as
# the read that can fail, so a shrink (or same-size rewrite) that correctly
# triggers _reset_state(), immediately followed by a read failure, used to
# commit the wipe and never restore it.


def test_shrink_then_read_failure_keeps_prior_generations_task_stops_and_kill_sentinels(
    tmp_path, monkeypatch,
):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _assistant_tool("stop1", "TaskStop", {"task_id": "tB"}, BASE + timedelta(minutes=1)),
        _kill(BASE + timedelta(minutes=2)),
    ])
    index = reader.ParentIndex.for_path(parent)
    assert len(index.generations) == 1
    assert len(index.task_stops) == 1
    assert len(index.kill_sentinels) == 1
    generations_before = list(index.generations)
    task_stops_before = list(index.task_stops)
    kill_sentinels_before = list(index.kill_sentinels)

    # Shrink, same inode: normally forces a rebuild from zero.
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))

    index2 = reader.ParentIndex.for_path(parent)
    assert index2 is index
    assert index2.inspection_complete is False
    assert index2.generations == generations_before
    assert index2.task_stops == task_stops_before
    assert index2.kill_sentinels == kill_sentinels_before


def test_generations_for_filters_by_run_id(tmp_path):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
        _workflow_launch("toolu_B", BASE + timedelta(minutes=1)),
        _workflow_launch_result("toolu_B", BASE + timedelta(minutes=1, seconds=1), RUN_Y, "tB"),
    ])
    index = reader.ParentIndex._scan(parent)
    assert [g.tool_use_id for g in index.generations_for(RUN_X)] == ["toolu_A"]
    assert [g.tool_use_id for g in index.generations_for(RUN_Y)] == ["toolu_B"]
    assert index.generations_for("wf_00000000-ba3") == []


# --- N1N2 review round 2, original finding 1 (PARTLY): one stable,         --
# line-specific Run diagnostic per decode or shape failure, and one stable,
# file-level diagnostic per read failure (docs/DESIGN.md section 2: "a bad
# line becomes a diagnostic string on the Run, never an exception"),
# preserved through snapshot/restore, deduplicated across refreshes. None of
# these ever quote transcript text: a filename, a line number when the
# failure is line-specific, and a fixed reason only.


def test_undecodable_parent_line_records_a_stable_line_specific_diagnostic(tmp_path):
    parent = tmp_path / "parent.jsonl"
    parent.write_text(
        json.dumps(_workflow_launch("toolu_A", BASE)) + "\n" + "{not valid json\n",
        encoding="utf-8",
    )
    index = reader.ParentIndex._scan(parent)
    assert index.inspection_complete is False
    assert f"{parent.name}:2: undecodable parent transcript line" in index.diagnostics


def test_malformed_shape_parent_entry_records_a_stable_line_specific_diagnostic(tmp_path):
    parent = tmp_path / "parent.jsonl"
    parent.write_text(
        '{"type":"user","message":"not-an-object","timestamp":"2026-09-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    index = reader.ParentIndex._scan(parent)
    assert index.inspection_complete is False
    assert f"{parent.name}:1: malformed parent transcript entry" in index.diagnostics


def test_unreadable_parent_file_records_a_stable_file_level_diagnostic(tmp_path, monkeypatch):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [_workflow_launch("toolu_A", BASE)])
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))

    index = reader.ParentIndex.for_path(parent)
    assert index.inspection_complete is False
    assert index.diagnostics == [f"{parent.name}: parent transcript unreadable"]


def test_unreadable_parent_diagnostic_is_deduplicated_across_repeated_refreshes(
    tmp_path, monkeypatch,
):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [_workflow_launch("toolu_A", BASE)])
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))

    index = reader.ParentIndex.for_path(parent)
    index2 = reader.ParentIndex.for_path(parent)
    index3 = reader.ParentIndex.for_path(parent)
    assert index is index2 is index3
    assert index3.diagnostics == [f"{parent.name}: parent transcript unreadable"]


def test_line_level_diagnostic_survives_a_failed_rebuilds_restore(tmp_path, monkeypatch):
    """N1N2 fix review finding 1's own snapshot/restore mechanism already
    preserves every per-content accumulator verbatim; this pins that a
    line-level diagnostic recorded before a shrink-triggered rebuild whose
    own read then fails is not silently dropped -- the restored diagnostics
    still name the original bad line, with the new file-level diagnostic for
    this attempt's own failure appended alongside it."""
    parent = tmp_path / "parent.jsonl"
    parent.write_text(
        json.dumps(_workflow_launch("toolu_A", BASE)) + "\n" + "{not valid json\n",
        encoding="utf-8",
    )
    index = reader.ParentIndex.for_path(parent)
    assert f"{parent.name}:2: undecodable parent transcript line" in index.diagnostics

    # Shrink, same inode: normally forces a rebuild from zero.
    parent.write_text(json.dumps(_workflow_launch("toolu_A", BASE)) + "\n", encoding="utf-8")
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))

    index2 = reader.ParentIndex.for_path(parent)
    assert index2 is index
    assert f"{parent.name}:2: undecodable parent transcript line" in index2.diagnostics
    assert f"{parent.name}: parent transcript unreadable" in index2.diagnostics


# --- N1N2 review round 2, original finding 7 (PARTLY): json.loads itself ---
# only ever runs once per physical parent line (docs/DESIGN.md 4.13.3's
# parent index cache paragraph: "parsing cost stays proportional to new
# lines"). The existing cross-workflow spy in test_n2_lifecycle.py
# (test_parent_not_fully_reread_for_completeness_across_three_workflows_
# sharing_one_parent) only ever watches Path.read_bytes, the old
# non-incremental full-read entry point -- it cannot see a regression that
# reparses the SAME already-read bytes an extra time inside the incremental
# pass itself without ever doing a second byte-level read (mutation M25:
# a second json.loads(text) call for every nonblank parent line left every
# existing N1/N2 lifecycle test passing). This test watches reader.json.loads
# directly instead.


def test_json_loads_called_once_per_parent_line_then_zero_then_k_more(tmp_path, monkeypatch):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])

    calls = {"n": 0}
    real_loads = reader.json.loads

    def counting_loads(*args, **kwargs):
        calls["n"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(reader.json, "loads", counting_loads)

    index = reader.ParentIndex.for_path(parent)  # a 2-line initial scan
    assert calls["n"] == 2, f"expected 2 json.loads calls for a 2-line initial scan, got {calls['n']}"
    assert len(index.generations) == 1

    calls["n"] = 0
    index2 = reader.ParentIndex.for_path(parent)  # unchanged: no new bytes at all
    assert index2 is index
    assert calls["n"] == 0, f"expected 0 json.loads calls on an unchanged refresh, got {calls['n']}"

    with parent.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed"))
        ) + "\n")
    calls["n"] = 0
    index3 = reader.ParentIndex.for_path(parent)  # a 1-line append
    assert index3 is index
    assert calls["n"] == 1, f"expected 1 more json.loads call for a 1-line append, got {calls['n']}"
    assert index3.generations[0].terminal == "completed"


# --- N1N2 review round 2, new finding 2 (MINOR): a failed rebuild must ----
# preserve EVERY state class _reset_state() clears, not just the three
# (generations, task_stops, kill_sentinels) the original N1N2 fix review
# finding 1 test above happens to seed -- mutation M27 (dropping
# tool_result_agent_ids from the snapshot dict) left every existing test in
# the suite passing. Every field below is read directly off the index's own
# attributes, never through index._state_snapshot() itself: that is exactly
# the function M27 targets, so building the "before" and "after" comparison
# from its own output would hide the same mutation from this test too.


def _full_observable_state(index: "reader.ParentIndex") -> dict:
    """Every field _reset_state() clears, read directly off the index's own
    attributes -- never self.diagnostics, which a failed refresh legitimately
    grows by one new file-level diagnostic (docs/DESIGN.md section 2, N1N2
    review round 2, original finding 1); that is checked separately, below."""
    return {
        "task_stops": list(index.task_stops),
        "kill_sentinels": list(index.kill_sentinels),
        "kill_timestamps": list(index.kill_timestamps),
        "compaction_timestamps": list(index.compaction_timestamps),
        "tool_result_agent_ids": set(index.tool_result_agent_ids),
        "generations": list(index.generations),
        "ambiguous_launches": list(index.ambiguous_launches),
        "inspection_complete": index.inspection_complete,
        "_launches_by_id": dict(index._launches_by_id),
        "_results_by_id": dict(index._results_by_id),
        "_notification_candidates": list(index._notification_candidates),
        "_first_origin_line": index._first_origin_line,
        "_parsed_offset": index._parsed_offset,
        "_next_line_no": index._next_line_no,
        "_prefix_digest": index._prefix_digest,
    }


def _seed_every_state_class(parent: Path) -> None:
    """One parent transcript line for every field _reset_state() clears: a
    resolved generation (toolu_C, terminal 'stopped' via its own TaskStop --
    also seeds task_stops), a kill sentinel, a compaction boundary, a
    tool_result carrying agentId (D2), an ambiguous launch (toolu_DUP --
    also seeds a gen_diagnostics entry), an origin-bearing notification
    candidate for an unrelated task (also seeds _first_origin_line), and one
    shape-violating line (a non-string queue-operation operation -- also
    seeds a line-specific entry in _raw_diagnostics and flips
    inspection_complete False)."""
    _write_jsonl(parent, [
        _workflow_launch("toolu_C", BASE),
        _workflow_launch_result("toolu_C", BASE + timedelta(seconds=1), RUN_X, "tC"),
        _assistant_tool("stop1", "TaskStop", {"task_id": "tC"}, BASE + timedelta(minutes=1)),
        _kill(BASE + timedelta(minutes=2)),
        {
            "type": "system", "subtype": "compact_boundary",
            "timestamp": (BASE + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_unrelated", "content": "x",
                 "agentId": "af000000000000000"},
            ]},
            "timestamp": (BASE + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        _workflow_launch("toolu_DUP", BASE + timedelta(minutes=5)),
        _workflow_launch("toolu_DUP", BASE + timedelta(minutes=5, seconds=1)),
        _workflow_launch_result("toolu_DUP", BASE + timedelta(minutes=5, seconds=2), RUN_X, "tD"),
        _notification_entry(
            BASE + timedelta(minutes=6), _envelope("t-other", "toolu-other", "completed"),
        ),
        {
            "type": "queue-operation", "operation": 42,
            "timestamp": (BASE + timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ])


def test_failed_rebuild_preserves_every_state_class_and_counters(tmp_path, monkeypatch):
    parent = tmp_path / "parent.jsonl"
    _seed_every_state_class(parent)

    baseline = reader.ParentIndex.for_path(parent)
    assert len(baseline.generations) == 1
    assert baseline.generations[0].tool_use_id == "toolu_C"
    assert baseline.generations[0].terminal == "stopped"
    assert len(baseline.task_stops) == 1
    assert len(baseline.kill_sentinels) == 1
    assert len(baseline.compaction_timestamps) == 1
    assert baseline.tool_result_agent_ids == {"af000000000000000"}
    assert len(baseline.ambiguous_launches) == 1
    assert baseline.inspection_complete is False  # the shape-violating line
    before = _full_observable_state(baseline)
    diagnostics_before = list(baseline.diagnostics)
    lines_parsed_before = baseline.lines_parsed
    rebuilds_before = baseline.rebuilds

    # Shrink, same inode: normally forces a rebuild from zero.
    parent.write_text(
        json.dumps(_workflow_launch("toolu_C", BASE)) + "\n"
        + json.dumps(_workflow_launch_result(
            "toolu_C", BASE + timedelta(seconds=1), RUN_X, "tC",
        )) + "\n",
        encoding="utf-8",
    )
    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_parse_increment")

    failed = reader.ParentIndex.for_path(parent)
    assert failed is baseline
    assert _full_observable_state(failed) == before
    # The only change allowed: this attempt's own new file-level diagnostic,
    # appended -- every diagnostic that was already there survives unchanged.
    assert failed.diagnostics == [
        *diagnostics_before, f"{parent.name}: parent transcript unreadable",
    ]
    assert failed.lines_parsed == lines_parsed_before
    assert failed.rebuilds == rebuilds_before
    assert failed.lines_parsed == lines_parsed_before
    assert failed.rebuilds == rebuilds_before


def test_shrink_triggered_rebuild_retries_correctly_after_a_failed_attempt(tmp_path, monkeypatch):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])
    index = reader.ParentIndex.for_path(parent)
    assert len(index.generations) == 1

    parent.write_text("", encoding="utf-8")  # shrink to empty, same inode
    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_parse_increment")

    index2 = reader.ParentIndex.for_path(parent)  # the rebuild's own read fails once
    assert index2 is index
    assert index2.inspection_complete is False
    assert len(index2.generations) == 1  # prior state preserved, not discarded

    index3 = reader.ParentIndex.for_path(parent)  # the retry succeeds
    assert index3 is index
    assert index3.inspection_complete is True
    assert index3.generations == []  # correctly reflects the now-empty file


def test_same_size_rewrite_triggered_rebuild_retries_correctly_after_a_failed_attempt(
    tmp_path, monkeypatch,
):
    parent = tmp_path / "parent.jsonl"

    def _lines(status: str, pad: int) -> list[dict]:
        return [
            _workflow_launch("toolu_A", BASE),
            _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
            _notification_entry(
                BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", status, pad=pad),
            ),
        ]

    failed_len = len(_envelope("tA", "toolu_A", "failed"))
    completed_len = len(_envelope("tA", "toolu_A", "completed"))
    pad = max(0, completed_len - failed_len)

    _write_jsonl(parent, _lines("failed", pad))
    stat1 = parent.stat()
    index = reader.ParentIndex.for_path(parent)
    assert _gen(index, "toolu_A").terminal == "failed"

    _write_jsonl(parent, _lines("completed", 0))
    assert parent.stat().st_size == stat1.st_size, "fixture bug: rewrite must keep the same size"
    os.utime(parent, (stat1.st_atime, stat1.st_mtime))
    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_parse_increment")

    index2 = reader.ParentIndex.for_path(parent)  # the rebuild's own read fails once
    assert index2 is index
    assert index2.inspection_complete is False
    assert _gen(index2, "toolu_A").terminal == "failed"  # prior state preserved

    index3 = reader.ParentIndex.for_path(parent)  # the retry succeeds
    assert index3 is index
    assert index3.inspection_complete is True
    assert _gen(index3, "toolu_A").terminal == "completed"  # reflects the new content


def test_inode_replacement_triggered_rebuild_retries_correctly_after_a_failed_attempt(
    tmp_path, monkeypatch,
):
    parent = tmp_path / "parent.jsonl"
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), RUN_X, "tA"),
    ])
    index = reader.ParentIndex.for_path(parent)
    assert [g.tool_use_id for g in index.generations] == ["toolu_A"]

    replacement = tmp_path / "parent.jsonl.new"
    _write_jsonl(replacement, [
        _workflow_launch("toolu_B", BASE),
        _workflow_launch_result("toolu_B", BASE + timedelta(seconds=1), RUN_Y, "tB"),
    ])
    replacement.replace(parent)  # same path, new inode
    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_parse_increment")

    index2 = reader.ParentIndex.for_path(parent)  # the rebuild's own read fails once
    assert index2 is index
    assert index2.inspection_complete is False
    assert [g.tool_use_id for g in index2.generations] == ["toolu_A"]  # prior state preserved

    index3 = reader.ParentIndex.for_path(parent)  # the retry succeeds
    assert index3 is index
    assert index3.inspection_complete is True
    assert [g.tool_use_id for g in index3.generations] == ["toolu_B"]  # reflects the new content
