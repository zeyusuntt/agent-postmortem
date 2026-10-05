"""N4 fix round, Fix A (reader, discovery, population): C1 (an orphan session meta
file), C2 (a read-time decode or I/O failure), S1 (a workflow transcript no journal
started event names), S2 (an unkeyed workflow attempt in the population), and the
parent, journal, and meta read guards (.reviews/v0.3/N4-dispositions.md).

Fixtures are synthetic; every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import ledger, reader
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _assistant_tool,
    _envelope,
    _no_config,
    _notification_entry,
    _parent_path,
    _user,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
)
from tests.test_n4b1_discovery import (
    CLEAN,
    SESSION,
    SLUG,
    _agent_result,
    _diag,
    _launch,
    _session,
)

ORPHAN_META = "child named by a meta file has no transcript"


def _subagents(tmp_path):
    return tmp_path / "root" / SLUG / SESSION / "subagents"


def _write_meta(tmp_path, name, body="{}"):
    path = _subagents(tmp_path) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- C1: an orphan session meta file ---------------------------------------------------------


def test_c1_a_meta_file_with_no_transcript_is_a_transcripts_diagnostic(tmp_path):
    main = _session(tmp_path, [])
    child = _aid(7)
    _write_meta(tmp_path, f"agent-{child}.meta.json")
    run = reader.load_session(main)
    assert run.discovery == {**CLEAN, "transcripts": [f"agent-{child}.meta.json: {ORPHAN_META}"]}
    assert run.attempts == []
    assert run.risk_population_completeness == "partial"
    assert run.risk_population_completeness_reasons == ["discovery_incomplete"]
    assert run.status == "COMPLETE"
    assert run.diagnostics == []


def test_c1_orphan_metas_are_listed_in_sorted_id_order(tmp_path):
    main = _session(tmp_path, [])
    low, high = _aid(3), _aid(9)
    _write_meta(tmp_path, f"agent-{high}.meta.json")
    _write_meta(tmp_path, f"agent-{low}.meta.json")
    assert reader.load_session(main).discovery["transcripts"] == [
        f"agent-{low}.meta.json: {ORPHAN_META}",
        f"agent-{high}.meta.json: {ORPHAN_META}",
    ]


def test_c1_a_meta_file_beside_its_transcript_is_clean(tmp_path):
    child = _aid(7)
    main = _session(tmp_path, [], children=[child])
    _write_meta(tmp_path, f"agent-{child}.meta.json", '{"description": "d"}')
    assert reader.load_session(main).discovery == CLEAN


def test_c1_a_meta_name_that_is_not_a_canonical_id_names_no_child(tmp_path):
    main = _session(tmp_path, [])
    _write_meta(tmp_path, "agent-x.meta.json")
    _write_meta(tmp_path, f"agent-{_aid(1).upper()}.meta.json")
    _write_meta(tmp_path, "notes.meta.json")
    assert reader.load_session(main).discovery == CLEAN


def test_c1_an_orphan_meta_is_reported_even_when_its_content_is_unreadable(tmp_path):
    main = _session(tmp_path, [])
    child = _aid(7)
    _write_meta(tmp_path, f"agent-{child}.meta.json", "not json")
    run = reader.load_session(main)
    assert run.discovery == {**CLEAN, "transcripts": [f"agent-{child}.meta.json: {ORPHAN_META}"]}


def test_c1_the_orphan_meta_diagnostics_precede_the_launch_reconciliation(tmp_path):
    main = _session(tmp_path, [_launch("toolu_1", "Agent"), _agent_result("toolu_1", entry=_aid(2))])
    child = _aid(7)
    _write_meta(tmp_path, f"agent-{child}.meta.json")
    assert reader.load_session(main).discovery["transcripts"] == [
        f"agent-{child}.meta.json: {ORPHAN_META}",
        _diag(2, "Agent", "undiscovered"),
    ]


def test_c1_a_view_of_named_subagents_keeps_its_own_child_list(tmp_path):
    main = _session(tmp_path, [])
    _write_meta(tmp_path, f"agent-{_aid(7)}.meta.json")
    assert reader.load_session_view(main, []).discovery == CLEAN


def test_c1_a_directory_named_like_a_meta_file_is_no_child(tmp_path):
    main = _session(tmp_path, [])
    (_subagents(tmp_path) / f"agent-{_aid(7)}.meta.json").mkdir(parents=True)
    assert reader.load_session(main).discovery == CLEAN


def test_c1_a_missing_subagents_directory_is_clean(tmp_path):
    assert reader.load_session(_session(tmp_path, [])).discovery == CLEAN
    assert not _subagents(tmp_path).exists()


# --- C2: a read-time decode or I/O failure ---------------------------------------------------

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
BAD_BYTES = b"\xff\xfe not utf-8\n"


def _jsonl_bytes(entries):
    return "".join(json.dumps(entry) + "\n" for entry in entries).encode("utf-8")


def _padding():
    """Valid lines past the 8 KiB text-read chunk, so a decode error further on
    arrives after earlier lines were already processed."""
    return _jsonl_bytes([_user("pad " + "x" * 100, T0) for _ in range(150)])


def _write_call_entries(agent_id=None):
    """A user line, then a write call whose result never arrives: a valid prefix
    that would put an `attempted` entry in the ledger had the read finished."""
    return [
        _user("Do it.", T0, agent_id),
        _assistant_tool("toolu_w", "Edit", {"file_path": "/tmp/fixa/a.txt"}, T0, agent_id),
    ]


def _assert_unreadable_attempt(attempt, name):
    assert attempt.events == []
    assert attempt.tool_calls == []
    assert attempt.facts is not None and attempt.facts.paths_written == "UNKNOWN"
    assert attempt.ledger is not None
    assert attempt.ledger.entries == []
    assert attempt.ledger.completeness == "partial"
    assert "transcript_unreadable" in attempt.ledger.reasons
    assert "undecodable_line" not in attempt.ledger.reasons
    assert "unexpected_entry_shape" not in attempt.ledger.reasons
    assert attempt._transcript_state.unreadable is True
    assert name


def _unreadable_diagnostics(items, name):
    return [item for item in items if item.startswith(f"{name}: unreadable: ")]


def _workflow_dir(tmp_path, wf_id, transcript_bytes, *, journal=None):
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    agent_id = _aid(1)
    if journal is None:
        journal = "".join(json.dumps(e) + "\n" for e in [
            {"type": "started", "key": "v2:k", "agentId": agent_id, "label": "l", "phase": "P"},
            {"type": "result", "key": "v2:k", "agentId": agent_id, "result": {}},
        ])
    (run_dir / "journal.jsonl").write_bytes(
        journal if isinstance(journal, bytes) else journal.encode("utf-8")
    )
    (run_dir / f"agent-{agent_id}.jsonl").write_bytes(transcript_bytes)
    return run_dir, agent_id


class _FailingHandle:
    """A text handle that yields its lines, then raises OSError mid-iteration."""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None

    def __iter__(self):
        yield from self._lines
        raise OSError("simulated read failure")


def _fail_midway(monkeypatch, name, lines):
    real_open = Path.open

    def fake_open(self, *args, **kwargs):
        if self.name == name:
            return _FailingHandle(lines)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)


def test_c2_an_undecodable_byte_after_a_valid_line_is_an_unreadable_transcript(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow_dir(
        tmp_path, "wf_00000000-f02", _jsonl_bytes(_write_call_entries(_aid(1))) + BAD_BYTES
    )
    run = reader.load_run(run_dir)
    name = f"agent-{agent_id}.jsonl"
    assert run.status == "INCOMPLETE"
    _assert_unreadable_attempt(run.attempts[0], name)
    assert len(_unreadable_diagnostics(run.discovery["transcripts"], name)) == 1
    assert len(_unreadable_diagnostics(run.diagnostics, name)) == 1
    assert run.attempts[0].ledger.reasons[0] == "transcript_unreadable"


@pytest.mark.parametrize("prefix", [
    "not json\n",
    "[1]\n",
    '{"type": "zzz-unknown", "timestamp": "2026-09-01T12:00:00Z"}\n',
], ids=["undecodable_line", "unexpected_shape", "unknown_type"])
def test_c2_everything_the_failed_read_noticed_before_it_failed_is_discarded(
    tmp_path, monkeypatch, prefix
):
    _no_config(monkeypatch, tmp_path)
    # the bad bytes sit past the first 8 KiB, so the lines before them are processed first
    run_dir, agent_id = _workflow_dir(
        tmp_path, "wf_00000000-f02",
        prefix.encode("utf-8") + _jsonl_bytes(_write_call_entries(_aid(1))) + _padding()
        + BAD_BYTES,
    )
    run = reader.load_run(run_dir)
    name = f"agent-{agent_id}.jsonl"
    _assert_unreadable_attempt(run.attempts[0], name)
    assert run.attempts[0].ledger.reasons[0] == "transcript_unreadable"
    assert [r for r in run.attempts[0].ledger.reasons if r != "unassignable_attempt"] == [
        "transcript_unreadable", "discovery_incomplete",
    ]
    assert len(run.discovery["transcripts"]) == 1
    assert run.discovery["transcripts"][0].startswith(f"{name}: unreadable: ")
    assert [d for d in run.diagnostics if d.startswith(name)] == run.discovery["transcripts"]


def test_c2_a_launch_the_failed_main_read_saw_is_discarded_with_it(tmp_path):
    main = _session(tmp_path, [])
    launch = _launch("toolu_1", "Agent")
    main.write_bytes(_jsonl_bytes([_user("go", T0), launch]) + _padding() + BAD_BYTES)
    run = reader.load_session(main)
    assert len(run.discovery["transcripts"]) == 1
    assert run.discovery["transcripts"][0].startswith(f"{main.name}: unreadable: ")


def test_c2_an_oserror_in_the_middle_of_a_read_is_an_unreadable_transcript(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow_dir(tmp_path, "wf_00000000-f02", b"")
    name = f"agent-{agent_id}.jsonl"
    lines = [json.dumps(entry) + "\n" for entry in _write_call_entries(agent_id)]
    _fail_midway(monkeypatch, name, lines)
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"
    _assert_unreadable_attempt(run.attempts[0], name)
    assert _unreadable_diagnostics(run.discovery["transcripts"], name) == [
        f"{name}: unreadable: simulated read failure"
    ]


def test_c2_a_session_main_transcript_that_fails_to_decode_is_unreadable(tmp_path):
    main = _session(tmp_path, [])
    main.write_bytes(_jsonl_bytes(_write_call_entries()) + BAD_BYTES)
    run = reader.load_session(main)
    assert run.status == "INCOMPLETE"
    _assert_unreadable_attempt(run.main, main.name)
    assert len(_unreadable_diagnostics(run.discovery["transcripts"], main.name)) == 1


def test_c2_a_session_subagent_transcript_that_fails_to_decode_is_unreadable(tmp_path):
    child = _aid(4)
    main = _session(tmp_path, [], children=[child])
    sub = _subagents(tmp_path) / f"agent-{child}.jsonl"
    sub.write_bytes(_jsonl_bytes(_write_call_entries(child)) + BAD_BYTES)
    run = reader.load_session(main)
    assert run.status == "INCOMPLETE"
    _assert_unreadable_attempt(run.attempts[0], sub.name)
    assert len(_unreadable_diagnostics(run.discovery["transcripts"], sub.name)) == 1


def test_c2_a_session_whose_first_bytes_do_not_decode_still_loads_and_is_discovered(tmp_path):
    main = _session(tmp_path, [])
    main.write_bytes(BAD_BYTES + _jsonl_bytes([{"type": "user", "sessionId": SESSION}]))
    root = tmp_path / "root"
    assert [ref.kind for ref in reader.discover_runs(root)] == ["session"]
    run = reader.load_run(main)
    assert run.kind == "session"
    assert run.status == "INCOMPLETE"
    _assert_unreadable_attempt(run.main, main.name)


# --- the parent, journal, and meta reads (reported, each guarded already) ---------------------


def test_c2_a_parent_line_that_fails_to_decode_is_a_parent_diagnostic_not_a_crash(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow_dir(tmp_path, "wf_00000000-f02", _jsonl_bytes([_user("x", T0)]))
    parent = _parent_path(tmp_path / "root")
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_bytes(BAD_BYTES)
    run = reader.load_run(run_dir)
    assert run.discovery["parent"] == [f"{parent.name}:1: undecodable parent transcript line"]


def test_c2_a_journal_that_fails_to_decode_is_a_journal_diagnostic_not_a_crash(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow_dir(
        tmp_path, "wf_00000000-f02", _jsonl_bytes([_user("x", T0)]),
        journal=_jsonl_bytes([{"type": "started", "key": "v2:k", "agentId": _aid(1)}]) + BAD_BYTES,
    )
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"
    assert len(run.discovery["journal"]) == 1
    assert run.discovery["journal"][0].startswith("journal.jsonl: undecodable: ")


def test_c2_an_oserror_in_the_middle_of_a_journal_read_is_a_journal_diagnostic(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow_dir(tmp_path, "wf_00000000-f02", _jsonl_bytes([_user("x", T0)]))
    _fail_midway(monkeypatch, "journal.jsonl", ['{"type": "unrelated"}\n'])
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"
    assert run.discovery["journal"] == ["journal.jsonl: unreadable: simulated read failure"]


def test_c2_a_meta_file_that_fails_to_decode_is_a_meta_diagnostic_not_a_crash(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow_dir(tmp_path, "wf_00000000-f02", _jsonl_bytes([_user("x", T0)]))
    (run_dir / f"agent-{agent_id}.meta.json").write_bytes(BAD_BYTES)
    run = reader.load_run(run_dir)
    assert len(run.discovery["meta"]) == 1
    assert run.discovery["meta"][0].startswith(f"agent-{agent_id}.meta.json: ")


# --- S1: a workflow transcript no journal started event names --------------------------------

UNNAMED = "transcript named by no journal started event"



def _workflow_with_journal_agent(tmp_path, wf_id="wf_00000000-f03"):
    run_dir, agent_id = _workflow_dir(tmp_path, wf_id, _jsonl_bytes([_user("x", T0)]))
    return run_dir, agent_id


def _extra(run_dir, name, body=b"{}"):
    path = run_dir / name
    path.write_bytes(body)
    return path


def test_s1_a_transcript_no_started_event_names_is_a_transcripts_diagnostic(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow_with_journal_agent(tmp_path)
    stray = _aid(5)
    _extra(run_dir, f"agent-{stray}.jsonl", _jsonl_bytes([_user("x", T0, stray)]))
    run = reader.load_run(run_dir)
    assert run.discovery == {**CLEAN, "transcripts": [f"agent-{stray}.jsonl: {UNNAMED}"]}
    assert run.risk_population_completeness == "partial"
    assert "discovery_incomplete" in run.risk_population_completeness_reasons
    assert [attempt.agent_id for attempt in run.attempts] == [_aid(1)]
    assert run.diagnostics == []


def test_s1_a_meta_file_no_started_event_names_is_a_transcripts_diagnostic(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow_with_journal_agent(tmp_path)
    stray = _aid(5)
    _extra(run_dir, f"agent-{stray}.meta.json")
    run = reader.load_run(run_dir)
    assert run.discovery == {**CLEAN, "transcripts": [f"agent-{stray}.meta.json: {UNNAMED}"]}


def test_s1_unnamed_files_are_listed_sorted_by_name(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow_with_journal_agent(tmp_path)
    low, high = _aid(3), _aid(9)
    for name in (f"agent-{high}.jsonl", f"agent-{low}.meta.json", f"agent-{low}.jsonl",
                 "agent-x.jsonl"):
        _extra(run_dir, name)
    run = reader.load_run(run_dir)
    assert run.discovery["transcripts"] == [
        f"agent-{low}.jsonl: {UNNAMED}",
        f"agent-{low}.meta.json: {UNNAMED}",
        f"agent-{high}.jsonl: {UNNAMED}",
        f"agent-x.jsonl: {UNNAMED}",
    ]


def test_s1_a_named_transcript_with_its_meta_is_clean(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow_with_journal_agent(tmp_path)
    _extra(run_dir, f"agent-{agent_id}.meta.json", b'{"agentType": "t"}')
    assert reader.load_run(run_dir).discovery == CLEAN


def test_s1_a_non_canonical_agent_id_a_started_event_names_is_not_unnamed(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    journal = _jsonl_bytes([{"type": "started", "key": "v2:k", "agentId": "short"}])
    run_dir, _ = _workflow_dir(tmp_path, "wf_00000000-f03", b"", journal=journal)
    _extra(run_dir, "agent-short.jsonl")
    run = reader.load_run(run_dir)
    # only the helper's own `agent-<aid>.jsonl`, which the journal does not name, is unnamed
    assert run.discovery["transcripts"] == [f"agent-{_aid(1)}.jsonl: {UNNAMED}"]


def test_s1_a_directory_or_a_file_of_another_name_is_no_transcript(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow_with_journal_agent(tmp_path)
    (run_dir / f"agent-{_aid(6)}.jsonl").mkdir()
    _extra(run_dir, "notes.jsonl")
    _extra(run_dir, "journal.jsonl.bak")
    assert reader.load_run(run_dir).discovery == CLEAN


def test_s1_every_transcript_is_unnamed_when_the_journal_is_missing(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow_with_journal_agent(tmp_path)
    (run_dir / "journal.jsonl").unlink()
    run = reader.load_run(reader.RunRef(kind="workflow", path=run_dir, run_id=run_dir.name))
    assert run.discovery["journal"] == ["journal.jsonl: missing"]
    assert run.discovery["transcripts"] == [f"agent-{agent_id}.jsonl: {UNNAMED}"]


# --- S2: an unkeyed workflow attempt is in the risk population -------------------------------


def _completed_generation_run(tmp_path, wf_id, journal_events, transcripts):
    """A workflow run whose one generation completed (launch, result, notification), so
    every attempt that starts after the launch result is assignable."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("".join(json.dumps(entry) + "\n" for entry in [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(T0 + timedelta(seconds=2), _envelope("tA", "toolu_A", "completed")),
    ]), encoding="utf-8")
    (run_dir / "journal.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in journal_events), encoding="utf-8"
    )
    for agent_id, entries in transcripts.items():
        (run_dir / f"agent-{agent_id}.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
        )
    return run_dir


