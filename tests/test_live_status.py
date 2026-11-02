"""docs/DESIGN.md 4.14.4: `agent-postmortem status`, one snapshot of one session from incremental
reads kept in the live store, plus the native background-session states passed through."""

from __future__ import annotations

import json
import os
import re
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
from test_live_session import SLUG, T0, _launch, _sub_path, _writes

from agent_postmortem import live, state
from agent_postmortem.cli import main


def _busy_session(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    running, clean, killed, claimed = aid(21), aid(22), aid(23), aid(24)
    for n, agent in enumerate((running, clean, killed, claimed)):
        _launch(main_path, agent, f"toolu_s{n}", T0)
    write_jsonl(_sub_path(main_path, running), [
        user(running, T0, "do it"),
        tool_use(running, T0, "toolu_r", "Bash", {"command": "make"}),
    ])
    old = time.time() - 400
    os.utime(_sub_path(main_path, running), (old, old))
    write_jsonl(_sub_path(main_path, clean), [user(clean, T0, "do it"),
                                             assistant_text(clean, T0, "Done.")])
    write_jsonl(_sub_path(main_path, killed), [
        user(killed, T0, "do it"), *_writes(killed, T0, 2),
        user(killed, T0 + timedelta(seconds=700), [{"type": "text", "text": KILL_SENTINEL_TOOL}]),
    ])
    write_jsonl(_sub_path(main_path, claimed), [
        user(claimed, T0, "do it"),
        assistant_text(claimed, T0, "API Error: Server error mid-response"),
    ])
    write_jsonl(main_path, [
        notification(T0 + timedelta(seconds=60), clean, "toolu_s1", "completed", 'Agent "d" finished'),
        notification(T0 + timedelta(seconds=60), claimed, "toolu_s3", "completed",
                     'Agent "d" finished'),
        tool_use(None, T0, "toolu_wf", "Workflow", {"script": "x"}),
        tool_result(None, T0, "toolu_wf", "launched",
                    toolUseResult={"runId": "wf_00000000-752", "status": "async_launched"}),
    ], mode="a")
    return main_path, (running, clean, killed, claimed)


def test_status_counts_every_subagent_and_lists_only_what_needs_a_look(tmp_path, capsys):
    main_path, _ids = _busy_session(tmp_path)
    code = main(["status", str(main_path)])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0].startswith("STATUS  session s_")
    assert "4 subagents (1 running, 2 returned, 1 interrupted, 0 failed)" in out[0]
    rows = [line.split()[0] for line in out[1:]
            if line.startswith("  ") and not line.startswith("   ")]
    assert rows == ["RUNNING", "INTERRUPTED", "CLAIM", "WORKFLOW"]
    running = next(line for line in out if line.startswith("  RUNNING"))
    assert "quiet 6m" in running and "Bash" in running
    index = next(i for i, line in enumerate(out) if line.startswith("  INTERRUPTED"))
    assert "nothing in this session stopped it" in out[index]
    assert "2 paths, 0 commits changed" in out[index + 1]
    assert all(len(line) <= 100 for line in out)


def test_status_json_has_one_row_per_subagent(tmp_path, capsys):
    main_path, (running, clean, killed, claimed) = _busy_session(tmp_path)
    assert main(["status", str(main_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"].startswith("s_")
    states = sorted(row["state"] for row in payload["subagents"])
    assert states == ["interrupted", "returned", "returned", "running"]
    assert all(row["agent"].startswith("a:") for row in payload["subagents"])
    assert payload["workflows"] == [{"run_id": "wf_00000000-752",
                                     "next_command": "agent-postmortem watch wf_00000000-752"}]


def test_a_second_status_reads_only_what_was_appended(tmp_path, capsys, monkeypatch):
    main_path, _ids = _busy_session(tmp_path)
    main(["status", str(main_path)])
    first = capsys.readouterr().out
    store = state.read_cache(state.live_store_path())
    offsets = {path: entry["offset"] for path, entry in store["transcripts"].items()}
    assert offsets[str(main_path)] == main_path.stat().st_size
    parsed: list[int] = []
    real = live.Tail.read

    def counting(self, want=None, with_lines=False):
        entries = real(self, want, with_lines)
        parsed.append(len(entries))
        return entries

    monkeypatch.setattr(live.Tail, "read", counting)
    main(["status", str(main_path)])
    assert capsys.readouterr().out == first
    assert sum(parsed) == 0


def test_status_passes_background_states_through(tmp_path, capsys):
    main_path, _ids = _busy_session(tmp_path)
    for short, word in (("00000001", "working"), ("00000002", "blocked"), ("00000003", "done")):
        sid = f"00000000-0000-4000-8000-00000000000{short[-1]}"
        write_jsonl(tmp_path / "projects" / SLUG / f"{sid}.jsonl", [user(None, T0, "x")])
        job = tmp_path / "jobs" / short
        job.mkdir(parents=True)
        (job / "state.json").write_text(json.dumps({
            "state": word, "detail": "private", "sessionId": sid,
            "updatedAt": "2026-10-30T12:00:00.000Z",
        }), encoding="utf-8")
    main(["status", str(main_path)])
    out = capsys.readouterr().out
    assert "BACKGROUND 3 sessions on this machine: 1 working, 1 blocked, 1 done" in out
    assert "private" not in out
    assert re.search(r"^  BLOCKED\s+s_[0-9a-f]{10}  for ", out, re.M)


def test_status_names_a_missing_output_only_at_local(tmp_path, capsys):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    agent = aid(25)
    _launch(main_path, agent, "toolu_m", T0)
    target = tmp_path / "home-out" / "notes.md"
    write_jsonl(_sub_path(main_path, agent), [user(agent, T0, f"write the notes to {target}"),
                                             assistant_text(agent, T0, "Done.")])
    write_jsonl(main_path, [notification(T0, agent, "toolu_m", "completed", 'Agent "d" finished')],
                mode="a")
    main(["status", str(main_path)])
    share = capsys.readouterr().out
    assert "CLAIM" in share and str(target) not in share and "notes.md" not in share
    main(["status", str(main_path), "--raw"])
    assert str(target) in capsys.readouterr().out


def test_status_of_the_callers_session(tmp_path, capsys, monkeypatch):
    _busy_session(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", FAKE_UUID)
    assert main(["status", "--root", str(tmp_path / "projects")]) == 0
    assert capsys.readouterr().out.startswith("STATUS")


def test_status_without_a_session_is_a_usage_error(tmp_path, capsys):
    assert main(["status", "--root", str(tmp_path)]) == 1


def test_status_of_an_unknown_session_exits_2(tmp_path, capsys):
    (tmp_path / "projects").mkdir()
    assert main(["status", FAKE_UUID, "--root", str(tmp_path / "projects")]) == 2
