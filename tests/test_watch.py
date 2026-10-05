"""Tests for `agent-postmortem watch` (docs/DESIGN.md 4.12).

Every test drives `watch` through `cli.main(["watch", ...])` against a run
directory under `tmp_path`, with `agent_postmortem.watch._now` and
`agent_postmortem.watch._sleep` monkeypatched so the suite never sleeps for
a real second. `_sleep` doubles as the script driver: each call runs the
next scripted action (appending journal/transcript bytes, or advancing the
fake clock) before the loop polls again.

Journal/transcript builders below follow the same conventions as the rest
of the fixture set (tests/fixtures/README.md): fake session id
00000000-0000-4000-8000-000000000000, fake cwd /home/user/project, agent
ids 17 hex chars starting with 'a', obviously synthetic prompt text.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from agent_postmortem import watch
from agent_postmortem.cli import main

SESSION_ID = "00000000-0000-4000-8000-000000000000"
CWD = "/home/user/project"

DEMO_RUN_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agent_postmortem"
    / "demo"
    / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000"
    / "subagents"
    / "workflows"
    / "wf_00000000-d30"
)


# --- small builders ---------------------------------------------------


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_lines(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _journal_line(**fields) -> str:
    return json.dumps(fields)


def _started(ts: str, key: str, agent_id: str, label: str) -> str:
    return _journal_line(
        type="started", timestamp=ts, key=key, agentId=agent_id, label=label, phase="Implement",
    )


def _result(ts: str, key: str, agent_id: str, result: dict | None = None) -> str:
    return _journal_line(
        type="result", timestamp=ts, key=key, agentId=agent_id, result=result or {"ok": True},
    )


def _failed(ts: str, key: str, agent_id: str) -> str:
    return _journal_line(type="failed", timestamp=ts, key=key, agentId=agent_id)


def _transcript_entry(entry_type: str, ts: str, content) -> str:
    return json.dumps(
        {
            "type": entry_type,
            "timestamp": ts,
            "uuid": SESSION_ID,
            "parentUuid": SESSION_ID,
            "cwd": CWD,
            "sessionId": SESSION_ID,
            "version": "2.1.267",
            "message": {"role": entry_type, "content": content},
        }
    )


def _user_text(ts: str, text: str) -> str:
    return _transcript_entry("user", ts, text)


def _assistant_text(ts: str, text: str) -> str:
    return _transcript_entry("assistant", ts, [{"type": "text", "text": text}])


def _assistant_tool_use(ts: str, tool_id: str, name: str, tool_input: dict, text: str = "") -> str:
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})
    return _transcript_entry("assistant", ts, blocks)


def _user_tool_result(ts: str, tool_id: str, content: str, is_error: bool = False) -> str:
    return _transcript_entry(
        "user", ts, [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}]
    )


def _kill_sentinel(ts: str) -> str:
    return _user_text(ts, "[Request interrupted by user]")


def _write_transcript(path: Path, lines: list[str]) -> None:
    _write_lines(path, lines)


def _transcript_path(run_dir: Path, agent_id: str) -> Path:
    return run_dir / f"agent-{agent_id}.jsonl"


def _meta(run_dir: Path, agent_id: str) -> None:
    (run_dir / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "", "workflowPhase": "Implement"}),
        encoding="utf-8",
    )


def _agent_form(agent_id: str) -> str:
    """N3b P14: the event attempt's own stable alias, the one `run` shows for
    it (replaces the v0.2 per-watch label pseudonym `agent-N`)."""
    from agent_postmortem import disclosure

    book = disclosure.register([("agent", agent_id)])
    return "a:" + book.alias("agent", agent_id)


# --- fake clock / sleep -------------------------------------------------


class _Clock:
    def __init__(self, start: float | None = None) -> None:
        self.now = start if start is not None else time.time()

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _patch_clock(monkeypatch, clock: _Clock) -> None:
    monkeypatch.setattr(watch, "_now", lambda: clock.now)


def _patch_scripted_sleep(monkeypatch, actions: list, clock: _Clock | None = None) -> None:
    """Each fake sleep runs the next scripted action, then advances the
    injected clock by the requested seconds when a clock is given (a watch
    only prints DONE after a grace period with no new start, so a clock
    that never moves would loop forever)."""
    state = {"i": 0}

    def _fake_sleep(seconds: float) -> None:
        if state["i"] < len(actions):
            actions[state["i"]]()
        state["i"] += 1
        if clock is not None:
            clock.advance(seconds)

    monkeypatch.setattr(watch, "_sleep", _fake_sleep)


def _patch_advancing_sleep(monkeypatch, clock: _Clock) -> None:
    def _fake_sleep(seconds: float) -> None:
        clock.advance(seconds)

    monkeypatch.setattr(watch, "_sleep", _fake_sleep)


# --- _read_new_journal_lines: appended-bytes parsing, truncation --------


def test_read_new_journal_lines_returns_new_complete_lines_and_offset(tmp_path):
    path = tmp_path / "journal.jsonl"
    _write_lines(path, ['{"a":1}', '{"a":2}'])
    lines, offset, truncated = watch._read_new_journal_lines(path, 0)
    assert lines == ['{"a":1}', '{"a":2}']
    assert offset == path.stat().st_size
    assert truncated is False


def test_read_new_journal_lines_yields_exactly_one_line_for_one_appended_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    _write_lines(path, ['{"a":1}'])
    _, offset, _ = watch._read_new_journal_lines(path, 0)
    _append_lines(path, ['{"a":2}'])
    lines, offset2, truncated = watch._read_new_journal_lines(path, offset)
    assert lines == ['{"a":2}']
    assert offset2 == path.stat().st_size
    assert truncated is False


def test_read_new_journal_lines_unchanged_size_returns_nothing(tmp_path):
    path = tmp_path / "journal.jsonl"
    _write_lines(path, ['{"a":1}'])
    _, offset, _ = watch._read_new_journal_lines(path, 0)
    lines, offset2, truncated = watch._read_new_journal_lines(path, offset)
    assert lines == []
    assert offset2 == offset
    assert truncated is False


def test_read_new_journal_lines_withholds_a_partial_trailing_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write('{"a":1}\n{"a":2 no newline yet')
    lines, offset, truncated = watch._read_new_journal_lines(path, 0)
    assert lines == ['{"a":1}']
    assert truncated is False
    with path.open("a", encoding="utf-8") as handle:
        handle.write("}\n")
    lines2, _offset2, _truncated2 = watch._read_new_journal_lines(path, offset)
    assert lines2 == ['{"a":2 no newline yet}']


def test_read_new_journal_lines_truncation_restarts_from_zero(tmp_path):
    """release finding F8: a file smaller than the stored offset reports
    `truncated=True` so the caller (watch_run) knows to reset every piece
    of derived state before processing the replacement content -- see
    test_truncation_resets_state_and_prints_reset_no_false_kill_or_done
    for the full-loop behavior this return value drives."""
    path = tmp_path / "journal.jsonl"
    _write_lines(path, ['{"a":1}', '{"a":2}'])
    _, offset, _ = watch._read_new_journal_lines(path, 0)
    # Truncated and rewritten smaller (a fresh run reusing the same path).
    _write_lines(path, ['{"b":1}'])
    lines, offset2, truncated = watch._read_new_journal_lines(path, offset)
    assert lines == ['{"b":1}']
    assert offset2 == path.stat().st_size
    assert truncated is True


def test_read_new_journal_lines_missing_file_is_quiet(tmp_path):
    path = tmp_path / "nope.jsonl"
    lines, offset, truncated = watch._read_new_journal_lines(path, 0)
    assert lines == []
    assert offset == 0
    assert truncated is False


def test_read_new_journal_lines_raises_when_a_partial_line_exceeds_1mb(tmp_path):
    """release finding F9: a trailing line with no newline yet that has
    already grown past 1 MB must not be silently reread and rechecked in
    full forever -- it raises JournalLineTooLong instead."""
    path = tmp_path / "journal.jsonl"
    with path.open("wb") as handle:
        handle.write(b'{"a":1}\n')
        handle.write(b"x" * (1_048_576 + 1))  # no trailing newline
    try:
        watch._read_new_journal_lines(path, 0)
    except watch.JournalLineTooLong:
        pass
    else:
        raise AssertionError("expected JournalLineTooLong")


# --- KILL --------------------------------------------------------------


def _build_kill_scenario(run_dir: Path) -> None:
    run_dir.mkdir(parents=True)
    agent1 = "aaa0000000000001a"
    agent2 = "aaa0000000000002a"
    key = "v2:watch-a"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:a"),
        ],
    )
    _meta(run_dir, agent1)
    _meta(run_dir, agent2)
    _write_transcript(
        _transcript_path(run_dir, agent1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement the watch-a module."),
            _assistant_tool_use(
                "2026-02-01T00:00:01.000Z", "toolu_1", "Edit",
                {"file_path": "/home/user/project/a.py"}, text="Editing a.",
            ),
            _user_tool_result("2026-02-01T00:00:02.000Z", "toolu_1", "ok"),
            _assistant_tool_use(
                "2026-02-01T00:00:03.000Z", "toolu_2", "Edit",
                {"file_path": "/home/user/project/b.py"}, text="Editing b.",
            ),
            _user_tool_result("2026-02-01T00:00:04.000Z", "toolu_2", "ok"),
            _assistant_text("2026-02-01T00:00:05.000Z", "Both edits applied; running tests next."),
            _kill_sentinel("2026-02-01T00:07:01.000Z"),  # +416s from 00:00:05
        ],
    )
    _write_transcript(
        _transcript_path(run_dir, agent2),
        [
            _user_text("2026-02-01T00:10:00.000Z", "Implement the watch-a module."),
            _assistant_text("2026-02-01T00:10:05.000Z", "Done."),
        ],
    )
    return key, agent1, agent2


def test_kill_line_then_second_line_then_done(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa1"
    key, agent1, agent2 = _build_kill_scenario(run_dir)
    journal = run_dir / "journal.jsonl"

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    actions = [
        lambda: _append_lines(journal, [_started("2026-02-01T00:10:00.000Z", key, agent2, "impl:a")]),
        lambda: _append_lines(journal, [_result("2026-02-01T00:10:06.000Z", key, agent2)]),
    ]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    out = capsys.readouterr().out
    lines = out.splitlines()

    assert lines == [
        # N8b K5.1: START first; K5.2: the restart's own result prints OK (the KILL put this key
        # inside its rate limit, so the OK is retried and still printed before DONE, K5.4)
        "START  wf_00000000-fa1: 1 agents started so far, 1 attempts.",
        f"KILL   {_agent_form(agent1)} attempt 1 after 416s of silence; restarted as attempt 2.",
        "       attempt 1 wrote 2 paths, reported 0 commits.  "
        "-> agent-postmortem rescue plan wf_00000000-fa1",
        f"OK     {_agent_form(agent2)} attempt 2 returned a result.",
        # silent_retry fires (major, no critical) whenever a key has more
        # than one attempt (docs/DESIGN.md 4.3), so this key's own restart
        # makes the run DEGRADED, not CLEAN.
        "DONE   1 agents, 2 attempts, DEGRADED.  -> agent-postmortem run wf_00000000-fa1",
    ]
    assert code == 0


def test_kill_json_line_carries_event(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa5"
    key, agent1, agent2 = _build_kill_scenario(run_dir)
    journal = run_dir / "journal.jsonl"

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    actions = [
        lambda: _append_lines(journal, [_started("2026-02-01T00:10:00.000Z", key, agent2, "impl:a")]),
        lambda: _append_lines(journal, [_result("2026-02-01T00:10:06.000Z", key, agent2)]),
    ]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--json"])
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines()]

    # N8b K5.1, K5.2: START first, and the restart's own result prints OK before DONE
    assert [r["event"] for r in records] == ["START", "KILL", "OK", "DONE"]
    records = records[1:]
    assert records[0]["agent"] == _agent_form(agent1)
    assert records[0]["attempt"] == 1
    assert records[0]["silence_seconds"] == 416
    assert records[0]["paths_written"] == 2
    assert records[0]["commits"] == 0
    assert records[-1]["verdict"] == "DEGRADED"
    assert code == 0


# --- FAILED --------------------------------------------------------------


def test_failed_line_uses_the_attempt_failed_cause_rule(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa2"
    run_dir.mkdir(parents=True)
    agent1 = "aab0000000000001a"
    key = "v2:watch-fail"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:fail"),
            _failed("2026-02-01T00:05:00.000Z", key, agent1),
        ],
    )
    _meta(run_dir, agent1)
    _write_transcript(
        _transcript_path(run_dir, agent1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement the watch-fail module."),
            _assistant_text(
                "2026-02-01T00:04:59.000Z", "You've hit your session limit · resets 18:00",
            ),
        ],
    )

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [], clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    out = capsys.readouterr().out
    lines = out.splitlines()

    # N8b K5.1: START is the first line
    assert lines[0] == "START  wf_00000000-fa2: 1 agents started so far, 1 attempts."
    assert lines[1] == (
        f"FAILED {_agent_form(agent1)} attempt 1: session usage limit reached, resets 18:00."
    )
    assert lines[-1].startswith("DONE   ")
    # attempt_failed is major/minor, never critical, so a lone `failed` key
    # never fails a run on its own (see test_failed_verdict_and_exit_code).
    assert code == 0


def test_failed_line_does_not_call_a_kill_sentinel_lookalike_interrupted(
    monkeypatch, capsys, tmp_path,
):
    """docs/DESIGN.md 4.13.2: the kill_sentinel marker anchors only when the
    WHOLE flattened text, trailing-stripped, equals a KILL_SENTINEL_VARIANT
    exactly -- the same rule detectors/attempt_failed.py now uses. watch's
    own cause phrase claims to use that same rule, so a last user entry that
    merely starts with the kill-sentinel prefix but carries extra trailing
    content on the same line must not be called "interrupted before
    finishing" there either; it must fall through to "cause not recorded",
    exactly as attempt_failed's own finding would for the same transcript.
    """
    run_dir = tmp_path / "wf_00000000-fa3"
    run_dir.mkdir(parents=True)
    agent1 = "aab0000000000003a"
    key = "v2:watch-fail-lookalike"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:fail"),
            _failed("2026-02-01T00:05:00.000Z", key, agent1),
        ],
    )
    _meta(run_dir, agent1)
    _write_transcript(
        _transcript_path(run_dir, agent1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement the watch-fail-lookalike module."),
            _user_text(
                "2026-02-01T00:04:59.000Z",
                "[Request interrupted by user] extra trailing text on the same line",
            ),
        ],
    )

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [], clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    out = capsys.readouterr().out
    lines = out.splitlines()

    # N8b K5.1: START is the first line
    assert lines[0].startswith("START  ")
    assert lines[1] == f"FAILED {_agent_form(agent1)} attempt 1: cause not recorded."
    assert lines[-1].startswith("DONE   ")
    assert code == 0


def test_failed_verdict_and_exit_code(monkeypatch, capsys, tmp_path):
    """A `failed` attempt alone is DEGRADED (major), not FAILED (critical) --
    exit 0. Kept separate from the FAILED-line test above so a wrong verdict
    assumption there does not mask a wrong exit code here."""
    run_dir = tmp_path / "wf_00000000-fa6"
    run_dir.mkdir(parents=True)
    agent1 = "aac0000000000001a"
    key = "v2:watch-fail2"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:fail2"),
            _failed("2026-02-01T00:05:00.000Z", key, agent1),
        ],
    )
    _meta(run_dir, agent1)
    _write_transcript(
        _transcript_path(run_dir, agent1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement the watch-fail2 module."),
            _assistant_text("2026-02-01T00:04:59.000Z", "API Error: Server error mid-response"),
        ],
    )
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [], clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    assert code == 0


def test_done_with_a_failed_lifecycle_exits_3(monkeypatch, capsys, tmp_path):
    # N5b (4.6, 4.13.4): FAILED is the harness reporting the workflow failed
    # (the lifecycle terminal), not a detector's severity; no detector is
    # critical any more. The run sits in the standard layout so its parent
    # transcript, which carries the failed notification, is found.
    session_dir = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    run_dir = session_dir / "subagents" / "workflows" / "wf_00000000-fa7"
    run_dir.mkdir(parents=True)
    agent1 = "aad0000000000001a"
    key = "v2:watch-critical"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:critical"),
            _result("2026-02-01T00:05:00.000Z", key, agent1),
        ],
    )
    _meta(run_dir, agent1)
    _write_transcript(
        _transcript_path(run_dir, agent1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Do the work."),
            _assistant_text("2026-02-01T00:04:59.000Z", "Done."),
        ],
    )
    envelope = (
        "<task-notification>\n<task-id>t1</task-id>\n"
        "<tool-use-id>toolu_1</tool-use-id>\n<status>failed</status>\n"
        "</task-notification>"
    )
    _write_lines(
        session_dir.parent / f"{session_dir.name}.jsonl",
        [
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant", "usage": {},
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "Workflow", "input": {}}],
                },
                "timestamp": "2026-01-31T23:59:00.000Z",
            }),
            json.dumps({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "launched"}],
                },
                "toolUseResult": {
                    "runId": "wf_00000000-fa7", "taskId": "t1", "status": "async_launched",
                },
                "timestamp": "2026-01-31T23:59:01.000Z",
            }),
            json.dumps({
                "type": "user", "message": {"role": "user", "content": envelope},
                "origin": {"kind": "task-notification"},
                "timestamp": "2026-02-01T00:06:00.000Z",
            }),
        ],
    )

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [], clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    out = capsys.readouterr().out
    assert "FAILED" in out.splitlines()[-1]
    assert code == 3


# --- QUIET --------------------------------------------------------------


def test_quiet_fires_once_then_again_only_when_silence_doubles(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa3"
    run_dir.mkdir(parents=True)
    agent1 = "aae0000000000001a"
    key = "v2:watch-quiet"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:quiet"),
        ],
    )
    _meta(run_dir, agent1)
    transcript = _transcript_path(run_dir, agent1)
    _write_transcript(
        transcript,
        [_user_text("2026-02-01T00:00:00.000Z", "Implement the watch-quiet module.")],
    )

    clock = _Clock()
    os.utime(transcript, (clock.now, clock.now))
    _patch_clock(monkeypatch, clock)

    actions = [
        lambda: clock.advance(300),  # -> silence 300: first QUIET
        lambda: clock.advance(200),  # -> silence 500: no QUIET yet (next is 600)
        lambda: clock.advance(100),  # -> silence 600: second QUIET (doubled)
        lambda: clock.advance(100000),  # push well past --max-hours to end the test
    ]
    _patch_scripted_sleep(monkeypatch, actions)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.2"])
    out = capsys.readouterr().out
    quiet_lines = [line for line in out.splitlines() if line.startswith("QUIET")]

    assert f"QUIET  {_agent_form(agent1)} attempt 1 has produced nothing for 300s." in quiet_lines
    assert f"QUIET  {_agent_form(agent1)} attempt 1 has produced nothing for 600s." in quiet_lines
    assert not any(" for 500s." in line for line in quiet_lines)
    assert any(line.startswith("LIMIT") for line in out.splitlines())
    assert code == 4


# --- rate limit -----------------------------------------------------------


def test_rate_limited_kill_is_suppressed_then_retried_and_printed_after_the_window_clears(
    monkeypatch, capsys, tmp_path,
):
    """Was test_rate_limit_prints_only_one_line_per_agent_per_60s, asserting
    exactly one KILL line and dropping the second forever. Release finding
    F10 changes that: an event suppressed by the rate limit is retried on
    later polls rather than dropped, so once the 60s window clears (the
    scripted clock jump below) the second KILL still prints -- both
    eventually appear, proving the rate limit still holds within one poll
    (only one prints immediately) while no longer permanently losing the
    other."""
    run_dir = tmp_path / "wf_00000000-fa4"
    run_dir.mkdir(parents=True)
    agent1 = "aaf0000000000001a"
    agent2 = "aaf0000000000002a"
    agent3 = "aaf0000000000003a"
    key = "v2:watch-rate"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:rate"),
        ],
    )
    for agent in (agent1, agent2, agent3):
        _meta(run_dir, agent)
    for agent, offset_minutes in ((agent1, 0), (agent2, 10)):
        _write_transcript(
            _transcript_path(run_dir, agent),
            [
                _user_text(
                    f"2026-02-01T00:{offset_minutes:02d}:00.000Z", "Implement the watch-rate module.",
                ),
                _assistant_text(
                    f"2026-02-01T00:{offset_minutes:02d}:05.000Z", "Working on it.",
                ),
                _kill_sentinel(f"2026-02-01T00:{offset_minutes + 1:02d}:00.000Z"),
            ],
        )
    _write_transcript(
        _transcript_path(run_dir, agent3),
        [_user_text("2026-02-01T00:20:00.000Z", "Implement the watch-rate module.")],
    )

    journal = run_dir / "journal.jsonl"
    # Both restarts land in the journal before the next poll, so both KILL
    # transitions are evaluated inside the same poll, with the same clock
    # reading -- the second must be suppressed by the 60s-per-agent limit.
    _append_lines(
        journal,
        [
            _started("2026-02-01T00:10:00.000Z", key, agent2, "impl:rate"),
            _started("2026-02-01T00:20:00.000Z", key, agent3, "impl:rate"),
        ],
    )

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    actions = [
        lambda: clock.advance(100000),  # end the test via --max-hours
    ]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.1"])
    out = capsys.readouterr().out
    kill_lines = [line for line in out.splitlines() if line.startswith("KILL")]

    # Both transitions are for the same key, so within the first poll only
    # one prints (the rate limit still holds); the second is retried and
    # prints once the scripted 100000s clock jump clears the 60s window.
    assert len(kill_lines) == 2
    assert code == 4


# --- LIMIT / no journal ----------------------------------------------------


def test_limit_after_max_hours_exits_4(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa8"
    run_dir.mkdir(parents=True)
    agent1 = "ab00000000000001a"
    key = "v2:watch-limit"
    _write_lines(
        run_dir / "journal.jsonl",
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:limit"),
        ],
    )
    _meta(run_dir, agent1)
    transcript = _transcript_path(run_dir, agent1)
    _write_transcript(
        transcript, [_user_text("2026-02-01T00:00:00.000Z", "Implement the watch-limit module.")],
    )
    clock = _Clock()
    os.utime(transcript, (clock.now, clock.now))
    _patch_clock(monkeypatch, clock)
    actions = [lambda: clock.advance(3600 * 7)]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "6"])
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == "LIMIT  6h reached; run not finished."
    assert code == 4


def test_no_journal_appears_within_60s_exits_2(monkeypatch, capsys, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_advancing_sleep(monkeypatch, clock)

    code = main(
        ["watch", "wf_never-appears", "--root", str(root), "--interval", "10"]
    )
    err = capsys.readouterr().err
    assert "no journal appeared within 60 s" in err
    assert code == 2


def test_a_run_directory_that_appears_after_the_command_starts_is_found(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa9"
    agent1 = "ab10000000000001a"
    key = "v2:watch-appears"

    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    def _create_run_dir():
        run_dir.mkdir(parents=True)
        _write_lines(
            run_dir / "journal.jsonl",
            [
                _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
                _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:appears"),
                _result("2026-02-01T00:05:00.000Z", key, agent1),
            ],
        )
        _meta(run_dir, agent1)
        _write_transcript(
            _transcript_path(run_dir, agent1),
            [_user_text("2026-02-01T00:00:00.000Z", "Implement the watch-appears module.")],
        )

    _patch_scripted_sleep(monkeypatch, [_create_run_dir], clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("DONE   1 agents, 1 attempts, CLEAN.")
    assert code == 0


# --- silent_retry_kill replay --------------------------------------------


def test_replays_the_silent_retry_kill_demo_run_printing_one_kill_per_kill(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 4.12: copy silent_retry_kill (packaged as
    wf_00000000-d30, docs/DESIGN.md 6) into tmp and grow its journal one
    line at a time, asserting exactly one KILL line per kill (2 total).

    The demo's own two keys are genuinely sequential (silent-beta's first
    'started' lands only after silent-alpha's own 'result', DESIGN.md 6):
    appended in that literal on-disk order, silent-alpha would look fully
    resolved -- reader.workflow_journal_finished's own definition, section
    3 -- the instant its 'result' line lands, before silent-beta's key has
    ever been mentioned, ending the watch before its kill can ever be seen.
    silent-beta's own first 'started' is therefore appended one step earlier
    (opening that key before silent-alpha's key closes) without disturbing
    either key's own internal, ordinal-determining order -- exactly what a
    workflow that fans its agents out concurrently, rather than strictly
    phase by phase, would produce on disk regardless of replay order.
    """
    run_dir = tmp_path / "wf_00000000-d30"
    shutil.copytree(DEMO_RUN_DIR, run_dir)
    # Copied files keep the repository's own (old) mtimes; reset every
    # transcript to "now" (in the fake clock's own frame) so QUIET never
    # fires here and steals a kill's place in the once-per-60s-per-agent
    # rate limit (docs/DESIGN.md 4.12).
    clock = _Clock()
    for transcript in run_dir.glob("agent-*.jsonl"):
        os.utime(transcript, (clock.now, clock.now))
    _patch_clock(monkeypatch, clock)

    journal_path = run_dir / "journal.jsonl"
    full_lines = [ln for ln in journal_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(full_lines) == 7  # launched, started x2, result, started x2, result
    launched, alpha_started1, alpha_started2, alpha_result, beta_started1, beta_started2, beta_result = (
        full_lines
    )

    _write_lines(journal_path, [launched, alpha_started1])
    ordered_appends = [alpha_started2, beta_started1, alpha_result, beta_started2, beta_result]

    actions = [lambda line=line: _append_lines(journal_path, [line]) for line in ordered_appends]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    out = capsys.readouterr().out
    kill_lines = [line for line in out.splitlines() if line.startswith("KILL")]

    assert len(kill_lines) == 2
    assert out.splitlines()[-1].startswith("DONE   2 agents, 4 attempts, ")
    assert code == 0


# --- ambiguity resolutions exercised structurally ------------------------


def test_watch_only_considers_workflow_runs_when_resolving_a_bare_id(tmp_path):
    """A bare RUN_ID is resolved against workflow runs only (watch has no
    meaning for a session, which carries no journal.jsonl)."""
    root = tmp_path / "root"
    project = root / "-home-user-project" / SESSION_ID
    workflow_dir = project / "subagents" / "workflows" / "wf_00000000-fab"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "journal.jsonl").write_text("", encoding="utf-8")
    found = watch.find_run_dir("wf_00000000-fab", root)
    assert found == workflow_dir


