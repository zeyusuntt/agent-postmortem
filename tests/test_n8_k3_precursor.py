"""N8a K3 (docs/DESIGN.md 4.13.8, 4.4): a mislabeled_kill title for a
`runtime` cause, or an `unattributed` cause with a known silence, names an
observed precursor, a Bash command moved to the background, when that
command's tool_result is the attempt's last tool activity before the kill.
The wording never says the backgrounding caused the kill."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import reader
from agent_postmortem.detectors import mislabeled_kill
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _assistant_tool,
    _kill,
    _parent_path,
    _tool_result,
    _user,
    _wf_dir,
    _write_jsonl,
)

NOTICE = (
    "Command did not complete within its 120s timeout and was moved to "
    "the background (ID: b1). Output is being written to: /tmp/x"
)
CLAUSE = ", after a Bash command was moved to the background (line {n})"
BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _two_tool_uses(ts_a: datetime, agent: str) -> dict:
    """One assistant entry with two parallel Bash tool_use blocks, t1 and t2."""
    entry = _assistant_tool("t1", "Bash", {"command": "make build"}, ts_a, agent)
    entry["message"]["content"].append(
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "make test"}}
    )
    return entry


def _two_results(id_a: str, text_a: str, id_b: str, text_b: str, ts: datetime, agent: str) -> dict:
    """One user entry carrying two tool_result blocks (one physical line)."""
    entry = _tool_result(id_a, text_a, ts, agent)
    entry["message"]["content"].append(
        {"type": "tool_result", "tool_use_id": id_b, "content": text_b, "is_error": False}
    )
    return entry


def _build(tmp_path: Path, wf_id: str, body: str, *, retried: bool = True, parent=None):
    """One attempt (agent 1) with `body`'s events, then the kill sentinel 350 s
    after the last body event; `retried` adds a later attempt of the same key
    (cause runtime), otherwise the key has one attempt (cause unattributed
    unless `parent` evidence says otherwise)."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    a1, a2 = _aid(0x8c0), _aid(0x8c1)
    base = BASE
    started = [{"type": "started", "key": "v2:x", "agentId": a1, "label": "impl", "phase": "I"}]
    if retried:
        started += [
            {"type": "started", "key": "v2:x", "agentId": a2, "label": "impl", "phase": "I"},
            {"type": "result", "key": "v2:x", "agentId": a2, "result": {"ok": True}},
        ]
    _write_jsonl(run_dir / "journal.jsonl", started)
    t = lambda s: base + timedelta(seconds=s)  # noqa: E731
    events = {
        "last": [
            _user("Run the long build.", t(0), a1),
            _assistant_tool("t1", "Bash", {"command": "make build"}, t(1), a1),
            _tool_result("t1", NOTICE, t(2), a1),
        ],
        "tool_use_after": [
            _user("Run the long build.", t(0), a1),
            _assistant_tool("t1", "Bash", {"command": "make build"}, t(1), a1),
            _tool_result("t1", NOTICE, t(2), a1),
            _assistant_tool("t2", "Bash", {"command": "ls"}, t(3), a1),
        ],
        "tool_result_after": [
            _user("Run both.", t(0), a1),
            _two_tool_uses(t(1), a1),
            _tool_result("t1", NOTICE, t(2), a1),
            _tool_result("t2", "ok", t(3), a1),
        ],
        "text_after": [
            _user("Run the long build.", t(0), a1),
            _assistant_tool("t1", "Bash", {"command": "make build"}, t(1), a1),
            _tool_result("t1", NOTICE, t(2), a1),
            _assistant_text("Waiting for it.", t(3), a1),
        ],
        "payload_only": [
            _user("Show the log.", t(0), a1),
            _assistant_tool("t1", "Bash", {"command": "cat notes.txt"}, t(1), a1),
            _tool_result("t1", "notes\n" + NOTICE, t(2), a1),
        ],
        "same_line_other_last": [
            # two results in ONE user entry (one physical line): the backgrounded
            # one first, an ordinary one after it in the same entry
            _user("Run both.", t(0), a1),
            _two_tool_uses(t(1), a1),
            _two_results("t1", NOTICE, "t2", "ok", t(2), a1),
        ],
        "same_line_backgrounded_last": [
            _user("Run both.", t(0), a1),
            _two_tool_uses(t(1), a1),
            _two_results("t1", "ok", "t2", NOTICE, t(2), a1),
        ],
        "plain": [
            _user("Run the long build.", t(0), a1),
            _assistant_tool("t1", "Bash", {"command": "make build"}, t(1), a1),
            _tool_result("t1", "ok", t(2), a1),
        ],
    }[body]
    last_ts = datetime.fromisoformat(events[-1]["timestamp"].replace("Z", "+00:00"))
    _write_jsonl(run_dir / f"agent-{a1}.jsonl", [*events, _kill(last_ts + timedelta(seconds=350), a1)])
    if retried:
        _write_jsonl(run_dir / f"agent-{a2}.jsonl", [
            _user("Retry.", t(500), a2), _assistant_text("Done.", t(501), a2),
        ])
    if parent is not None:
        _write_jsonl(_parent_path(root), parent(a1, last_ts + timedelta(seconds=350)))
    return run_dir


