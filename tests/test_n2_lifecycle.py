"""N2 acceptance tests: the run lifecycle (docs/DESIGN.md 4.1, 4.13.3).

Every scenario below is named after the run lifecycle table row (or the
extra acceptance case) it exercises, and asserts the complete Lifecycle (all
five fields: terminal, source, activity, generations, diagnostics), per
4.13.3's own "N2 asserts the complete Lifecycle (all five fields) for each
row of the run lifecycle table, a zero-generation legacy run (resolved and
unresolved journal), an older failed generation followed by an unfinished
resume, and a session (null)."

Builders are imported from tests.test_field_v03 (CLAUDE.md: never copy a
fixture builder another test file already has). Every wf_ id used here is
already registered in tests/synthetic-ids.txt (reused from test_field_v03's
own allocation, f01-f10/f0a-f0e), except wf_00000000-e20 through -e2f, newly
registered below for this file's own scenarios, plus wf_00000000-e2e and
wf_00000000-e30 (N2 review Finding 1: the two parent-inspection shape
sub-rules, assistant tool_use.name and queue-operation enqueue content,
that no fixture exercised before).

Activity assertions use real, unbackdated mtimes ('recent') or os.utime to
push a file's mtime far into the past ('stale') -- the same convention
test_scan_triage.py already uses, since reader.activity_word compares
against the real wall clock, never report._now()'s injectable one (that
clock only ever governs human-facing relative ages computed from parsed
transcript timestamps, never file freshness).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, hooks_cli, reader
from tests.test_field_v03 import (
    FAKE_SLUG,
    FAKE_UUID,
    _aid,
    _assistant_text,
    _envelope,
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

BASE = datetime.now(timezone.utc) - timedelta(minutes=10)


def _lifecycle(run):
    lifecycle = run.lifecycle
    assert lifecycle is not None
    return lifecycle


def _age(wf_dir: Path, seconds_ago: float) -> None:
    """Push every file directly inside wf_dir back by seconds_ago, so the
    run's own activity reads 'stale' rather than the natural 'recent' a
    freshly-written fixture gets."""
    when = datetime.now(timezone.utc).timestamp() - seconds_ago
    for path in wf_dir.iterdir():
        if path.is_file():
            os.utime(path, (when, when))


def _basic_run(tmp_path: Path, monkeypatch, wf_id: str, agent_n: int):
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(agent_n)
    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", BASE, agent_id),
        _assistant_text("Done.", BASE + timedelta(seconds=30), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)
    return root, run_dir


def _unresolved_run(tmp_path: Path, monkeypatch, wf_id: str, agent_n: int):
    """A journal whose only key is still open (no 'result'/'failed' event):
    journal_resolved is False."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(agent_n)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", BASE, agent_id),
        _assistant_text("Working on it.", BASE + timedelta(seconds=5), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)
    return root, run_dir


# --- activity: the exact 10-minute boundary ---------------------------------
#
# Every other activity assertion in this file goes through a real file mtime
# (fresh for 'recent', os.utime'd an hour back for 'stale'), which exercises
# _workflow_activity's file-scanning but never pins the exact threshold
# reader.ACTIVITY_RECENT_SECONDS (600.0) names. reader.activity_word takes an
# injectable `now` for exactly this: a direct, no-filesystem unit test of the
# boundary docs/DESIGN.md 4.13.3 states ("recent ... under 10 minutes old,
# stale when older").


def test_activity_word_boundary_is_exactly_10_minutes():
    now = 2_000_000.0
    assert reader.activity_word(now - 599.0, now) == "recent"
    assert reader.activity_word(now - 600.0, now) == "stale"
    assert reader.activity_word(now - 600.001, now) == "stale"
    assert reader.activity_word(None, now) == "unknown"


# --- run lifecycle table, row by row ----------------------------------------