# --- DONE waits for a grace period without a new start ---------------------


def test_done_is_not_printed_during_a_gap_between_sequential_phases(monkeypatch, capsys, tmp_path):
    """A workflow that runs phase B after phase A has a moment where every
    key seen so far is resolved. DONE must wait for the grace period (30 s
    by default) with no new `started` before it is printed, so the watch
    survives the gap and reports both phases in one DONE."""
    run_dir = tmp_path / "wf_00000000-fac"
    run_dir.mkdir(parents=True)
    key_a, key_b = "v2:phase-a", "v2:phase-b"
    a, b = "aaa0000000000001a", "aaa0000000000002a"
    _write_lines(run_dir / "journal.jsonl", [
        _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
        _started("2026-02-01T00:00:00.000Z", key_a, a, "phase:a"),
        _result("2026-02-01T00:00:30.000Z", key_a, a),
    ])
    _meta(run_dir, a)
    _meta(run_dir, b)
    _write_transcript(_transcript_path(run_dir, a), [
        _user_text("2026-02-01T00:00:00.000Z", "Phase a."),
        _assistant_text("2026-02-01T00:00:29.000Z", "Done."),
    ])
    _write_transcript(_transcript_path(run_dir, b), [
        _user_text("2026-02-01T00:00:40.000Z", "Phase b."),
        _assistant_text("2026-02-01T00:00:50.000Z", "Done."),
    ])
    journal = run_dir / "journal.jsonl"
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    # Poll 1 sees phase a resolved; phase b starts 10 s later (well inside
    # the 30 s grace) and resolves 10 s after that.
    actions = (
        [lambda: None] * 9
        + [lambda: _append_lines(journal, [_started("2026-02-01T00:00:40.000Z", key_b, b, "phase:b")])]
        + [lambda: None] * 9
        + [lambda: _append_lines(journal, [_result("2026-02-01T00:00:50.000Z", key_b, b)])]
    )
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    lines = capsys.readouterr().out.splitlines()
    # N8b K5.1, K5.2: START first, one OK per result; the point of the test is that exactly one
    # DONE is printed, after both phases, never in the gap between them
    assert len(lines) == 4, lines
    assert lines[0] == "START  wf_00000000-fac: 1 agents started so far, 1 attempts."
    assert lines[1] == f"OK     {_agent_form(a)} attempt 1 returned a result."
    assert lines[2] == f"OK     {_agent_form(b)} attempt 1 returned a result."
    assert lines[3].startswith("DONE   2 agents, 2 attempts, CLEAN.")
    assert code == 0