def _title(run_dir: Path) -> str:
    run = reader.load_run(run_dir)
    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    return findings[0].title


def test_k3_runtime_precursor_present_and_last_is_named_with_its_line(tmp_path):
    title = _title(_build(tmp_path, "wf_00000000-8c0", "last"))
    assert title == (
        "impl attempt 1 was killed by the runtime after 350s of silence, after a Bash "
        "command was moved to the background (line 3), logged as a user interrupt"
    )


def test_k3_unattributed_precursor_is_named_after_the_silence_clause(tmp_path):
    title = _title(_build(tmp_path, "wf_00000000-8c1", "last", retried=False))
    assert title == (
        "impl interrupted after 350s of silence" + CLAUSE.format(n=3)
        + ", cause unattributed, consistent with a runtime kill"
    )


def test_k3_a_later_tool_use_means_no_precursor(tmp_path):
    assert "moved to the background" not in _title(_build(tmp_path, "wf_00000000-8c2", "tool_use_after"))


def test_k3_a_later_tool_result_means_no_precursor(tmp_path):
    assert "moved to the background" not in _title(_build(tmp_path, "wf_00000000-8c3", "tool_result_after"))


def test_k3_assistant_text_after_the_precursor_does_not_hide_it(tmp_path):
    title = _title(_build(tmp_path, "wf_00000000-8c4", "text_after"))
    assert CLAUSE.format(n=3) in title


def test_k3_a_marker_only_in_the_payload_is_not_a_precursor(tmp_path):
    assert "moved to the background" not in _title(_build(tmp_path, "wf_00000000-8c5", "payload_only"))


def test_k3_a_result_of_another_call_in_the_same_entry_after_it_means_no_precursor(tmp_path):
    title = _title(_build(tmp_path, "wf_00000000-8ca", "same_line_other_last"))
    assert "moved to the background" not in title


def test_k3_the_backgrounded_result_last_in_a_shared_entry_is_named_with_that_line(tmp_path):
    title = _title(_build(tmp_path, "wf_00000000-8cb", "same_line_backgrounded_last"))
    assert CLAUSE.format(n=3) in title


def test_k3_no_backgrounding_no_clause(tmp_path):
    assert "moved to the background" not in _title(_build(tmp_path, "wf_00000000-8c6", "plain"))


def test_k3_a_parent_stop_title_never_names_a_precursor(tmp_path):
    def parent(agent, kill_ts):
        return [_assistant_tool("stop1", "TaskStop", {"agent_id": agent}, kill_ts - timedelta(seconds=5))]

    title = _title(_build(tmp_path, "wf_00000000-8c7", "last", parent=parent))
    assert title == "impl stopped by the parent session"


def test_k3_a_human_title_never_names_a_precursor(tmp_path):
    def parent(agent, kill_ts):
        return [_kill(kill_ts - timedelta(seconds=5))]

    title = _title(_build(tmp_path, "wf_00000000-8c8", "last", parent=parent))
    assert title == "impl interrupted together with the parent session"


def test_k3_an_unattributed_kill_with_unknown_silence_never_names_a_precursor(tmp_path):
    run_dir = _build(tmp_path, "wf_00000000-8c9", "last", retried=False)
    transcript = next(run_dir.glob("agent-*.jsonl"))
    # drop every timestamp: the silence is then unknown
    import json
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    for line in lines:
        line.pop("timestamp", None)
    _write_jsonl(transcript, lines)
    title = _title(run_dir)
    assert title == "impl interrupted with silence unknown, cause unattributed"


FIXTURES = Path(__file__).parent / "fixtures" / "runs"


def _every_fixture_title():
    for child in sorted(FIXTURES.iterdir()):
        if not child.is_dir():
            continue
        if (child / "journal.jsonl").exists():
            targets = [child]
        else:
            targets = [ref for ref in reader.discover_runs(child)]
        for target in targets:
            try:
                run = reader.load_run(target)
            except Exception:  # noqa: BLE001 - a hostile fixture may not load
                continue
            for finding in mislabeled_kill.DETECTOR.run(run):
                yield child.name, finding.title


@pytest.mark.parametrize("word", ["caused", "because", "due to"])
def test_k3_no_title_in_any_fixture_claims_a_cause_by_the_precursor(word):
    titles = list(_every_fixture_title())
    assert titles, "the sweep found no mislabeled_kill title at all"
    for name, title in titles:
        assert not re.search(rf"\b{word}\b", title.lower()), (name, title)