def test_parent_inspection_incomplete_is_unknown_parent_unavailable(tmp_path, monkeypatch):
    """A parent transcript that EXISTS but carries an entry violating
    4.13.3's own shape rules (here: a 'user' entry whose own 'message' is
    not an object at all) makes parent inspection incomplete -- distinct
    from a parent transcript that simply does not exist yet, which is
    tolerated (docs/DESIGN.md 4.2/4.6's 'unreadable, not merely absent'
    convention, applied the same way here)."""
    wf_id = "wf_00000000-e20"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 201)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        '{"type":"user","message":"not-an-object","timestamp":"2026-09-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.activity in ("recent", "stale", "unknown")
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_parent_inspection_incomplete_assistant_tool_use_missing_name_is_unknown_parent_unavailable(
    tmp_path, monkeypatch,
):
    """4.13.3's shape rule: 'every tool_use block has string id and name.'
    An assistant tool_use block with a string id but no name at all is the
    half of that rule the bare-string-message case above never exercises
    (N2 review Finding 1: this sub-rule had no fixture at all, so a future
    refactor dropping the name check would go uncaught)."""
    wf_id = "wf_00000000-e2e"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 215)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"toolu_X","input":{}}],"usage":{}},'
        '"timestamp":"2026-09-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_parent_inspection_incomplete_queue_operation_enqueue_missing_content_is_unknown_parent_unavailable(
    tmp_path, monkeypatch,
):
    """4.13.3's shape rule: 'a queue-operation entry has a string operation,
    and a string content when operation is enqueue.' A queue-operation entry
    with operation 'enqueue' and no content at all is the other sub-rule no
    fixture exercised before (N2 review Finding 1)."""
    wf_id = "wf_00000000-e30"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 216)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        '{"type":"queue-operation","operation":"enqueue",'
        '"timestamp":"2026-09-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_parent_inspection_incomplete_tool_result_missing_tool_use_id_is_unknown_parent_unavailable(
    tmp_path, monkeypatch,
):
    """4.13.3's shape rule: 'every tool_result block has a string
    tool_use_id.' A tool_result block with no tool_use_id at all (N1N2
    review Finding 5, mutation M15: accepting this was never caught by any
    existing lifecycle test)."""
    wf_id = "wf_00000000-e31"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 217)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        '{"type":"user","message":{"role":"user","content":'
        '[{"type":"tool_result","content":"x"}]},'
        '"timestamp":"2026-09-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_parent_inspection_incomplete_assistant_content_not_a_list_is_unknown_parent_unavailable(
    tmp_path, monkeypatch,
):
    """4.13.3's shape rule: 'an assistant entry has a dict message whose
    content is a list.' An assistant entry whose content is a bare string,
    not a list at all (N1N2 review Finding 5, mutation M16)."""
    wf_id = "wf_00000000-e32"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 218)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        '{"type":"assistant","message":{"role":"assistant","content":"not-a-list","usage":{}},'
        '"timestamp":"2026-09-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_parent_inspection_incomplete_queue_operation_non_string_operation_is_unknown_parent_unavailable(
    tmp_path, monkeypatch,
):
    """4.13.3's shape rule: 'a queue-operation entry has a string
    operation.' A queue-operation entry whose operation is not a string at
    all (N1N2 review Finding 5, mutation M17)."""
    wf_id = "wf_00000000-e33"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 219)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        '{"type":"queue-operation","operation":42,"timestamp":"2026-09-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_parent_malformed_json_line_is_unknown_parent_unavailable(tmp_path, monkeypatch):
    """4.13.3's Run lifecycle paragraph: parent inspection is complete only
    when the parent transcript 'has no undecodable line.' A line that is
    simply not valid JSON at all (N1N2 review Finding 5). (N1N2 review round
    2, original finding 1 PARTLY: the bad line must also become a Run
    diagnostic, docs/DESIGN.md section 2.)"""
    wf_id = "wf_00000000-e34"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 220)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("{not valid json\n", encoding="utf-8")

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False
    assert f"{parent.name}:1: undecodable parent transcript line" in run.diagnostics


