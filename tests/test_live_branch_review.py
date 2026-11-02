"""Reproductions of the branch-review findings on `live` (lenses A to D), one test per finding,
each failing before its fix (docs/DESIGN.md 4.14)."""

from __future__ import annotations

import io
import json
import os
import time
from datetime import timedelta

from live_helpers import (
    FAKE_UUID,
    KILL_SENTINEL_TOOL,
    aid,
    assistant_text,
    notification,
    tool_result,
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


def _row(snap, agent):
    return next(r for r in snap["subagents"] if r["agent_id"] == agent)


# 1. --since keeps a workflow whose start the loader cannot know
def test_since_keeps_a_workflow_whose_transcripts_carry_no_timestamp(tmp_path, capsys):
    from agent_postmortem import cli

    root = tmp_path / "projects"
    run_dir = root / SLUG / FAKE_UUID / "subagents" / "workflows" / "wf_00000000-759"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(
        '{"type": "launched", "timestamp": "2026-09-01T00:00:00Z"}\n', encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(run_dir / "journal.jsonl", (old, old))
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 10 * 86400))
    with_filter = main(["scan", "--root", str(root), "--since", since, "--no-cache", "--json"])
    out_with = capsys.readouterr().out
    cli._SINCE_MTIME_PREFILTER = False
    try:
        without = main(["scan", "--root", str(root), "--since", since, "--no-cache", "--json"])
        out_without = capsys.readouterr().out
    finally:
        cli._SINCE_MTIME_PREFILTER = True
    assert (with_filter, out_with) == (without, out_without)


# 2. a synchronous return with no status, or with the agent id on the result block
def test_a_synchronous_return_without_a_status_is_a_return(tmp_path):
    main_path = _main(tmp_path)
    agent = aid(90)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    write_jsonl(main_path, [
        tool_use(None, T0 + timedelta(seconds=5), "toolu_s1", "Agent", {"prompt": "p"}),
        tool_result(None, T0 + timedelta(seconds=6), "toolu_s1", "report",
                    toolUseResult={"agentId": agent}),
    ], mode="a")
    assert _row(status.snapshot(main_path), agent)["state"] == "returned"
    f = _follower(tmp_path, main_path)
    assert len([e for e in _all(f) if e["event"] == "CLAIM"]) == 1


def test_an_agent_id_on_the_result_block_links_the_return(tmp_path):
    main_path = _main(tmp_path)
    agent = aid(91)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "go"),
                                             assistant_text(agent, T0, "Done.")])
    entry = tool_result(None, T0 + timedelta(seconds=6), "toolu_s2", "report")
    entry["message"]["content"][0]["agentId"] = agent
    write_jsonl(main_path, [
        tool_use(None, T0 + timedelta(seconds=5), "toolu_s2", "Agent", {"prompt": "p"}), entry],
        mode="a")
    assert _row(status.snapshot(main_path), agent)["state"] == "returned"


def test_the_hook_checks_a_synchronous_return_without_a_status(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(92)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Agent",
               "transcript_path": str(main_path), "tool_response": {"agentId": agent}}
    monkeypatch.setenv("AGENT_POSTMORTEM_WATCH_HINT", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks_cli.dispatch("agent-launched")
    assert "returned, but" in capsys.readouterr().out


# 3. a quoted notification is not a notification
def test_a_notification_quoted_in_prose_changes_nothing(tmp_path):
    main_path = _main(tmp_path)
    agent = aid(93)
    _launch(main_path, agent, "toolu_q1", T0)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "go"),
                                             tool_use(agent, T0, "toolu_b", "Bash", {"command": "x"})])
    quoted = notification(T0 + timedelta(seconds=9), agent, "toolu_zz", "completed",
                          'Agent "d" finished')
    quoted["message"]["content"] = "Please explain this example:\n" + quoted["message"]["content"]
    write_jsonl(main_path, [quoted], mode="a")
    no_origin = notification(T0 + timedelta(seconds=10), agent, "toolu_zz", "completed",
                             'Agent "d" finished')
    del no_origin["origin"]
    write_jsonl(main_path, [no_origin], mode="a")
    assert _row(status.snapshot(main_path), agent)["state"] == "running"


