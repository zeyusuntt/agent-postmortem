"""Tests for detectors/base.py's shared helpers (relative_path, evidence_line,
is_bash_event): the single home for logic every detector module used to
copy-paste independently. See docs/review-claude.md's MAJOR finding on
duplicated helper logic.
"""

from __future__ import annotations

from pathlib import Path

from agent_postmortem.detectors import base
from agent_postmortem.models import Event, Run


def _run(path: Path) -> Run:
    return Run(
        run_id="wf_00000000-001", path=path, project_slug="-home-user-project",
        session_id=None, claude_version=None, journal=[], agents=[], attempts=[],
        started_at=None, ended_at=None, diagnostics=[],
    )


def _event(path: Path, kind: str = "tool_result", tool_name: str | None = "Bash", text: str = "ok") -> Event:
    return Event(
        path=path, line_no=3, ts=None, kind=kind, raw_type="user", tool_name=tool_name,
        text=text, exit_code=None, duration_ms=None, is_error=False, usage={},
    )


def test_relative_path_is_relative_to_run_path():
    run = _run(Path("/home/user/project/subagents/workflows/wf_x"))
    path = run.path / "agent-a1111111111111111.jsonl"
    assert base.relative_path(run, path) == "agent-a1111111111111111.jsonl"


def test_relative_path_falls_back_to_name_when_outside_run_path():
    run = _run(Path("/home/user/project/subagents/workflows/wf_x"))
    path = Path("/somewhere/else/agent-a1111111111111111.jsonl")
    assert base.relative_path(run, path) == "agent-a1111111111111111.jsonl"


def test_evidence_line_formats_path_line_and_excerpt():
    run = _run(Path("/home/user/project/subagents/workflows/wf_x"))
    path = run.path / "agent-a1111111111111111.jsonl"
    event = _event(path, text="  multiple   spaces   collapsed  ")
    line = base.evidence_line(run, event)
    assert line == "agent-a1111111111111111.jsonl:3: multiple spaces collapsed"


def test_evidence_line_truncates_to_the_given_limit():
    run = _run(Path("/home/user/project/subagents/workflows/wf_x"))
    path = run.path / "agent-a1111111111111111.jsonl"
    event = _event(path, text="x" * 500)
    line = base.evidence_line(run, event, limit=160)
    _, _, excerpt = line.partition(": ")
    assert len(excerpt) == 160


def test_is_bash_event_requires_tool_result_kind_and_bash_tool_name():
    path = Path("/home/user/project/subagents/workflows/wf_x/agent-a1.jsonl")
    assert base.is_bash_event(_event(path, kind="tool_result", tool_name="Bash")) is True
    assert base.is_bash_event(_event(path, kind="tool_result", tool_name="Read")) is False
    assert base.is_bash_event(_event(path, kind="attachment", tool_name=None, text="command ran")) is False


def test_attempt_seconds_and_format_duration():
    from datetime import datetime, timedelta, timezone

    from agent_postmortem.models import Attempt

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    attempt = Attempt(
        agent_id="a1", key="v2:a", label="impl:a", phase="Implement", agent_type=None,
        meta={}, transcript_path=None, events=[], first_ts=start, last_ts=start + timedelta(seconds=90),
        ordinal=1, outcome="completed", usage={},
    )
    assert base.attempt_seconds(attempt) == 90.0
    assert base.format_duration(90) == "1m"
    assert base.format_duration(0) == "0s"


def test_relative_path_falls_back_to_name_when_path_equals_run_path():
    """A session's cause_evidence (docs/DESIGN.md 4.4) can point at the main
    transcript, which for a session run.path is that same file: a literal
    relative_to would collapse to '.', not a usable path."""
    path = Path("/home/user/project/00000000-0000-4000-8000-000000000000.jsonl")
    run = _run(path)
    assert base.relative_path(run, path) == "00000000-0000-4000-8000-000000000000.jsonl"


# --- meaningful_event_before (docs/DESIGN.md 4.5) --------------------------


def _attempt_with_events(events):
    from agent_postmortem.models import Attempt

    return Attempt(
        agent_id="a1", key=None, label="impl:a", phase="", agent_type=None,
        meta={}, transcript_path=None, events=events, first_ts=None, last_ts=None,
        ordinal=1, outcome="killed", usage={},
    )


def test_meaningful_event_before_skips_thinking_and_attachment():
    path = Path("/home/user/project/agent-a1.jsonl")
    real = _event(path, kind="assistant", tool_name=None, text="real text")
    thinking = _event(path, kind="thinking", tool_name=None, text="")
    attachment = _event(path, kind="attachment", tool_name=None, text="hook noise")
    kill = _event(path, kind="user", tool_name=None, text="[Request interrupted by user]")
    attempt = _attempt_with_events([real, thinking, attachment, kill])
    assert base.meaningful_event_before(attempt, 3) is real


def test_meaningful_event_before_skips_events_with_empty_text():
    path = Path("/home/user/project/agent-a1.jsonl")
    real = _event(path, kind="assistant", tool_name=None, text="real text")
    empty = _event(path, kind="user", tool_name=None, text="   ")
    kill = _event(path, kind="user", tool_name=None, text="[Request interrupted by user]")
    attempt = _attempt_with_events([real, empty, kill])
    assert base.meaningful_event_before(attempt, 2) is real


def test_meaningful_event_before_stops_and_returns_none_at_a_compaction():
    path = Path("/home/user/project/agent-a1.jsonl")
    real = _event(path, kind="assistant", tool_name=None, text="real text")
    compaction = _event(path, kind="compaction", tool_name=None, text="")
    kill = _event(path, kind="user", tool_name=None, text="[Request interrupted by user]")
    attempt = _attempt_with_events([real, compaction, kill])
    assert base.meaningful_event_before(attempt, 2) is None


def test_meaningful_event_before_returns_none_when_nothing_precedes():
    path = Path("/home/user/project/agent-a1.jsonl")
    kill = _event(path, kind="user", tool_name=None, text="[Request interrupted by user]")
    attempt = _attempt_with_events([kill])
    assert base.meaningful_event_before(attempt, 0) is None


def test_parent_windows_are_30_seconds():
    assert base.PARENT_STOP_WINDOW_SECONDS == 30
    assert base.PARENT_SENTINEL_WINDOW_SECONDS == 30
