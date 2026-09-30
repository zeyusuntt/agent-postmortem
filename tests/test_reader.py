"""Tests for reader.py against the synthetic reader_minimal fixture.

reader_minimal mirrors the real <root>/<project>/<session>/subagents/workflows
layout so discover_runs() and load_run() both exercise the code paths they
use against a real ~/.claude/projects tree.
"""

import os
import re
from pathlib import Path

import pytest

from agent_postmortem import reader
from agent_postmortem.markers import GIT_NOT_REPO, KILL_SENTINEL_PREFIX, SHELL_GUARD_BLOCK_REGEX

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs" / "reader_minimal"
WORKFLOWS_DIR = (
    FIXTURES_ROOT
    / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000"
    / "subagents"
    / "workflows"
)
MAIN_RUN = WORKFLOWS_DIR / "wf_b0000000-002"


def _load_main_run():
    return reader.load_run(MAIN_RUN)


def test_discover_runs_finds_every_wf_dir_sorted():
    # docs/DESIGN.md 4.2 (v0.1): discover_runs now returns RunRef objects,
    # not bare Paths, so both run kinds carry a public run_id alongside path.
    found = reader.discover_runs(FIXTURES_ROOT)
    assert [ref.run_id for ref in found] == [
        "wf_a0000000-001",
        "wf_b0000000-002",
        "wf_c0000000-003",
    ]
    assert {ref.kind for ref in found} == {"workflow"}


def test_discover_runs_on_missing_root_returns_empty_list(tmp_path):
    assert reader.discover_runs(tmp_path / "does-not-exist") == []


# --- discover_runs: both kinds (docs/DESIGN.md 4.2) ------------------------


def _write_minimal_session(root: Path, slug: str, uuid: str) -> Path:
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    session_path = slug_dir / f"{uuid}.jsonl"
    session_path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},'
        f'"sessionId":"{uuid}","timestamp":"2026-09-01T10:00:00Z"}}\n',
        encoding="utf-8",
    )
    return session_path


def _write_minimal_workflow(root: Path, slug: str, uuid: str, wf_id: str) -> Path:
    run_dir = root / slug / uuid / "subagents" / "workflows" / wf_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "journal.jsonl").write_text("", encoding="utf-8")
    return run_dir


def test_discover_runs_returns_both_kinds_sorted_by_path(tmp_path):
    session_path = _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    workflow_path = _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-001"
    )
    found = reader.discover_runs(tmp_path)
    assert [ref.path for ref in found] == sorted([session_path, workflow_path])
    assert {ref.kind for ref in found} == {"workflow", "session"}


def test_discover_runs_kind_filter_workflow_only(tmp_path):
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-001"
    )
    found = reader.discover_runs(tmp_path, kinds=frozenset({"workflow"}))
    assert [ref.kind for ref in found] == ["workflow"]


def test_discover_runs_kind_filter_session_only(tmp_path):
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-001"
    )
    found = reader.discover_runs(tmp_path, kinds=frozenset({"session"}))
    assert [ref.kind for ref in found] == ["session"]


def test_session_run_id_is_s_plus_ten_hex_chars(tmp_path):
    session_path = _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    found = reader.discover_runs(tmp_path, kinds=frozenset({"session"}))
    assert len(found) == 1
    assert re.fullmatch(r"s_[0-9a-f]{10}", found[0].run_id)
    assert found[0].path == session_path


def _write_bridge_session(root: Path, slug: str, uuid: str) -> Path:
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    session_path = slug_dir / f"{uuid}.jsonl"
    session_path.write_text(
        f'{{"type":"bridge-session","sessionId":"{uuid}","cwd":"/home/user/project"}}\n',
        encoding="utf-8",
    )
    return session_path


def test_discover_runs_skips_a_bridge_session_file_with_no_timestamped_entry(tmp_path):
    """docs/DESIGN.md 4.2: a session file with no timestamped user/assistant
    entry (a bridge-session marker, observed on real ~/.claude/projects
    trees) is not a run and must not show up in discovery at all."""
    _write_bridge_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    found = reader.discover_runs(tmp_path, kinds=frozenset({"session"}))
    assert found == []


def test_discover_runs_still_finds_a_real_session_alongside_a_bridge_session(tmp_path):
    real_path = _write_minimal_session(tmp_path, "-home-user-other", "00000000-0000-4000-8000-000000000000")
    _write_bridge_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    found = reader.discover_runs(tmp_path, kinds=frozenset({"session"}))
    assert [ref.path for ref in found] == [real_path]


def test_load_session_raises_not_a_run_for_a_bridge_session_file():
    session_path = (
        Path(__file__).parent / "fixtures" / "runs" / "session_bridge_only" / "-home-user-project"
        / "00000000-0000-4000-8000-000000000000.jsonl"
    )
    try:
        reader.load_session(session_path)
        assert False, "expected NotARun"
    except reader.NotARun:
        pass


def test_load_run_also_raises_not_a_run_for_a_bridge_session_file():
    session_path = (
        Path(__file__).parent / "fixtures" / "runs" / "session_bridge_only" / "-home-user-project"
        / "00000000-0000-4000-8000-000000000000.jsonl"
    )
    try:
        reader.load_run(session_path)
        assert False, "expected NotARun"
    except reader.NotARun:
        pass


# --- load_run dispatch and NotARun -----------------------------------------