# 4. resume uses the same per-ending keys as the watch
def test_resume_does_not_repeat_an_ending_the_watch_reported(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(94)
    _launch(main_path, agent, "toolu_rr", T0)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), *_writes(agent, T0, 1),
        user(agent, T0 + timedelta(seconds=640), [{"type": "text", "text": KILL_SENTINEL_TOOL}])])
    f = _follower(tmp_path, main_path)
    assert len([e for e in _all(f) if e["event"] == "KILL"]) == 1
    f.close()
    payload = {"hook_event_name": "SessionStart", "source": "resume",
               "transcript_path": str(main_path)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks_cli.dispatch("session-start")
    assert "interrupted" not in capsys.readouterr().out


# 5. the watch claims through the locked gate: a hook claim made mid-poll wins
def test_the_watch_skips_an_ending_the_hook_claimed_during_its_poll(tmp_path, monkeypatch):
    main_path = _main(tmp_path)
    agent = aid(95)
    f = _follower(tmp_path, main_path)
    f.poll()
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"), assistant_text(agent, T0, "API Error: Server error mid-response")])
    _launch(main_path, agent, "toolu_g5", T0 + timedelta(seconds=5), background=False)
    real = live.final_cause

    def hook_claims_first(path):
        facts = live.SubFacts()
        facts.update(live.Tail(path).read(live._sub_want))
        live.claim_delivery(main_path, f"{live.ending_key(agent, facts)}:claim")
        return real(path)

    monkeypatch.setattr(live, "final_cause", hook_claims_first)
    assert [e for e in _all(f) if e["event"] == "CLAIM"] == []


# 6. a fork's inherited prefix is its parent's
def test_a_fork_does_not_count_its_parents_subagents(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    parent = "parent-session"  # any sessionId other than the file's own marks the prefix
    agent = aid(96)
    inherited = [
        dict(user(None, T0, "start"), sessionId=parent),
        dict(tool_use(None, T0, "toolu_f1", "Agent", {"prompt": "p", "run_in_background": True}),
             sessionId=parent),
        dict(tool_result(None, T0, "toolu_f1", "launched",
                         toolUseResult={"agentId": agent, "status": "async_launched"}),
             sessionId=parent),
        dict(notification(T0, agent, "toolu_f1", "completed", 'Agent "d" finished'),
             sessionId=parent),
    ]
    write_jsonl(main_path, [*inherited, user(None, T0 + timedelta(seconds=60), "fork's own")])
    snap = status.snapshot(main_path)
    assert snap["subagents"] == []


# 7. a cause phrase from transcript text is redacted in every share sink
def test_a_reset_suffix_with_a_path_is_redacted_everywhere(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(97)
    secret = "/srv/PrivateClient/report"
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"),
        assistant_text(agent, T0, f"You've hit your session limit · resets {secret}")])
    _launch(main_path, agent, "toolu_r7", T0 + timedelta(seconds=5), background=False)
    f = _follower(tmp_path, main_path)
    claims = [e for e in _all(f) if e["event"] == "CLAIM"]
    assert claims and secret not in live.render_json(claims[0])
    f.close()
    main(["status", str(main_path), "--json"])
    assert secret not in capsys.readouterr().out
    payload = {"transcript_path": str(main_path), "columns": 200,
               "tasks": [{"id": agent, "status": "completed", "label": "x", "tokenCount": 1}]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main(["statusline", "subagents"])
    assert secret not in capsys.readouterr().out


# 8. a killed notice naming another cause keeps that cause in status
def test_a_killed_notice_naming_a_usage_limit_says_so_in_status(tmp_path):
    main_path = _main(tmp_path)
    agent = aid(98)
    _launch(main_path, agent, "toolu_k8", T0)
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, "go"), *_writes(agent, T0, 1)])
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=30), agent, "toolu_k8", "killed",
                                         'Agent "d" was stopped by a usage limit')], mode="a")
    text = status.render_text(status.snapshot(main_path), Redactor())
    assert "nothing in this session stopped it" not in text
    assert "usage limit" in text