def test_parent_undecodable_byte_line_is_unknown_parent_unavailable(tmp_path, monkeypatch):
    """Same rule as above, the decode-failure half: a line that is not even
    valid UTF-8 (N1N2 review Finding 5)."""
    wf_id = "wf_00000000-e35"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 221)
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_bytes(b"\x80\x81\x82\n")

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert lifecycle.generations == 0
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_ambiguous_launch_involving_x_is_unknown_ambiguous_launch(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e21"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 202)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch("toolu_A", BASE + timedelta(seconds=1)),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=2), wf_id, "tA"),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "ambiguous_launch"
    assert lifecycle.generations == 0
    assert lifecycle.diagnostics == [
        "ambiguous launch 'toolu_A': lines 1, 2, 3",
    ]
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_ambiguous_launch_not_involving_x_is_ignored(tmp_path, monkeypatch):
    """4.13.3's association rule 1: an ambiguous launch whose candidate run
    ids never name X leaves X alone entirely -- zero generations, terminal
    decided by the ordinary journal_resolved/none rule, no diagnostic."""
    wf_id = "wf_00000000-e22"
    other_wf_id = "wf_00000000-e2f"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 203)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch("toolu_A", BASE + timedelta(seconds=1)),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=2), other_wf_id, "tA"),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "journal_resolved"
    assert lifecycle.source == "journal"
    assert lifecycle.generations == 0
    assert lifecycle.diagnostics == []
    assert run.status == "COMPLETE"
    assert run.finished is True


def test_incomplete_generation_is_unknown_incomplete_generation(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e23"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 204)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_B", BASE, resume_from=wf_id),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "incomplete_generation"
    assert lifecycle.generations == 1
    assert lifecycle.diagnostics == ["launch without result at line 1"]
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_newest_generation_terminal_completed(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e24"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 205)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "completed"
    assert lifecycle.source == "notification"
    assert lifecycle.generations == 1
    assert lifecycle.diagnostics == []
    assert run.status == "COMPLETE"
    assert run.finished is True


def test_newest_generation_terminal_failed(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e25"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 206)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "failed")),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "failed"
    assert lifecycle.source == "notification"
    assert lifecycle.generations == 1
    assert lifecycle.diagnostics == []
    assert run.status == "COMPLETE"
    # docs/DESIGN.md 4.13.3: Run.finished is true for a 'failed' terminal --
    # the harness gave a definite word, even though that word was failure.
    assert run.finished is True


def test_newest_generation_terminal_stopped(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e26"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 207)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "stopped")),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "stopped"
    assert lifecycle.source == "notification"
    assert lifecycle.generations == 1
    assert run.status == "COMPLETE"
    assert run.finished is True


def test_newest_generation_terminal_unknown_unrecognized_status(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e27"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 208)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result(
            "toolu_A", BASE + timedelta(seconds=1), wf_id, "tA", status="weird_status",
        ),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "unrecognized_status"
    assert lifecycle.generations == 1
    assert lifecycle.diagnostics == ["unrecognized launch status"]
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_newest_generation_terminal_unknown_conflict(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e28"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 209)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
        _notification_entry(BASE + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed")),
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "conflict"
    assert lifecycle.generations == 1
    assert lifecycle.diagnostics != []
    assert run.status == "INCOMPLETE"
    assert run.finished is False


def test_zero_generation_legacy_run_resolved_journal_is_journal_resolved(tmp_path, monkeypatch):
    """No Workflow launch ever recorded for this run at all (a legacy run,
    or simply one whose parent never launched it the way the other
    scenarios do) -- 0 generations -- but the journal itself fully resolved,
    so the lifecycle table's 'no generation exists, and every journal key is
    resolved' row applies."""
    wf_id = "wf_00000000-e29"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 210)
    # No parent transcript at all: the standard-layout path is reachable but
    # the file itself was never written (docs/DESIGN.md 4.2/4.6's "not
    # merely absent" convention -- tolerated, not "incomplete").

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "journal_resolved"
    assert lifecycle.source == "journal"
    assert lifecycle.activity in ("recent", "stale", "unknown")
    assert lifecycle.generations == 0
    assert lifecycle.diagnostics == []
    assert run.status == "COMPLETE"
    assert run.finished is True


def test_zero_generation_legacy_run_unresolved_journal_is_none(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e2a"
    root, run_dir = _unresolved_run(tmp_path, monkeypatch, wf_id, 211)
    _age(run_dir, 3600)  # 1 hour stale, so activity is deterministically 'stale'

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "none"
    assert lifecycle.source is None
    assert lifecycle.activity == "stale"
    assert lifecycle.generations == 0
    assert lifecycle.diagnostics == []
    assert run.status == "COMPLETE"
    assert run.finished is False


def test_older_failed_generation_followed_by_unfinished_resume_is_none(tmp_path, monkeypatch):
    """4.13.3: 'An earlier generation's terminal never decides the run while
    a later one exists.' The newest generation (the resume) has no terminal
    evidence yet -- pending, not failed -- so the run's own lifecycle
    terminal is 'none', never shadowed by the first generation's own
    'failed'. The journal itself is also unresolved here (its one key never
    closes): were it resolved instead, the table's own 'N exists with no
    terminal ... and every journal key is resolved' row would make this
    journal_resolved, not none -- a pending generation does not, by itself,
    override a resolved journal (that is a different row of the table)."""
    wf_id = "wf_00000000-e2b"
    root, run_dir = _unresolved_run(tmp_path, monkeypatch, wf_id, 212)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "failed")),
        _workflow_launch("toolu_B", BASE + timedelta(minutes=2), resume_from=wf_id),
        _workflow_launch_result("toolu_B", BASE + timedelta(minutes=2, seconds=1), wf_id, "tB"),
        # No notification yet for tB: the resume is still running.
    ])

    run = reader.load_run(run_dir)
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "none"
    assert lifecycle.source is None
    assert lifecycle.generations == 2
    assert lifecycle.diagnostics == []
    assert run.status == "COMPLETE"
    assert run.finished is False


def test_session_lifecycle_and_finished_are_both_none(tmp_path, monkeypatch):
    root = tmp_path / "root"
    session_path = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps({
            "type": "user", "message": {"role": "user", "content": "hi"},
            "sessionId": FAKE_UUID, "timestamp": BASE.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }) + "\n",
        encoding="utf-8",
    )
    _no_config(monkeypatch, tmp_path)

    run = reader.load_run(session_path)
    assert run.kind == "session"
    assert run.lifecycle is None
    assert run.finished is None
    assert run.finished is None