def test_load_run_dispatches_a_session_path():
    session_path = (
        Path(__file__).parent / "fixtures" / "runs" / "session_clean" / "-home-user-project"
        / "00000000-0000-4000-8000-000000000000.jsonl"
    )
    run = reader.load_run(session_path)
    assert run.kind == "session"
    assert run.main is not None
    assert run.main.label == "main"


def test_load_run_raises_not_a_run_for_an_arbitrary_file(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("not a run\n", encoding="utf-8")
    try:
        reader.load_run(readme)
        assert False, "expected NotARun"
    except reader.NotARun:
        pass


def test_load_run_raises_not_a_run_for_a_directory_without_journal(tmp_path):
    empty_dir = tmp_path / "not-a-run"
    empty_dir.mkdir()
    try:
        reader.load_run(empty_dir)
        assert False, "expected NotARun"
    except reader.NotARun:
        pass


def test_load_run_on_a_workflow_ref_without_journal_loads_diagnostically(tmp_path):
    """discover_runs finds every wf_* directory regardless of contents (docs/
    DESIGN.md 4.2); a stray/incomplete one must still load through the
    RunRef it produced, with a missing-journal diagnostic, not raise NotARun
    and abort a fleet scan."""
    run_dir = (
        tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
        / "subagents" / "workflows" / "wf_00000000-777"
    )
    run_dir.mkdir(parents=True)
    refs = reader.discover_runs(tmp_path, kinds=frozenset({"workflow"}))
    assert len(refs) == 1
    run = reader.load_run(refs[0])
    assert run.kind == "workflow"
    assert run.attempts == []
    assert any("journal.jsonl" in diag for diag in run.diagnostics)


def test_sniff_session_file_skips_a_malformed_leading_line(tmp_path):
    """A malformed physical line before the first decodable one must not
    make a real session file sniff as 'not a run' (docs/DESIGN.md 4.2's
    'first parsed line' means the first line that decodes, not the first
    physical line)."""
    session_path = tmp_path / "00000000-0000-4000-8000-000000000000.jsonl"
    session_path.write_text(
        "not valid json at all\n"
        '{"type":"user","message":{"role":"user","content":"hi"},'
        '"sessionId":"00000000-0000-4000-8000-000000000000",'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    assert reader._sniff_session_file(session_path) is True
    run = reader.load_run(session_path)
    assert run.kind == "session"
    assert any(session_path.name in diag for diag in run.diagnostics)


def test_load_run_groups_two_attempts_under_one_logical_agent():
    run = _load_main_run()
    assert len(run.attempts) == 2
    assert len(run.agents) == 1
    agent = run.agents[0]
    assert agent.key == "v2:demo0001"
    assert sorted(attempt.ordinal for attempt in agent.attempts) == [1, 2]


def test_ordinals_follow_journal_order():
    run = _load_main_run()
    by_agent_id = {attempt.agent_id: attempt for attempt in run.attempts}
    assert by_agent_id["a1111111111111111"].ordinal == 1
    assert by_agent_id["a2222222222222222"].ordinal == 2


def test_first_attempt_is_killed_second_completes():
    run = _load_main_run()
    by_ordinal = sorted(run.attempts, key=lambda a: a.ordinal)
    assert by_ordinal[0].outcome == "killed"
    assert by_ordinal[1].outcome == "completed"


def test_kill_sentinel_is_the_last_user_kind_event():
    run = _load_main_run()
    killed_attempt = sorted(run.attempts, key=lambda a: a.ordinal)[0]
    user_events = [event for event in killed_attempt.events if event.kind == "user"]
    assert user_events[-1].text.startswith(KILL_SENTINEL_PREFIX)


def test_tool_use_and_tool_result_resolve_tool_name():
    run = _load_main_run()
    killed_attempt = sorted(run.attempts, key=lambda a: a.ordinal)[0]
    tool_uses = [event for event in killed_attempt.events if event.kind == "tool_use"]
    tool_results = [event for event in killed_attempt.events if event.kind == "tool_result"]
    assert len(tool_uses) == 1
    assert tool_uses[0].tool_name == "Bash"
    assert len(tool_results) == 1
    assert tool_results[0].tool_name == "Bash"


def test_tool_result_exit_code_and_is_error_parsed():
    run = _load_main_run()
    killed_attempt = sorted(run.attempts, key=lambda a: a.ordinal)[0]
    tool_result = [event for event in killed_attempt.events if event.kind == "tool_result"][0]
    assert tool_result.exit_code == 143
    assert tool_result.is_error is True


def test_second_attempt_tool_result_has_no_error_or_exit_code():
    run = _load_main_run()
    completed_attempt = sorted(run.attempts, key=lambda a: a.ordinal)[1]
    tool_result = [event for event in completed_attempt.events if event.kind == "tool_result"][0]
    assert tool_result.tool_name == "Read"
    assert tool_result.is_error is False
    assert tool_result.exit_code is None


def test_assistant_usage_is_captured():
    """The killed attempt's one assistant message (a tool_use plus a text
    block) carries usage exactly once across the blocks it produced, not per
    block, so the attempt-level sum is neither lost nor doubled."""
    run = _load_main_run()
    killed_attempt = sorted(run.attempts, key=lambda a: a.ordinal)[0]
    message_events = [event for event in killed_attempt.events if event.kind in ("assistant", "tool_use")]
    carriers = [event for event in message_events if event.usage]
    assert len(carriers) == 1
    assert carriers[0].usage.get("input_tokens") == 10
    assert killed_attempt.usage.get("input_tokens") == 10
    assert killed_attempt.usage.get("output_tokens") == 20


def test_bad_json_line_becomes_exactly_one_diagnostic():
    run = _load_main_run()
    assert len(run.diagnostics) == 1
    assert "agent-a1111111111111111.jsonl" in run.diagnostics[0]


def _write_workflow(run_dir: Path, agent_id: str) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(
        "\n".join([
            '{"type":"launched"}',
            '{"type":"started","key":"v2:demo0001","agentId":"' + agent_id
            + '","label":"impl:demo","phase":"Implement"}',
            '{"type":"result","key":"v2:demo0001","agentId":"' + agent_id + '","result":"completed"}',
        ]),
        encoding="utf-8",
    )


def test_malformed_journal_agent_id_is_replaced_and_diagnosed_without_echo(tmp_path):
    """docs/DESIGN.md 5.3: a workflow agent id must be 'a' + 16 hex chars.
    A malformed one (here one that happens to carry a name) must never reach
    Attempt.agent_id or a diagnostic verbatim."""
    run_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000" \
        / "subagents" / "workflows" / "wf_00000000-799"
    _write_workflow(run_dir, "alice-client-x")
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    agent_id = run.attempts[0].agent_id
    assert re.match(r"^agent-[0-9a-f]{8}$", agent_id)
    assert any("agent id did not match the expected format" in diag for diag in run.diagnostics)
    assert not any("alice-client-x" in diag for diag in run.diagnostics)


def test_conforming_journal_agent_id_is_untouched(tmp_path):
    run_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000" \
        / "subagents" / "workflows" / "wf_00000000-798"
    _write_workflow(run_dir, "a1234567890abcdef")
    run = reader.load_run(run_dir)
    assert run.attempts[0].agent_id == "a1234567890abcdef"
    assert run.diagnostics == []


def test_malformed_workflow_run_directory_name_is_replaced_and_diagnosed_without_echo(tmp_path):
    """docs/DESIGN.md 5.3: a workflow run id must be 'wf_' + 8 hex + '-' +
    3 hex. A directory name that does not conform must never reach
    Run.run_id or a diagnostic verbatim."""
    run_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000" \
        / "subagents" / "workflows" / "wf_not-conforming-name"
    _write_workflow(run_dir, "a1234567890abcdef")
    run = reader.load_run(run_dir)
    assert re.match(r"^wf_[0-9a-f]{8}-x$", run.run_id)
    assert any("run id did not match the expected format" in diag for diag in run.diagnostics)
    assert not any("not-conforming-name" in diag for diag in run.diagnostics)


def test_run_metadata_is_resolved():
    run = _load_main_run()
    assert run.run_id == "wf_b0000000-002"
    assert run.session_id == "00000000-0000-4000-8000-000000000000"
    assert run.claude_version == "2.1.267"
    assert run.project_slug == "-home-user-project"


def test_started_at_and_ended_at_span_every_event():
    run = _load_main_run()
    assert run.started_at is not None
    assert run.ended_at is not None
    assert run.started_at < run.ended_at


# --- usage accounting: exactly once per assistant transcript entry --------


def test_completed_attempt_usage_includes_a_tool_only_message():
    """agent-a2222222222222222.jsonl's second line is a Read tool_use with no
    text block; its usage (5 input, 8 output) must still be counted, on top
    of the later text-only message's usage (3, 4)."""
    run = _load_main_run()
    completed_attempt = sorted(run.attempts, key=lambda a: a.ordinal)[1]
    assert completed_attempt.usage.get("input_tokens") == 8
    assert completed_attempt.usage.get("output_tokens") == 12


def test_assistant_tool_use_only_message_keeps_its_usage():
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}],
            "usage": {"input_tokens": 5, "output_tokens": 8},
        },
    }
    events = reader._assistant_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "assistant", {}
    )
    assert len(events) == 1
    assert events[0].usage == {"input_tokens": 5, "output_tokens": 8}


