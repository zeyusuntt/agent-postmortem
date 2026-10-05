"""N4 fix round, Fix A, M-N4-1 (b): N1's shape rule treats a `queue-operation`
`enqueue` entry whose key set is exactly {type, operation, sessionId, timestamp}
as benign (docs/DESIGN.md 4.13.3). Every extra key and every missing key keeps
the entry malformed. Fixtures are synthetic; every wf_ id used here is in
tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import reader
from tests.test_field_v03 import (
    FAKE_UUID,
    _aid,
    _assistant_text,
    _envelope,
    _no_config,
    _notification_entry,
    _parent_path,
    _user,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
BENIGN = {
    "type": "queue-operation",
    "operation": "enqueue",
    "sessionId": FAKE_UUID,
    "timestamp": "2026-09-01T12:00:03Z",
}


def _index(tmp_path, entries):
    parent = tmp_path / f"{FAKE_UUID}.jsonl"
    parent.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return reader.ParentIndex.for_path(parent)


def test_an_enqueue_with_exactly_the_four_keys_is_benign(tmp_path):
    index = _index(tmp_path, [BENIGN])
    assert index.inspection_complete is True
    assert index.diagnostics == []


@pytest.mark.parametrize("operation", ["dequeue", "remove", "popAll"])
def test_another_operation_with_the_four_keys_is_still_fine(tmp_path, operation):
    index = _index(tmp_path, [{**BENIGN, "operation": operation}])
    assert index.inspection_complete is True


@pytest.mark.parametrize("extra", [
    ("content", None), ("content", 5), ("content", ["x"]), ("content", {"a": 1}),
    ("message", {"content": "x"}), ("origin", {"kind": "task-notification"}),
    ("uuid", "u"), ("extra", 1), ("cwd", "/tmp"), ("requestId", "r"),
])
def test_an_enqueue_with_any_extra_key_and_no_string_content_stays_malformed(tmp_path, extra):
    entry = {**BENIGN, extra[0]: extra[1]}
    index = _index(tmp_path, [entry])
    assert index.inspection_complete is False
    assert index.diagnostics == [f"{FAKE_UUID}.jsonl:1: malformed parent transcript entry"]


@pytest.mark.parametrize("missing", ["sessionId", "timestamp", "operation"])
def test_an_enqueue_missing_any_one_of_the_four_keys_stays_malformed(tmp_path, missing):
    entry = {key: value for key, value in BENIGN.items() if key != missing}
    index = _index(tmp_path, [entry])
    assert index.inspection_complete is False


def test_an_enqueue_with_a_string_content_is_still_fine(tmp_path):
    assert _index(tmp_path, [{**BENIGN, "content": "hello"}]).inspection_complete is True


def test_an_enqueue_with_the_four_keys_does_not_hide_a_malformed_neighbour(tmp_path):
    index = _index(tmp_path, [BENIGN, {**BENIGN, "operation": 42}])
    assert index.inspection_complete is False
    assert index.diagnostics == [f"{FAKE_UUID}.jsonl:2: malformed parent transcript entry"]


def test_a_benign_enqueue_does_not_make_the_lifecycle_unknown(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    wf_id = "wf_00000000-f05"
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    run_dir.mkdir(parents=True)
    agent_id = _aid(1)
    start = T0 + timedelta(seconds=10)
    (run_dir / "journal.jsonl").write_text(
        json.dumps({"type": "started", "key": "v2:k", "agentId": agent_id}) + "\n"
        + json.dumps({"type": "result", "key": "v2:k", "agentId": agent_id, "result": {}}) + "\n",
        encoding="utf-8",
    )
    (run_dir / f"agent-{agent_id}.jsonl").write_text(
        json.dumps(_user("go", start, agent_id)) + "\n"
        + json.dumps(_assistant_text("done", start, agent_id)) + "\n",
        encoding="utf-8",
    )
    parent = _parent_path(root)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("".join(json.dumps(entry) + "\n" for entry in [
        BENIGN,
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(T0 + timedelta(seconds=2), _envelope("tA", "toolu_A", "completed")),
    ]), encoding="utf-8")
    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "completed"
    assert run.finished is True
    assert run.discovery["parent"] == []
