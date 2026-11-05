"""A subagent resumed by a message ends with a notice naming no tool-use id (field report 2026-11-04,
"status keeps a finished subagent as RUNNING after its hand-back"; docs/DESIGN.md 4.14.4).

Its first notice named the launch's tool-use id and said it was waiting on background work. Each later
one names only the task, so the live read dropped it and kept the first. The subagent's transcript had
grown past that first notice, which reads as "resumed since", so `status` said RUNNING although the
harness had told the session it finished.

Synthetic data only.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta

import pytest
from live_helpers import FAKE_UUID, tool_result, tool_use, user, write_jsonl
from test_live_session import SLUG, T0

from agent_postmortem import status

AGENT = "a" + "c" * 16
OTHER = "a" + "d" * 16
WAITING = 'Agent "x" is waiting on its own background work'
FINISHED = 'Agent "x" finished'


def _envelope(task: str, state: str, summary: str, tool: str | None = None) -> str:
    named = f"<tool-use-id>{tool}</tool-use-id>" if tool else ""
    return (f"<task-notification><task-id>{task}</task-id>{named}<status>{state}</status>"
            f"<summary>{summary}</summary></task-notification>")


def _notice(text: str, at) -> list[dict]:
    """The two copies a notice is written as: the queue's and the user entry."""
    stamp = at.isoformat().replace("+00:00", "Z")
    queue = {"type": "queue-operation", "operation": "enqueue", "timestamp": stamp, "content": text,
             "sessionId": FAKE_UUID}
    entry = user(None, at, text)
    entry["origin"] = {"kind": "task-notification"}
    return [queue, entry]


def _session(tmp_path, *later: list[dict]):
    main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    tool = "toolu_launch"
    entries = [
        user(None, T0, "start"),
        tool_use(None, T0, tool, "Agent", {"prompt": "x", "subagent_type": "g",
                                           "run_in_background": True}),
        tool_result(None, T0, tool, "started",
                    toolUseResult={"agentId": AGENT, "status": "async_launched"}),
        *_notice(_envelope(AGENT, "completed", WAITING, tool), T0 + timedelta(minutes=10)),
    ]
    for chunk in later:
        entries.extend(chunk)
    write_jsonl(main, entries)
    sub = main.parent / FAKE_UUID / "subagents" / f"agent-{AGENT}.jsonl"
    write_jsonl(sub, [user(AGENT, T0, "work"),
                      user(AGENT, T0 + timedelta(minutes=30), "more work after the message")])
    return main


def _row(main) -> dict:
    (row,) = status.snapshot(main, background=False)["subagents"]
    return row


def test_the_later_notice_with_no_tool_use_id_says_it_returned(tmp_path):
    main = _session(tmp_path, _notice(_envelope(AGENT, "completed", FINISHED),
                                      T0 + timedelta(minutes=31)))
    assert _row(main)["state"] == "returned"


def test_with_no_later_notice_it_is_still_running(tmp_path):
    assert _row(_session(tmp_path))["state"] == "running"


@pytest.mark.parametrize("word,state", [("failed", "failed"), ("killed", "interrupted")])
def test_a_later_notice_can_also_say_it_failed(tmp_path, word, state):
    main = _session(tmp_path, _notice(_envelope(AGENT, word, FINISHED), T0 + timedelta(minutes=31)))
    assert _row(main)["state"] == state


def test_a_notice_for_a_subagent_this_session_did_not_launch_changes_nothing(tmp_path):
    main = _session(tmp_path, _notice(_envelope(OTHER, "completed", FINISHED),
                                      T0 + timedelta(minutes=31)))
    snap = status.snapshot(main, background=False)
    assert [row["state"] for row in snap["subagents"]] == ["running"], json.dumps(snap["subagents"])


def test_a_copy_quoted_in_prose_is_no_notice(tmp_path):
    quoted = "the log said " + _envelope(AGENT, "completed", FINISHED)
    main = _session(tmp_path, _notice(quoted, T0 + timedelta(minutes=31)))
    assert _row(main)["state"] == "running"


def test_a_user_entry_with_no_notification_origin_is_no_notice(tmp_path):
    entry = user(None, T0 + timedelta(minutes=31), _envelope(AGENT, "completed", FINISHED))
    assert _row(_session(tmp_path, [entry]))["state"] == "running"


def test_a_notice_listing_several_tasks_is_no_subagents_end(tmp_path):
    text = (f"<task-notification><task-id>{AGENT}</task-id><task-id>{OTHER}</task-id>"
            f"<status>completed</status><summary>{FINISHED}</summary></task-notification>")
    assert _row(_session(tmp_path, _notice(text, T0 + timedelta(minutes=31))))["state"] == "running"


def test_a_resume_after_the_later_notice_reads_running_again(tmp_path):
    main = _session(tmp_path, _notice(_envelope(AGENT, "completed", FINISHED),
                                      T0 + timedelta(minutes=20)))
    assert _row(main)["state"] == "running"  # the transcript grew past it, 10 minutes on


# --- review round 3 of this change ---------------------------------------------------------------


def test_a_delayed_delivery_copy_does_not_end_a_resumed_subagent(tmp_path):
    """The user entry is the queue entry's copy, delivered when the session got to it."""
    text = _envelope(AGENT, "completed", FINISHED)
    queue, _copy = _notice(text, T0 + timedelta(minutes=20))
    _queue, copy = _notice(text, T0 + timedelta(minutes=31))
    assert _row(_session(tmp_path, [queue, copy]))["state"] == "running"


def test_a_second_real_notice_with_the_same_words_still_counts(tmp_path):
    text = _envelope(AGENT, "completed", FINISHED)
    main = _session(tmp_path, [
        *_notice(text, T0 + timedelta(minutes=20)), *_notice(text, T0 + timedelta(minutes=31))])
    assert _row(main)["state"] == "returned"


def test_a_notice_delivered_with_no_queue_entry_still_counts(tmp_path):
    _queue, copy = _notice(_envelope(AGENT, "completed", FINISHED), T0 + timedelta(minutes=31))
    assert _row(_session(tmp_path, [copy]))["state"] == "returned"


@pytest.mark.parametrize("rule,told", [(2, False), (3, True)])
def test_an_old_ending_newly_readable_is_not_told_as_new(tmp_path, rule, told):
    """A store from before this rule takes such an ending into its baseline at the first complete
    read, so a return from hours ago does not come back as a CLAIM; under the current rule a
    new one is told."""
    from live_helpers import assistant_text

    from agent_postmortem import live, state

    main = _session(tmp_path, _notice(_envelope(AGENT, "completed", FINISHED),
                                      T0 + timedelta(minutes=31)))
    sub = main.parent / FAKE_UUID / "subagents" / f"agent-{AGENT}.jsonl"
    write_jsonl(sub, [user(AGENT, T0, "write /__postmortem_absent__/report.md"),
                      assistant_text(AGENT, T0 + timedelta(minutes=30), "done")])
    hours_ago = time.time() - 7200
    os.utime(sub, (hours_ago, hours_ago))
    with state.cache_transaction(state.live_store_path()) as data:
        record = live._store_session(data, live.session_key(main))
        record.update(baseline=[], baseline_rule=rule, reported=[], seen=0.0)
    follower = live.SessionFollower(main, caller_session=None)
    follower._read()
    assert follower.load_endings(new_baseline=True)
    kinds = [event.get("event") for event, _key in follower._agent_events(AGENT) if event]
    assert ("CLAIM" in kinds) is told, kinds
