"""G-A1 (fix round for G-A, docs/DESIGN.md 7.2): GA-2 a payload `finished` stays hook evidence and
the digest re-opens the unit when the notification arrives, GA-3 the fingerprint carries the
current return decision and unreadable tokens, GA-6 an unknown state is not terminal, GA-8 a
re-opened unit re-evaluates its finished evidence at once, GA-12 the sentinel and nested-launcher
rows.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agent_postmortem import hooks_cli, reader, returns
from agent_postmortem.cli import main
from tests.g_helpers import (
    SESSION,
    Session,
    X,
    Y,
    append_lines,
    async_launch,
    child_entries,
    iso,
    note,
    queued,
    result,
    resumed,
    send,
    sync_launch,
)

SENTINEL = "[Request interrupted by user]"


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def check(monkeypatch, capsys, s, **extra):
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(s.main_path),
        "session_id": "s-g-a1-hooks", "stop_hook_active": False, **extra,
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main(["hook", "check"])
    return capsys.readouterr().out


def unit(s, child):
    state = Path(os.environ["XDG_STATE_HOME"]) / "agent-postmortem" / "state.json"
    data = json.loads(state.read_text())
    key = hooks_cli._session_state_key(s.root, SESSION)
    return data["sessions"][key]["units"][f"subagents/agent-{child}.jsonl"]


def fingerprint(s, child):
    return hooks_cli._unit_fingerprint("subagent", s.child_path(child), s.main_path, {})


# --- GA-8: a re-opened unit re-evaluates its finished evidence at once ----------------------------

UNCHANGED_RETURN = {
    "rejected": [send("ts", 10, X, recipient=X), result("ts", 11, tur="no such agent", is_error=True)],
    "queued": [send("ts", 10, X, recipient=X), queued("ts", 11)],
}


@pytest.mark.parametrize("name", sorted(UNCHANGED_RETURN))
def test_a_request_that_leaves_the_child_returned_analyzes_the_unit_again_at_once(
    monkeypatch, capsys, tmp_path, name,
):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    first = unit(s, X)
    assert first["state"] == "analyzed"
    append_lines(s.main_path, UNCHANGED_RETURN[name])
    check(monkeypatch, capsys, s)
    again = unit(s, X)
    assert again["fingerprint"] != first["fingerprint"]
    assert again["state"] == "analyzed"
    for _ in range(4):
        check(monkeypatch, capsys, s)
    final = unit(s, X)
    assert final["state"] == "analyzed"
    assert "INCOMPLETE" not in final["verdict"]


def test_a_request_with_no_result_reopens_and_stays_open(monkeypatch, capsys, tmp_path):
    """delivery_unknown is not terminal: the re-evaluation leaves the unit running."""
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    append_lines(s.main_path, [send("ts", 10, X, recipient=X)])
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "running"


def test_a_replaced_terminal_status_analyzes_the_unit_again(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    s.main(async_launch("t1", X, 1), note(30, X, "t1", "failed")).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"
    text = s.main_path.read_text(encoding="utf-8")
    s.main_path.write_text(text.replace("<status>failed", "<status>killed"), encoding="utf-8")
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"


def test_a_resume_in_progress_reopens_and_stays_open(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    append_lines(s.main_path, [send("ts", 10, X, recipient=X), resumed("ts", 11, X)])
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "running"


# --- GA-2: the payload's finished status stays evidence; the notification re-opens the unit -------


def test_a_pending_child_finished_by_the_payload_is_reopened_by_its_failed_notification(
    monkeypatch, capsys, tmp_path,
):
    s = Session(tmp_path)
    s.main(async_launch("t1", X, 1)).child(X)
    s.build()
    assert reader.load_session(s.main_path).attempts[0].return_state == "pending"
    out = check(monkeypatch, capsys, s, background_tasks=[{"agent_id": X, "status": "completed"}])
    first = unit(s, X)
    assert first["state"] == "analyzed"
    assert "DEGRADED" not in out
    append_lines(s.main_path, [note(30, X, "t1", "failed")])
    out = check(monkeypatch, capsys, s)
    again = unit(s, X)
    assert again["fingerprint"] != first["fingerprint"]
    assert again["state"] == "analyzed"
    assert "DEGRADED" in out
    assert again["reported"] != first["reported"]


def test_the_payload_running_still_wins_over_a_terminal_return(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s, background_tasks=[{"agent_id": X, "status": "running"}])
    assert unit(s, X)["state"] == "running"


# --- GA-3: the fingerprint carries the decision and unreadable tokens --------------------------


def _session(tmp_path, nested=False):
    s = Session(tmp_path)
    if nested:
        s.main(sync_launch("tx", X, 1), note(30, Y, "ty", "completed")).child(X)
        s.child(X, child_entries(X), async_launch("ty", Y, 4))
        s.child(Y, meta={"parentAgentId": X})
    else:
        s.main(sync_launch("t1", X, 1)).child(X, meta={"description": "alpha"})
    s.build()
    return s


def test_an_unreadable_main_transcript_changes_the_fingerprint(tmp_path):
    s = _session(tmp_path)
    before = fingerprint(s, X)
    os.chmod(s.main_path, 0)
    try:
        after = fingerprint(s, X)
    finally:
        os.chmod(s.main_path, 0o644)
    assert after != before


def test_an_unreadable_meta_file_changes_the_fingerprint(tmp_path):
    s = _session(tmp_path)
    meta = s.child_path(X).with_name(f"agent-{X}.meta.json")
    before = fingerprint(s, X)
    os.chmod(meta, 0)
    try:
        after = fingerprint(s, X)
    finally:
        os.chmod(meta, 0o644)
    assert after != before


def test_an_unreadable_nested_launcher_changes_the_fingerprint(tmp_path):
    s = _session(tmp_path, nested=True)
    before = fingerprint(s, Y)
    os.chmod(s.child_path(X), 0)
    try:
        after = fingerprint(s, Y)
    finally:
        os.chmod(s.child_path(X), 0o644)
    assert after != before


def test_a_decision_that_raises_has_its_own_token(monkeypatch, tmp_path):
    s = _session(tmp_path)
    before = fingerprint(s, X)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reader, "session_child_return", boom)
    after = fingerprint(s, X)
    assert after != before
    assert after == fingerprint(s, X)
    assert [e for e in after if "unreadable" in e[0]], after


def test_the_current_return_state_and_reason_are_in_the_fingerprint(monkeypatch, tmp_path):
    s = _session(tmp_path)
    real = reader.session_child_return

    def with_decision(state, reason):
        def fake(main_path, agent_path, cache=None):
            decision, digest, launcher, *rest = real(main_path, agent_path, cache)
            return (returns.Decision(state, reason), digest, launcher, *rest)
        return fake

    prints = []
    for state, reason in (("returned", "none"), ("unknown", "parent_unreadable"),
                          ("unknown", "malformed"), ("pending", "none")):
        monkeypatch.setattr(reader, "session_child_return", with_decision(state, reason))
        prints.append(json.dumps(fingerprint(s, X)))
    assert len(set(prints)) == 4


def test_the_fingerprint_is_stable_for_unchanged_files(tmp_path):
    s = _session(tmp_path)
    assert fingerprint(s, X) == fingerprint(s, X)


def test_a_same_stat_read_failure_reopens_an_analyzed_unit(monkeypatch, capsys, tmp_path):
    s = _session(tmp_path)
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"
    meta = s.child_path(X).with_name(f"agent-{X}.meta.json")
    os.chmod(meta, 0)
    try:
        check(monkeypatch, capsys, s)
        state = unit(s, X)["state"]
    finally:
        os.chmod(meta, 0o644)
    assert state == "running"


# --- GA-6 and GA-12: rows the G-A mutation review found missing ----------------------------------


def test_an_unknown_return_state_is_not_finished(monkeypatch, capsys, tmp_path):
    """No launch evidence at all (`unknown`/`no_evidence`) and no sentinel: not terminal."""
    s = Session(tmp_path)
    s.main().child(X)
    s.build()
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "running"


def test_the_kill_sentinel_finishes_a_pending_child(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    entries = child_entries(X, 1, 2)
    entries.append({
        "type": "user", "message": {"role": "user", "content": SENTINEL},
        "timestamp": iso(3), "agentId": X,
    })
    s.main(async_launch("t1", X, 1)).child(X, entries, default=False)
    s.build()
    assert reader.load_session(s.main_path).attempts[0].return_state == "pending"
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"


def test_a_pending_child_without_a_sentinel_is_not_finished(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    s.main(async_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "running"


def test_growth_of_the_nested_launcher_changes_the_fingerprint(tmp_path):
    """An unrelated line appended to the launcher changes nothing the decision reads: only the
    launcher's size, mtime and inode can tell."""
    s = _session(tmp_path, nested=True)
    before = fingerprint(s, Y)
    append_lines(s.child_path(X), [child_entries(X, 9, 10)[1]])
    assert fingerprint(s, Y) != before


def test_a_second_read_failure_behind_an_unchanged_decision_changes_the_fingerprint(tmp_path):
    """An unreadable meta file makes the decision `malformed` first; the main transcript then
    failing too leaves the decision alone, and only the unreadable tokens can tell."""
    s = _session(tmp_path)
    meta = s.child_path(X).with_name(f"agent-{X}.meta.json")
    os.chmod(meta, 0)
    try:
        first = fingerprint(s, X)
        os.chmod(s.main_path, 0)
        try:
            second = fingerprint(s, X)
        finally:
            os.chmod(s.main_path, 0o644)
    finally:
        os.chmod(meta, 0o644)
    assert second != first
