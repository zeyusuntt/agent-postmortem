"""Reproductions of the round-1 cross-model review findings on branch `live` (docs/DESIGN.md
4.14), one test per finding, each failing before its fix."""

from __future__ import annotations

import io
import json
import os
import time
from datetime import timedelta

from live_helpers import (
    FAKE_UUID,
    aid,
    assistant_text,
    cost_state,
    notification,
    tool_use,
    user,
    write_jsonl,
)
from test_live_session import SLUG, T0, _follower, _launch, _sub_path, _writes

from agent_postmortem import hooks_cli, live, state, status
from agent_postmortem.cli import main
from agent_postmortem.redact import Redactor


def _main(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    return main_path


def _all(follower, polls=3):
    events = []
    for _ in range(polls):
        events += follower.poll()
    return events


# 1. a user-authored tool name never reaches share output
def test_status_never_prints_a_tool_name_that_is_not_built_in(tmp_path, capsys):
    main_path = _main(tmp_path)
    agent = aid(70)
    _launch(main_path, agent, "toolu_c1", T0)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), tool_use(agent, T0, "toolu_x", "ConfidentialClient", {})])
    main(["status", str(main_path)])
    assert "ConfidentialClient" not in capsys.readouterr().out
    main(["status", str(main_path), "--json"])
    assert "ConfidentialClient" not in capsys.readouterr().out


# 2. a resumed agent's next return is checked again, and it runs again meanwhile
def test_a_resumed_agent_is_running_again_and_its_next_return_is_checked(tmp_path):
    main_path = _main(tmp_path)
    agent = aid(71)
    _launch(main_path, agent, "toolu_r1", T0)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "go"),
                                             assistant_text(agent, T0 + timedelta(seconds=5), "Done.")])
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=6), agent, "toolu_r1", "completed",
                                         'Agent "d" finished')], mode="a")
    f = _follower(tmp_path, main_path)
    assert [e for e in _all(f) if e["event"] == "CLAIM"] == []
    # resumed with SendMessage: its transcript grows past the notice
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0 + timedelta(seconds=60), "one more thing"),
        tool_use(agent, T0 + timedelta(seconds=61), "toolu_b", "Bash", {"command": "make"})],
        mode="a")
    snap = status.snapshot(main_path)
    assert next(r for r in snap["subagents"] if r["agent_id"] == agent)["state"] == "running"
    write_jsonl(_sub_path(main_path, agent), [
        assistant_text(agent, T0 + timedelta(seconds=90), "API Error: Server error mid-response")],
        mode="a")
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=91), agent, "toolu_r1",
                                         "completed", 'Agent "d" finished')], mode="a")
    claims = [e for e in _all(f) if e["event"] == "CLAIM"]
    assert len(claims) == 1


# 3. a replaced main transcript forgets what the old one said
def test_a_replaced_main_transcript_drops_its_old_facts(tmp_path):
    main_path = _main(tmp_path)
    agent = aid(72)
    _launch(main_path, agent, "toolu_m1", T0)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "go"),
                                             tool_use(agent, T0, "toolu_b", "Bash", {"command": "x"})])
    write_jsonl(main_path, [notification(T0, agent, "toolu_m1", "completed", 'Agent "d" finished')],
                mode="a")
    f = _follower(tmp_path, main_path)
    _all(f, 1)
    assert agent in f.main.notices
    main_path.write_text(json.dumps(user(None, T0, "x")) + "\n", encoding="utf-8")  # shorter
    f.poll()
    assert agent not in f.main.notices


# 4. ENDED needs both trailing cost-state records, and any later entry clears them
def test_one_cost_state_does_not_end_the_watch(tmp_path):
    main_path = _main(tmp_path)
    f = _follower(tmp_path, main_path)
    f.poll()
    write_jsonl(main_path, [cost_state()], mode="a")
    assert [e for e in _all(f, 4) if e["event"] == "ENDED"] == []


def test_a_spaced_json_entry_after_cost_state_clears_it(tmp_path):
    main_path = _main(tmp_path)
    f = _follower(tmp_path, main_path)
    f.poll()
    with open(main_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(cost_state()) + "\n" + json.dumps(cost_state()) + "\n")
        handle.write(json.dumps({"type": "user", "message": {"role": "user", "content": "more"}},
                                separators=(", ", ": ")) + "\n")
    assert [e for e in _all(f, 4) if e["event"] == "ENDED"] == []


