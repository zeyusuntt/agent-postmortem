"""docs/DESIGN.md 4.14.2 and 4.14.3: `watch --session`'s follower. Synthetic sessions built under
tmp_path (tests/live_helpers.py); every test drives SessionFollower.poll() directly, one poll per
call, so no test sleeps on the real poll interval.

Shapes copied from the upstream reports (synthetic, no real text):
- #84346: a watchdog abort written as "[Request interrupted by user for tool use]";
- #92716: a killed background subagent with no SubagentStop and a `killed` notification;
- #98170 / #85066: a "completed" result while the child's own transcript ends on an error.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_helpers import (
    FAKE_UUID,
    KILL_SENTINEL_TOOL,
    aid,
    assistant_text,
    backdate_tree,
    cost_state,
    notification,
    tool_result,
    tool_use,
    user,
    write_jsonl,
)

from agent_postmortem import live

SLUG = "-home-user-project"
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def _session(tmp_path: Path) -> Path:
    main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main, [user(None, T0, "start")])
    return main


def _sub_path(main: Path, agent_id: str) -> Path:
    return main.parent / main.stem / "subagents" / f"agent-{agent_id}.jsonl"


def _launch(main: Path, agent_id: str, tool_id: str, ts: datetime, background: bool = True) -> None:
    status = "async_launched" if background else "completed"
    write_jsonl(main, [
        tool_use(None, ts, tool_id, "Agent", {"description": "d", "prompt": "p",
                                             "subagent_type": "general-purpose",
                                             "run_in_background": background}),
        tool_result(None, ts + timedelta(seconds=1), tool_id, "launched",
                    toolUseResult={"agentId": agent_id, "status": status, "isAsync": background}),
    ], mode="a")


def _follower(tmp_path: Path, main: Path, **kw) -> live.SessionFollower:
    kw.setdefault("jobs_root", tmp_path / "jobs")
    kw.setdefault("now", lambda: T0.timestamp() + 600)
    return live.SessionFollower(main, **kw)


def _events(follower, *kinds):
    out = follower.poll()
    return [e for e in out if not kinds or e["event"] in kinds]


def _writes(agent_id: str, ts: datetime, n: int) -> list[dict]:
    entries = []
    for i in range(n):
        tid = f"toolu_w{i}"
        entries.append(tool_use(agent_id, ts, tid, "Write",
                                {"file_path": f"/tmp/live-fixture/out{i}.md", "content": "x"}))
        entries.append(tool_result(agent_id, ts + timedelta(seconds=1), tid, "File created"))
    return entries


# --- START -------------------------------------------------------------------


def test_start_counts_subagents_and_is_printed_once(tmp_path):
    main = _session(tmp_path)
    a = aid(1)
    _launch(main, a, "toolu_l1", T0)
    write_jsonl(_sub_path(main, a), [user(a, T0, "do it")])
    f = _follower(tmp_path, main)
    first = f.poll()
    assert first[0]["event"] == "START"
    assert first[0]["subagents"] == 1
    assert [e for e in f.poll() if e["event"] == "START"] == []


# --- R5: runtime-shaped kill ---------------------------------------------------


def _killed_subagent(main: Path, agent_id: str, writes: int = 2) -> None:
    write_jsonl(_sub_path(main, agent_id), [
        user(agent_id, T0, "do it"),
        *_writes(agent_id, T0 + timedelta(seconds=5), writes),
        tool_use(agent_id, T0 + timedelta(seconds=20), "toolu_b", "Bash", {"command": "sleep 900"}),
        user(agent_id, T0 + timedelta(seconds=640), [{"type": "text", "text": KILL_SENTINEL_TOOL}]),
    ])


def test_kill_with_nothing_in_the_session_that_stopped_it_names_the_changes(tmp_path):
    """#84346 shape: the marker reads as a user interrupt, but no TaskStop and no user interrupt
    in the session explain it."""
    main = _session(tmp_path)
    a = aid(2)
    _launch(main, a, "toolu_l2", T0)
    f = _follower(tmp_path, main)
    f.poll()
    _killed_subagent(main, a, writes=2)
    kills = _events(f, "KILL") or _events(f, "KILL")  # one poll of grace for a notification
    assert len(kills) == 1
    kill = kills[0]
    assert kill["agent"].startswith("a:")
    assert kill["paths"] == 2 and kill["commits"] == 0
    assert kill["paths_partial"] is True  # its Bash call was still in flight: a lower bound
    text = live.render_text(kill)
    assert text[0].startswith("KILL   a:")
    assert "nothing in this session stopped it" in text[0]
    joined = "\n".join(text)
    assert "it had changed 2+ paths, 0 commits." in joined
    assert "-> agent-postmortem run s_" in joined
    assert all(len(line) <= 78 for line in text)
    assert _events(f, "KILL") == []  # once per agent


def test_no_kill_line_when_this_session_stopped_the_agent(tmp_path):
    main = _session(tmp_path)
    a = aid(3)
    _launch(main, a, "toolu_l3", T0)
    write_jsonl(main, [tool_use(None, T0 + timedelta(seconds=630), "toolu_s", "TaskStop",
                                {"task_id": a})], mode="a")
    _killed_subagent(main, a)
    f = _follower(tmp_path, main)
    events = f.poll() + f.poll() + f.poll()
    assert [e for e in events if e["event"] == "KILL"] == []


def test_no_kill_line_when_the_user_interrupted_the_session_too(tmp_path):
    main = _session(tmp_path)
    a = aid(4)
    _launch(main, a, "toolu_l4", T0)
    write_jsonl(main, [user(None, T0 + timedelta(seconds=650),
                            [{"type": "text", "text": "[Request interrupted by user]"}])], mode="a")
    _killed_subagent(main, a)
    f = _follower(tmp_path, main)
    events = f.poll() + f.poll() + f.poll()
    assert [e for e in events if e["event"] == "KILL"] == []


# --- R6 on a native failure -------------------------------------------------------


def test_native_stall_failure_with_changes_gets_a_failed_line(tmp_path):
    main = _session(tmp_path)
    a = aid(5)
    _launch(main, a, "toolu_l5", T0)
    write_jsonl(_sub_path(main, a), [user(a, T0, "do it"), *_writes(a, T0, 3)])
    write_jsonl(main, [notification(T0 + timedelta(seconds=700), a, "toolu_l5", "failed",
                                    'Agent "d" failed: Agent stalled: no progress for 600s '
                                    "(stream watchdog did not recover)")], mode="a")
    f = _follower(tmp_path, main)
    events = f.poll() + f.poll()
    failed = [e for e in events if e["event"] == "FAILED"]
    assert len(failed) == 1
    assert failed[0]["cause"] == "stalled, no progress for 600s"
    assert failed[0]["paths"] == 3
    text = live.render_text(failed[0])
    assert text[0].startswith("FAILED a:") and "stalled, no progress for 600s" in text[0]
    assert "d" not in text[0].split(":", 2)[-1].split()  # the description is never printed
    assert not [e for e in events if e["event"] == "KILL"]


def test_native_failure_without_changes_prints_nothing(tmp_path):
    main = _session(tmp_path)
    a = aid(6)
    _launch(main, a, "toolu_l6", T0)
    write_jsonl(_sub_path(main, a), [user(a, T0, "do it")])
    write_jsonl(main, [notification(T0 + timedelta(seconds=30), a, "toolu_l6", "failed",
                                    'Agent "d" failed: Agent terminated early due to an API error: '
                                    "You've hit your session limit · resets 4:10am")], mode="a")
    f = _follower(tmp_path, main)
    events = f.poll() + f.poll()
    assert [e for e in events if e["event"] not in ("START",)] == []


def test_a_killed_notification_with_no_subagentstop_still_gets_its_changes(tmp_path):
    """#92716 shape: the background subagent was killed; the only native trace is the notice."""
    main = _session(tmp_path)
    a = aid(7)
    _launch(main, a, "toolu_l7", T0)
    write_jsonl(_sub_path(main, a), [user(a, T0, "do it"), *_writes(a, T0, 1)])
    write_jsonl(main, [notification(T0 + timedelta(seconds=40), a, "toolu_l7", "killed",
                                    'Agent "d" was stopped by Claude')], mode="a")
    f = _follower(tmp_path, main)
    events = f.poll() + f.poll()
    failed = [e for e in events if e["event"] == "FAILED"]
    assert len(failed) == 1 and failed[0]["cause"] == "stopped by this session"
    assert failed[0]["paths"] == 1


