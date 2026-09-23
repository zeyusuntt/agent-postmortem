"""Tests for reader.py's session-run behavior (docs/DESIGN.md 4.2): dedup,
envelope events, compaction, thinking blocks, and the parent transcript
index. Session-specific fixtures live under tests/fixtures/runs/session_*/,
each a main transcript plus (where the case needs one) subagent transcripts,
following tests/fixtures/README.md.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_postmortem import reader
from agent_postmortem.detectors import codex_write_denied

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
FAKE_SLUG = "-home-user-project"
FAKE_UUID = "00000000-0000-4000-8000-000000000000"


def _session_path(case: str) -> Path:
    return FIXTURES_ROOT / case / FAKE_SLUG / f"{FAKE_UUID}.jsonl"


def _load(case: str):
    return reader.load_session(_session_path(case))


# --- session_clean -----------------------------------------------------


def test_session_clean_has_a_main_attempt_and_two_subagents():
    run = _load("session_clean")
    assert run.kind == "session"
    assert run.diagnostics == []
    assert run.main is not None
    assert run.main.label == "main"
    assert run.main.key is None
    assert run.main.ordinal == 1
    # Run.attempts is child attempts only; main is never in it.
    assert len(run.attempts) == 2
    assert len(run.agents) == 2
    assert {agent.label for agent in run.agents} == {"impl:alpha", "impl:beta"}


def test_session_clean_has_no_markers_anywhere():
    run = _load("session_clean")
    all_events = list(run.main.events) + [event for attempt in run.attempts for event in attempt.events]
    assert all(not event.markers for event in all_events)


# --- session_dedup -------------------------------------------------------


def test_session_dedup_usage_counted_once_not_doubled():
    """The main transcript's two assistant entries share (message.id,
    requestId) with identical usage (10 input / 15 output); the run's usage
    must reflect that pair once (13/19 total with the closing message), not
    twice (23/34)."""
    run = _load("session_dedup")
    assert run.main.usage == {"input_tokens": 13, "output_tokens": 19}


def test_session_dedup_duplicate_events_usage_is_empty():
    run = _load("session_dedup")
    assistant_events = [event for event in run.main.events if event.kind == "assistant"]
    assert len(assistant_events) == 3
    assert assistant_events[0].usage == {"input_tokens": 10, "output_tokens": 15}
    assert assistant_events[1].usage == {}  # the duplicate: usage already counted
    assert assistant_events[2].usage == {"input_tokens": 3, "output_tokens": 4}


def test_session_dedup_conflicting_usage_adds_a_diagnostic(tmp_path):
    """A duplicate (message.id, requestId) pair whose usage numbers differ
    from the first occurrence is still deduplicated (usage={} on the second),
    but the mismatch itself is recorded as a diagnostic (docs/DESIGN.md 4.2)."""
    slug_dir = tmp_path / FAKE_SLUG
    slug_dir.mkdir(parents=True)
    session_path = slug_dir / f"{FAKE_UUID}.jsonl"
    session_path.write_text(
        "\n".join([
            (
                '{"type":"assistant","message":{"id":"msg_1","role":"assistant",'
                '"content":[{"type":"text","text":"first"}],'
                '"usage":{"input_tokens":10,"output_tokens":15}},'
                '"requestId":"req_1","sessionId":"' + FAKE_UUID + '",'
                '"timestamp":"2026-09-16T09:00:00Z"}'
            ),
            (
                '{"type":"assistant","message":{"id":"msg_1","role":"assistant",'
                '"content":[{"type":"text","text":"second, different usage"}],'
                '"usage":{"input_tokens":999,"output_tokens":999}},'
                '"requestId":"req_1","sessionId":"' + FAKE_UUID + '",'
                '"timestamp":"2026-09-16T09:00:10Z"}'
            ),
        ]) + "\n",
        encoding="utf-8",
    )
    run = reader.load_session(session_path)
    assert run.main.usage == {"input_tokens": 10, "output_tokens": 15}
    assert any("duplicate usage" in diag for diag in run.diagnostics)


# --- id validation (docs/DESIGN.md 5.3) -----------------------------------


def test_malformed_session_subagent_id_is_replaced_and_diagnosed_without_echo(tmp_path):
    """docs/DESIGN.md 5.3: a session subagent id must be 'a' + 16 hex chars
    (the same shape as a workflow agent id). A subagent transcript filename
    that does not conform (here one that happens to carry a name) must never
    reach Attempt.agent_id/key or a diagnostic verbatim."""
    slug_dir = tmp_path / FAKE_SLUG
    slug_dir.mkdir(parents=True)
    session_path = slug_dir / f"{FAKE_UUID}.jsonl"
    session_path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},'
        f'"sessionId":"{FAKE_UUID}","timestamp":"2026-09-16T09:00:00Z"}}\n',
        encoding="utf-8",
    )
    subagents_dir = slug_dir / FAKE_UUID / "subagents"
    subagents_dir.mkdir(parents=True)
    bad_id = "bob-internal-name"
    (subagents_dir / f"agent-{bad_id}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"go"},'
        f'"agentId":"{bad_id}","sessionId":"{FAKE_UUID}",'
        '"timestamp":"2026-09-16T09:00:10Z"}\n',
        encoding="utf-8",
    )
    (subagents_dir / f"agent-{bad_id}.meta.json").write_text(
        '{"agentType":"general-purpose","description":"impl:demo","workflowPhase":"Implement"}',
        encoding="utf-8",
    )

    run = reader.load_session(session_path)
    assert len(run.attempts) == 1
    attempt = run.attempts[0]
    assert re.match(r"^agent-[0-9a-f]{8}$", attempt.agent_id)
    assert attempt.key == attempt.agent_id
    assert any("agent id did not match the expected format" in diag for diag in run.diagnostics)
    assert not any(bad_id in diag for diag in run.diagnostics)


# --- session_envelope ----------------------------------------------------


def test_session_envelope_main_tool_result_is_enveloped_with_no_markers():
    run = _load("session_envelope")
    tool_results = [event for event in run.main.events if event.kind == "tool_result"]
    assert len(tool_results) == 1
    result = tool_results[0]
    assert result.tool_name == "Agent"
    assert result.envelope is True
    assert result.markers == frozenset()
    # The excerpt itself still carries the text; only markers is scrubbed.
    assert "WRITE_DENIED" in result.text


def test_session_envelope_child_tool_result_is_not_enveloped_and_has_the_marker():
    run = _load("session_envelope")
    child = run.attempts[0]
    tool_results = [event for event in child.events if event.kind == "tool_result"]
    assert len(tool_results) == 1
    result = tool_results[0]
    assert result.tool_name == "Bash"
    assert result.envelope is False
    assert "WRITE_DENIED" in result.markers


def test_session_envelope_codex_write_denied_fires_once_on_the_child_only():
    run = _load("session_envelope")
    findings = codex_write_denied.DETECTOR.run(run)
    # Two findings (R-CODEX-CONTRACT, R-ASSERT-ARTIFACT), both counting a
    # single hit: the parent's enveloped quote must not double it.
    assert len(findings) == 2
    child_agent_id = run.attempts[0].agent_id
    for finding in findings:
        assert finding.metrics["count"] == 1
        assert finding.agent_id == child_agent_id


# --- session_compaction ---------------------------------------------------


def test_session_compaction_kills_the_subagent_with_silence_none():
    run = _load("session_compaction")
    assert len(run.attempts) == 1
    attempt = run.attempts[0]
    assert attempt.outcome == "killed"
    assert attempt.interrupt is not None
    assert attempt.interrupt.cause == "unattributed"
    assert attempt.interrupt.cause_evidence is None
    assert attempt.interrupt.silence_seconds is None


def test_session_compaction_preceding_is_none_because_the_walk_stops_there():
    """docs/DESIGN.md 4.5 (v0.1): meaningful_event_before stops (returns
    None) at a compaction event rather than walking past it to an earlier
    real event -- a context reset makes anything before it not "preceding"
    in any sense a caller can use. This supersedes the v0 behavior, which
    walked past the compaction to the assistant message before it."""
    run = _load("session_compaction")
    attempt = run.attempts[0]
    assert attempt.interrupt.preceding is None


def test_session_compaction_event_itself_has_empty_text_and_no_markers():
    run = _load("session_compaction")
    attempt = run.attempts[0]
    compaction_events = [event for event in attempt.events if event.kind == "compaction"]
    assert len(compaction_events) == 1
    assert compaction_events[0].text == ""
    assert compaction_events[0].markers == frozenset()


# --- thinking blocks -------------------------------------------------------


def test_thinking_block_becomes_a_thinking_event_with_empty_text():
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reasoning the harness should never surface"},
                {"type": "text", "text": "Here is my answer."},
            ],
            "usage": {"input_tokens": 7, "output_tokens": 11},
        },
    }
    events = reader._assistant_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "assistant", {}
    )
    assert len(events) == 2
    assert events[0].kind == "thinking"
    assert events[0].text == ""
    assert events[0].markers == frozenset()
    # Usage is attached to exactly one block in message order; the thinking
    # block came first, so it carries it and the text block does not.
    assert events[0].usage == {"input_tokens": 7, "output_tokens": 11}
    assert events[1].kind == "assistant"
    assert events[1].usage == {}


# --- ParentIndex -----------------------------------------------------------


def _write_parent_transcript(path: Path) -> None:
    path.write_text(
        "\n".join([
            (
                '{"type":"assistant","message":{"role":"assistant","content":'
                '[{"type":"tool_use","id":"toolu_1","name":"TaskStop",'
                '"input":{"task_id":"task-abc","agent_id":"ac900000000000001"}}]},'
                '"sessionId":"' + FAKE_UUID + '","timestamp":"2026-09-17T09:02:50Z"}'
            ),
            (
                '{"type":"system","subtype":"compact_boundary",'
                '"sessionId":"' + FAKE_UUID + '","timestamp":"2026-09-17T09:03:00Z"}'
            ),
            (
                '{"type":"user","message":{"role":"user","content":'
                '[{"type":"text","text":"[Request interrupted by user]"}]},'
                '"sessionId":"' + FAKE_UUID + '","timestamp":"2026-09-17T09:03:10Z"}'
            ),
            # An unrelated assistant line with no tool_use must not pollute the index.
            (
                '{"type":"assistant","message":{"role":"assistant","content":'
                '[{"type":"text","text":"unrelated narration"}]},'
                '"sessionId":"' + FAKE_UUID + '","timestamp":"2026-09-17T09:03:20Z"}'
            ),
        ]) + "\n",
        encoding="utf-8",
    )


def test_parent_index_keeps_only_task_stops_kill_and_compaction_timestamps(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    _write_parent_transcript(parent_path)

    index = reader.ParentIndex.for_path(parent_path)

    assert len(index.task_stops) == 1
    assert index.task_stops[0].ids == frozenset({"task-abc", "ac900000000000001"})
    assert len(index.kill_timestamps) == 1
    assert len(index.compaction_timestamps) == 1


def test_parent_index_for_path_caches_by_path(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    _write_parent_transcript(parent_path)

    first = reader.ParentIndex.for_path(parent_path)
    # Mutate the file after the first scan; a cached second call must not
    # see the change (it is a process-level cache, scanned once).
    parent_path.write_text("", encoding="utf-8")
    second = reader.ParentIndex.for_path(parent_path)

    assert second is first
    assert len(second.task_stops) == 1


def test_parent_index_on_a_missing_file_is_empty(tmp_path):
    index = reader.ParentIndex.for_path(tmp_path / "does-not-exist.jsonl")
    assert index.task_stops == []
    assert index.kill_timestamps == []
    assert index.compaction_timestamps == []


def test_parent_index_finds_a_kill_sentinel_given_as_a_plain_string_list_item(tmp_path):
    """docs/DESIGN.md section 3: a user content list item may be a bare
    string, not only a {"type":"text",...} block. ParentIndex._scan must
    still recognize the kill sentinel when it arrives that way."""
    parent_path = tmp_path / "parent_string_sentinel.jsonl"
    parent_path.write_text(
        '{"type":"user","message":{"role":"user","content":'
        '["[Request interrupted by user]"]},'
        '"sessionId":"' + FAKE_UUID + '","timestamp":"2026-09-17T09:03:10Z"}\n',
        encoding="utf-8",
    )

    index = reader.ParentIndex.for_path(parent_path)

    assert len(index.kill_timestamps) == 1


def test_parent_index_tolerates_a_non_object_message_without_crashing(tmp_path):
    """A malformed parent transcript line ("message" present but not an
    object) must become a diagnostic, never an uncaught AttributeError that
    aborts the whole scan."""
    parent_path = tmp_path / "parent_bad_message.jsonl"
    parent_path.write_text(
        '{"type":"user","message":[1],'
        '"sessionId":"' + FAKE_UUID + '","timestamp":"2026-09-17T09:03:10Z"}\n',
        encoding="utf-8",
    )

    index = reader.ParentIndex.for_path(parent_path)

    assert index.task_stops == []
    assert index.kill_timestamps == []
    assert any("message is not an object" in diag for diag in index.diagnostics)


# --- session public id collision --------------------------------------


def test_discover_runs_gives_two_colliding_sessions_distinct_12_hex_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(reader, "_SESSION_ID_REGISTRY", {})
    # A payload-dependent forced digest: identical first 10 hex characters
    # (forcing a collision on the short id) but distinct trailing
    # characters, so the 12-hex fallback can actually disambiguate the two
    # sessions -- unlike a digest that ignores its payload and returns the
    # same value for both, which would hide whether disambiguation works at
    # all. This is the seam docs/DESIGN.md 4.2 calls for testing collision
    # detection through.
    def fake_digest(payload: str) -> str:
        suffix = "1" if "-home-user-project" in payload else "2"
        return "a" * 10 + suffix * 54

    monkeypatch.setattr(reader, "_session_id_digest", fake_digest)

    # Both sessions share the one fake UUID tests/fixtures/README.md
    # mandates; they collide on slug alone, which is enough to exercise two
    # distinct (slug, uuid) pairs landing on the same forced digest.
    _write_minimal_session_file(tmp_path, "-home-user-project", FAKE_UUID)
    _write_minimal_session_file(tmp_path, "-home-user-other", FAKE_UUID)

    found = reader.discover_runs(tmp_path, kinds=frozenset({"session"}))
    assert len(found) == 2
    assert {ref.run_id for ref in found} == {"s_" + "a" * 10 + "11", "s_" + "a" * 10 + "22"}

    # A later, individual load_session call for either path also sees the
    # collision (the registry is process-level), keeps the same distinct
    # 12-hex id discovery already assigned it, and records the diagnostic.
    for ref in found:
        run = reader.load_session(ref.path)
        assert run.run_id == ref.run_id
        assert any("collision" in diag for diag in run.diagnostics)


def _write_minimal_session_file(root: Path, slug: str, uuid: str) -> Path:
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    session_path = slug_dir / f"{uuid}.jsonl"
    session_path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},'
        f'"sessionId":"{uuid}","timestamp":"2026-09-01T10:00:00Z"}}\n',
        encoding="utf-8",
    )
    return session_path