def test_assistant_two_text_blocks_attach_usage_exactly_once():
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "First half."},
                {"type": "text", "text": "Second half."},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }
    events = reader._assistant_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "assistant", {}
    )
    assert len(events) == 2
    non_empty = [event.usage for event in events if event.usage]
    assert non_empty == [{"input_tokens": 10, "output_tokens": 20}]


# --- tolerant parsing: valid JSON in the wrong shape -----------------------


def _minimal_started_journal_line() -> str:
    return (
        '{"type":"started","key":"v2:a","agentId":"a1111111111111111",'
        '"label":"impl:x","phase":"Implement"}\n'
    )


def test_non_dict_journal_line_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_x"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(
        "[1, 2, 3]\n" + _minimal_started_journal_line(), encoding="utf-8"
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert any("journal.jsonl" in diag for diag in run.diagnostics)


def test_non_dict_meta_json_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_y"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.meta.json").write_text("[1, 2, 3]", encoding="utf-8")
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert run.attempts[0].meta == {}
    assert any("meta.json" in diag for diag in run.diagnostics)


def test_non_dict_transcript_line_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_z"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        "[1, 2, 3]\n"
        '{"type":"user","message":{"role":"user","content":"hi"},'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert len(run.attempts[0].events) == 1
    assert any("agent-a1111111111111111.jsonl" in diag for diag in run.diagnostics)