# --- R3, R4: checks at return -------------------------------------------------


def test_completed_while_its_transcript_ends_on_an_api_error_is_a_claim(tmp_path):
    """#98170 / #85066 shape: success reported, the child's own last entry is an error."""
    main = _session(tmp_path)
    a = aid(8)
    _launch(main, a, "toolu_l8", T0)
    write_jsonl(_sub_path(main, a), [
        user(a, T0, "do it"),
        assistant_text(a, T0 + timedelta(seconds=9), "API Error: Server error mid-response"),
    ])
    write_jsonl(main, [notification(T0 + timedelta(seconds=10), a, "toolu_l8", "completed",
                                    'Agent "d" finished')], mode="a")
    f = _follower(tmp_path, main)
    claims = [e for e in f.poll() + f.poll() if e["event"] == "CLAIM"]
    assert len(claims) == 1
    assert claims[0]["check"] == "returned_on_error"
    text = live.render_text(claims[0])
    assert text[0].startswith("CLAIM  a:")
    assert "a server error occurred" in "\n".join(text)
    assert all(len(line) <= 78 for line in text)


def test_completed_while_the_prompted_output_file_is_missing_is_a_claim(tmp_path):
    main = _session(tmp_path)
    a = aid(9)
    _launch(main, a, "toolu_l9", T0)
    target = tmp_path / "out" / "report.md"
    write_jsonl(_sub_path(main, a), [
        user(a, T0, f"Review the diff and write your report to {target}"),
        assistant_text(a, T0 + timedelta(seconds=9), "Done."),
    ])
    write_jsonl(main, [notification(T0 + timedelta(seconds=10), a, "toolu_l9", "completed",
                                    'Agent "d" finished')], mode="a")
    f = _follower(tmp_path, main)
    claims = [e for e in f.poll() + f.poll() if e["event"] == "CLAIM"]
    assert len(claims) == 1 and claims[0]["check"] == "output_missing_at_return"
    text = "\n".join(live.render_text(claims[0]))
    assert str(target) not in text  # share: the path is never printed
    assert "agent-postmortem status" in text