# 6. one delivery gate for watch and the agent-launched hook, in both orders
def test_the_hook_does_not_repeat_a_claim_the_watch_already_gave(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(73)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    _launch(main_path, agent, "toolu_g1", T0, background=False)
    f = _follower(tmp_path, main_path)
    assert len([e for e in _all(f) if e["event"] == "CLAIM"]) == 1
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Agent",
               "transcript_path": str(main_path),
               "tool_response": {"agentId": agent, "status": "completed"}}
    monkeypatch.setenv("AGENT_POSTMORTEM_WATCH_HINT", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks_cli.dispatch("agent-launched")
    assert "returned, but" not in capsys.readouterr().out


def test_a_running_watch_does_not_repeat_a_claim_the_hook_gave(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(74)
    f = _follower(tmp_path, main_path)
    f.poll()
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Agent",
               "transcript_path": str(main_path),
               "tool_response": {"agentId": agent, "status": "completed"}}
    monkeypatch.setenv("AGENT_POSTMORTEM_WATCH_HINT", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks_cli.dispatch("agent-launched")
    assert "returned, but" in capsys.readouterr().out
    _launch(main_path, agent, "toolu_g2", T0, background=False)
    assert [e for e in _all(f) if e["event"] == "CLAIM"] == []


# 7. --since keeps a run whose start it cannot know without reading it
def test_since_keeps_an_old_run_with_no_timestamp_anywhere(tmp_path, capsys):
    root = tmp_path / "projects"
    run_dir = root / SLUG / FAKE_UUID / "subagents" / "workflows" / "wf_00000000-757"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text('{"type": "launched"}\nnot json\n', encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(run_dir / "journal.jsonl", (old, old))
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 10 * 86400))
    with_filter = main(["scan", "--root", str(root), "--since", since, "--no-cache", "--json"])
    out_with = capsys.readouterr().out
    from agent_postmortem import cli

    cli._SINCE_MTIME_PREFILTER = False
    try:
        without = main(["scan", "--root", str(root), "--since", since, "--no-cache", "--json"])
        out_without = capsys.readouterr().out
    finally:
        cli._SINCE_MTIME_PREFILTER = True
    assert with_filter == without and out_with == out_without


# 8. a cache that cannot be written never stops the command
def test_a_cache_write_error_does_not_stop_scan_status_or_watch(tmp_path, capsys, monkeypatch):
    main_path = _main(tmp_path)

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(state, "_write_cache", denied)
    assert main(["status", str(main_path)]) == 0
    assert capsys.readouterr().out.startswith("STATUS")
    assert main(["scan", "--root", str(tmp_path / "projects")]) in (0, 4)
    assert "ALL TIME" in capsys.readouterr().out
    follower = _follower(tmp_path, main_path)
    assert follower.poll()[0]["event"] == "START"
    follower.close()


# 9. the statusline keeps its offsets: a second tick parses nothing
def test_a_second_statusline_tick_parses_nothing(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(75)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "go"),
                                             tool_use(agent, T0, "toolu_q", "Bash", {"command": "x"})])
    payload = {"transcript_path": str(main_path), "columns": 100,
               "tasks": [{"id": agent, "status": "running", "label": "x", "tokenCount": 1}]}

    def tick():
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        main(["statusline", "subagents"])
        capsys.readouterr()

    tick()
    parsed: list[int] = []
    real = live.Tail.read

    def counting(self, want=None, with_lines=False):
        entries = real(self, want, with_lines)
        parsed.append(len(entries))
        return entries

    monkeypatch.setattr(live.Tail, "read", counting)
    tick()
    assert sum(parsed) == 0


# 10. a native `killed` notice is not "stopped by this session" without evidence
def test_a_killed_notice_without_a_taskstop_is_not_blamed_on_the_session(tmp_path):
    main_path = _main(tmp_path)
    agent = aid(76)
    _launch(main_path, agent, "toolu_k1", T0)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "go"), *_writes(agent, T0, 1)])
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=30), agent, "toolu_k1", "killed",
                                         'Agent "d" was killed')], mode="a")
    snap = status.snapshot(main_path)
    row = next(r for r in snap["subagents"] if r["agent_id"] == agent)
    assert row["state"] == "interrupted"
    assert row["cause"] != "stopped by this session"
    text = status.render_text(snap, Redactor())
    assert "stopped by this session" not in text