# --- tolerant parsing: malformed nested shapes -----------------------------


def test_user_message_not_an_object_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_msg_user"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"user","message":[1],"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert run.attempts[0].events == []
    assert any("message is not an object" in diag for diag in run.diagnostics)


def test_assistant_message_not_an_object_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_msg_assistant"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"assistant","message":[1],"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert any("message is not an object" in diag for diag in run.diagnostics)


def test_assistant_usage_not_an_object_is_dropped_with_a_diagnostic(tmp_path):
    run_dir = tmp_path / "wf_usage_shape"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"hi"}],"usage":[1,2]},'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert run.attempts[0].usage == {}
    assert any("usage is not an object" in diag for diag in run.diagnostics)


def test_assistant_block_text_not_a_string_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_text_shape"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":123}]},'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert run.attempts[0].events[0].text == ""
    assert any("block text is not a string" in diag for diag in run.diagnostics)


def test_assistant_tool_use_id_not_a_string_does_not_crash(tmp_path):
    """A malformed (non-string) tool_use id must not be stored as a dict key
    (it would raise TypeError: unhashable type for a list/dict id); the
    tool_use event itself is still produced, just without a resolvable
    tool_result lookup later."""
    run_dir = tmp_path / "wf_tool_use_id_shape"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"tool_use","id":["x"],"name":"Bash","input":{}}]},'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert run.attempts[0].events[0].tool_name == "Bash"


def test_assistant_tool_use_input_not_an_object_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_tool_input_shape"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":[1,2]}]},'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    assert any("tool_use input is not an object" in diag for diag in run.diagnostics)


# --- duplicate tool_use_id (docs/DESIGN.md 4.2, F5 hardening) --------------


def test_duplicate_tool_use_id_in_one_transcript_diagnostic_incomplete_and_ambiguous(tmp_path):
    """Two tool_use blocks sharing a tool_use_id in the same transcript: the
    reader must diagnose it, force Run.status INCOMPLETE, and mark every
    ToolCall that shares the id ambiguous (never counted as ok/error by a
    downstream consumer like `rescue status`)."""
    run_dir = tmp_path / "wf_dup_tool_id"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"tool_use","id":"toolu_dup","name":"Bash",'
        '"input":{"command":"echo one"}}]},"timestamp":"2026-09-01T10:00:00Z"}\n'
        '{"type":"user","message":{"role":"user","content":['
        '{"type":"tool_result","tool_use_id":"toolu_dup","content":"one","is_error":false}]},'
        '"timestamp":"2026-09-01T10:00:01Z"}\n'
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"tool_use","id":"toolu_dup","name":"Bash",'
        '"input":{"command":"echo two"}}]},"timestamp":"2026-09-01T10:00:02Z"}\n'
        '{"type":"user","message":{"role":"user","content":['
        '{"type":"tool_result","tool_use_id":"toolu_dup","content":"two","is_error":false}]},'
        '"timestamp":"2026-09-01T10:00:03Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"
    assert any(
        "duplicate tool_use_id toolu_dup at lines 1 and 3" in diag for diag in run.diagnostics
    )
    (attempt,) = run.attempts
    assert len(attempt.tool_calls) == 2
    assert all(call.completion == "ambiguous" for call in attempt.tool_calls)


def test_attachment_not_an_object_becomes_a_diagnostic_not_a_crash(tmp_path):
    run_dir = tmp_path / "wf_attachment_shape"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"attachment","attachment":[1,2,3],"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    # attachment falls back to {} (not the malformed [1, 2, 3]); the event's
    # text is that empty object's JSON dump, the same fallback a real
    # attachment missing every known field would produce.
    assert run.attempts[0].events[0].text == "{}"
    assert any("attachment is not an object" in diag for diag in run.diagnostics)


# --- user content list may hold plain strings (docs/DESIGN.md section 3) --


def test_user_content_list_with_a_plain_string_becomes_a_user_event(tmp_path):
    """docs/DESIGN.md section 3: 'User blocks: tool_result (...) or a plain
    string.' A content list item can be a bare string, not only a dict block;
    it must still become a user Event, the same as a {"type":"text",...}
    block would."""
    run_dir = tmp_path / "wf_user_string_block"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":["hello there"]},'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    events = run.attempts[0].events
    assert len(events) == 1
    assert events[0].kind == "user"
    assert events[0].text == "hello there"


def test_kill_sentinel_as_a_plain_string_list_item_is_still_attributed_killed(tmp_path):
    """The same omission, at the point that matters most: a final user entry
    whose content is a list containing the bare kill-sentinel string (not a
    {"type":"text",...} block) must still flip the attempt's outcome to
    'killed' and give it an Interrupt, exactly as the dict-block form does."""
    run_dir = tmp_path / "wf_user_string_sentinel"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        "\n".join([
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"text","text":"working on it"}]},"timestamp":"2026-09-01T10:00:00Z"}',
            '{"type":"user","message":{"role":"user","content":'
            f'["{KILL_SENTINEL_PREFIX}]"]}},"timestamp":"2026-09-01T10:00:05Z"}}',
        ]) + "\n",
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert len(run.attempts) == 1
    attempt = run.attempts[0]
    assert attempt.outcome == "killed"
    assert attempt.interrupt is not None
    assert attempt.interrupt.kill_ts is not None