def _expect_usage_error(argv: list[str], capsys) -> tuple[int, str]:
    """docs/DESIGN.md 4.8/4.12 (release finding F11): an out-of-range or
    non-finite watch flag is a usage error -- cli._Parser.error raises
    SystemExit(1) with a message to stderr naming the flag, the same
    contract test_cli_smoke.py's own --write test already documents."""
    try:
        code = main(argv)
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr().err


@pytest.mark.parametrize(
    "flag,bad_value",
    [
        ("--interval", "0.1"),
        ("--interval", "300.1"),
        ("--interval", "nan"),
        ("--interval", "inf"),
        ("--interval", "-1"),
        ("--max-hours", "0.001"),
        ("--max-hours", "168.1"),
        ("--quiet-after", "9.9"),
        ("--quiet-after", "86400.1"),
        ("--done-grace", "-0.1"),
        ("--done-grace", "3600.1"),
    ],
)
def test_watch_flag_out_of_range_is_a_usage_error_naming_the_flag(capsys, tmp_path, flag, bad_value):
    code, err = _expect_usage_error(
        ["watch", "wf_00000000-aaa", "--root", str(tmp_path), flag, bad_value], capsys,
    )
    assert code == 1
    assert flag in err


@pytest.mark.parametrize(
    "flag,good_value",
    [
        ("--interval", "0.2"),
        ("--interval", "300"),
        ("--max-hours", "0.01"),
        ("--max-hours", "168"),
        ("--quiet-after", "10"),
        ("--quiet-after", "86400"),
        ("--done-grace", "0"),
        ("--done-grace", "3600"),
    ],
)
def test_watch_flag_boundary_values_are_accepted(monkeypatch, capsys, tmp_path, flag, good_value):
    """The bounds themselves are inclusive -- a boundary value must reach
    watch_run rather than being rejected by argparse. Proven by letting the
    real 60s no-journal wait fire (with the clock patched so the test does
    not actually wait), which only happens once argparse has accepted the
    flag."""
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_advancing_sleep(monkeypatch, clock)
    root = tmp_path / "root"
    root.mkdir()
    code = main(["watch", "wf_never-appears", "--root", str(root), flag, good_value])
    assert capsys.readouterr().err
    assert code == 2  # no journal appeared -- proves parsing succeeded


