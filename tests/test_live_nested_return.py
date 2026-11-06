"""docs/DESIGN.md 4.14.3, 4.14.4, 4.14.8: a foreground subagent launched by another subagent of the
session (meta `parentAgentId`, spawn depth 2) returns into its launcher's transcript, as a plain
`[Subagent hand-back]` tool_result, and the main transcript gets no notice for it. The live reads
took a return from the main transcript only, so `status` kept such a child RUNNING forever, the
resume sentence said it never returned, and no return-time check ever ran on it, while `run`
(DESIGN T1, T2) read it as completed.

The live reads now take a nested child's return from its launcher's result for the child's meta
`toolUseId`, with the same positive synchronous signal `run` measured: a background nested
launch also gets a result with no `toolUseResult` ("Async agent launched ..."), and that one is
no return. An older live store is read again, and a baseline taken under the old rule is
extended once, so a nested return from last week is never told as new.
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from live_helpers import (
    FAKE_UUID,
    aid,
    assistant_text,
    tool_result,
    tool_use,
    user,
    write_jsonl,
)
from test_live_session import SLUG, T0, _launch, _sub_path

from agent_postmortem import hooks_cli, live, state, status

HAND_BACK = "[Subagent hand-back] The subagent finished. Its report follows."


def _session(tmp_path) -> Path:
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    return main_path


def _meta(main_path: Path, agent: str, **fields) -> None:
    path = _sub_path(main_path, agent).with_name(f"agent-{agent}.meta.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agentType": "general-purpose", "description": "d", **fields}))


def _parent_with_nested_children(main_path: Path, parent: str, children: list[tuple[str, str]],
                                 *, returned: bool = True, result_text: str = HAND_BACK) -> None:
    """A background depth-1 `parent` that launched each (child, tool id) in the foreground; with
    `returned`, each launch has its result in the parent's own transcript."""
    _launch(main_path, parent, "toolu_p", T0, background=True)
    _meta(main_path, parent, toolUseId="toolu_p", spawnDepth=1, requestShape="background")
    entries = [user(parent, T0, "read the notes and ask two readers")]
    for n, (child, tool_id) in enumerate(children):
        at = T0 + timedelta(seconds=10 + n)
        entries.append(tool_use(parent, at, tool_id, "Agent",
                                {"description": "reader", "prompt": "p",
                                 "subagent_type": "general-purpose"}))
        _meta(main_path, child, toolUseId=tool_id, parentAgentId=parent, spawnDepth=2,
              requestShape="foreground")
    if returned:
        for n, (_child, tool_id) in enumerate(children):
            entries.append(tool_result(parent, T0 + timedelta(seconds=60 + n), tool_id,
                                       result_text))
    write_jsonl(_sub_path(main_path, parent), entries)


def _child(main_path: Path, child: str, last: str = "The notes read clearly.") -> Path:
    path = _sub_path(main_path, child)
    write_jsonl(path, [user(child, T0 + timedelta(seconds=11), "read this"),
                       assistant_text(child, T0 + timedelta(seconds=50), last)])
    return path


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _states(main_path: Path) -> dict[str, str]:
    snap = status.snapshot(main_path)
    return {row["agent_id"]: row["state"] for row in snap["subagents"]}


def _hook(monkeypatch, capsys, command: str, payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch(command) == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return ""
    return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")


def _post_tool(main_path: Path) -> dict:
    return {"hook_event_name": "PostToolUse", "session_id": FAKE_UUID,
            "transcript_path": str(main_path), "tool_name": "Bash",
            "tool_input": {"command": "ls"}, "tool_response": {"stdout": ""}}


# --- status ------------------------------------------------------------------


def test_a_nested_foreground_child_that_handed_back_reads_returned(tmp_path):
    main_path = _session(tmp_path)
    parent, first, second = aid(501), aid(502), aid(503)
    _parent_with_nested_children(main_path, parent, [(first, "toolu_n1"), (second, "toolu_n2")])
    _child(main_path, first)
    _child(main_path, second)
    write_jsonl(_sub_path(main_path, parent), [
        assistant_text(parent, T0 + timedelta(seconds=90), "Both readers answered.")], mode="a")
    states = _states(main_path)
    assert states[first] == "returned", states
    assert states[second] == "returned", states


def test_the_status_line_counts_no_nested_child_as_running(tmp_path, capsys):
    from agent_postmortem.cli import main

    main_path = _session(tmp_path)
    parent, child = aid(504), aid(505)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child)
    assert main(["status", str(main_path)]) == 0
    out = capsys.readouterr().out
    assert "(1 running, 1 returned," in out.splitlines()[0], out  # the parent is still running
    assert "RUNNING" in out and out.count("RUNNING") == 1