# --- usage never silently disappears (docs/DESIGN.md 4.2) ------------------


def test_assistant_empty_content_still_carries_usage_via_an_unknown_event():
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        },
    }
    events = reader._assistant_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "assistant", {}
    )
    assert len(events) == 1
    assert events[0].kind == "unknown"
    assert events[0].usage == {"input_tokens": 5, "output_tokens": 7}


def test_assistant_unknown_only_block_still_carries_usage():
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "future_block"}],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        },
    }
    events = reader._assistant_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "assistant", {}
    )
    assert len(events) == 1
    assert events[0].kind == "unknown"
    assert events[0].usage == {"input_tokens": 5, "output_tokens": 7}


# --- malformed timestamps ---------------------------------------------------


def test_malformed_timestamp_becomes_a_diagnostic(tmp_path):
    run_dir = tmp_path / "wf_ts"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"not-a-timestamp"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert run.attempts[0].events[0].ts is None
    assert any(
        "timestamp" in diag and "agent-a1111111111111111.jsonl" in diag for diag in run.diagnostics
    )


def test_malformed_timestamp_diagnostic_never_echoes_the_raw_value(tmp_path):
    """docs/DESIGN.md 4.2/5.2: a diagnostic is a fixed reason phrase plus
    file name and line number, never a repr() of transcript-derived text --
    a malformed timestamp field could carry anything, including a secret a
    prompt pasted into the wrong field."""
    secret = "Project Phoenix secret"
    run_dir = tmp_path / "wf_ts2"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"' + secret + '"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert not any(secret in diag for diag in run.diagnostics)
    assert any("timestamp could not be parsed" in diag for diag in run.diagnostics)


def test_malformed_message_field_diagnostic_never_echoes_the_raw_value(tmp_path):
    """Same guarantee as the timestamp case, for a malformed 'message' field
    (docs/DESIGN.md section 3 expects an object there)."""
    run_dir = tmp_path / "wf_ts3"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"user","message":"Project Phoenix secret"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert not any("Project Phoenix secret" in diag for diag in run.diagnostics)
    assert any("message is not an object" in diag for diag in run.diagnostics)


# --- exit-code detection must not be blinded by the excerpt limit ----------


def test_exit_code_detected_even_when_it_falls_after_the_excerpt_limit():
    padding = "x" * 450
    content = f"{padding}\nExit code 143"
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": content, "is_error": True}
            ],
        },
    }
    events = reader._user_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {"toolu_1": "Bash"}
    )
    assert len(events) == 1
    assert events[0].exit_code == 143
    assert len(events[0].text) <= 400


def test_a_marker_past_the_excerpt_limit_is_still_recorded_on_the_event():
    """A denial marker, a git error, or any other known runtime marker must
    not disappear just because it happens to land after the 400-char
    excerpt: reader.py scans the full field before truncating it and
    records what it found on Event.markers."""
    padding = "x" * 450
    content = f"{padding}\n{GIT_NOT_REPO[0]}"
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": content, "is_error": True}
            ],
        },
    }
    events = reader._user_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {"toolu_1": "Bash"}
    )
    assert len(events) == 1
    assert GIT_NOT_REPO[0] in events[0].markers
    assert len(events[0].text) <= 400
    # The excerpt itself is still just the bounded prefix: the marker is
    # nowhere in events[0].text, only in the derived .markers set.
    assert GIT_NOT_REPO[0] not in events[0].text


def test_scan_markers_matches_the_shell_guard_regex():
    text = "Security: node blocked in workspace"
    assert re.search(SHELL_GUARD_BLOCK_REGEX, text)
    assert "SHELL_GUARD_BLOCK_REGEX" in reader._scan_markers(text)


def test_scan_markers_on_empty_or_none_text_is_empty():
    assert reader._scan_markers("") == frozenset()
    assert reader._scan_markers(None) == frozenset()


# --- harness noise stripping (docs/DESIGN.md 4.5) --------------------------


def test_system_reminder_span_is_stripped_from_the_excerpt():
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": "Real answer. <system-reminder>internal note</system-reminder> More real text.",
            }],
        },
    }
    events = reader._assistant_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "assistant", {}
    )
    assert len(events) == 1
    assert "<system-reminder>" not in events[0].text
    assert "internal note" not in events[0].text
    assert "Real answer." in events[0].text
    assert "More real text." in events[0].text


def test_total_tokens_span_is_stripped_from_the_excerpt():
    entry = {
        "type": "user",
        "message": {"role": "user", "content": "Before. <total_tokens>12345</total_tokens> After."},
    }
    events = reader._user_events(entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {})
    assert len(events) == 1
    assert "<total_tokens>" not in events[0].text
    assert "12345" not in events[0].text
    assert "Before." in events[0].text
    assert "After." in events[0].text


def test_harness_note_span_is_stripped_from_the_excerpt():
    entry = {
        "type": "user",
        "message": {"role": "user", "content": "Before. [harness: computed task] After."},
    }
    events = reader._user_events(entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {})
    assert len(events) == 1
    assert "[harness:" not in events[0].text
    assert "Before." in events[0].text
    assert "After." in events[0].text