def test_no_journal_wait_caps_sleep_to_the_remaining_budget_with_a_long_interval(
    monkeypatch, capsys, tmp_path,
):
    """docs/DESIGN.md 4.12 (release finding F11): every sleep is capped to
    the remaining journal-wait deadline, so a large --interval cannot
    overshoot the 60s no-journal limit. With --interval 300 (the max
    allowed), the very first sleep must be capped to 60s, not 300s -- proven
    by checking the injected clock lands on exactly 60, not 300."""
    root = tmp_path / "root"
    root.mkdir()
    clock = _Clock(start=0.0)
    _patch_clock(monkeypatch, clock)
    _patch_advancing_sleep(monkeypatch, clock)

    code = main(["watch", "wf_never-appears", "--root", str(root), "--interval", "300"])
    err = capsys.readouterr().err
    assert "no journal appeared within 60 s" in err
    assert code == 2
    assert clock.now == 60.0


def test_done_grace_flag_zero_prints_done_at_once(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fad"
    run_dir.mkdir(parents=True)
    key, a = "v2:only", "aaa0000000000001a"
    _write_lines(run_dir / "journal.jsonl", [
        _started("2026-02-01T00:00:00.000Z", key, a, "impl"),
        _result("2026-02-01T00:00:30.000Z", key, a),
    ])
    _meta(run_dir, a)
    _write_transcript(_transcript_path(run_dir, a), [
        _user_text("2026-02-01T00:00:00.000Z", "Only."),
        _assistant_text("2026-02-01T00:00:29.000Z", "Done."),
    ])
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    sleeps = []
    monkeypatch.setattr(watch, "_sleep", lambda s: sleeps.append(s))
    code = main(["watch", str(run_dir), "--interval", "1", "--done-grace", "0"])
    out_lines = capsys.readouterr().out.splitlines()
    # N8b K5.1, K5.2: START and the OK for the one result come before the DONE, all on the
    # first poll (no sleep at all)
    assert [line.split()[0] for line in out_lines] == ["START", "OK", "DONE"]
    assert out_lines[-1].startswith("DONE   1 agents, 1 attempts, CLEAN.")
    assert sleeps == []
    assert code == 0


# --- truncation resets state (release finding F8) --------------------------


def test_truncation_resets_state_and_prints_reset_no_false_kill_or_done(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 4.12 (release finding F8): when the journal shrinks
    (truncated or replaced -- a fresh run reusing the same directory), every
    piece of derived state is reset before the replacement content is
    processed, and one RESET line is printed. Proven through the full loop:
    a real KILL happens first (so there is old state to reset), then the
    journal is truncated to a single fresh `started` line for a brand new
    key/agent -- afterward there must be no second, false KILL line for the
    old key, and no false DONE (the new key never resolves)."""
    run_dir = tmp_path / "wf_00000000-fae"
    run_dir.mkdir(parents=True)
    key1, agent1, agent2 = "v2:before", "aaa0000000000001a", "aaa0000000000002a"
    journal = run_dir / "journal.jsonl"
    _write_lines(
        journal,
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key1, agent1, "impl:before"),
        ],
    )
    _meta(run_dir, agent1)
    _write_transcript(
        _transcript_path(run_dir, agent1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement before."),
            _assistant_text("2026-02-01T00:00:05.000Z", "Working."),
            _kill_sentinel("2026-02-01T00:07:00.000Z"),
        ],
    )
    _meta(run_dir, agent2)

    key2, agent3 = "v2:after", "aaa0000000000003a"
    _meta(run_dir, agent3)
    _write_transcript(
        _transcript_path(run_dir, agent3),
        [_user_text("2026-02-01T00:20:00.000Z", "Implement after.")],
    )

    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    def _trigger_kill():
        _append_lines(journal, [_started("2026-02-01T00:10:00.000Z", key1, agent2, "impl:before")])

    def _truncate_to_fresh_run():
        _write_lines(journal, [_started("2026-02-01T00:20:00.000Z", key2, agent3, "impl:after")])

    def _end_test():
        clock.advance(100000)

    actions = [_trigger_kill, _truncate_to_fresh_run, _end_test]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.1"])
    lines = capsys.readouterr().out.splitlines()

    kill_lines = [line for line in lines if line.startswith("KILL")]
    reset_lines = [line for line in lines if line.startswith("RESET")]
    done_lines = [line for line in lines if line.startswith("DONE")]

    assert len(kill_lines) == 1, lines  # only the pre-truncation kill, never a second false one
    assert reset_lines == ["RESET  journal was truncated or replaced; state cleared."], lines
    assert done_lines == [], lines  # key2 never resolves -- no false DONE
    assert lines[-1].startswith("LIMIT")
    assert code == 4


# --- malformed / oversized journal lines (release finding F9) --------------


def test_oversized_partial_line_exits_4_with_the_1mb_message(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-faf"
    run_dir.mkdir(parents=True)
    journal = run_dir / "journal.jsonl"
    with journal.open("wb") as handle:
        handle.write(
            (
                _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z") + "\n"
            ).encode("utf-8")
        )
        handle.write(b"x" * (1_048_576 + 1))  # no trailing newline: a stuck partial line

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [], clock)

    code = main(["watch", str(run_dir), "--interval", "1"])
    out = capsys.readouterr().out
    assert out.splitlines() == ["MALFORMED journal line longer than 1 MB"]
    assert code == 4


def test_malformed_complete_line_blocks_done_and_prints_limit_instead(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 4.12 (release finding F9): a complete line that is not
    a JSON object (here, a bare JSON string rather than an object) is
    printed once as MALFORMED, and once every key would otherwise have
    resolved (the point DONE would fire), LIMIT is printed instead and the
    process exits 4 -- the run's own final state can no longer be trusted."""
    run_dir = tmp_path / "wf_00000000-fb0"
    run_dir.mkdir(parents=True)
    key, agent = "v2:malformed", "aaa0000000000009a"
    journal = run_dir / "journal.jsonl"
    _write_lines(
        journal,
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent, "impl:malformed"),
        ],
    )
    _meta(run_dir, agent)
    _write_transcript(
        _transcript_path(run_dir, agent),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement the malformed module."),
            _assistant_text("2026-02-01T00:00:29.000Z", "Done."),
        ],
    )

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    actions = [
        lambda: _append_lines(journal, ['"just a bare json string, not an object"']),
        lambda: _append_lines(journal, [_result("2026-02-01T00:00:30.000Z", key, agent)]),
    ]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--done-grace", "0"])
    lines = capsys.readouterr().out.splitlines()

    assert "MALFORMED journal line 3 is not JSON" in lines
    assert not any(line.startswith("DONE") for line in lines)
    assert lines[-1] == "LIMIT  journal has malformed lines; run state unknown."
    assert code == 4