# --- N1N2 review findings 1 and 7: ParentIndex never raises for a parent ---
# read/decode failure, and parent inspection completeness is computed inside
# ParentIndex's own incremental pass rather than by a second, independent
# full re-read of the whole file (docs/DESIGN.md 4.13.3).


def _permission_denied_open_for(target: Path):
    """A Path.open replacement that raises PermissionError only for `target`
    (the exact parent transcript path), passing every other path through to
    the real implementation unchanged -- so journal/meta/transcript reads for
    the run under test still work normally."""
    real_open = Path.open

    def fake_open(self, *args, **kwargs):
        if self == target:
            raise PermissionError("permission denied (synthetic, test-only)")
        return real_open(self, *args, **kwargs)

    return fake_open


def test_unreadable_existing_parent_is_unknown_parent_unavailable_and_never_raises(
    tmp_path, monkeypatch,
):
    """(N1N2 review round 2, original finding 1 PARTLY: an unreadable parent
    must also become a stable, file-level Run diagnostic, docs/DESIGN.md
    section 2.)"""
    wf_id = "wf_00000000-e3a"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 226)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
    ])
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))

    run = reader.load_run(run_dir)  # must not raise
    lifecycle = _lifecycle(run)
    assert lifecycle.terminal == "unknown"
    assert lifecycle.source == "parent_unavailable"
    assert run.finished is False
    assert run.status == "INCOMPLETE"
    assert f"{parent.name}: parent transcript unreadable" in run.diagnostics


def test_unreadable_existing_parent_scan_does_not_raise(tmp_path, monkeypatch, capsys):
    wf_id = "wf_00000000-e3b"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 227)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
    ])
    # Stale, so the hook-free "possibly live" heuristic does not take
    # priority over the lifecycle-derived unfinished_state this test means
    # to pin (docs/DESIGN.md 4.13.3/5.3b: live wins first when true).
    _age(run_dir, 3600)
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))

    exit_code = cli.main(["scan", "--root", str(root), "--json"])  # must not raise
    payload = json.loads(capsys.readouterr().out)
    assert exit_code in (0, 3, 4)
    assert payload["rows"][0]["run_id"] == wf_id
    assert payload["rows"][0]["finished"] is False
    assert payload["rows"][0]["unfinished_state"] == "unknown"


