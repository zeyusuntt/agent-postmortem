"""Fix E (Codex round 3: omitted timestamps, rejected initial launches), Amendment A5
(continued): an event-bearing line with no usable timestamp (absent, not a string, or
unparseable) at or before the earliest line carrying first_ts makes the attempt
unassignable (R3-1); an initial rejected Workflow launch is always undiscovered in the
session launch table (R3-2). docs/DESIGN.md 4.13.1.

Fixtures are synthetic; every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

from tests.test_n4b1_discovery import _diag, _launch, _session, _transcripts, _workflow_result
from tests.test_n4fixc_a4 import RUN_C, RUN_D, _rejected_result, _sec
from tests.test_n4fixd_a5 import (
    _GOOD,
    _OMIT,
    _population_ids,
    _resume_launch,
    _ts_run,
    _unassignable,
    _write_call,
)


def _result(tool_id, ts):
    """A successful tool result; the top-level `timestamp` key is absent when ts is _OMIT."""
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
        },
    }
    if ts is not _OMIT:
        entry["timestamp"] = ts
    return entry


def _text_line(ts):
    entry = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "done"}], "usage": {}}}
    if ts is not _OMIT:
        entry["timestamp"] = ts
    return entry


# --- R3-1: an omitted timestamp is as unplaceable as a malformed one ---------------------


def test_r3_1_an_omitted_timestamp_before_the_first_ts_line_is_unassignable(
    tmp_path, monkeypatch
):
    # Codex's exact input: line 1 omits the key, line 2 carries the first valid timestamp.
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _OMIT), _result("toolu_w", _GOOD),
    ])
    assert run.attempts[0].ledger.entries[0].effect == "observed"
    _unassignable(run, agent_id)


def test_r3_1_an_omitted_timestamp_after_the_first_ts_line_does_not_matter(
    tmp_path, monkeypatch
):
    run, _ = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _GOOD), _result("toolu_w", _OMIT),
    ])
    assert _population_ids(run) == []
    assert run.risk_population_completeness == "complete"
    assert run.risk_population_completeness_reasons == []


def test_r3_1_every_timestamp_omitted_is_unassignable(tmp_path, monkeypatch):
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _OMIT), _result("toolu_w", _OMIT),
    ])
    _unassignable(run, agent_id)


def test_r3_1_an_omitted_timestamp_between_valid_lines_before_the_first_ts_line(
    tmp_path, monkeypatch
):
    # first_ts is the minimum event timestamp, on line 3; line 2 omits the key.
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", "2026-09-01T12:00:09Z"),
        _result("toolu_w", _OMIT),
        _text_line("2026-09-01T12:00:05Z"),
    ])
    _unassignable(run, agent_id)


def test_r3_1_an_omitted_timestamp_on_the_line_that_set_nothing_later_is_fine(
    tmp_path, monkeypatch
):
    run, _ = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _GOOD), _text_line(_GOOD), _result("toolu_w", _OMIT),
    ])
    assert _population_ids(run) == []
    assert run.risk_population_completeness == "complete"


def test_r3_1_a_non_string_timestamp_is_still_unassignable_before_the_first_ts_line(
    tmp_path, monkeypatch
):
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", None), _result("toolu_w", _GOOD),
    ])
    _unassignable(run, agent_id)


# --- R3-2: an initial rejected launch is always undiscovered -----------------------------


def test_r3_2_a_later_resume_naming_a_directory_does_not_prove_the_rejected_launch_empty(
    tmp_path,
):
    # Codex's exact input.
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2)),
         _resume_launch("toolu_2", RUN_C), _workflow_result("toolu_2", RUN_C)],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r3_2_an_initial_rejected_launch_with_no_directory_at_all_is_undiscovered(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2))])
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r3_2_every_directory_associated_with_another_launch_still_undiscovered(tmp_path):
    main = _session(
        tmp_path,
        [_launch("toolu_1", "Workflow"), _rejected_result("toolu_1", _sec(2)),
         _launch("toolu_2", "Workflow"),
         _workflow_result("toolu_2", RUN_C, ts="2026-09-01T12:00:03Z")],
        workflow_dirs=[RUN_C],
    )
    assert _transcripts(main) == [_diag(2, "Workflow", "undiscovered")]


def test_r3_2_a_rejected_resume_launch_whose_directory_exists_still_reconciles(tmp_path):
    main = _session(
        tmp_path, [_resume_launch("toolu_1", RUN_C), _rejected_result("toolu_1", _sec(2))],
        workflow_dirs=[RUN_C, RUN_D],
    )
    assert _transcripts(main) == []


def test_r3_1_the_earliest_omitted_line_counts_not_the_latest(tmp_path, monkeypatch):
    # Line 1 omits the key and so does line 3; first_ts is on line 2. Only line 1 is before it.
    run, agent_id = _ts_run(tmp_path, monkeypatch, [
        _write_call("toolu_w", _OMIT), _result("toolu_w", _GOOD), _text_line(_OMIT),
    ])
    _unassignable(run, agent_id)