def test_a_clean_return_prints_nothing(tmp_path):
    main = _session(tmp_path)
    a = aid(10)
    _launch(main, a, "toolu_la", T0)
    target = tmp_path / "out" / "report.md"
    target.parent.mkdir(parents=True)
    target.write_text("ok", encoding="utf-8")
    write_jsonl(_sub_path(main, a), [
        user(a, T0, f"write your report to {target}"),
        assistant_text(a, T0 + timedelta(seconds=9), "Done."),
    ])
    write_jsonl(main, [notification(T0 + timedelta(seconds=10), a, "toolu_la", "completed",
                                    'Agent "d" finished')], mode="a")
    f = _follower(tmp_path, main)
    events = f.poll() + f.poll()
    assert [e["event"] for e in events] == ["START"]


def test_a_foreground_return_is_checked_too(tmp_path):
    main = _session(tmp_path)
    a = aid(11)
    write_jsonl(_sub_path(main, a), [
        user(a, T0, "do it"),
        assistant_text(a, T0 + timedelta(seconds=9), "API Error: Server error mid-response"),
    ])
    _launch(main, a, "toolu_lb", T0 + timedelta(seconds=10), background=False)
    f = _follower(tmp_path, main)
    claims = [e for e in f.poll() + f.poll() if e["event"] == "CLAIM"]
    assert len(claims) == 1


# --- dedupe, baseline, re-arm --------------------------------------------------