def test_a_marker_hidden_inside_a_stripped_span_is_still_recorded():
    """markers.py scans the raw text before reader.py strips harness noise
    (docs/DESIGN.md 4.5): a marker fragment that happens to land inside a
    <system-reminder> span must still be seen even though the span itself
    never survives into the excerpt."""
    content = f"<system-reminder>{GIT_NOT_REPO[0]}</system-reminder>"
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}],
        },
    }
    events = reader._user_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {"toolu_1": "Bash"}
    )
    assert len(events) == 1
    assert GIT_NOT_REPO[0] in events[0].markers
    assert GIT_NOT_REPO[0] not in events[0].text


# --- read_only_output (docs/DESIGN.md 4.3's self-reference rule, D-A) -----


def _bash_tool_use_and_result(command: str, result_text: str):
    tool_names = {"toolu_1": "Bash"}
    tool_commands = {"toolu_1": command}
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": result_text}],
        },
    }
    return reader._user_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", tool_names, tool_commands
    )


def test_read_tool_result_is_read_only_output():
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file contents"}],
        },
    }
    events = reader._user_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {"toolu_1": "Read"}
    )
    assert events[0].read_only_output is True


def test_write_tool_result_is_not_read_only_output():
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
        },
    }
    events = reader._user_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {"toolu_1": "Write"}
    )
    assert events[0].read_only_output is False


def test_bash_cat_command_is_read_only_output():
    events = _bash_tool_use_and_result("cat src/agent_postmortem/markers.py", "file text")
    assert events[0].read_only_output is True


def test_bash_command_after_cd_and_env_prefix_is_still_read_only():
    events = _bash_tool_use_and_result(
        "cd /home/user/project && PYTHONPATH=src sed -n '1,40p' src/x.py", "file text",
    )
    assert events[0].read_only_output is True


def test_bash_git_show_is_read_only_output():
    events = _bash_tool_use_and_result("git show HEAD:docs/DESIGN.md", "diff text")
    assert events[0].read_only_output is True


def test_bash_git_status_is_not_read_only_output():
    """git status is a real write-adjacent command (it can trigger index
    locking, staging side effects some tooling relies on); only the
    specific read-only git subcommands docs/DESIGN.md 4.3 lists count."""
    events = _bash_tool_use_and_result("git status", "clean")
    assert events[0].read_only_output is False


def test_bash_node_command_is_not_read_only_output():
    events = _bash_tool_use_and_result("node codex-companion.mjs task run", "WRITE_DENIED")
    assert events[0].read_only_output is False


def test_bash_loop_wrapping_cat_is_read_only_output():
    """docs/DESIGN.md 4.3's D-B: a read command need not be the first word
    of the command text; a loop wrapping `cat` still counts."""
    events = _bash_tool_use_and_result(
        'for f in src/x.py; do echo "=== $f ==="; cat "$f"; done', "file text",
    )
    assert events[0].read_only_output is True


def test_bash_command_piping_cat_into_node_is_not_read_only():
    """docs/DESIGN.md 4.3's D-B: a read command's presence is not enough --
    a write/execute-indicating token anywhere in the same command text
    disqualifies it (here, `cat` feeding `node`)."""
    events = _bash_tool_use_and_result("cat plan.json | node apply.mjs", "WRITE_DENIED")
    assert events[0].read_only_output is False


def test_bash_command_with_redirection_is_not_read_only():
    """docs/DESIGN.md 4.3's D-B: '>' redirection means the command writes,
    even though it also contains a read command."""
    events = _bash_tool_use_and_result("cat notes.md > /tmp/copy.md", "ok")
    assert events[0].read_only_output is False


def test_bash_uv_run_is_not_read_only():
    events = _bash_tool_use_and_result("uv run pytest tests/test_x.py -q", "WRITE_DENIED")
    assert events[0].read_only_output is False


def test_bash_command_with_no_paired_tool_use_is_not_read_only():
    """A tool_result whose tool_use_id has no entry in tool_commands (the
    paired tool_use was never seen, or carried no command) must not be
    guessed at."""
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_missing", "content": "ok"}],
        },
    }
    events = reader._user_events(
        entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "user", {"toolu_missing": "Bash"}, {},
    )
    assert events[0].read_only_output is False


def test_assistant_events_records_the_bash_command_for_later_tool_result_lookup():
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "ls -la"}}],
        },
    }
    tool_names: dict = {}
    tool_commands: dict = {}
    reader._assistant_events(entry, Path("/home/user/project/agent-x.jsonl"), 1, None, "assistant", tool_names, tool_commands)
    assert tool_commands["toolu_9"] == "ls -la"


# --- load_session_view (docs/DESIGN.md 7.2's D6) ----------------------------

SESSION_ENVELOPE_ROOT = Path(__file__).parent / "fixtures" / "runs" / "session_envelope" / "-home-user-project"
SESSION_ENVELOPE_MAIN = SESSION_ENVELOPE_ROOT / "00000000-0000-4000-8000-000000000000.jsonl"
SESSION_ENVELOPE_CHILD = (
    SESSION_ENVELOPE_ROOT
    / "00000000-0000-4000-8000-000000000000"
    / "subagents"
    / "agent-ac900000000000001.jsonl"
)


def test_load_session_view_parses_only_the_named_subagent():
    run = reader.load_session_view(SESSION_ENVELOPE_MAIN, [SESSION_ENVELOPE_CHILD])
    assert len(run.attempts) == 1
    assert run.attempts[0].transcript_path == SESSION_ENVELOPE_CHILD
    assert run.main is not None
    assert run.main.label == "main"