# 9. an uncertain count is never printed as a definite zero
def test_a_partial_zero_is_printed_as_a_lower_bound(tmp_path):
    event = {"event": "KILL", "agent": "a:000000000000", "paths": 0, "paths_partial": True,
             "commits": 0, "commits_unknown": True, "next_command": "agent-postmortem run s_x"}
    text = "\n".join(live.render_text(event))
    assert "0+ paths" in text and "commit count unknown" in text


# 10. the checks map is pruned with the transcripts
def test_the_checks_map_drops_entries_for_transcripts_that_are_gone(tmp_path):
    main_path = _main(tmp_path)
    with state.cache_transaction(state.live_store_path()) as data:
        data["checks"] = {"claim:/tmp/live-gone/agent-a.jsonl": {"sig": [1], "value": None}}
    status.snapshot(main_path)
    assert "claim:/tmp/live-gone/agent-a.jsonl" not in state.read_cache(
        state.live_store_path()).get("checks", {})


# repair review of 7: an agent id quoted in a cause shows as its alias in all six outputs
def _reset_store():
    with state.cache_transaction(state.live_store_path()) as data:
        data.clear()


def _watch(tmp_path, main_path, monkeypatch, json_mode):
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(live, "_clock", lambda: float(next(ticks)))
    monkeypatch.setattr(live, "_sleep", lambda seconds: None)
    out = io.StringIO()
    live.watch_session(main_path, max_hours=3 / 3600, json_mode=json_mode, out=out,
                       err=io.StringIO(), follower=_follower(tmp_path, main_path))
    return out.getvalue()


def test_an_agent_id_in_a_reset_suffix_is_shown_as_its_alias(tmp_path, monkeypatch, capsys):
    main_path = _main(tmp_path)
    agent = aid(99)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"),
        assistant_text(agent, T0, f"You've hit your session limit · resets {agent}")])
    _launch(main_path, agent, "toolu_r8", T0 + timedelta(seconds=5), background=False)
    outputs = {}
    outputs["watch json"] = _watch(tmp_path, main_path, monkeypatch, json_mode=True)
    _reset_store()
    outputs["watch text"] = _watch(tmp_path, main_path, monkeypatch, json_mode=False)
    _reset_store()
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Agent",
               "transcript_path": str(main_path), "tool_response": {"agentId": agent}}
    monkeypatch.setenv("AGENT_POSTMORTEM_WATCH_HINT", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks_cli.dispatch("agent-launched")
    outputs["hook"] = capsys.readouterr().out
    main(["status", str(main_path), "--json"])
    outputs["status json"] = capsys.readouterr().out
    main(["status", str(main_path)])
    outputs["status text"] = capsys.readouterr().out
    line = {"transcript_path": str(main_path), "columns": 200,
            "tasks": [{"id": agent, "status": "completed", "label": "x", "tokenCount": 1}]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(line)))
    main(["statusline", "subagents"])
    # the row's own id is the native task id the statusline protocol echoes back
    outputs["statusline"] = json.loads(capsys.readouterr().out)["content"]
    for name, text in outputs.items():
        assert "usage limit" in text, (name, text)
        assert agent not in text, name


def test_the_session_id_in_a_reset_suffix_is_shown_as_its_alias(tmp_path, capsys):
    main_path = _main(tmp_path)
    agent = aid(100)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "go"),
        assistant_text(agent, T0, f"You've hit your session limit · resets {FAKE_UUID}")])
    _launch(main_path, agent, "toolu_r9", T0 + timedelta(seconds=5), background=False)
    main(["status", str(main_path), "--json"])
    detail = _row_detail(json.loads(capsys.readouterr().out))
    assert detail.startswith("session usage limit reached, resets s:"), detail


def _row_detail(snap):
    return next(r["claim"]["detail"] for r in snap["subagents"] if r.get("claim"))