def test_s2_an_unkeyed_workflow_attempt_is_in_the_risk_population(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    keyed, unkeyed = _aid(1), _aid(2)
    start = T0 + timedelta(seconds=10)
    truncated = [_user("go", start, unkeyed), _assistant_tool(
        "toolu_w", "Edit", {"file_path": "/tmp/fixa/a.txt"}, start + timedelta(seconds=1), unkeyed
    )]
    run_dir = _completed_generation_run(
        tmp_path, "wf_00000000-f04",
        [
            {"type": "started", "key": "v2:k", "agentId": keyed, "label": "l", "phase": "P"},
            {"type": "result", "key": "v2:k", "agentId": keyed, "result": {}},
            {"type": "started", "agentId": unkeyed, "label": "l", "phase": "P"},
        ],
        {
            keyed: [_user("go", start, keyed), _assistant_text("done", start, keyed)],
            unkeyed: truncated,
        },
    )
    run = reader.load_run(run_dir)
    by_id = {attempt.agent_id: attempt for attempt in run.attempts}
    assert by_id[unkeyed].key is None and by_id[unkeyed].journal_state is None
    assert by_id[keyed].journal_state == "completed"
    assert run.lifecycle.terminal == "completed"
    assert [a.agent_id for a in ledger.risk_population(run)] == [unkeyed]
    assert run.risk_population_completeness == "complete"


def test_s2_an_unkeyed_attempt_that_finished_normally_is_still_in_the_population(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    unkeyed = _aid(2)
    start = T0 + timedelta(seconds=10)
    run_dir = _completed_generation_run(
        tmp_path, "wf_00000000-f04",
        [{"type": "started", "agentId": unkeyed, "label": "l", "phase": "P"}],
        {unkeyed: [_user("go", start, unkeyed), _assistant_text("done", start, unkeyed)]},
    )
    run = reader.load_run(run_dir)
    assert run.attempts[0].outcome == "completed"
    assert [a.agent_id for a in ledger.risk_population(run)] == [unkeyed]


def test_s2_a_keyed_completed_attempt_in_a_completed_generation_stays_out(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    keyed = _aid(1)
    start = T0 + timedelta(seconds=10)
    run_dir = _completed_generation_run(
        tmp_path, "wf_00000000-f04",
        [
            {"type": "started", "key": "v2:k", "agentId": keyed, "label": "l", "phase": "P"},
            {"type": "result", "key": "v2:k", "agentId": keyed, "result": {}},
        ],
        {keyed: [_user("go", start, keyed), _assistant_text("done", start, keyed)]},
    )
    run = reader.load_run(run_dir)
    assert ledger.risk_population(run) == []
    assert run.risk_population_completeness == "complete"


def test_s2_the_unkeyed_rule_never_applies_to_a_session(tmp_path):
    child = _aid(4)
    main = _session(tmp_path, [], children=[child])
    run = reader.load_session(main)
    assert run.attempts[0].journal_state is None
    assert ledger.risk_population(run) == []


def test_s1_unnamed_files_follow_the_attempt_transcript_diagnostics(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow_dir(
        tmp_path, "wf_00000000-f03", b"[1]\n" + _jsonl_bytes([_user("x", T0)])
    )
    stray = _aid(5)
    _extra(run_dir, f"agent-{stray}.jsonl")
    run = reader.load_run(run_dir)
    assert run.discovery["transcripts"] == [
        f"agent-{agent_id}.jsonl:1: not a JSON object",
        f"agent-{stray}.jsonl: {UNNAMED}",
    ]