def test_unreadable_existing_parent_hook_workflow_finished_does_not_raise(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e3c"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 228)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
    ])
    monkeypatch.setattr(Path, "open", _permission_denied_open_for(parent))

    assert hooks_cli._workflow_finished(run_dir / "journal.jsonl") is False  # must not raise


def test_parent_not_fully_reread_for_completeness_across_three_workflows_sharing_one_parent(
    tmp_path, monkeypatch,
):
    """Path.read_bytes() is reader.py's one and only call for a full,
    non-incremental read of a parent transcript (the old, now-removed
    _parent_inspection_complete); ParentIndex's own incremental pass never
    calls it (it uses Path.open()/handle.read() instead). Three workflows
    sharing one parent, each loaded separately, must therefore see zero
    calls to it -- the shared ParentIndex cache's own incremental pass is
    the only thing that ever touches the file's bytes."""
    root = tmp_path / "root"
    wf_ids = ["wf_00000000-e36", "wf_00000000-e37", "wf_00000000-e38"]
    run_dirs = []
    for offset, wf_id in enumerate(wf_ids):
        run_dir = _wf_dir(root, wf_id)
        agent_id = _aid(230 + offset)
        _simple_completed_journal(run_dir, agent_id)
        _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
            _user("Do the thing.", BASE, agent_id),
            _assistant_text("Done.", BASE + timedelta(seconds=30), agent_id),
        ])
        run_dirs.append(run_dir)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_ids[0], "tA"),
        _workflow_launch("toolu_B", BASE + timedelta(seconds=2)),
        _workflow_launch_result("toolu_B", BASE + timedelta(seconds=3), wf_ids[1], "tB"),
        _workflow_launch("toolu_C", BASE + timedelta(seconds=4)),
        _workflow_launch_result("toolu_C", BASE + timedelta(seconds=5), wf_ids[2], "tC"),
    ])
    _no_config(monkeypatch, tmp_path)

    calls = {"n": 0}
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self, *args, **kwargs):
        if self == parent:
            calls["n"] += 1
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    for run_dir in run_dirs:
        reader.load_run(run_dir)

    assert calls["n"] == 0, (
        f"the parent transcript was fully re-read {calls['n']} extra time(s) outside "
        "ParentIndex's own incremental pass"
    )


def test_journal_read_exactly_once_per_load_run(tmp_path, monkeypatch):
    wf_id = "wf_00000000-e39"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 225)

    calls = {"n": 0}
    original = reader._read_journal

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, "_read_journal", counting)

    reader.load_run(run_dir)
    assert calls["n"] == 1, f"_read_journal was called {calls['n']} times for one load_run()"


# --- N1N2 review round 2, new finding 1: a transient parent read failure ---
# must not permanently poison completeness (docs/DESIGN.md 4.13.3's parent
# index cache paragraph). The bug: once a read failure anywhere in
# ParentIndex._refresh's own stat/rehash/reparse steps flips
# inspection_complete False, nothing ever sets it back to True unless the
# very next access also happens to trigger a rebuild-from-zero (a real
# shrink, inode change, or content rewrite) -- a one-time, non-recurring I/O
# hiccup (the file itself never changed) leaves the cached index permanently
# incomplete for the rest of the process. Each test below injects exactly one
# such failure at a different point, lets it clear up on its own, and asserts
# both that ParentIndex.inspection_complete comes back True and that the
# run's own Lifecycle terminal (read through a fresh load_run()) is the file's
# true, current terminal -- never stuck at 'unknown'/'parent_unavailable'.