# 11. a background session that is done ends the watch
def test_a_done_background_session_ends_the_watch(tmp_path):
    main_path = _main(tmp_path)
    job = tmp_path / "jobs" / "00000000"
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"state": "working", "sessionId": FAKE_UUID}))
    f = _follower(tmp_path, main_path)
    f.poll()
    (job / "state.json").write_text(json.dumps({"state": "done", "sessionId": FAKE_UUID}))
    events = f.poll()
    assert [e["event"] for e in events] == ["STATE", "ENDED"]


# 13. a re-armed watch does not repeat an unchanged background state
def test_a_rearmed_watch_does_not_repeat_an_unchanged_state(tmp_path):
    main_path = _main(tmp_path)
    job = tmp_path / "jobs" / "00000000"
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"state": "blocked", "sessionId": FAKE_UUID}))
    f = _follower(tmp_path, main_path)
    assert [e["event"] for e in f.poll()] == ["START", "STATE"]
    f.close()
    again = _follower(tmp_path, main_path)
    assert [e["event"] for e in again.poll()] == ["START"]


# 14. the first foreground failure keeps the hook message within its budget
def test_a_claim_and_the_first_hint_stay_within_400_characters(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(77)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Agent",
               "transcript_path": str(main_path),
               "tool_response": {"agentId": agent, "status": "completed"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks_cli.dispatch("agent-launched")
    out = json.loads(capsys.readouterr().out)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert len(text) <= 400 and "returned, but" in text
    # the deferred hint comes with the next launch
    payload["tool_response"] = {"agentId": aid(78), "status": "async_launched"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks_cli.dispatch("agent-launched")
    assert "watch --session" in capsys.readouterr().out


# 15. a narrow statusline row keeps a whole fact
def test_a_narrow_statusline_row_keeps_a_whole_short_fact(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(79)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    payload = {"transcript_path": str(main_path), "columns": 24,
               "tasks": [{"id": agent, "status": "completed", "label": "x" * 40, "tokenCount": 1}]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main(["statusline", "subagents"])
    row = json.loads(capsys.readouterr().out)
    assert len(row["content"]) <= 24
    assert row["content"].endswith("returned on an error")


# 5. R2 is decided when the result is read, not when a rate-limited line finally prints
def test_a_report_created_after_the_result_was_read_does_not_turn_claim_into_ok(
    tmp_path, monkeypatch, capsys,
):
    import test_watch as tw

    run_dir = (tmp_path / "-home-user-project" / FAKE_UUID / "subagents" / "workflows"
               / "wf_00000000-758")
    run_dir.mkdir(parents=True)
    first, second, key = aid(80), aid(81), "v2:" + "1" * 64
    report = tmp_path / "out" / "review.md"
    for agent in (first, second):
        tw._meta(run_dir, agent)
    tw._write_transcript(tw._transcript_path(run_dir, first), [
        tw._user_text("2026-02-01T00:00:00.000Z", "Do it."),
        tw._assistant_tool_use("2026-02-01T00:00:01.000Z", "toolu_1", "Bash", {"command": "x"}),
        tw._user_text("2026-02-01T00:05:00.000Z", "[Request interrupted by user for tool use]"),
    ])
    tw._write_transcript(tw._transcript_path(run_dir, second), [
        tw._user_text("2026-02-01T00:05:01.000Z", "Do it."),
        tw._assistant_text("2026-02-01T00:05:30.000Z", "Done."),
    ])
    tw._write_lines(run_dir / "journal.jsonl", [
        tw._started("2026-02-01T00:00:00.000Z", key, first, "task"),
        tw._started("2026-02-01T00:05:01.000Z", key, second, "task"),
        tw._result("2026-02-01T00:05:31.000Z", key, second,
                   {"status": "done", "report_path": str(report)}),
    ])
    clock = tw._Clock()
    tw._patch_clock(monkeypatch, clock)

    def create_report():
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("now it exists", encoding="utf-8")

    tw._patch_scripted_sleep(monkeypatch, [create_report], clock)
    main(["watch", str(run_dir), "--interval", "1", "--done-grace", "0"])
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("KILL") for line in lines)
    assert any(line.startswith("CLAIM") for line in lines), lines
    assert not any(line.startswith("OK ") for line in lines)