def test_a_nested_child_still_working_stays_running(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(506), aid(507)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    _child(main_path, child)
    assert _states(main_path)[child] == "running"


def test_a_background_nested_launch_is_no_return(tmp_path):
    """Measured: 6 background nested launches got a result with no toolUseResult, text "Async
    agent launched ...", and their endings came as main-transcript notices."""
    main_path = _session(tmp_path)
    parent, child = aid(508), aid(509)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")],
                                 result_text="Async agent launched successfully.")
    _child(main_path, child)
    assert _states(main_path)[child] == "running"


def test_a_launch_the_child_meta_does_not_name_is_no_return(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(510), aid(511)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _meta(main_path, child, toolUseId="toolu_other", parentAgentId=parent, spawnDepth=2)
    _child(main_path, child)
    assert _states(main_path)[child] == "running"


def test_an_error_result_is_no_return(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(512), aid(513)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", HAND_BACK, is_error=True),
    ], mode="a")
    _child(main_path, child)
    assert _states(main_path)[child] == "running"


def test_a_nested_child_that_returned_on_an_api_error_is_a_claim_row(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(514), aid(515)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child, last="API Error: Server error mid-response")
    rows = {row["agent_id"]: row for row in status.snapshot(main_path)["subagents"]}
    assert rows[child]["state"] == "returned"
    assert rows[child]["claim"]["check"] == "returned_on_error", rows[child]


# --- the resume sentence -----------------------------------------------------


def test_the_resume_sentence_does_not_count_a_nested_child_that_handed_back(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    parent, child = aid(516), aid(517)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child)
    write_jsonl(_sub_path(main_path, parent), [
        assistant_text(parent, T0 + timedelta(seconds=90), "Done.")], mode="a")
    write_jsonl(main_path, [
        user(None, T0 + timedelta(seconds=95), [{"type": "text", "text": "thanks"}]),
    ], mode="a")
    from live_helpers import notification

    write_jsonl(main_path, [notification(T0 + timedelta(seconds=92), parent, "toolu_p",
                                         "completed", 'Agent "d" finished')], mode="a")
    payload = {"hook_event_name": "SessionStart", "source": "resume", "session_id": FAKE_UUID,
               "transcript_path": str(main_path)}
    assert "never returned" not in _hook(monkeypatch, capsys, "session-start", payload)


# --- the per-call check ------------------------------------------------------


def test_a_nested_child_returning_on_an_error_is_told_at_the_next_tool_call(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    parent, child = aid(518), aid(519)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    child_path = _sub_path(main_path, child)
    write_jsonl(child_path, [user(child, T0 + timedelta(seconds=11), "read this")])
    assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    write_jsonl(child_path, [
        assistant_text(child, T0 + timedelta(seconds=50), "API Error: Server error mid-response"),
    ], mode="a")
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", HAND_BACK)], mode="a")
    text = _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path))
    assert "CLAIM" in text and "returned, but its own last entry says" in text, text
    assert child not in text
    assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""


# --- an older live store -----------------------------------------------------


def _old_store(main_path: Path, monkeypatch) -> None:
    """Build the live store as the previous build left it: sub facts without launch results
    (version-less entries read up to their end) and a baseline taken under the main-only rule."""
    real_update = live.SubFacts.update

    def old_update(self, entries):
        real_update(self, entries)
        for name in ("launch_results", "launches"):
            if hasattr(self, name):
                getattr(self, name).clear()

    with monkeypatch.context() as m:
        m.setattr(live.SubFacts, "update", old_update)
        m.setattr(live, "SUB_FACTS_VERSION", None, raising=False)
        m.setattr(live, "BASELINE_RULE", None, raising=False)
        follower = live.SessionFollower(main_path, caller_session=None)
        status._restore(follower, state.read_cache(follower.store_path))
        follower._read()
        assert follower.load_endings(new_baseline=True)
        status._save(follower, {}, time.time())
    data = state.read_cache(state.live_store_path())
    with state.cache_transaction(state.live_store_path()) as live_data:
        for entry in live_data.get("transcripts", {}).values():
            entry.pop("sub_facts_version", None)
        for record in live_data.get("sessions", {}).values():
            if isinstance(record, dict):
                record.pop("baseline_rule", None)
    assert data.get("transcripts"), data


