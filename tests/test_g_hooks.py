"""G-A, H1: the hook's finished rule is the reader's `return_state` (docs/DESIGN.md 7.2, plan
rev 5 H1). A child is finished only when its state is `returned`, `reported_failed` or
`reported_killed`; the runtime payload still wins; every parent-side fact for the child is in the
unit fingerprint, so any change re-opens the unit.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agent_postmortem import hooks_cli
from agent_postmortem.cli import main
from tests.g_helpers import (
    SESSION,
    Session,
    X,
    Y,
    append_lines,
    async_launch,
    child_entries,
    note,
    queued,
    result,
    resumed,
    send,
    sync_launch,
)


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def check(monkeypatch, capsys, s, **extra):
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(s.main_path),
        "session_id": "s-g-hooks", "stop_hook_active": False, **extra,
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main(["hook", "check"])
    return capsys.readouterr().out


def unit(s, child):
    state = Path(os.environ["XDG_STATE_HOME"]) / "agent-postmortem" / "state.json"
    data = json.loads(state.read_text())
    key = hooks_cli._session_state_key(s.root, SESSION)
    return data["sessions"][key]["units"][f"subagents/agent-{child}.jsonl"]


def test_a_child_with_no_notification_is_not_finished_and_a_new_notification_analyzes_it(
    monkeypatch, capsys, tmp_path,
):
    s = Session(tmp_path)
    s.main(async_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "running"
    append_lines(s.main_path, [note(30, X, "t1", "completed")])
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"


def test_the_runtime_payload_running_wins(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s, background_tasks=[X])
    assert unit(s, X)["state"] == "running"


def test_a_delivered_resume_reopens_a_finished_unit_and_its_notification_finishes_it(
    monkeypatch, capsys, tmp_path,
):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"
    append_lines(s.main_path, [send("ts", 10, X), resumed("ts", 11, X)])
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "running"
    append_lines(s.main_path, [note(30, X, "ts", "completed")])
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"


PARENT_SIDE_CHANGES = {
    "rejected": [send("ts", 10, X), result("ts", 11, tur="no such agent", is_error=True)],
    "queued": [send("ts", 10, X), queued("ts", 11)],
    "result-less": [send("ts", 10, X)],
    "malformed": [send("ts", 10, X, recipient=Y), resumed("ts", 11, X)],
}


# G-A1 (GA-8): the re-opened unit re-evaluates its finished evidence in the same check, so a
# request that leaves the child `returned` (rejected, queued) is analyzed again at once (was:
# `running` for all four); a still-undecided one (result-less, a new resume round) stays open.
STATE_AFTER_REOPEN = {
    "rejected": "analyzed", "queued": "analyzed", "result-less": "running", "malformed": "running",
}


@pytest.mark.parametrize("name", sorted(PARENT_SIDE_CHANGES))
def test_any_parent_side_request_reopens_a_finished_unit(monkeypatch, capsys, tmp_path, name):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    first = unit(s, X)
    assert first["state"] == "analyzed"
    append_lines(s.main_path, PARENT_SIDE_CHANGES[name])
    check(monkeypatch, capsys, s)
    assert unit(s, X)["fingerprint"] != first["fingerprint"]
    assert unit(s, X)["state"] == STATE_AFTER_REOPEN[name]


def test_a_same_count_status_replacement_reopens_the_unit(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    s.main(async_launch("t1", X, 1), note(30, X, "t1", "failed")).child(X)
    s.build()
    check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"
    text = s.main_path.read_text(encoding="utf-8")
    assert "<status>failed</status>" in text
    s.main_path.write_text(text.replace("<status>failed", "<status>killed"), encoding="utf-8")
    first = unit(s, X)
    check(monkeypatch, capsys, s)
    # G-A1 (GA-8): re-opened and re-evaluated at once; `killed` is terminal too (was: running)
    assert unit(s, X)["fingerprint"] != first["fingerprint"]
    assert unit(s, X)["state"] == "analyzed"


def test_a_reported_failure_is_notified(monkeypatch, capsys, tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1, "failed")).child(X)
    s.build()
    out = check(monkeypatch, capsys, s)
    assert unit(s, X)["state"] == "analyzed"
    assert "DEGRADED" in out


def test_a_nested_parent_is_evidence_only_and_its_problems_never_reach_the_unit(
    monkeypatch, capsys, tmp_path,
):
    s = Session(tmp_path)
    s.main(sync_launch("tx", X, 1), note(30, Y, "ty", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("ty", Y, 4))
    s.child(Y, meta={"parentAgentId": X})
    s.build()
    with s.child_path(X).open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n" * 60)
    check(monkeypatch, capsys, s)
    entry = unit(s, Y)
    assert entry["state"] == "analyzed"
    assert "INCOMPLETE" not in entry["verdict"]
    assert entry["reported"] == []
