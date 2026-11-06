"""docs/DESIGN.md 4.14.5 (0.4), review rounds 1 and 2 of the hook-driven live checks: each test
is one reproduction of a finding, red before its fix."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from live_helpers import (
    FAKE_UUID,
    aid,
    assistant_text,
    notification,
    user,
    write_jsonl,
)
from test_live_post_tool import (
    REPO,
    _hook,
    _kill,
    _payload,
    _running_background_agent,
    _session,
)
from test_live_session import T0, _launch, _sub_path

from agent_postmortem import hooks_cli, live, state


def _stop(monkeypatch, capsys, main_path) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "Stop", "session_id": FAKE_UUID, "transcript_path": str(main_path),
        "stop_hook_active": False,
    })))
    assert hooks_cli.dispatch("check") == 0
    return capsys.readouterr().out.strip()


def _quiet(*paths) -> None:
    old = time.time() - 3600
    for path in paths:
        os.utime(path, (old, old))  # the session is quiet, so Stop analyzes it


# --- 1: a call that dies after its first claim must not lose that ending --------------------


def test_a_call_that_dies_while_it_works_out_a_second_ending_loses_neither(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    first, second = aid(101), aid(102)
    transcripts = [_running_background_agent(main_path, agent) for agent in (first, second)]
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    for transcript, agent in zip(transcripts, (first, second)):
        _kill(transcript, agent)
    real = live.SessionFollower._kill_event
    made: list = []

    def dies_on_the_second(self, agent_id, sub):
        made.append(agent_id)
        if len(made) == 2:
            raise RuntimeError("the run.sh deadline, in the middle of the second ending")
        return real(self, agent_id, sub)

    monkeypatch.setattr(live.SessionFollower, "_kill_event", dies_on_the_second)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""
    monkeypatch.setattr(live.SessionFollower, "_kill_event", real)
    text = _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    assert text.count("was interrupted") == 2, text


# --- 2: a partial last line must be scanned again once it is complete ----------------------


def test_a_return_notice_written_in_two_parts_still_opens_the_gate(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(103)
    _launch(main_path, agent, "toolu_l103", T0, background=True)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"),
        assistant_text(agent, T0 + timedelta(seconds=9), "API Error: Server error mid-response"),
    ])
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""
    line = json.dumps(notification(T0 + timedelta(seconds=11), agent, "toolu_l103", "completed",
                                   'Agent "d" finished')) + "\n"
    # the first part runs more than the gate's 64-byte overlap past the line's last mark, so the
    # second part, which carries no mark, is all a gate that saved the end of the file would see
    cut = line.rindex("task-notification") + len("task-notification") + 70
    assert 0 < len(line) - cut and "task-notification" not in line[cut - 64:]
    with main_path.open("a", encoding="utf-8") as handle:
        handle.write(line[:cut])
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # not parsed yet
    with main_path.open("a", encoding="utf-8") as handle:
        handle.write(line[cut:])
    text = _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    assert "CLAIM" in text, text


# --- 3: a partial first read must reach every transcript even while one keeps growing --------


def test_a_partial_read_reaches_a_new_transcript_while_an_older_one_keeps_growing(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    busy, killed = aid(104), aid(105)
    busy_transcript = _running_background_agent(main_path, busy)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    killed_transcript = _running_background_agent(main_path, killed)
    _kill(killed_transcript, killed)
    monkeypatch.setattr(hooks_cli, "_FIRST_READ_BUDGET_SECONDS", 0.0)
    texts = []
    for step in range(4):
        write_jsonl(busy_transcript, [assistant_text(busy, T0 + timedelta(seconds=10 + step),
                                                     f"step {step}")], mode="a")
        texts.append(_hook(monkeypatch, capsys, "post-tool", _payload(main_path)))
    assert any("was interrupted" in text for text in texts), texts


# --- 4: Stop and the hook tell a kill that was answered by a completed notice once ----------


def _killed_then_completed(tmp_path, agent):
    main_path = _session(tmp_path)
    transcript = _running_background_agent(main_path, agent)
    return main_path, transcript


def _complete(main_path, agent) -> None:
    write_jsonl(main_path, [notification(T0 + timedelta(seconds=701), agent, "toolu_bg",
                                         "completed", 'Agent "d" finished')], mode="a")


def test_stop_does_not_repeat_a_kill_the_hook_told_as_a_claim(tmp_path, monkeypatch, capsys):
    agent = aid(106)
    main_path, transcript = _killed_then_completed(tmp_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _kill(transcript, agent)
    _complete(main_path, agent)
    assert "CLAIM" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _quiet(main_path, transcript)
    assert _stop(monkeypatch, capsys, main_path) == ""


def test_the_hook_does_not_repeat_a_kill_stop_told_before_its_completed_notice(
    tmp_path, monkeypatch, capsys,
):
    agent = aid(107)
    main_path, transcript = _killed_then_completed(tmp_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _kill(transcript, agent)
    _complete(main_path, agent)
    _quiet(main_path, transcript)
    assert "agents a:" in _stop(monkeypatch, capsys, main_path)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""


def test_control_a_kill_answered_by_a_completed_notice_is_told_by_stop_alone(
    tmp_path, monkeypatch, capsys,
):
    agent = aid(108)
    main_path, transcript = _killed_then_completed(tmp_path, agent)
    _kill(transcript, agent)
    _complete(main_path, agent)
    _quiet(main_path, transcript)
    assert "agents a:" in _stop(monkeypatch, capsys, main_path)


# --- 5: run.sh ends a per-call hook inside the 5 s hooks.json timeout, probe included -------


def test_run_sh_ends_a_hung_post_tool_call_after_3_s(tmp_path):
    # with the 1 s probe deadline (the next test) the two fit 4 s of the 5 s hooks.json timeout;
    # the call alone had 4 s before, which a slow probe pushed past the timeout. Timed from the
    # call's own start, so run.sh's startup and probe, which vary with load, are left out.
    from test_plugin import _run_run_sh, _write_fake_cli

    bin_dir = tmp_path / "bin"
    started = tmp_path / "call-started"
    _write_fake_cli(bin_dir, "agent-postmortem", (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then\n'
        "  printf '{\"protocol\": 1, \"version\": \"0.0.0\"}\\n'\n"
        "  exit 0\n"
        "fi\n"
        f"'{sys.executable}' -c 'import time; print(time.time())' > '{started}'\n"
        "sleep 10\n"
    ))
    result = _run_run_sh("post-tool", path_entries=[str(bin_dir)],
                         plugin_data=tmp_path / "plugin-data", stdin="{}", timeout=15)
    ended = time.time()
    assert result.returncode == 0 and result.stdout == "", result
    call = ended - float(started.read_text())
    assert 2.5 < call < 4.0, f"the call ran {call:.2f} s"  # 4 s before: as wide as that allows


def test_run_sh_probes_a_failing_path_cli_once_per_call(tmp_path):
    from test_plugin import _run_run_sh, _write_fake_cli

    bin_dir = tmp_path / "bin"
    count = tmp_path / "probes"
    _write_fake_cli(bin_dir, "agent-postmortem", (
        "#!/usr/bin/env bash\n"
        f"echo probe >> '{count}'\n"
        "sleep 10\n"
    ))
    start = time.monotonic()
    result = _run_run_sh("post-tool", path_entries=[str(bin_dir)],
                         plugin_data=tmp_path / "plugin-data", stdin="{}", timeout=15)
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result
    assert count.read_text().count("probe") == 1, count.read_text()
    # the count is what says once; the clock only that the 1 s probe deadline cut the 10 s sleep. A
    # fixed 2 s failed at 2.8 s on a slow shared runner, a timing flake with a correct build
    assert elapsed < 8.0, f"{elapsed:.1f} s"


def test_the_capabilities_probe_starts_without_the_cli():
    script = (
        "import io, json, sys\n"
        "from agent_postmortem import entry\n"
        "sys.stdin = io.StringIO('')\n"
        "code = entry.main(['hook', 'capabilities'])\n"
        "mods = sorted(m for m in sys.modules if m.startswith('agent_postmortem'))\n"
        "sys.stderr.write(json.dumps(mods))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data["protocol"] == 1 and isinstance(data["version"], str), data
    mods = json.loads(done.stderr.strip().splitlines()[-1])
    assert "agent_postmortem.cli" not in mods and "agent_postmortem.hooks_cli" not in mods, mods


# --- 6: a malformed live store is read as empty, never as a reason to skip Stop -------------


def test_a_malformed_live_store_does_not_stop_the_stop_check(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(109)
    transcript = _running_background_agent(main_path, agent)
    _kill(transcript, agent)
    _quiet(main_path, transcript)
    with state.cache_transaction(state.live_store_path()) as data:
        data["sessions"] = []
    assert "agents a:" in _stop(monkeypatch, capsys, main_path)


def test_a_malformed_live_store_does_not_stop_the_resume_sentence(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(110)
    transcript = _running_background_agent(main_path, agent)
    _kill(transcript, agent)
    with state.cache_transaction(state.live_store_path()) as data:
        data["sessions"] = []
    assert hooks_cli._resume_sentence(main_path) is not None


def test_a_malformed_live_store_does_not_stop_the_hook(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    agent = aid(111)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    _kill(transcript, agent)
    with state.cache_transaction(state.live_store_path()) as data:
        data["sessions"] = []
        data["transcripts"] = []
    assert "KILL" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))


# --- round 2 -----------------------------------------------------------------------------------


def test_a_call_that_dies_while_it_claims_the_second_ending_loses_neither(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    agents = sorted([aid(112), aid(113)])
    transcripts = [_running_background_agent(main_path, agent) for agent in agents]
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    for transcript, agent in zip(transcripts, agents):
        _kill(transcript, agent)
    real = state._write_cache

    def dies_writing_the_second_claim(cache_file, data):
        if f"{agents[1]}@" in json.dumps(data.get("sessions", {})):
            raise RuntimeError("the run.sh deadline, while the second ending is claimed")
        return real(cache_file, data)

    monkeypatch.setattr(state, "_write_cache", dies_writing_the_second_claim)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""
    monkeypatch.setattr(state, "_write_cache", real)
    text = _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    assert text.count("was interrupted") == 2, text


def test_a_partial_read_reaches_an_older_transcript_while_another_keeps_growing(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    busy, killed = aid(114), aid(115)
    busy_transcript = _running_background_agent(main_path, busy)
    killed_transcript = _running_background_agent(main_path, killed)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))  # both read once
    _kill(killed_transcript, killed)
    monkeypatch.setattr(hooks_cli, "_FIRST_READ_BUDGET_SECONDS", 0.0)
    texts = []
    for step in range(5):
        write_jsonl(busy_transcript, [assistant_text(busy, T0 + timedelta(seconds=10 + step),
                                                     f"step {step}")], mode="a")
        texts.append(_hook(monkeypatch, capsys, "post-tool", _payload(main_path)))
    assert any("was interrupted" in text for text in texts), texts


_MALFORMED_RECORDS = [
    {"reported": [{}]}, {"reported": 123}, {"reported": ["valid", None]},
    {"baseline": [{}]}, {"baseline": 5}, {"reported": [["x"]], "baseline": [None]},
]


@pytest.mark.parametrize("record", _MALFORMED_RECORDS, ids=lambda record: json.dumps(record))
def test_a_malformed_session_record_stops_neither_stop_nor_the_hook(
    tmp_path, monkeypatch, capsys, record,
):
    main_path = _session(tmp_path)
    agent = aid(116)
    transcript = _running_background_agent(main_path, agent)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    with state.cache_transaction(state.live_store_path()) as data:
        data.setdefault("sessions", {})[live.session_key(main_path)] = dict(record)
    _kill(transcript, agent)
    assert "KILL" in _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    with state.cache_transaction(state.live_store_path()) as data:
        data["sessions"][live.session_key(main_path)] = dict(record)  # malformed again for Stop
    _quiet(main_path, transcript)
    assert "agents a:" in _stop(monkeypatch, capsys, main_path)


def test_a_first_call_inside_a_last_line_longer_than_64_kb_still_sees_its_notice(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    agent = aid(117)
    _launch(main_path, agent, "toolu_l117", T0, background=True)
    write_jsonl(_sub_path(main_path, agent), [
        user(agent, T0, "do it"),
        assistant_text(agent, T0 + timedelta(seconds=9), "API Error: Server error mid-response"),
    ])
    entry = notification(T0 + timedelta(seconds=11), agent, "toolu_l117", "completed",
                         'Agent "d" finished')
    entry["pad"] = "x" * 70000  # the line's marks all come more than 64 KB before its end
    line = json.dumps(entry) + "\n"
    with main_path.open("a", encoding="utf-8") as handle:
        handle.write(line[:-10])
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""  # the first call
    with main_path.open("a", encoding="utf-8") as handle:
        handle.write(line[-10:])
    text = _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    assert "CLAIM" in text, text


# --- round 3 (still open from round 2) ---------------------------------------------------------


def _end_key(main_path, agent) -> str:
    facts = live.SubFacts()
    facts.update(live.Tail(_sub_path(main_path, agent)).read(live._sub_want))
    return f"{live.ending_key(agent, facts)}:end"


def test_partial_claim_win_interrupted_during_render_retries_remaining_ending(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    first, second = sorted([aid(118), aid(119)])
    transcripts = [_running_background_agent(main_path, agent) for agent in (first, second)]
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    for transcript, agent in zip(transcripts, (first, second)):
        _kill(transcript, agent)
    real = hooks_cli._render_endings
    renders: list = []

    def watch_wins_one_then_dies(events, main, follower):
        renders.append(len(events))
        if len(renders) == 1:
            assert live.claim_delivery(main_path, _end_key(main_path, first))  # a watch, now
            return real(events, main, follower)
        raise RuntimeError("the run.sh deadline, while the rest is rendered")

    monkeypatch.setattr(hooks_cli, "_render_endings", watch_wins_one_then_dies)
    assert _hook(monkeypatch, capsys, "post-tool", _payload(main_path)) == ""
    assert renders == [2, 1]
    monkeypatch.setattr(hooks_cli, "_render_endings", real)
    text = _hook(monkeypatch, capsys, "post-tool", _payload(main_path))
    assert text.count("was interrupted") == 1, text  # the second, which the watch never told


def test_two_growing_transcripts_do_not_block_a_third_agents_ending(
    tmp_path, monkeypatch, capsys,
):
    main_path = _session(tmp_path)
    busy = [aid(120), aid(121)]
    killed = aid(122)
    busy_transcripts = [_running_background_agent(main_path, agent) for agent in busy]
    killed_transcript = _running_background_agent(main_path, killed)
    _hook(monkeypatch, capsys, "post-tool", _payload(main_path))  # complete: the baseline
    _kill(killed_transcript, killed)
    monkeypatch.setattr(hooks_cli, "_FIRST_READ_BUDGET_SECONDS", 0.0)
    texts = []
    for step in range(6):
        for agent, transcript in zip(busy, busy_transcripts):
            write_jsonl(transcript, [assistant_text(agent, T0 + timedelta(seconds=10 + step),
                                                    f"step {step}")], mode="a")
        texts.append(_hook(monkeypatch, capsys, "post-tool", _payload(main_path)))
    assert any("was interrupted" in text for text in texts), texts


def test_a_malformed_session_record_does_not_end_a_watch(tmp_path):
    main_path = _session(tmp_path)
    agent = aid(123)
    transcript = _running_background_agent(main_path, agent)
    follower = live.SessionFollower(main_path, caller_session=None)
    follower.poll()
    with state.cache_transaction(state.live_store_path()) as data:
        data["sessions"][live.session_key(main_path)]["reported"] = [{}]
    _kill(transcript, agent)
    events = [e["event"] for e in follower.poll()] + [e["event"] for e in follower.poll()]
    assert "KILL" in events, events


def test_a_malformed_session_record_does_not_stop_the_launch_hint(tmp_path, monkeypatch, capsys):
    main_path = _session(tmp_path)
    with state.cache_transaction(state.live_store_path()) as data:
        data.setdefault("sessions", {})[live.session_key(main_path)] = {"reported": [{}]}
    payload = {**_payload(main_path), "tool_name": "Agent",
               "tool_input": {"description": "d", "prompt": "p", "run_in_background": True},
               "tool_response": {"agentId": aid(124), "status": "async_launched",
                                 "isAsync": True}}
    assert "next tool call" in _hook(monkeypatch, capsys, "agent-launched", payload)


# --- round 4 (still open from rounds 2 and 3) --------------------------------------------------


def test_two_growing_transcripts_do_not_block_the_first_baseline(tmp_path, monkeypatch, capsys):
    # no baseline yet and no single call reads everything: once every transcript was read at
    # least once the baseline is taken (a growing one is recent, so never in it) and the new
    # kill is told, while the ending from two hours ago never is
    main_path = _session(tmp_path)
    busy = [aid(125), aid(126)]
    killed, old = aid(127), aid(128)
    busy_transcripts = [_running_background_agent(main_path, agent) for agent in busy]
    killed_transcript = _running_background_agent(main_path, killed)
    _kill(killed_transcript, killed)
    old_transcript = _running_background_agent(main_path, old)
    _kill(old_transcript, old)
    _quiet(old_transcript)
    os.utime(old_transcript, (time.time() - 7200, time.time() - 7200))
    monkeypatch.setattr(hooks_cli, "_FIRST_READ_BUDGET_SECONDS", 0.0)
    texts = []
    for step in range(8):
        for agent, transcript in zip(busy, busy_transcripts):
            write_jsonl(transcript, [assistant_text(agent, T0 + timedelta(seconds=10 + step),
                                                    f"step {step}")], mode="a")
        texts.append(_hook(monkeypatch, capsys, "post-tool", _payload(main_path)))
    assert sum(text.count("was interrupted") for text in texts) == 1, texts