def test_an_older_store_is_read_again_and_tells_no_week_old_nested_return(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    parent, child = aid(520), aid(521)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child, last="API Error: Server error mid-response")
    write_jsonl(_sub_path(main_path, parent), [
        assistant_text(parent, T0 + timedelta(seconds=90), "Done.")], mode="a")
    for path in (_sub_path(main_path, parent), _sub_path(main_path, child), main_path):
        _age(path, 7 * 86400)
    _old_store(main_path, monkeypatch)
    # the hook first: an ending the old rule could not see is a week old, never news
    assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    assert _states(main_path)[child] == "returned"
    assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""


def test_control_a_fresh_nested_return_after_the_upgrade_is_still_told(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    parent, child = aid(522), aid(523)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    child_path = _sub_path(main_path, child)
    write_jsonl(child_path, [user(child, T0 + timedelta(seconds=11), "read this")])
    _old_store(main_path, monkeypatch)
    write_jsonl(child_path, [
        assistant_text(child, T0 + timedelta(seconds=50), "API Error: Server error mid-response"),
    ], mode="a")
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", HAND_BACK)], mode="a")
    text = _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path))
    assert "CLAIM" in text, text


def test_an_older_baseline_is_extended_only_over_a_complete_read(tmp_path, monkeypatch, capsys):
    """A first call after the upgrade that cannot read every transcript again in its budget
    extends nothing; the call that completes the read does, so the week-old return stays
    untold whichever transcript the bounded read reached first."""
    main_path = _session(tmp_path)
    parent, child, other = aid(524), aid(525), aid(526)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child, last="API Error: Server error mid-response")
    write_jsonl(_sub_path(main_path, parent), [
        assistant_text(parent, T0 + timedelta(seconds=90), "Done.")], mode="a")
    _launch(main_path, other, "toolu_o", T0, background=True)
    write_jsonl(_sub_path(main_path, other), [user(other, T0, "x"),
                                             assistant_text(other, T0, "Done.")])
    for path in (_sub_path(main_path, parent), _sub_path(main_path, child),
                 _sub_path(main_path, other), main_path):
        _age(path, 7 * 86400)
    _old_store(main_path, monkeypatch)
    monkeypatch.setattr(hooks_cli, "_FIRST_READ_BUDGET_SECONDS", 0.0)
    told = [_hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) for _ in range(5)]
    assert told == [""] * 5, told
    assert _states(main_path)[child] == "returned"


# --- further cases ------------------------------------------------------------


def test_an_unreadable_launcher_keeps_the_upgrade_pending(tmp_path, monkeypatch, capsys):
    """A launcher that cannot be opened is not a complete read: the upgrade waits for it, so a
    week-old nested return is still untold once it can be read."""
    main_path = _session(tmp_path)
    parent, child = aid(527), aid(528)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child, last="API Error: Server error mid-response")
    launcher = _sub_path(main_path, parent)
    write_jsonl(launcher, [assistant_text(parent, T0 + timedelta(seconds=90), "Done.")], mode="a")
    for path in (launcher, _sub_path(main_path, child), main_path):
        _age(path, 7 * 86400)
    _old_store(main_path, monkeypatch)
    launcher.chmod(0)
    try:
        assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    finally:
        launcher.chmod(0o600)
    other = aid(540)  # something new in the session, so the gate opens and the launcher is read
    write_jsonl(_sub_path(main_path, other), [user(other, T0, "x")])
    told = [_hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) for _ in range(3)]
    assert told == ["", "", ""], told
    assert _states(main_path)[child] == "returned"  # the launcher was read this time


def test_a_watch_over_an_unreadable_launcher_extends_its_baseline_once_it_reads(tmp_path,
                                                                                monkeypatch):
    main_path = _session(tmp_path)
    parent, child = aid(529), aid(530)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child, last="API Error: Server error mid-response")
    launcher = _sub_path(main_path, parent)
    for path in (launcher, _sub_path(main_path, child), main_path):
        _age(path, 7 * 86400)
    _old_store(main_path, monkeypatch)
    launcher.chmod(0)
    follower = live.SessionFollower(main_path, jobs_root=tmp_path / "jobs", caller_session=None)
    try:
        first = follower.poll()
    finally:
        launcher.chmod(0o600)
    later = follower.poll() + follower.poll()
    follower.close()
    assert [e["event"] for e in first + later if e["event"] != "START"] == [], first + later