def test_malformed_line_is_reported_only_once(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fb1"
    run_dir.mkdir(parents=True)
    key, agent = "v2:malformed2", "aaa0000000000010a"
    journal = run_dir / "journal.jsonl"
    _write_lines(
        journal,
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent, "impl:malformed2"),
            "not even json at all {{{",
            "still not json )))",
        ],
    )
    _meta(run_dir, agent)
    _write_transcript(
        _transcript_path(run_dir, agent),
        [_user_text("2026-02-01T00:00:00.000Z", "Implement.")],
    )
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    actions = [lambda: clock.advance(100000)]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.1"])
    lines = capsys.readouterr().out.splitlines()
    malformed_lines = [line for line in lines if line.startswith("MALFORMED")]
    assert malformed_lines == ["MALFORMED journal line 3 is not JSON"]
    assert code == 4


# --- retry of a suppressed KILL/FAILED event (release finding F10) ---------


def test_kill_whose_transcript_appears_one_poll_later_is_printed(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 4.12 (release finding F10): the supersede transition
    (started key, agent2 while agent1's attempt was open) is detected
    before agent1's own transcript file exists on disk; it must be retried
    on the next poll rather than silently dropped, and print once the
    transcript appears."""
    run_dir = tmp_path / "wf_00000000-fb2"
    run_dir.mkdir(parents=True)
    key = "v2:late-transcript"
    agent1, agent2 = "aaa0000000000011a", "aaa0000000000012a"
    journal = run_dir / "journal.jsonl"
    _write_lines(
        journal,
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:late"),
            _started("2026-02-01T00:10:00.000Z", key, agent2, "impl:late"),
        ],
    )
    _meta(run_dir, agent1)
    _meta(run_dir, agent2)
    _write_transcript(
        _transcript_path(run_dir, agent2),
        [_user_text("2026-02-01T00:10:00.000Z", "Implement late.")],
    )
    # agent1's own transcript does not exist yet -- written only once the
    # scripted poll below creates it, simulating the write lagging behind
    # the journal's own 'started' line for agent2.
    agent1_transcript = _transcript_path(run_dir, agent1)
    assert not agent1_transcript.exists()

    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    def _write_agent1_transcript_late():
        _write_transcript(
            agent1_transcript,
            [
                _user_text("2026-02-01T00:00:00.000Z", "Implement late."),
                _assistant_text("2026-02-01T00:00:05.000Z", "Working."),
                _kill_sentinel("2026-02-01T00:05:00.000Z"),
            ],
        )

    actions = [_write_agent1_transcript_late, lambda: clock.advance(100000)]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.1"])
    lines = capsys.readouterr().out.splitlines()
    kill_lines = [line for line in lines if line.startswith("KILL")]

    assert len(kill_lines) == 1, lines
    assert "attempt 1" in kill_lines[0] and "attempt 2" in kill_lines[0]
    assert code == 4


def test_two_kill_events_10s_apart_both_print_the_second_after_the_rate_window(
    monkeypatch, capsys, tmp_path,
):
    """docs/DESIGN.md 4.12 (release finding F10): two KILL-eligible
    transitions for the same key, 10s apart by the clock, both eventually
    print -- the second is suppressed by the 60s-per-key rate limit at
    first, then retried each poll until the window (measured from the
    first line's own print time) clears."""
    run_dir = tmp_path / "wf_00000000-fb3"
    run_dir.mkdir(parents=True)
    key = "v2:two-kills"
    agent1, agent2, agent3 = (
        "aaa0000000000013a", "aaa0000000000014a", "aaa0000000000015a",
    )
    journal = run_dir / "journal.jsonl"
    _write_lines(
        journal,
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:two"),
        ],
    )
    for agent in (agent1, agent2, agent3):
        _meta(run_dir, agent)
    _write_transcript(
        _transcript_path(run_dir, agent1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement two."),
            _assistant_text("2026-02-01T00:00:05.000Z", "Working."),
            _kill_sentinel("2026-02-01T00:01:00.000Z"),
        ],
    )
    _write_transcript(
        _transcript_path(run_dir, agent2),
        [
            _user_text("2026-02-01T00:10:00.000Z", "Implement two."),
            _assistant_text("2026-02-01T00:10:05.000Z", "Working."),
            _kill_sentinel("2026-02-01T00:11:00.000Z"),
        ],
    )
    _write_transcript(
        _transcript_path(run_dir, agent3),
        [_user_text("2026-02-01T00:20:00.000Z", "Implement two.")],
    )

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    actions = (
        # poll 1: agent2 restarts -- agent1's own KILL prints.
        [lambda: _append_lines(journal, [_started("2026-02-01T00:10:00.000Z", key, agent2, "impl:two")])]
        # 10s later (real clock advance via the interval below), agent3
        # restarts -- agent2's own KILL transition is detected but
        # suppressed: under 60s since agent1's KILL just printed.
        + [lambda: None] * 9
        + [lambda: _append_lines(journal, [_started("2026-02-01T00:20:00.000Z", key, agent3, "impl:two")])]
        # keep polling (no-ops) until the 60s window (from agent1's print)
        # clears, then push well past --max-hours to end the test.
        + [lambda: None] * 60
        + [lambda: clock.advance(100000)]
    )
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.1"])
    lines = capsys.readouterr().out.splitlines()
    kill_lines = [line for line in lines if line.startswith("KILL")]

    assert len(kill_lines) == 2, lines
    assert "attempt 1" in kill_lines[0]
    assert "attempt 2" in kill_lines[1]
    assert code == 4


def test_kill_forced_after_12_polls_when_transcript_never_appears(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 4.12 (release finding F10): a supersede transition
    whose earlier transcript never appears on disk at all is force-printed,
    without transcript-derived details, after _MAX_PENDING_POLLS (12) polls
    -- never dropped forever."""
    run_dir = tmp_path / "wf_00000000-fb4"
    run_dir.mkdir(parents=True)
    key = "v2:never-appears"
    agent1, agent2 = "aaa0000000000016a", "aaa0000000000017a"
    journal = run_dir / "journal.jsonl"
    _write_lines(
        journal,
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key, agent1, "impl:never"),
            _started("2026-02-01T00:10:00.000Z", key, agent2, "impl:never"),
        ],
    )
    _meta(run_dir, agent1)
    _meta(run_dir, agent2)
    _write_transcript(
        _transcript_path(run_dir, agent2),
        [_user_text("2026-02-01T00:10:00.000Z", "Implement never.")],
    )
    # agent1's transcript is never written at all.

    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    actions = [lambda: None] * 12 + [lambda: clock.advance(100000)]
    _patch_scripted_sleep(monkeypatch, actions, clock)

    code = main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.1"])
    lines = capsys.readouterr().out.splitlines()
    kill_lines = [line for line in lines if line.startswith("KILL")]

    assert len(kill_lines) == 1, lines
    assert kill_lines[0] == (
        f"KILL   {_agent_form(agent1)} attempt 1 (details unavailable); restarted as attempt 2."
    )
    assert code == 4


def test_kill_and_failed_are_emitted_before_quiet_within_one_poll(monkeypatch, capsys, tmp_path):
    """docs/DESIGN.md 4.12: within one poll, KILL and FAILED lines are
    emitted before QUIET -- proven with all three eligible in the very
    first poll."""
    run_dir = tmp_path / "wf_00000000-fb5"
    run_dir.mkdir(parents=True)
    key_kill, key_failed, key_quiet = "v2:order-kill", "v2:order-failed", "v2:order-quiet"
    a1, a2, f1, q1 = (
        "aaa0000000000018a", "aaa0000000000019a", "aaa0000000000020a", "aaa0000000000021a",
    )
    journal = run_dir / "journal.jsonl"
    _write_lines(
        journal,
        [
            _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
            _started("2026-02-01T00:00:00.000Z", key_kill, a1, "impl:kill"),
            _started("2026-02-01T00:00:00.000Z", key_kill, a2, "impl:kill"),
            _started("2026-02-01T00:00:00.000Z", key_failed, f1, "impl:failed"),
            _failed("2026-02-01T00:00:01.000Z", key_failed, f1),
            _started("2026-02-01T00:00:00.000Z", key_quiet, q1, "impl:quiet"),
        ],
    )
    for agent in (a1, a2, f1, q1):
        _meta(run_dir, agent)
    _write_transcript(
        _transcript_path(run_dir, a1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement kill."),
            _assistant_text("2026-02-01T00:00:05.000Z", "Working."),
            _kill_sentinel("2026-02-01T00:01:00.000Z"),
        ],
    )
    _write_transcript(
        _transcript_path(run_dir, a2),
        [_user_text("2026-02-01T00:10:00.000Z", "Implement kill.")],
    )
    _write_transcript(
        _transcript_path(run_dir, f1),
        [
            _user_text("2026-02-01T00:00:00.000Z", "Implement failed."),
            _assistant_text("2026-02-01T00:00:00.500Z", "API Error: Server error mid-response"),
        ],
    )
    quiet_transcript = _transcript_path(run_dir, q1)
    _write_transcript(quiet_transcript, [_user_text("2026-02-01T00:00:00.000Z", "Implement quiet.")])

    clock = _Clock()
    os.utime(quiet_transcript, (clock.now, clock.now))
    _patch_clock(monkeypatch, clock)
    actions = [lambda: clock.advance(400)]  # past the 300s default --quiet-after
    _patch_scripted_sleep(monkeypatch, actions, clock)

    main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.2"])
    lines = capsys.readouterr().out.splitlines()
    event_words = [line.split(None, 1)[0] for line in lines if line and not line.startswith(" ")]

    assert "KILL" in event_words and "FAILED" in event_words and "QUIET" in event_words
    assert event_words.index("KILL") < event_words.index("QUIET")
    assert event_words.index("FAILED") < event_words.index("QUIET")