def test_a_rearmed_watch_does_not_repeat_what_was_reported(tmp_path):
    main = _session(tmp_path)
    a = aid(12)
    _launch(main, a, "toolu_lc", T0)
    f = _follower(tmp_path, main)
    f.poll()
    _killed_subagent(main, a)
    assert len([e for e in f.poll() + f.poll() if e["event"] == "KILL"]) == 1
    f.close()
    again = _follower(tmp_path, main)
    events = again.poll() + again.poll()
    assert [e["event"] for e in events] == ["START"]


def test_first_arm_does_not_replay_old_endings_but_counts_them(tmp_path):
    main = _session(tmp_path)
    a = aid(13)
    _launch(main, a, "toolu_ld", T0)
    _killed_subagent(main, a)
    old = T0 - timedelta(hours=2)
    backdate_tree(_sub_path(main, a), old)
    f = _follower(tmp_path, main, now=lambda: T0.timestamp() + 7200)
    events = f.poll() + f.poll()
    assert [e["event"] for e in events] == ["START"]
    assert events[0]["ended_before"] == 1


def test_an_ending_between_two_arms_is_reported_by_the_second(tmp_path):
    main = _session(tmp_path)
    a = aid(14)
    _launch(main, a, "toolu_le", T0)
    f = _follower(tmp_path, main)
    f.poll()
    f.close()
    _killed_subagent(main, a)
    backdate_tree(_sub_path(main, a), T0 - timedelta(hours=2))  # even when it looks old
    again = _follower(tmp_path, main, now=lambda: T0.timestamp() + 7200)
    kills = [e for e in again.poll() + again.poll() if e["event"] == "KILL"]
    assert len(kills) == 1


def test_a_second_watch_of_the_same_session_says_so(tmp_path):
    main = _session(tmp_path)
    f = _follower(tmp_path, main)
    f.poll()
    other = _follower(tmp_path, main)
    start = other.poll()[0]
    assert start["event"] == "START" and start["watched_by"] == os.getpid()
    assert other.duplicate


# --- ENDED, STATE, WORKFLOW ----------------------------------------------------


def test_ended_after_two_quiet_polls_on_trailing_cost_state(tmp_path):
    main = _session(tmp_path)
    f = _follower(tmp_path, main)
    f.poll()
    write_jsonl(main, [cost_state(), cost_state()], mode="a")
    assert _events(f, "ENDED") == []
    assert _events(f, "ENDED") == []
    ended = _events(f, "ENDED")
    assert len(ended) == 1


def _job(tmp_path: Path, state: str) -> None:
    job = tmp_path / "jobs" / "00000000"
    job.mkdir(parents=True, exist_ok=True)
    (job / "state.json").write_text(json.dumps({
        "state": state, "detail": "private detail text", "sessionId": FAKE_UUID,
    }), encoding="utf-8")


def test_background_state_changes_pass_through_without_detail(tmp_path):
    main = _session(tmp_path)
    _job(tmp_path, "working")
    f = _follower(tmp_path, main)
    assert _events(f, "STATE") == []
    _job(tmp_path, "blocked")
    states = _events(f, "STATE")
    assert len(states) == 1 and states[0]["state"] == "blocked"
    text = live.render_text(states[0])[0]
    assert "private detail text" not in text
    assert text.startswith("STATE  background session ")
    assert _events(f, "STATE") == []


def test_stopped_background_session_ends_the_watch(tmp_path):
    main = _session(tmp_path)
    _job(tmp_path, "working")
    f = _follower(tmp_path, main)
    f.poll()
    _job(tmp_path, "stopped")
    events = f.poll()
    assert [e["event"] for e in events] == ["STATE", "ENDED"]