def _raise_once_then_call(monkeypatch, owner, method_name: str, *, target=None):
    """Monkeypatch owner.method_name so its next call -- the first one for
    `target` specifically when `target` is given, otherwise simply the first
    call at all -- raises a synthetic OSError, and every call after that runs
    the original implementation unchanged: a one-time transient failure that
    clears up on its own, never recurring."""
    real = getattr(owner, method_name)
    state = {"raised": False}

    def wrapper(self, *args, **kwargs):
        if not state["raised"] and (target is None or self == target):
            state["raised"] = True
            raise OSError("synthetic, test-only, one-shot failure")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(owner, method_name, wrapper)


_UNREADABLE = "parent transcript unreadable"


def _has_unreadable(diagnostics) -> bool:
    """Whether a diagnostics list still carries ParentIndex's file-level
    'parent transcript unreadable' diagnostic (N1N2 Codex round 3, MINOR 1:
    recovery must clear it, and it must not reappear)."""
    return any(_UNREADABLE in d for d in diagnostics)


def test_recovery_after_one_time_stat_failure_restores_inspection_complete_and_true_terminal(
    tmp_path, monkeypatch,
):
    wf_id = "wf_00000000-e40"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 240)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
    ])
    _raise_once_then_call(monkeypatch, Path, "stat", target=parent)

    index = reader.ParentIndex.for_path(parent)
    assert index.inspection_complete is False

    index2 = reader.ParentIndex.for_path(parent)  # stat succeeds now: must fully recover
    assert index2 is index
    assert index2.inspection_complete is True
    assert not _has_unreadable(index2.diagnostics)
    assert len(index2.generations) == 1

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "completed"
    assert run.lifecycle.source == "notification"
    assert run.finished is True
    assert run.status == "COMPLETE"


def test_recovery_after_one_time_initial_parse_failure_restores_inspection_complete_and_true_terminal(
    tmp_path, monkeypatch,
):
    wf_id = "wf_00000000-e41"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 241)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
    ])
    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_parse_increment")

    index = reader.ParentIndex.for_path(parent)  # the very first scan ever, fails once
    assert index.inspection_complete is False
    assert index.generations == []

    index2 = reader.ParentIndex.for_path(parent)  # the retry succeeds
    assert index2 is index
    assert index2.inspection_complete is True
    assert not _has_unreadable(index2.diagnostics)
    assert len(index2.generations) == 1

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "completed"
    assert run.finished is True
    assert run.status == "COMPLETE"


def test_recovery_after_one_time_prefix_hash_failure_restores_inspection_complete_and_true_terminal(
    tmp_path, monkeypatch,
):
    """The reviewer's own repro: parse a valid parent successfully, make the
    next prefix read raise once, then restore normal reads without changing
    the file at all -- no shrink, no rewrite, nothing to re-trigger a rebuild
    on its own."""
    wf_id = "wf_00000000-e42"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 242)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
    ])
    baseline = reader.ParentIndex.for_path(parent)
    assert baseline.inspection_complete is True
    assert len(baseline.generations) == 1

    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_hash_prefix")

    index = reader.ParentIndex.for_path(parent)  # nothing changed on disk; only the hash fails
    assert index is baseline
    assert index.inspection_complete is False

    index2 = reader.ParentIndex.for_path(parent)  # the retry succeeds
    assert index2 is baseline
    assert index2.inspection_complete is True
    assert not _has_unreadable(index2.diagnostics)
    assert len(index2.generations) == 1

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "completed"
    assert run.finished is True
    assert run.status == "COMPLETE"


def test_recovery_after_one_time_suffix_parse_failure_restores_inspection_complete_and_true_terminal(
    tmp_path, monkeypatch,
):
    wf_id = "wf_00000000-e43"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 243)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
    ])
    baseline = reader.ParentIndex.for_path(parent)
    assert baseline.inspection_complete is True
    assert baseline.generations[0].terminal is None  # async_launched, no evidence yet

    with parent.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed"))
        ) + "\n")

    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_parse_increment")

    index = reader.ParentIndex.for_path(parent)  # reading the appended suffix fails once
    assert index is baseline
    assert index.inspection_complete is False

    index2 = reader.ParentIndex.for_path(parent)  # the retry succeeds
    assert index2 is baseline
    assert index2.inspection_complete is True
    assert not _has_unreadable(index2.diagnostics)
    assert index2.generations[0].terminal == "completed"

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "completed"
    assert run.finished is True
    assert run.status == "COMPLETE"