def test_a_result_naming_another_agent_is_no_return(tmp_path):
    main_path = _session(tmp_path)
    parent, child, other = aid(531), aid(532), aid(533)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", "done",
                    toolUseResult={"agentId": other, "status": "completed"}),
    ], mode="a")
    _child(main_path, child, last="API Error: Server error mid-response")
    assert _states(main_path)[child] == "running"  # a cold read
    assert _states(main_path)[child] == "running"  # restored from the live store


def test_a_result_naming_the_child_itself_is_its_return(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(534), aid(535)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", "done",
                    toolUseResult={"agentId": child, "status": "completed"}),
    ], mode="a")
    _child(main_path, child)
    assert _states(main_path)[child] == "returned"
    assert _states(main_path)[child] == "returned"


def test_a_status_that_is_not_a_string_is_no_return_and_no_crash(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(536), aid(537)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", "done",
                    toolUseResult={"agentId": child, "status": []}),
    ], mode="a")
    _child(main_path, child)
    assert _states(main_path)[child] == "running"


def test_a_stored_launch_result_of_the_wrong_shape_is_dropped():
    facts = live.SubFacts.from_dict({"launches": ["toolu_x", 3],
                                     "launch_results": {"toolu_x": {"status": {}, "ts": 1.0},
                                                        "toolu_y": {"status": [], "ts": 1.0},
                                                        "toolu_z": {"status": "completed",
                                                                    "ts": True, "agent": 4}}})
    assert facts.launches == {"toolu_x"}
    assert facts.launch_results == {}


def test_a_meta_file_still_being_written_is_read_again(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(538), aid(539)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    meta = _sub_path(main_path, child).with_name(f"agent-{child}.meta.json")
    whole = meta.read_text()
    meta.write_text(whole[: len(whole) // 2])
    _child(main_path, child)
    follower = live.SessionFollower(main_path, jobs_root=tmp_path / "jobs", caller_session=None)
    follower._read()
    assert follower.foreground_done(child) is None
    meta.write_text(whole)
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", HAND_BACK)], mode="a")
    follower._read()
    assert follower.foreground_done(child) is not None


# --- further cases ------------------------------------------------------------


def _week_old_nested_claim(tmp_path, parent: str, child: str) -> Path:
    """A week-old nested return with an R3 claim; the parent's own return is in the main
    transcript, so no read of the parent's meta file stands in for the read under test."""
    from live_helpers import notification

    main_path = _session(tmp_path)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")])
    _child(main_path, child, last="API Error: Server error mid-response")
    write_jsonl(_sub_path(main_path, parent), [
        assistant_text(parent, T0 + timedelta(seconds=90), "Done.")], mode="a")
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=92), parent, "toolu_p",
                                         "completed", 'Agent "d" finished')], mode="a")
    for path in (_sub_path(main_path, parent), _sub_path(main_path, child), main_path):
        _age(path, 7 * 86400)
    return main_path


def _meta_path(main_path: Path, agent: str) -> Path:
    return _sub_path(main_path, agent).with_name(f"agent-{agent}.meta.json")


def _watch_events(main_path: Path, tmp_path, polls: int, before=None, between=None) -> list[str]:
    follower = live.SessionFollower(main_path, jobs_root=tmp_path / "jobs", caller_session=None)
    events = []
    try:
        if before is not None:
            before()
        try:
            events += follower.poll()
        finally:
            if between is not None:
                between()
        for _ in range(polls):
            events += follower.poll()
    finally:
        follower.close()
    return [e["event"] for e in events if e["event"] != "START"]


