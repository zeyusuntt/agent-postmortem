"""N4 fix round, Fix A (reader, discovery, population): what reaches
Run.discovery (A2, docs/DESIGN.md 4.13.1). Fixtures are synthetic; every wf_
id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent_postmortem import reader
from tests.test_field_v03 import _aid, _no_config, _user, _wf_dir

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
CLEAN = {"journal": [], "parent": [], "transcripts": [], "meta": []}


def _text(entries):
    return "".join(json.dumps(e) + "\n" for e in entries)


def _assistant(content, *, usage=None, ts="2026-09-01T12:00:01Z", message_id="m1", request="r1",
               extra=None):
    message = {"role": "assistant", "content": content, "id": message_id}
    if usage is not None:
        message["usage"] = usage
    entry = {"type": "assistant", "message": message, "requestId": request, "timestamp": ts}
    entry.update(extra or {})
    return entry


def _workflow(tmp_path, wf_id, transcript, *, journal_extra=None):
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    agent_id = _aid(1)
    started = {"type": "started", "key": "v2:k", "agentId": agent_id, "label": "l", "phase": "P"}
    started.update(journal_extra or {})
    (run_dir / "journal.jsonl").write_text(_text([
        started,
        {"type": "result", "key": "v2:k", "agentId": agent_id, "result": {}},
    ]), encoding="utf-8")
    (run_dir / f"agent-{agent_id}.jsonl").write_text(
        transcript if isinstance(transcript, str) else _text(transcript), encoding="utf-8"
    )
    return run_dir, agent_id


def _base():
    return [_user("Do it.", T0), _assistant([{"type": "text", "text": "Done."}], usage={})]


EXCLUDED = [
    pytest.param(
        [*_base(),
         _assistant([{"type": "text", "text": "a"}], usage={"input_tokens": 1}, message_id="m2",
                    request="r2"),
         _assistant([{"type": "text", "text": "b"}], usage={"input_tokens": 2}, message_id="m2",
                    request="r2")],
        "duplicate usage for a (message id, requestId) pair differs from its first occurrence",
        id="duplicate_usage",
    ),
    pytest.param(
        [_user("Do it.", T0), _assistant([{"type": "text", "text": "x"}], usage={}, ts=5)],
        "timestamp is not a string",
        id="timestamp_not_string",
    ),
    pytest.param(
        [_user("Do it.", T0),
         _assistant([{"type": "text", "text": "x"}], usage={}, ts="not a time")],
        "timestamp could not be parsed",
        id="timestamp_unparseable",
    ),
    pytest.param(
        [*_base(), _assistant([{"type": "text", "text": "x"}], usage=5, message_id="m3",
                              request="r3")],
        "usage is not an object (got int)",
        id="usage_not_object",
    ),
    pytest.param(
        [*_base(), {"type": "attachment", "attachment": 5, "timestamp": "2026-09-01T12:00:02Z"}],
        "attachment is not an object (got int)",
        id="attachment_not_object",
    ),
    pytest.param(
        [*_base(), _assistant([{"type": "text", "text": 5}], usage={}, message_id="m4",
                              request="r4")],
        "block text is not a string (got int)",
        id="assistant_block_text_not_string",
    ),
    pytest.param(
        [_user("Do it.", T0),
         {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": 5}]},
          "timestamp": "2026-09-01T12:00:01Z"},
         _assistant([{"type": "text", "text": "Done."}], usage={})],
        "block text is not a string (got int)",
        id="user_block_text_not_string",
    ),
]


@pytest.mark.parametrize("entries, fragment", EXCLUDED)
def test_a_transcript_data_quality_note_is_not_a_discovery_diagnostic(
    tmp_path, monkeypatch, entries, fragment
):
    _no_config(monkeypatch, tmp_path)
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-f01", entries)
    run = reader.load_run(run_dir)
    assert any(fragment in d for d in run.diagnostics)
    assert run.discovery == CLEAN


@pytest.mark.parametrize("field", ["label", "phase"])
def test_a_cosmetic_journal_field_note_is_not_a_discovery_diagnostic(
    tmp_path, monkeypatch, field
):
    _no_config(monkeypatch, tmp_path)
    run_dir, _ = _workflow(tmp_path, "wf_00000000-f01", _base(), journal_extra={field: 5})
    run = reader.load_run(run_dir)
    assert any(f"started event has a non-string {field}" in d for d in run.diagnostics)
    assert run.discovery == CLEAN


def test_an_excluded_note_does_not_hide_a_kept_one_beside_it(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    entries = [
        _user("Do it.", T0),
        _assistant([{"type": "text", "text": "x"}], usage=5, message_id="m5", request="r5"),
        {"type": "user", "message": 5, "timestamp": "2026-09-01T12:00:03Z"},
    ]
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-f01", entries)
    run = reader.load_run(run_dir)
    name = f"agent-{agent_id}.jsonl"
    assert run.discovery["transcripts"] == [f"{name}:3: message is not an object (got int)"]
    assert any("usage is not an object" in d for d in run.diagnostics)


def test_the_flat_diagnostics_keep_every_note_in_the_reported_order(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    entries = [
        _user("Do it.", T0),
        _assistant([{"type": "text", "text": "x"}], usage=5, message_id="m5", request="r5"),
        {"type": "user", "message": 5, "timestamp": "2026-09-01T12:00:03Z"},
        {"type": "attachment", "attachment": 5, "timestamp": "2026-09-01T12:00:04Z"},
    ]
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-f01", entries)
    run = reader.load_run(run_dir)
    name = f"agent-{agent_id}.jsonl"
    assert [d for d in run.diagnostics if d.startswith(name)] == [
        f"{name}:2: usage is not an object (got int)",
        f"{name}:3: message is not an object (got int)",
        f"{name}:4: attachment is not an object (got int)",
    ]


# --- the kept diagnostics, pinned as negatives ---------------------------------------------


def test_a_transcript_message_that_is_not_an_object_is_a_discovery_diagnostic(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    entries = [*_base(), {"type": "user", "message": 5, "timestamp": "2026-09-01T12:00:02Z"}]
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-f01", entries)
    run = reader.load_run(run_dir)
    assert run.discovery["transcripts"] == [
        f"agent-{agent_id}.jsonl:3: message is not an object (got int)"
    ]


def test_a_tool_use_input_that_is_not_an_object_is_a_discovery_diagnostic(
    tmp_path, monkeypatch
):
    _no_config(monkeypatch, tmp_path)
    block = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": 5}
    entries = [*_base(), _assistant([block], usage={}, message_id="m6", request="r6")]
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-f01", entries)
    run = reader.load_run(run_dir)
    assert run.discovery["transcripts"] == [
        f"agent-{agent_id}.jsonl:3: tool_use input is not an object (got int)"
    ]


def test_a_duplicate_tool_use_id_is_a_discovery_diagnostic(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    block = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}
    entries = [
        *_base(),
        _assistant([block], usage={}, message_id="m6", request="r6"),
        _assistant([block], usage={}, message_id="m7", request="r7"),
    ]
    run_dir, agent_id = _workflow(tmp_path, "wf_00000000-f01", entries)
    run = reader.load_run(run_dir)
    assert run.discovery["transcripts"] == [
        f"agent-{agent_id}.jsonl:4: duplicate tool_use_id toolu_1 at lines 3 and 4"
    ]


# --- the same notes in a session run ------------------------------------------------------------


def _session_files(tmp_path, main_entries, child_entries=None):
    from tests.test_n4b1_discovery import SESSION, SLUG

    root = tmp_path / "root"
    main = root / SLUG / f"{SESSION}.jsonl"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(_text(main_entries), encoding="utf-8")
    child = _aid(8)
    if child_entries is not None:
        path = root / SLUG / SESSION / "subagents" / f"agent-{child}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_text(child_entries), encoding="utf-8")
    return main, child


def test_a_session_main_transcript_note_is_not_a_discovery_diagnostic(tmp_path):
    main, _ = _session_files(tmp_path, [
        _user("go", T0),
        _assistant([{"type": "text", "text": "x"}], usage=5),
    ])
    run = reader.load_session(main)
    assert any("usage is not an object" in d for d in run.diagnostics)
    assert run.discovery == CLEAN


def test_a_session_subagent_transcript_note_is_not_a_discovery_diagnostic(tmp_path):
    main, child = _session_files(
        tmp_path,
        [_user("go", T0), _assistant([{"type": "text", "text": "x"}], usage={})],
        [_user("go", T0, _aid(8)), _assistant([{"type": "text", "text": "x"}], usage=5)],
    )
    run = reader.load_session(main)
    assert any("usage is not an object" in d for d in run.diagnostics)
    assert run.discovery == CLEAN


def test_a_cached_session_view_keeps_its_main_notes_out_of_discovery(tmp_path):
    main, child = _session_files(tmp_path, [
        _user("go", T0),
        _assistant([{"type": "text", "text": "x"}], usage=5),
    ])
    cache: dict = {}
    first = reader.load_session_view(main, [], main_cache=cache)
    second = reader.load_session_view(main, [], main_cache=cache)
    assert first.discovery == CLEAN
    assert second.discovery == CLEAN