def test_recovery_after_one_time_rebuild_parse_failure_restores_inspection_complete_and_true_terminal(
    tmp_path, monkeypatch,
):
    """Unlike the four scenarios above, a shrink (or any other trigger that
    makes needs_reset True on its own) already self-heals without this fix:
    the shrink keeps being detected on every retry until one succeeds, so
    this one already passes before the production change below -- it stays
    here as regression coverage for the no-fix-needed row of the same new
    finding 1, not as a fifth RED case."""
    wf_id = "wf_00000000-e44"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 244)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "failed")),
    ])
    baseline = reader.ParentIndex.for_path(parent)
    assert baseline.generations[0].terminal == "failed"

    # Shrink back to just launch+result, same inode: normally forces a
    # rebuild from zero.
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
    ])
    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_parse_increment")

    index = reader.ParentIndex.for_path(parent)  # the rebuild's own read fails once
    assert index is baseline
    assert index.inspection_complete is False
    # The failed rebuild must not discard the prior (pre-shrink) state (N1N2
    # fix review finding 1, unaffected by this newer fix).
    assert index.generations[0].terminal == "failed"

    index2 = reader.ParentIndex.for_path(parent)  # the retry succeeds
    assert index2 is baseline
    assert index2.inspection_complete is True
    assert not _has_unreadable(index2.diagnostics)
    # The successful retry reflects the now-shrunk file's own true content:
    # the notification is gone, so there is no terminal evidence yet.
    assert index2.generations[0].terminal is None

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "journal_resolved"
    assert run.finished is True
    assert run.status == "COMPLETE"


def test_recovered_unreadable_diagnostic_is_cleared_and_never_reappears_on_the_run(
    tmp_path, monkeypatch,
):
    """N1N2 Codex round 3, MINOR 1: after a one-time read failure the
    file-level diagnostic is present; after the successful retry it is gone
    from the index, stays gone on a further unchanged refresh, and never
    reaches Run.diagnostics."""
    wf_id = "wf_00000000-e45"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 245)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
    ])
    baseline = reader.ParentIndex.for_path(parent)
    assert not _has_unreadable(baseline.diagnostics)

    _raise_once_then_call(monkeypatch, reader.ParentIndex, "_hash_prefix")
    failed = reader.ParentIndex.for_path(parent)
    assert _has_unreadable(failed.diagnostics)

    recovered = reader.ParentIndex.for_path(parent)
    assert recovered.inspection_complete is True
    assert not _has_unreadable(recovered.diagnostics)

    again = reader.ParentIndex.for_path(parent)  # unchanged file, plain refresh
    assert not _has_unreadable(again.diagnostics)

    run = reader.load_run(run_dir)
    assert not _has_unreadable(run.diagnostics)
    assert run.lifecycle.terminal == "completed"


# --- hooks_cli's workflow-unit "finished" must equal Run.finished ---------
# (N2 root-cause fix, docs/DESIGN.md section 3 / 7.2)
#
# Before this fix, hooks_cli._workflow_finished derived "finished" from the
# journal alone (reader.workflow_journal_finished), while Run.finished above
# already came from the full Lifecycle (parent index first, journal only as
# a fallback). The two rules can disagree in either direction:
#
# - the parent can report a terminal (failed/stopped) before the journal's
#   own open key ever closes -- `run` already says FAILED; the old
#   journal-only hook rule still said "not finished" and would wait out the
#   quiet threshold instead of reporting it promptly;
# - an ambiguous launch can make the lifecycle `unknown` even though the
#   journal itself is fully resolved -- `run` already says finished=False;
#   the old journal-only hook rule said "finished" regardless.
#
# reader.workflow_lifecycle/reader.lifecycle_finished are now the one shared
# rule hooks_cli._workflow_finished calls, so these two tests pin both
# directions down at the hook's own call site, never only at
# reader.load_run's (which test_n2_lifecycle.py's table tests above already
# covered for the 'failed' row).


