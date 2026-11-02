"""Live-mode findings of the 0.4.0 release review (the whole branch against 0.3.6), each one a
reproduction that failed before its fix (docs/DESIGN.md 4.14.3, 4.14.5)."""

from __future__ import annotations

import io
import json
import time
from datetime import timedelta

from live_helpers import (
    FAKE_UUID,
    aid,
    assistant_text,
    notification,
    tool_result,
    tool_use,
    user,
    write_jsonl,
)
from test_live_session import SLUG, T0, _launch, _sub_path

from agent_postmortem import hooks_cli, live, state


def _main(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    return main_path


def _run(monkeypatch, capsys, command: str, payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch(command) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def _post_tool(main_path) -> dict:
    return {"hook_event_name": "PostToolUse", "session_id": FAKE_UUID,
            "transcript_path": str(main_path), "tool_name": "Bash",
            "tool_input": {"command": "ls"}, "tool_response": {"stdout": ""}}


# --- the resume sentence: a partial count is a lower bound, also at zero (4.14.3) ---------------


def test_resume_says_0_plus_paths_when_the_only_writes_cannot_be_listed(
    tmp_path, monkeypatch, capsys,
):
    main_path = _main(tmp_path)
    agent = aid(140)
    _launch(main_path, agent, "toolu_r140", T0)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "build it"),
        tool_use(agent, T0 + timedelta(seconds=2), "toolu_b1", "Bash", {"command": "python build.py"}),
        tool_result(agent, T0 + timedelta(seconds=3), "toolu_b1", "built"),
    ])
    payload = {"hook_event_name": "SessionStart", "source": "resume", "session_id": FAKE_UUID,
               "transcript_path": str(main_path)}
    text = _run(monkeypatch, capsys, "session-start", payload)
    assert "1 subagent of this session never returned" in text, text
    assert "0+ paths changed" in text, text


# --- the post-tool call budget holds when no ending needs a line (4.14.5) ----------------------


def test_a_call_whose_endings_need_no_line_still_stops_inside_its_budget(
    tmp_path, monkeypatch, capsys,
):
    """Endings that need no line (here clean background returns) can each need a full parse; a
    call must stop at its 2 s budget even when none of them is told, save what it handled and go
    on at the next call, or run.sh's 3 s deadline kills it before it saves and every later call
    repeats the same work."""
    main_path = _main(tmp_path)
    agents = [aid(150 + i) for i in range(4)]
    for i, agent in enumerate(agents):
        _launch(main_path, agent, f"toolu_bg15{i}", T0)
        write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "look it up")])
    assert _run(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""  # baseline
    for i, agent in enumerate(agents):
        write_jsonl(_sub_path(main_path, agent), [
            assistant_text(agent, T0 + timedelta(seconds=20), "found it"),
        ], mode="a")
        write_jsonl(main_path, [notification(T0 + timedelta(seconds=21), agent, f"toolu_bg15{i}",
                                             "completed", 'Agent "d" finished')], mode="a")
    seen: list[str] = []
    real = live.SessionFollower._agent_events

    def slow(self, agent_id):
        out = list(real(self, agent_id))
        if out and agent_id in agents:  # an ending to work out: a large transcript parsed in full
            seen.append(agent_id)
            time.sleep(0.8)  # 4 of them pass run.sh's 3 s deadline
        return out

    monkeypatch.setattr(live.SessionFollower, "_agent_events", slow)
    assert _run(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    first = list(seen)  # the 2 s budget stops the call before the fourth parse
    assert 0 < len(first) < len(agents), first
    seen.clear()
    assert _run(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    assert set(seen) == set(agents) - set(first), (first, seen)  # the rest, not the same again


# --- agent-launched claims an ending only once its message is ready (4.14.5, 4.14.6 rule 3) -----


def test_a_store_error_after_a_foreground_claim_leaves_the_claim_for_the_next_channel(
    tmp_path, monkeypatch, capsys,
):
    main_path = _main(tmp_path)
    agent = aid(160)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"),
        assistant_text(agent, T0 + timedelta(seconds=5), "API Error: Server error mid-response"),
    ])
    payload = {
        "hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": FAKE_UUID,
        "transcript_path": str(main_path),
        "tool_input": {"description": "d", "prompt": "p", "subagent_type": "general-purpose"},
        "tool_response": {"agentId": agent, "status": "completed"},
    }
    real = live.mark_reported

    def locked(*args, **kwargs):
        raise state.StateError("state lock held past its timeout")

    monkeypatch.setattr(live, "mark_reported", locked)
    assert _run(monkeypatch, capsys, "agent-launched", payload) == ""  # the hook never fails
    monkeypatch.setattr(live, "mark_reported", real)
    text = _run(monkeypatch, capsys, "agent-launched", payload)
    assert "returned, but its own last entry says" in text, text