def test_a_meta_repaired_after_the_check_reopens_the_hook(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    parent, child = aid(541), aid(542)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    child_path = _sub_path(main_path, child)
    write_jsonl(child_path, [user(child, T0 + timedelta(seconds=11), "read this")])
    assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    meta = _meta_path(main_path, child)
    whole = meta.read_text()
    meta.write_text(whole[: len(whole) // 2])
    write_jsonl(child_path, [
        assistant_text(child, T0 + timedelta(seconds=50), "API Error: Server error mid-response"),
    ], mode="a")
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", HAND_BACK)], mode="a")
    assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    meta.write_text(whole)  # nothing else changes
    assert "CLAIM" in _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path))


def test_an_unparsable_meta_keeps_the_upgrade_pending_in_a_watch(tmp_path, monkeypatch):
    parent, child = aid(543), aid(544)
    main_path = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main_path, monkeypatch)
    meta = _meta_path(main_path, child)
    whole = meta.read_text()
    meta.write_text(whole[: len(whole) // 2])
    assert _watch_events(main_path, tmp_path, 2, between=lambda: meta.write_text(whole)) == []


def test_an_unparsable_meta_keeps_the_upgrade_pending_in_the_hook(tmp_path, monkeypatch, capsys):
    parent, child = aid(545), aid(546)
    main_path = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main_path, monkeypatch)
    meta = _meta_path(main_path, child)
    whole = meta.read_text()
    meta.write_text(whole[: len(whole) // 2])
    assert _hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) == ""
    meta.write_text(whole)
    other = aid(555)  # something new in the session, so the gate opens whatever it decided
    write_jsonl(_sub_path(main_path, other), [user(other, T0, "x")])
    told = [_hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) for _ in range(3)]
    assert told == ["", "", ""], told
    assert _states(main_path)[child] == "returned"


def test_a_subagents_directory_that_cannot_be_listed_is_no_complete_read(tmp_path, monkeypatch):
    parent, child = aid(547), aid(548)
    main_path = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main_path, monkeypatch)
    sub_dir = _sub_path(main_path, child).parent
    events = _watch_events(main_path, tmp_path, 2, before=lambda: sub_dir.chmod(0),
                           between=lambda: sub_dir.chmod(0o700))
    assert events == [], events


def test_a_transcript_that_cannot_be_stat_ed_is_no_complete_read(tmp_path, monkeypatch):
    parent, child = aid(549), aid(550)
    main_path = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main_path, monkeypatch)
    launcher = _sub_path(main_path, parent)
    real_stat = Path.stat
    patch = monkeypatch.context()

    def failing_stat(self, *args, **kwargs):
        if self == launcher:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    def before():
        patch.__enter__().setattr(Path, "stat", failing_stat)

    events = _watch_events(main_path, tmp_path, 2, before=before,
                           between=lambda: patch.__exit__(None, None, None))
    assert events == [], events


def test_a_null_status_is_no_return(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(551), aid(552)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", "done",
                    toolUseResult={"agentId": child, "status": None}),
    ], mode="a")
    _child(main_path, child)
    assert _states(main_path)[child] == "running"


def test_a_null_tool_use_result_with_the_hand_back_text_is_no_return(tmp_path):
    main_path = _session(tmp_path)
    parent, child = aid(553), aid(554)
    _parent_with_nested_children(main_path, parent, [(child, "toolu_n1")], returned=False)
    write_jsonl(_sub_path(main_path, parent), [
        tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", HAND_BACK, toolUseResult=None),
    ], mode="a")
    _child(main_path, child)
    assert _states(main_path)[child] == "running"


# --- further cases ------------------------------------------------------------


def _failing_stat_for(target: Path):
    real_stat = Path.stat

    def failing_stat(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    return failing_stat


def _failing_tail_stat_for(target: Path):
    """The reading layer's view of a transcript whose stat fails (Tail.stat's own contract);
    everything else, the store's own checks included, still stats it."""
    real = live.Tail.stat

    def stat(self):
        if self.path == target:
            self.failed = True
            return None
        return real(self)

    return stat


def test_the_hook_judges_no_ending_while_the_upgrade_is_pending(tmp_path, monkeypatch, capsys):
    """Current-version facts restored (so every transcript counts as read once), then the
    child's stat fails: its age is unknown, so it cannot join the baseline, and the call must
    not judge it."""
    parent, child = aid(556), aid(557)
    main_path = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main_path, monkeypatch)
    _states(main_path)  # status saves current-version facts; it takes no baseline
    with monkeypatch.context() as m:
        m.setattr(live.Tail, "stat", _failing_tail_stat_for(_sub_path(main_path, child)))
        deliver, _pending = hooks_cli.post_tool_check(str(main_path), _post_tool(main_path))
        assert deliver() is None  # called directly: an exception would fail the test
    other = aid(558)
    write_jsonl(_sub_path(main_path, other), [user(other, T0, "x")])
    told = [_hook(monkeypatch, capsys, "post-tool", _post_tool(main_path)) for _ in range(3)]
    assert told == ["", "", ""], told