def test_hook_workflow_finished_true_when_parent_reports_failed_over_an_open_journal_key(
    tmp_path, monkeypatch,
):
    """The concrete divergence docs/DESIGN.md section 3/7.2 call out: the
    parent already reports the workflow failed while the journal's one key
    is still open (no 'result'/'failed' event of its own). `run` already
    reports this correctly via N2's Lifecycle; the hook must now agree
    instead of treating the unit as still running."""
    wf_id = "wf_00000000-e2c"
    root, run_dir = _unresolved_run(tmp_path, monkeypatch, wf_id, 213)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_F", BASE),
        _workflow_launch_result("toolu_F", BASE + timedelta(seconds=1), wf_id, "tF"),
        _notification_entry(BASE + timedelta(minutes=1), _envelope("tF", "toolu_F", "failed")),
    ])

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "failed"
    assert run.finished is True

    assert hooks_cli._workflow_finished(run_dir / "journal.jsonl") is True


def test_hook_workflow_finished_false_when_journal_resolved_but_launch_is_ambiguous(
    tmp_path, monkeypatch,
):
    """The opposite divergence: the journal itself is fully resolved, but an
    ambiguous launch makes the run's own lifecycle `unknown` (an incomplete
    parent inspection, tested in test_parent_inspection_incomplete_is_
    unknown_parent_unavailable above, makes the same point the same way).
    `run` already reports finished=False for this (N2); a journal-only hook
    rule used to disagree and call the unit finished."""
    wf_id = "wf_00000000-e2d"
    root, run_dir = _basic_run(tmp_path, monkeypatch, wf_id, 214)
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", BASE),
        _workflow_launch("toolu_A", BASE + timedelta(seconds=1)),
        _workflow_launch_result("toolu_A", BASE + timedelta(seconds=2), wf_id, "tA"),
    ])

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "unknown"
    assert run.lifecycle.source == "ambiguous_launch"
    assert run.finished is False

    assert hooks_cli._workflow_finished(run_dir / "journal.jsonl") is False


# --- property: the hook's decision equals load_run(...).finished, over ----
# every workflow fixture under tests/fixtures/runs and the packaged demo run


FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
DEMO_ROOT = Path(reader.__file__).parent / "demo"


def _all_workflow_run_dirs() -> list[Path]:
    """Every workflow run directory any fixture case holds: a flat case
    directory that is itself a workflow run (docs/DESIGN.md 6: a case
    directory is its own root; most tests/fixtures/runs cases, e.g.
    clean_run, are shaped this way), or one discovered under a case
    directory's own project/session/subagents/workflows layout (e.g.
    hook_session's wf_00000000-710/-720), plus the packaged demo run."""
    case_dirs = sorted(p for p in FIXTURES_ROOT.iterdir() if p.is_dir())
    case_dirs += [DEMO_ROOT]
    dirs: list[Path] = []
    for case_dir in case_dirs:
        if reader._is_workflow_dir(case_dir):
            dirs.append(case_dir)
        for ref in reader.discover_runs(case_dir, kinds=frozenset({"workflow"})):
            dirs.append(ref.path)
    return dirs


_ALL_WORKFLOW_RUN_DIRS = _all_workflow_run_dirs()


@pytest.mark.parametrize(
    "run_dir", _ALL_WORKFLOW_RUN_DIRS, ids=[str(p) for p in _ALL_WORKFLOW_RUN_DIRS],
)
def test_hook_finished_parity_over_every_fixture_workflow(run_dir):
    """docs/DESIGN.md section 3: 'One function ... is the only definition of
    this rule.' Over every workflow fixture this repository ships (including
    ones that predate N2 and carry no parent transcript at all, where the
    old journal-only rule and the new lifecycle-based one necessarily agree
    already), hooks_cli._workflow_finished's decision must equal
    reader.load_run(...).finished exactly -- never merely 'usually agree'."""
    run = reader.load_run(run_dir)
    hook_finished = hooks_cli._workflow_finished(run_dir / "journal.jsonl")
    assert hook_finished == run.finished, (
        f"{run_dir}: hooks_cli._workflow_finished={hook_finished} "
        f"but reader.load_run(...).finished={run.finished}"
    )