def test_load_session_view_with_no_subagents_is_main_only():
    run = reader.load_session_view(SESSION_ENVELOPE_MAIN, [])
    assert run.attempts == []
    assert run.agents == []
    assert run.main is not None


def test_load_session_view_status_reflects_only_what_it_parsed():
    """A view over just the main transcript plus one clean subagent must
    not be INCOMPLETE because some OTHER subagent elsewhere is broken --
    load_session_view is never handed that other subagent's path at all."""
    run = reader.load_session_view(SESSION_ENVELOPE_MAIN, [SESSION_ENVELOPE_CHILD])
    assert run.status == "COMPLETE"


def test_load_session_view_shares_a_cached_main_parse_across_calls():
    cache: dict = {}
    reader.load_session_view(SESSION_ENVELOPE_MAIN, [SESSION_ENVELOPE_CHILD], main_cache=cache)
    assert SESSION_ENVELOPE_MAIN in cache
    first_main_attempt = cache[SESSION_ENVELOPE_MAIN][0]
    reader.load_session_view(SESSION_ENVELOPE_MAIN, [], main_cache=cache)
    assert cache[SESSION_ENVELOPE_MAIN][0] is first_main_attempt


def test_load_session_view_matches_attempt_by_path_not_a_rederived_id():
    """A subagent filename that does not conform to the documented agent id
    shape still gets an Attempt in the view (with a sanitized agent_id);
    matching it back by transcript_path, not by recomputing the id from the
    filename, must still find it."""
    run = reader.load_session_view(SESSION_ENVELOPE_MAIN, [SESSION_ENVELOPE_CHILD])
    matched = next((a for a in run.attempts if a.transcript_path == SESSION_ENVELOPE_CHILD), None)
    assert matched is not None
    assert matched.agent_id == "ac900000000000001"


# --- Run.finished (docs/DESIGN.md 7.4's D8) ---------------------------


def test_workflow_run_is_finished_when_every_started_key_has_a_result(tmp_path):
    run_dir = _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-799",
    )
    (run_dir / "journal.jsonl").write_text(
        '{"type":"launched","timestamp":"2026-01-01T00:00:00Z"}\n'
        '{"type":"started","timestamp":"2026-01-01T00:00:01Z","key":"v2:a",'
        '"agentId":"a1111111111111111","label":"impl:a","phase":"Implement"}\n'
        '{"type":"result","timestamp":"2026-01-01T00:00:02Z","agentId":"a1111111111111111",'
        '"key":"v2:a","outcome":"completed"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert run.finished is True


def test_workflow_run_is_not_finished_with_a_started_key_missing_its_result(tmp_path):
    run_dir = _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-798",
    )
    (run_dir / "journal.jsonl").write_text(
        '{"type":"launched","timestamp":"2026-01-01T00:00:00Z"}\n'
        '{"type":"started","timestamp":"2026-01-01T00:00:01Z","key":"v2:a",'
        '"agentId":"a1111111111111111","label":"impl:a","phase":"Implement"}\n',
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert run.finished is False


def test_workflow_run_with_no_started_key_at_all_is_not_finished(tmp_path):
    run_dir = _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-001",
    )
    run = reader.load_run(run_dir)
    assert run.finished is False


def test_session_run_is_always_finished(tmp_path):
    session_path = _write_minimal_session(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000",
    )
    run = reader.load_run(session_path)
    assert run.finished is True


# --- ParentIndex.tool_result_agent_ids (docs/DESIGN.md 7.2's D2) -----------


def test_parent_index_collects_agent_ids_from_tool_result_blocks(tmp_path):
    transcript = tmp_path / "main.jsonl"
    lines = [
        '{"type":"user","message":{"role":"user","content":"go"},'
        '"timestamp":"2026-01-01T00:00:00Z"}',
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"toolu_1","name":"Agent","input":{}}]},'
        '"timestamp":"2026-01-01T00:00:01Z"}',
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
        '"tool_use_id":"toolu_1","agentId":"ae300000000000009","content":"done"}]},'
        '"timestamp":"2026-01-01T00:00:02Z"}',
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    index = reader.ParentIndex._scan(transcript)
    assert "ae300000000000009" in index.tool_result_agent_ids


def test_parent_index_tool_result_agent_ids_empty_with_no_parent(tmp_path):
    index = reader.ParentIndex._scan(tmp_path / "does-not-exist.jsonl")
    assert index.tool_result_agent_ids == frozenset()


def test_parent_index_collects_agent_ids_from_entry_level_tool_use_result(tmp_path):
    """docs/DESIGN.md 7.2's D-A: a real main transcript carries the Agent
    tool's result at the entry level (entry["toolUseResult"]), not inside
    the tool_result block itself -- the block there carries only
    content/tool_use_id/type. Without this, no subagent ever gets
    authoritative finished evidence from the parent transcript at all."""
    transcript = tmp_path / "main.jsonl"
    lines = [
        '{"type":"user","message":{"role":"user","content":"go"},'
        '"timestamp":"2026-01-01T00:00:00Z"}',
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"toolu_1","name":"Agent","input":{}}]},'
        '"timestamp":"2026-01-01T00:00:01Z"}',
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
        '"tool_use_id":"toolu_1","content":"done"}]},'
        '"toolUseResult":{"isAsync":false,"status":"completed",'
        '"agentId":"ae400000000000001","description":"impl","resolvedModel":"x",'
        '"prompt":"do it","outputFile":null},'
        '"timestamp":"2026-01-01T00:00:02Z"}',
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    index = reader.ParentIndex._scan(transcript)
    assert "ae400000000000001" in index.tool_result_agent_ids