def test_a_watch_judges_no_ending_while_the_upgrade_is_pending(tmp_path, monkeypatch):
    parent, child = aid(559), aid(560)
    main_path = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main_path, monkeypatch)
    _states(main_path)
    patch = monkeypatch.context()

    def before():
        patch.__enter__().setattr(live.Tail, "stat",
                                  _failing_tail_stat_for(_sub_path(main_path, child)))

    events = _watch_events(main_path, tmp_path, 2, before=before,
                           between=lambda: patch.__exit__(None, None, None))
    assert events == [], events


def test_a_present_agent_id_that_is_not_an_id_is_no_return():
    child = aid(561)
    block = {"type": "tool_result", "tool_use_id": "toolu_n1", "agentId": child,
             "content": "done"}
    assert live.launch_end(block, {"agentId": None, "status": "completed"}) is None
    assert live.launch_end({**block, "agentId": 7}, {"agentId": child, "status": "completed"}) \
        is None
    assert live.launch_end(block, {"agentId": child, "status": "completed"}) == ("completed", child)
    assert live.launch_end(block, {"status": "completed"}) == ("completed", child)


# --- further cases ------------------------------------------------------------


def test_main_notice_releases_pending_meta(tmp_path, monkeypatch):
    from live_helpers import notification

    parent, child = aid(562), aid(563)
    main = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main, monkeypatch)
    _meta_path(main, child).write_text("{")
    follower = live.SessionFollower(main, jobs_root=tmp_path / "jobs", caller_session=None)
    try:
        follower.poll()
        assert follower.rule_pending
        write_jsonl(main, [notification(T0 + timedelta(seconds=70), child, "toolu_n1",
                                        "completed", "done")], mode="a")
        follower.poll()
        assert not follower.rule_pending
    finally:
        follower.close()


def test_a_meta_file_that_goes_away_releases_the_pending_upgrade(tmp_path, monkeypatch):
    parent, child = aid(566), aid(567)
    main = _week_old_nested_claim(tmp_path, parent, child)
    _old_store(main, monkeypatch)
    _meta_path(main, child).write_text("{")
    follower = live.SessionFollower(main, jobs_root=tmp_path / "jobs", caller_session=None)
    try:
        follower.poll()
        assert follower.rule_pending
        _meta_path(main, child).unlink()
        follower.poll()
        assert not follower.rule_pending
    finally:
        follower.close()


def test_old_main_notice_does_not_hide_nested_return(tmp_path):
    from live_helpers import notification

    main = _session(tmp_path)
    parent, child = aid(564), aid(565)
    _parent_with_nested_children(main, parent, [(child, "toolu_n1")])
    _child(main, child)
    write_jsonl(main, [notification(T0 + timedelta(seconds=1), child, "toolu_old",
                                    "completed", "done")], mode="a")
    assert _states(main)[child] == "returned"


def test_a_stored_entry_of_the_first_launch_rule_is_read_again(tmp_path):
    """Version 2 kept results a later rule rejects (a present null agent id): read again."""
    main = _session(tmp_path)
    parent, child = aid(568), aid(569)
    _parent_with_nested_children(main, parent, [(child, "toolu_n1")], returned=False)
    entry = tool_result(parent, T0 + timedelta(seconds=60), "toolu_n1", "done",
                        toolUseResult={"agentId": child, "status": "completed"})
    entry["message"]["content"][0]["agentId"] = None  # present, and not an id
    write_jsonl(_sub_path(main, parent), [entry], mode="a")
    _child(main, child)
    assert _states(main)[child] == "running"
    with state.cache_transaction(state.live_store_path()) as data:
        for path, entry in data["transcripts"].items():
            if path.endswith(f"agent-{parent}.jsonl"):
                entry["sub_facts_version"] = 2
                entry["facts"]["launch_results"] = {
                    "toolu_n1": {"status": "completed", "ts": entry["facts"]["last_ts"],
                                 "agent": child}}
    assert _states(main)[child] == "running"
