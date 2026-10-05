"""Part FA4 (field milestone F, T4): a fork's inherited prefix belongs to the parent
(docs/DESIGN.md 4.2; .reviews/v0.3/F-plan.md FA4).

The field case: a forked session's transcript starts with a contiguous block of copied
entries (417 in the real one) whose `sessionId` is the PARENT's. The reader treated them as
the fork's own history, so the fork repeated the parent's findings (same line numbers) and
duration. The rule: in a session run's main transcript, the leading block of entries up to
the last entry whose `sessionId` is present and differs from the transcript's own stem,
counted only when a later entry carries the transcript's own `sessionId`, is inherited. It
is a raw boundary shared by `_read_transcript` and `ParentIndex`, applied before any
accumulator is updated; physical line numbers of the own entries do not change.

Fixtures are synthetic: the two session ids are built at run time (the privacy audit allows
only the one fake UUID as a literal), the cwds are under /tmp.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, reader
from tests.test_field_v03 import (
    FAKE_SLUG,
    _aid,
    _assistant_text,
    _assistant_tool,
    _envelope,
    _iso,
    _kill,
    _no_config,
    _notification_entry,
    _tool_result,
    _user,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
ANCHORED = "Command did not complete within its 120s timeout and was moved to the background"
PARENT_CWD = "/tmp/parent-work"
FORK_CWD = "/tmp/fork-work"
CHILD = _aid(0x4F)
INHERITED_NOTE = "inherited {n} leading lines from another session (a fork); they are that session's"


def _sid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


PARENT_ID, FORK_ID = _sid(0xAAA1), _sid(0xBBB2)


def _with(entry: dict, session: str, cwd: str | None = None) -> dict:
    entry = dict(entry)
    entry["sessionId"] = session
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _parent_history(session: str) -> list[dict]:
    """The records a parent session wrote that a fork would copy: a backgrounded Bash call with
    the anchored marker, an Agent launch with its notification, a malformed Workflow launch, a
    TaskStop, a human interrupt sentinel, and a marker. Ten entries."""
    entries = [
        _user("Plan the work.", _at(0)),
        _assistant_tool("tb1", "Bash", {"command": "sleep 300"}, _at(1)),
        _tool_result("tb1", ANCHORED + "\nrunning", _at(2)),
        _assistant_tool("ta1", "Agent", {"description": "child"}, _at(3)),
        {**_tool_result("ta1", "launched", _at(4)),
         "toolUseResult": {"agentId": CHILD, "status": "async_launched", "isAsync": True}},
        _notification_entry(_at(5), _envelope(CHILD, "ta1", "completed")),
        _workflow_launch("tw1", _at(6)),
        _workflow_launch_result("tw1", _at(7), "wf_00000000-fa1", "tW"),
        _assistant_tool("ts1", "TaskStop", {"task_id": "tX"}, _at(8)),
        _kill(_at(9)),
    ]
    return [_with(entry, session, PARENT_CWD) for entry in entries]


def _own_history(session: str, base: float = 100.0) -> list[dict]:
    entries = [
        _user("Fork: continue.", _at(base)),
        _assistant_tool("own1", "Bash", {"command": "ls"}, _at(base + 1)),
        _tool_result("own1", "ok", _at(base + 2)),
        _assistant_text("Done.", _at(base + 3)),
    ]
    return [_with(entry, session, FORK_CWD) for entry in entries]


def _layout(tmp_path: Path):
    root = tmp_path / "root"
    return root, root / FAKE_SLUG


def _write_fork(tmp_path, monkeypatch, own, *, with_child=False, prefix=None):
    """parent P.jsonl = its history; fork F.jsonl = the copied prefix (P's entries) + own entries."""
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    prefix = _parent_history(PARENT_ID) if prefix is None else prefix
    parent = slug_dir / f"{PARENT_ID}.jsonl"
    fork = slug_dir / f"{FORK_ID}.jsonl"
    _write_jsonl(parent, prefix + [_with(_assistant_text("Parent goes on.", _at(50)), PARENT_ID, PARENT_CWD)])
    _write_jsonl(fork, prefix + own)
    if with_child:
        _write_jsonl(slug_dir / FORK_ID / "subagents" / f"agent-{CHILD}.jsonl", [
            _user("child work", _at(101), CHILD),
            _assistant_text("working", _at(102), CHILD),
            _kill(_at(109), CHILD),
        ])
    return parent, fork


def _findings(run_path, capsys, *flags):
    cli.main(["run", str(run_path), "--json", "--no-redact", *flags])
    return json.loads(capsys.readouterr().out)


# --- the boundary function ---------------------------------------------------------------


def test_fa4_the_boundary_is_the_last_foreign_line_before_the_first_own_entry(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, _parent_history(PARENT_ID) + _own_history(FORK_ID))
    assert reader._fork_boundary(path) == 10


def test_fa4_entries_lacking_a_session_id_inside_the_block_are_inherited(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    entries = _parent_history(PARENT_ID)
    bare = {"type": "summary", "summary": "x"}
    entries.insert(3, bare)          # inside the block
    entries.insert(0, dict(bare))    # leading, before any foreign entry
    _write_jsonl(path, entries + _own_history(FORK_ID))
    assert reader._fork_boundary(path) == 12


def test_fa4_entries_lacking_a_session_id_after_the_last_foreign_one_are_not_inherited(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    entries = _parent_history(PARENT_ID) + [{"type": "summary", "summary": "x"}]
    _write_jsonl(path, entries + _own_history(FORK_ID))
    assert reader._fork_boundary(path) == 10


def test_fa4_an_entry_whose_session_id_is_not_a_string_is_not_inherited(tmp_path):
    """A `sessionId` that is null, a number, or only quoted inside a message is no foreign
    session: it never moves the boundary."""
    path = tmp_path / f"{FORK_ID}.jsonl"
    quoting = _assistant_text('the field "sessionId" is a string', _at(20))
    entries = _parent_history(PARENT_ID) + [
        {**_assistant_text("a", _at(21)), "sessionId": None},
        {**_assistant_text("b", _at(22)), "sessionId": 7},
        quoting,
    ]
    _write_jsonl(path, entries + _own_history(FORK_ID))
    assert reader._fork_boundary(path) == 10


def test_fa4_a_file_with_no_own_entry_inherits_nothing(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, _parent_history(PARENT_ID))
    assert reader._fork_boundary(path) == 0


def test_fa4_a_file_that_starts_with_its_own_entry_inherits_nothing(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, _own_history(FORK_ID))
    assert reader._fork_boundary(path) == 0


def test_fa4_a_file_with_no_session_ids_at_all_inherits_nothing(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, [_user("a", _at(0)), _assistant_text("b", _at(1))])
    assert reader._fork_boundary(path) == 0


def test_fa4_a_lone_foreign_line_after_own_lines_is_kept(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    own = _own_history(FORK_ID)
    foreign = _with(_assistant_text("stray", _at(200)), PARENT_ID)
    _write_jsonl(path, own[:2] + [foreign] + own[2:])
    assert reader._fork_boundary(path) == 0


def test_fa4_undecodable_and_blank_lines_inside_the_block_do_not_move_the_boundary(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    lines = [json.dumps(e) for e in _parent_history(PARENT_ID)]
    lines.insert(2, "not json at all")
    lines.insert(4, "")
    lines += [json.dumps(e) for e in _own_history(FORK_ID)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert reader._fork_boundary(path) == 12


def test_fa4_a_missing_or_unreadable_file_inherits_nothing(tmp_path):
    assert reader._fork_boundary(tmp_path / "nope.jsonl") == 0


# --- the session run: the fork reports none of the parent's records --------------------------


def test_fa4_the_fork_reports_none_of_the_copied_records(tmp_path, monkeypatch, capsys):
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    run = reader.load_run(fork)
    main = run.main
    assert [call.tool_use_id for call in main.tool_calls] == ["own1"]
    assert [event.line_no for event in main.events][0] == 11
    assert main.marker_hits == []
    assert main.facts.cwds == [FORK_CWD]
    assert run.discovery == {"journal": [], "parent": [], "transcripts": [], "meta": []}
    assert main.interrupt is None
    payload = _findings(fork, capsys)
    assert [f["detector"] for f in payload["findings"]] == ["wasted_time"]
    assert payload["run"]["status"] == "COMPLETE"


def test_fa4_duration_and_timestamps_are_the_forks_own(tmp_path, monkeypatch):
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    run = reader.load_run(fork)
    assert run.started_at == _at(100)
    assert run.ended_at == _at(103)
    assert run.main.first_ts == _at(100) and run.main.last_ts == _at(103)


def test_fa4_identity_and_version_come_from_own_entries(tmp_path, monkeypatch):
    own = _own_history(FORK_ID)
    own[0]["version"] = "9.9.9"
    prefix = _parent_history(PARENT_ID)
    prefix[0]["version"] = "1.1.1"
    _, fork = _write_fork(tmp_path, monkeypatch, own, prefix=prefix)
    run = reader.load_run(fork)
    assert run.claude_version == "9.9.9"
    assert run.session_id == FORK_ID


def test_fa4_one_diagnostic_names_the_inherited_line_count(tmp_path, monkeypatch):
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    run = reader.load_run(fork)
    assert [d for d in run.diagnostics if "inherited" in d] == [INHERITED_NOTE.format(n=10)]
    # a data-quality note: it never reaches a discovery list or makes the run partial
    assert all("inherited" not in d for key in run.discovery for d in run.discovery[key])
    assert run.risk_population_completeness == "complete"


def test_fa4_the_parent_is_unchanged(tmp_path, monkeypatch, capsys):
    parent, _ = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    run = reader.load_run(parent)
    assert len(run.main.tool_calls) == 4
    assert [d for d in run.diagnostics if "inherited" in d] == []
    assert run.started_at == _at(0)
    detectors = {f["detector"] for f in _findings(parent, capsys)["findings"]}
    assert "bash_backgrounded" in detectors
    assert any("no discovered child" in d for d in run.discovery["transcripts"])


def test_fa4_the_same_records_after_the_first_own_entry_still_count(tmp_path, monkeypatch, capsys):
    own = _own_history(FORK_ID)
    late = [_with(entry, FORK_ID, FORK_CWD) for entry in _parent_history(FORK_ID)[1:4]]
    _, fork = _write_fork(tmp_path, monkeypatch, own[:1] + late + own[1:])
    detectors = {f["detector"] for f in _findings(fork, capsys)["findings"]}
    assert "bash_backgrounded" in detectors


def test_fa4_a_file_with_no_own_entry_is_read_whole(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    fork = slug_dir / f"{FORK_ID}.jsonl"
    _write_jsonl(fork, _parent_history(PARENT_ID))
    run = reader.load_run(fork)
    assert len(run.main.tool_calls) == 4
    assert [d for d in run.diagnostics if "inherited" in d] == []


def test_fa4_a_foreign_line_after_own_lines_is_kept(tmp_path, monkeypatch):
    own = _own_history(FORK_ID)
    foreign = _with(_assistant_tool("late1", "Bash", {"command": "pwd"}, _at(104)), PARENT_ID, PARENT_CWD)
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    fork = slug_dir / f"{FORK_ID}.jsonl"
    _write_jsonl(fork, own + [foreign])
    run = reader.load_run(fork)
    assert [call.tool_use_id for call in run.main.tool_calls] == ["own1", "late1"]


def test_fa4_the_hostile_records_of_the_prefix_feed_no_marker_usage_or_cwd(tmp_path, monkeypatch):
    prefix = _parent_history(PARENT_ID)
    prefix[1]["message"]["usage"] = {"input_tokens": 1000, "output_tokens": 500}
    prefix[1]["message"]["id"] = "msg_prefix"
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID), prefix=prefix)
    run = reader.load_run(fork)
    assert run.main.usage == {}
    assert run.main.entry_cwds == (FORK_CWD,)


def test_fa4_inherited_undecodable_lines_add_no_diagnostic_and_no_threshold_pressure(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    fork = slug_dir / f"{FORK_ID}.jsonl"
    fork.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in _parent_history(PARENT_ID)]
    lines[2:2] = ["garbage"] * 6                       # 6 of 16 inherited lines are undecodable
    lines += [json.dumps(e) for e in _own_history(FORK_ID)]
    fork.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run = reader.load_run(fork)
    assert run.status == "COMPLETE"
    assert not any("Expecting value" in d or "undecodable" in d for d in run.diagnostics)
    assert [d for d in run.diagnostics if "inherited" in d] == [INHERITED_NOTE.format(n=16)]


def test_fa4_a_subagent_kill_is_not_attributed_to_the_copied_stop_or_sentinel(
    tmp_path, monkeypatch
):
    """The copied TaskStop names tX, and the copied sentinel is a human interrupt: neither
    may attribute the fork's own subagent kill (which has no TaskStop and no sentinel of its
    own), whatever the timestamps."""
    prefix = _parent_history(PARENT_ID)
    for entry in prefix:
        entry["timestamp"] = _iso(_at(105))
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID), with_child=True, prefix=prefix)
    run = reader.load_run(fork)
    (attempt,) = run.attempts
    assert attempt.interrupt is not None
    assert attempt.interrupt.cause == "unattributed"


def test_fa4_the_copied_stop_would_have_attributed_it_in_the_parent(tmp_path, monkeypatch):
    """The control: the same prefix in a file that is NOT a fork (its own entries come first
    or it has no own entries) does attribute, so the test above is the boundary at work."""
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    prefix = _parent_history(FORK_ID)       # the same records, but the transcript's own
    for entry in prefix:
        entry["timestamp"] = _iso(_at(105))
    _write_jsonl(slug_dir / f"{FORK_ID}.jsonl", prefix + _own_history(FORK_ID))
    _write_jsonl(slug_dir / FORK_ID / "subagents" / f"agent-{CHILD}.jsonl", [
        _user("child work", _at(101), CHILD), _assistant_text("working", _at(102), CHILD),
        _kill(_at(109), CHILD),
    ])
    run = reader.load_run(slug_dir / f"{FORK_ID}.jsonl")
    assert run.attempts[0].interrupt.cause == "parent_stop"


# --- ParentIndex: the same boundary ---------------------------------------------------------


def _index(path):
    return reader.ParentIndex._scan(path)


def test_fa4_parent_index_ignores_the_copied_prefix(tmp_path, monkeypatch):
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    index = _index(fork)
    assert index.task_stops == [] and index.kill_sentinels == []
    assert index.generations == [] and index.ambiguous_launches == []
    assert index.tool_result_agent_ids == set()
    assert index._accepted_notifications() == []
    assert index.lines_parsed == 14            # every physical line still counts


def test_fa4_parent_index_keeps_physical_line_numbers_of_own_entries(tmp_path, monkeypatch):
    own = _own_history(FORK_ID)
    own.insert(2, _with(_assistant_tool("tsx", "TaskStop", {"task_id": "tZ"}, _at(150)), FORK_ID))
    own.insert(3, _with(_kill(_at(151)), FORK_ID))
    _, fork = _write_fork(tmp_path, monkeypatch, own)
    index = _index(fork)
    assert [stop.line_no for stop in index.task_stops] == [13]
    assert [s.line_no for s in index.kill_sentinels] == [14]


def test_fa4_parent_index_of_the_parent_is_unchanged(tmp_path, monkeypatch):
    parent, _ = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    index = _index(parent)
    assert [s.line_no for s in index.task_stops] == [9]
    assert [s.line_no for s in index.kill_sentinels] == [10]
    assert len(index._accepted_notifications()) == 1
    assert [g.run_id for g in index.generations] == ["wf_00000000-fa1"]


def test_fa4_a_workflow_launch_inherited_by_a_fork_makes_no_generation_in_the_fork(
    tmp_path, monkeypatch
):
    prefix = _parent_history(PARENT_ID)
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID), prefix=prefix)
    assert _index(fork).generations_for("wf_00000000-fa1") == []
    parent_index = _index(fork.with_name(f"{PARENT_ID}.jsonl"))
    assert [g.run_id for g in parent_index.generations_for("wf_00000000-fa1")] == ["wf_00000000-fa1"]


def test_fa4_a_growing_fork_is_rebuilt_once_its_first_own_entry_appears(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    fork = slug_dir / f"{FORK_ID}.jsonl"
    _write_jsonl(fork, _parent_history(PARENT_ID))
    reader.ParentIndex._cache.pop(fork, None)
    first = reader.ParentIndex.for_path(fork)
    assert len(first.task_stops) == 1              # no own entry yet: nothing is inherited
    assert first.rebuilds == 0
    with fork.open("a", encoding="utf-8") as handle:
        for entry in _own_history(FORK_ID):
            handle.write(json.dumps(entry) + "\n")
    second = reader.ParentIndex.for_path(fork)
    assert second is first
    assert second.task_stops == [] and second.kill_sentinels == []
    assert second.rebuilds == 1
    reader.ParentIndex._cache.pop(fork, None)


def test_fa4_an_initial_build_of_a_fork_is_not_a_rebuild(tmp_path, monkeypatch):
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    reader.ParentIndex._cache.pop(fork, None)
    index = reader.ParentIndex.for_path(fork)
    assert index.rebuilds == 0
    reader.ParentIndex._cache.pop(fork, None)


def test_fa4_workflow_agent_and_subagent_transcripts_are_untouched(tmp_path, monkeypatch):
    """The rule is for a session run's main transcript only: a subagent transcript whose first
    entries carry another sessionId is read whole."""
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    _write_jsonl(slug_dir / f"{FORK_ID}.jsonl", _own_history(FORK_ID))
    sub = slug_dir / FORK_ID / "subagents" / f"agent-{CHILD}.jsonl"
    entries = [_with(_user("a", _at(101), CHILD), PARENT_ID), _with(_assistant_text("b", _at(102), CHILD), FORK_ID)]
    _write_jsonl(sub, entries)
    run = reader.load_run(slug_dir / f"{FORK_ID}.jsonl")
    (attempt,) = run.attempts
    assert [event.line_no for event in attempt.events] == [1, 2]
    assert attempt.first_ts == _at(101)


@pytest.mark.parametrize("flag", ["--no-redact", "--local", None])
def test_fa4_the_note_reaches_run_json_at_every_level(tmp_path, monkeypatch, capsys, flag):
    _, fork = _write_fork(tmp_path, monkeypatch, _own_history(FORK_ID))
    argv = ["run", str(fork), "--json"] + ([flag] if flag else [])
    cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert INHERITED_NOTE.format(n=10) in payload["run"]["diagnostics"]