# --- Run.status (docs/DESIGN.md 4.6) ---------------------------------------


def test_status_is_incomplete_when_the_journal_has_an_undecodable_line(tmp_path):
    run_dir = tmp_path / "wf_bad_journal"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(
        "NOT-VALID-JSON\n" + _minimal_started_journal_line(), encoding="utf-8"
    )
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"


def test_status_stays_complete_on_a_clean_journal(tmp_path):
    run_dir = tmp_path / "wf_clean_journal"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    run = reader.load_run(run_dir)
    assert run.status == "COMPLETE"


def test_status_is_incomplete_when_a_transcript_exceeds_5_percent_undecodable(tmp_path):
    run_dir = tmp_path / "wf_bad_transcript"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    good_line = '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n'
    # 18 good lines plus 2 bad ones out of 20 total is 10%, comfortably over
    # the 5% threshold (see the boundary test below for exactly 5%).
    lines = [good_line] * 18 + ["NOT-VALID-JSON\n", "ALSO-NOT-VALID-JSON\n"]
    (run_dir / "agent-a1111111111111111.jsonl").write_text("".join(lines), encoding="utf-8")
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"


def test_status_stays_complete_at_exactly_5_percent_undecodable(tmp_path):
    run_dir = tmp_path / "wf_boundary_transcript"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    good_line = '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n'
    lines = [good_line] * 19 + ["NOT-VALID-JSON\n"]  # 1/20 = 5.0%, not over the threshold
    (run_dir / "agent-a1111111111111111.jsonl").write_text("".join(lines), encoding="utf-8")
    run = reader.load_run(run_dir)
    assert run.status == "COMPLETE"


def _skip_if_root():
    # root bypasses file permission bits entirely, so a chmod(0o000) test
    # would see the file as readable anyway and fail under a root test
    # runner.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")


def test_status_is_incomplete_when_a_transcript_is_unreadable(tmp_path):
    _skip_if_root()
    run_dir = tmp_path / "wf_unreadable"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    transcript = run_dir / "agent-a1111111111111111.jsonl"
    transcript.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8")
    transcript.chmod(0o000)
    try:
        run = reader.load_run(run_dir)
    finally:
        transcript.chmod(0o644)  # restore so tmp_path cleanup can remove it
    assert run.status == "INCOMPLETE"
    assert any("unreadable" in diag for diag in run.diagnostics)


def test_status_is_incomplete_when_the_journal_itself_is_unreadable(tmp_path):
    """docs/DESIGN.md 4.6 and section 2: a journal.jsonl that cannot even be
    opened (permission denied) must produce a diagnostic and INCOMPLETE
    status, not an uncaught PermissionError that aborts load_run."""
    _skip_if_root()
    run_dir = tmp_path / "wf_unreadable_journal"
    run_dir.mkdir()
    journal = run_dir / "journal.jsonl"
    journal.write_text(_minimal_started_journal_line(), encoding="utf-8")
    journal.chmod(0o000)
    try:
        run = reader.load_run(run_dir)
    finally:
        journal.chmod(0o644)  # restore so tmp_path cleanup can remove it
    assert run.status == "INCOMPLETE"
    assert any("journal.jsonl" in diag and "unreadable" in diag for diag in run.diagnostics)


def test_status_is_incomplete_when_the_journal_has_undecodable_bytes(tmp_path):
    """docs/DESIGN.md 4.6 and section 2: invalid UTF-8 in journal.jsonl must
    become a diagnostic and INCOMPLETE status, not an uncaught
    UnicodeDecodeError."""
    run_dir = tmp_path / "wf_undecodable_journal"
    run_dir.mkdir()
    journal = run_dir / "journal.jsonl"
    with journal.open("wb") as handle:
        handle.write(_minimal_started_journal_line().encode("utf-8"))
        handle.write(b"\xff\xfe-not-valid-utf8\n")
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"
    assert any("journal.jsonl" in diag and "undecodable" in diag for diag in run.diagnostics)


def test_status_is_incomplete_when_the_last_event_has_a_timestamp_diagnostic(tmp_path):
    run_dir = tmp_path / "wf_bad_edge_ts"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        "\n".join([
            '{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}',
            '{"type":"user","message":{"role":"user","content":"bye"},"timestamp":"not-a-timestamp"}',
        ]) + "\n",
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert run.status == "INCOMPLETE"


def test_status_stays_complete_when_a_timestamp_diagnostic_is_not_on_an_edge_event(tmp_path):
    run_dir = tmp_path / "wf_bad_middle_ts"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(_minimal_started_journal_line(), encoding="utf-8")
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        "\n".join([
            '{"type":"user","message":{"role":"user","content":"first"},"timestamp":"2026-01-01T00:00:00Z"}',
            '{"type":"user","message":{"role":"user","content":"middle"},"timestamp":"not-a-timestamp"}',
            '{"type":"user","message":{"role":"user","content":"last"},"timestamp":"2026-01-01T00:02:00Z"}',
        ]) + "\n",
        encoding="utf-8",
    )
    run = reader.load_run(run_dir)
    assert run.status == "COMPLETE"
