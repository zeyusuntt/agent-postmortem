"""docs/DESIGN.md 4.14.5: the live hooks. `hook agent-launched` (PostToolUse on Agent or Task):
the once-per-session watch hint and the return check for a foreground call; `hook check` R7 (a
headless run ending while background work still runs); `hook session-start` on resume naming the
subagents that ended while the session was down."""

from __future__ import annotations

import io
import json
import os
from datetime import timedelta

from live_helpers import FAKE_UUID, aid, assistant_text, user, write_jsonl
from test_live_session import SLUG, T0, _launch, _sub_path, _writes

from agent_postmortem import hooks_cli, live, state


def _main(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    return main_path


def _run_hook(monkeypatch, capsys, command: str, payload: dict) -> dict | None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch(command) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def _launch_payload(main_path, agent_id: str, status: str = "async_launched") -> dict:
    return {
        "hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": FAKE_UUID,
        "transcript_path": str(main_path),
        "tool_input": {"description": "d", "prompt": "p", "subagent_type": "general-purpose"},
        "tool_response": {"agentId": agent_id, "status": status},
    }


def _context(out: dict | None) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_the_first_agent_launch_hints_the_session_watch_once(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    first = _run_hook(monkeypatch, capsys, "agent-launched", _launch_payload(main_path, aid(31)))
    assert first["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    text = _context(first)
    assert "agent-postmortem watch --session" in text
    assert "timeout_ms 1800000" in text
    assert len(text) <= 400
    second = _run_hook(monkeypatch, capsys, "agent-launched", _launch_payload(main_path, aid(32)))
    assert second is None


def test_no_hint_when_opted_out(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    monkeypatch.setenv("AGENT_POSTMORTEM_WATCH_HINT", "0")
    assert _run_hook(monkeypatch, capsys, "agent-launched", _launch_payload(main_path, aid(33))) is None


def test_no_hint_while_the_session_is_watched(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    with state.cache_transaction(state.live_store_path()) as data:
        live._store_session(data, live.session_key(main_path))["watch"] = {
            "pid": os.getpid(), "token": "someone-else",
        }
    assert _run_hook(monkeypatch, capsys, "agent-launched", _launch_payload(main_path, aid(34))) is None


def test_a_foreground_return_on_an_error_is_told_at_once(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(35)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"),
        assistant_text(agent, T0 + timedelta(seconds=5), "API Error: Server error mid-response"),
    ])
    out = _run_hook(monkeypatch, capsys, "agent-launched",
                    _launch_payload(main_path, agent, status="completed"))
    text = _context(out)
    assert "returned, but its own last entry says: a server error occurred" in text
    assert text.startswith("Subagent a:")
    # the watch never repeats it
    record = state.read_cache(state.live_store_path())["sessions"][live.session_key(main_path)]
    assert any(key.startswith(f"{agent}@") and key.endswith(":claim")
               for key in record["reported"])  # one key per ending (4.14.6 rule 3)


def test_a_payload_from_a_subagent_is_ignored(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    payload = _launch_payload(main_path, aid(36))
    payload["agent_id"] = aid(37)
    assert _run_hook(monkeypatch, capsys, "agent-launched", payload) is None


def _stop_payload(main_path, tasks) -> dict:
    return {"hook_event_name": "Stop", "session_id": FAKE_UUID, "transcript_path": str(main_path),
            "stop_hook_active": False, "background_tasks": tasks}


def test_stop_says_nothing_about_background_work_claude_code_already_waits_for(
    tmp_path, monkeypatch, capsys,
):
    """R7 is not built (docs/DESIGN.md 4.14.2): fault injection on 2.1.314 showed a headless
    run waiting for its background subagent's notification on its own, so a sentence telling it
    to wait would only repeat native behavior."""
    main_path = _main(tmp_path)
    (main_path.parent / main_path.stem / "subagents").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    tasks = [{"id": aid(38), "type": "subagent", "status": "running"}]
    assert _run_hook(monkeypatch, capsys, "check", _stop_payload(main_path, tasks)) is None


def test_resume_names_subagents_that_ended_while_the_session_was_down(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(40)
    _launch(main_path, agent, "toolu_r1", T0)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "do it"), *_writes(agent, T0, 2)])
    payload = {"hook_event_name": "SessionStart", "source": "resume", "session_id": FAKE_UUID,
               "transcript_path": str(main_path)}
    out = _run_hook(monkeypatch, capsys, "session-start", payload)
    text = _context(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "1 subagent of this session never returned" in text
    assert "2 paths changed" in text
    assert "-> agent-postmortem status" in text
    again = _run_hook(monkeypatch, capsys, "session-start", payload)
    assert "never returned" not in _context(again)


def test_startup_says_nothing_about_own_subagents(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(41)
    _launch(main_path, agent, "toolu_r2", T0)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "do it")])
    payload = {"hook_event_name": "SessionStart", "source": "startup", "session_id": FAKE_UUID,
               "transcript_path": str(main_path)}
    assert "never returned" not in _context(_run_hook(monkeypatch, capsys, "session-start", payload))