def test_a_workflow_launch_in_another_session_is_handed_on(tmp_path):
    main = _session(tmp_path)
    write_jsonl(main, [
        tool_use(None, T0, "toolu_wf", "Workflow", {"script": "x"}),
        tool_result(None, T0, "toolu_wf", "launched",
                    toolUseResult={"runId": "wf_00000000-750", "status": "async_launched"}),
    ], mode="a")
    f = _follower(tmp_path, main, caller_session=None)
    wf = [e for e in f.poll() if e["event"] == "WORKFLOW"]
    assert len(wf) == 1 and wf[0]["run_id"] == "wf_00000000-750"
    assert live.render_text(wf[0])[0] == (
        "WORKFLOW wf_00000000-750 launched.  -> agent-postmortem watch wf_00000000-750"
    )
    own = _follower(tmp_path, main, caller_session=FAKE_UUID)
    own.close()


def test_json_rendering_is_one_object_per_event(tmp_path):
    main = _session(tmp_path)
    f = _follower(tmp_path, main)
    start = f.poll()[0]
    line = live.render_json(start)
    assert json.loads(line)["event"] == "START"
    assert "\n" not in line


# --- incremental reading ---------------------------------------------------------


def test_a_partial_last_line_is_read_on_the_next_poll(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"type": "user"}) + "\n" + '{"type": "assi', encoding="utf-8")
    tail = live.Tail(path)
    assert [e["type"] for e in tail.read()] == ["user"]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('stant"}\n')
    assert [e["type"] for e in tail.read()] == ["assistant"]


def test_a_truncated_file_is_read_again_from_the_start(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"type": "user", "n": 1}) + "\n" * 1 + json.dumps({"type": "x"}) + "\n",
                    encoding="utf-8")
    tail = live.Tail(path)
    assert len(tail.read()) == 2
    path.write_text(json.dumps({"type": "user", "n": 2}) + "\n", encoding="utf-8")
    entries = tail.read()
    assert tail.reset and [e.get("n") for e in entries] == [2]


def test_an_artifact_path_must_start_a_token_not_sit_inside_a_relative_one():
    """A relative path or a placeholder prefix is not an absolute path the prompt names: the
    regex once read `data/v2/x.json` as `/v2/x.json` and `<vault>/Board.md` as `/Board.md`."""
    from agent_postmortem.detectors.missing_artifact import artifact_paths

    assert artifact_paths("write data/v2/items/out.json and <vault>/Board.md") == []
    assert artifact_paths("then write ~/notes/out.md please") == []
    assert artifact_paths("write the report to /tmp/x/report.md") == ["/tmp/x/report.md"]
    assert artifact_paths("Write it to `/tmp/x/a.json`.") == ["/tmp/x/a.json"]
    assert artifact_paths('save "/tmp/y/b.md"') == ["/tmp/y/b.md"]
    # a sentence may end right after the path; a longer file name still does not match
    assert artifact_paths("Write your report to /tmp/x/report.md. Then stop.") == ["/tmp/x/report.md"]
    assert artifact_paths("Write it to /tmp/x/report.md.") == ["/tmp/x/report.md"]
    assert artifact_paths("write /tmp/x/report.md.backup now") == []
    # a template names no one file: <case>, {id}, *, $VAR
    assert artifact_paths("each agent must write /app/submission/<case>/report.json") == []
    assert artifact_paths("write /tmp/out/{id}.json and /tmp/out/*.md and /tmp/$X/a.md") == []


def test_no_kill_line_when_the_person_rejected_its_tool_call(tmp_path):
    """Found on the reference corpus: the person answered the subagent's permission prompt with
    a rejection, and the runtime wrote the interrupt marker right after it."""
    main = _session(tmp_path)
    a = aid(15)
    _launch(main, a, "toolu_lf", T0)
    write_jsonl(_sub_path(main, a), [
        user(a, T0, "research it"),
        tool_use(a, T0 + timedelta(seconds=5), "toolu_wf", "WebFetch", {"url": "https://example.com"}),
        tool_result(a, T0 + timedelta(seconds=9), "toolu_wf",
                    "The user doesn't want to proceed with this tool use. The tool use was rejected",
                    is_error=True),
        user(a, T0 + timedelta(seconds=9), [{"type": "text", "text": KILL_SENTINEL_TOOL}]),
    ])
    f = _follower(tmp_path, main)
    events = f.poll() + f.poll() + f.poll()
    assert [e for e in events if e["event"] == "KILL"] == []
