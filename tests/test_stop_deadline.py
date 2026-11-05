"""The Stop hook asks for a deadline when a turn ends with background commands running (item 1 of
the field report 2026-11-03 "no silent failures"; docs/DESIGN.md 4.14.5).

Claude Code wakes a session when one of its background commands ends and never when one hangs.
The field worker ended its turn two minutes after launching a `codex exec` that never ended, and
sat idle 26 hours. At Stop, the payload's `background_tasks` lists that command as a running
`shell` task; the hook adds, once per task, a request to arm a deadline before stopping, unless
the session already continues from a Stop hook or has a deadline armed (a cron, a Monitor, a
timer task, a command under `timeout`).

Synthetic data only: the fake session id, paths under tmp_path.
"""

from __future__ import annotations

import io
import json
import os

import pytest
from live_helpers import FAKE_UUID, user, write_jsonl
from test_live_session import SLUG, T0

from agent_postmortem import hooks_cli

CODEX = {"id": "b00000001", "type": "shell", "status": "running",
         "description": "Run the Codex review", "command": "codex exec x > r.md 2> r.log"}


def _stop(tmp_path, monkeypatch, capsys, tasks, *, crons=(), active=False, subagents=False):
    main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    if not main.exists():
        write_jsonl(main, [user(None, T0, "start")])
    if subagents:
        (main.parent / FAKE_UUID / "subagents").mkdir(parents=True, exist_ok=True)
    payload = {"hook_event_name": "Stop", "session_id": FAKE_UUID, "transcript_path": str(main),
               "stop_hook_active": active, "last_assistant_message": "waiting on the review",
               "background_tasks": list(tasks), "session_crons": list(crons)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.dispatch("check") == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return ""
    return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.mark.parametrize("subagents", [False, True], ids=["no subagents", "with subagents"])
def test_a_turn_ending_with_a_running_command_is_asked_for_a_deadline(tmp_path, monkeypatch,
                                                                      capsys, subagents):
    text = _stop(tmp_path, monkeypatch, capsys, [CODEX], subagents=subagents)
    assert "DEADLINE b00000001" in text and "TaskStop" in text and "</dev/null" in text, text
    assert len(text) <= 400


def test_the_deadline_request_is_made_once_per_command(tmp_path, monkeypatch, capsys):
    assert _stop(tmp_path, monkeypatch, capsys, [CODEX])
    assert _stop(tmp_path, monkeypatch, capsys, [CODEX]) == ""
    other = {**CODEX, "id": "b00000002"}
    assert "b00000002" in _stop(tmp_path, monkeypatch, capsys, [CODEX, other])


@pytest.mark.parametrize(
    "tasks,crons,active",
    [
        pytest.param([], (), False, id="nothing running"),
        pytest.param([{**CODEX, "status": "completed"}], (), False, id="a command that ended"),
        pytest.param([CODEX], (), True, id="already continuing from a Stop hook"),
        pytest.param([CODEX], ({"id": "c1", "schedule": "*/10 * * * *", "recurring": True,
                                "prompt": "check"},), False, id="a cron is armed"),
        pytest.param([CODEX, {"id": "m1", "type": "monitor", "status": "running",
                              "description": "watch"}], (), False, id="a Monitor is armed"),
        pytest.param([CODEX, {"id": "b2", "type": "shell", "status": "running",
                              "description": "deadline", "command": "sleep 1800"}], (), False,
                     id="a timer task is armed"),
        pytest.param([{**CODEX, "command": "timeout 900 codex exec x < /dev/null > r.md"}], (),
                     False, id="a command under timeout ends on its own"),
        pytest.param([{"id": "a1", "type": "subagent", "status": "running",
                       "description": "x"}], (), False, id="a subagent, which notifies itself"),
    ],
)
def test_no_deadline_request(tmp_path, monkeypatch, capsys, tasks, crons, active):
    assert _stop(tmp_path, monkeypatch, capsys, tasks, crons=crons, active=active) == ""


def test_the_request_stays_within_400_characters_with_many_tasks(tmp_path, monkeypatch, capsys):
    tasks = [{**CODEX, "id": f"b0000000{n}"} for n in range(1, 6)]
    text = _stop(tmp_path, monkeypatch, capsys, tasks)
    assert "and 2 more" in text and len(text) <= 400, len(text)


# --- review round 1 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("timeout 1 true; tail -f /dev/null", id="timeout bounds only its own command"),
        pytest.param("timeout 0 codex exec x < /dev/null", id="timeout 0 sets no timeout"),
        pytest.param("timeout 5m make && make test", id="an and-list after the bounded command"),
    ],
)
def test_a_command_not_wholly_under_a_positive_timeout_is_asked_about(tmp_path, monkeypatch,
                                                                     capsys, command):
    text = _stop(tmp_path, monkeypatch, capsys, [{**CODEX, "command": command}])
    assert "DEADLINE b00000001" in text


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('sleep 1; echo "$(tail -f /dev/null)"', id="a substitution after the sleep"),
        pytest.param("sleep 1; echo x > /tmp/fifo", id="a redirect after the sleep"),
        pytest.param("sleep 1; make", id="a real command after the sleep"),
    ],
)
def test_a_task_that_is_not_only_a_timer_arms_nothing(tmp_path, monkeypatch, capsys, command):
    timer = {"id": "b00000009", "type": "shell", "status": "running", "description": "t",
             "command": command}
    text = _stop(tmp_path, monkeypatch, capsys, [CODEX, timer])
    assert "DEADLINE b00000001" in text


@pytest.mark.parametrize("command", ["sleep 1800", "sleep 30m && echo deadline",
                                     "sleep 600; printf 'time is up'"])
def test_a_literal_timer_arms_a_deadline(tmp_path, monkeypatch, capsys, command):
    timer = {"id": "b00000009", "type": "shell", "status": "running", "description": "t",
             "command": command}
    assert _stop(tmp_path, monkeypatch, capsys, [CODEX, timer]) == ""


def test_only_tasks_not_yet_asked_about_are_named(tmp_path, monkeypatch, capsys):
    first = [{**CODEX, "id": f"b0000000{n}"} for n in range(1, 4)]
    assert _stop(tmp_path, monkeypatch, capsys, first)
    later = [*first, {**CODEX, "id": "b00000004"}]
    text = _stop(tmp_path, monkeypatch, capsys, later)
    assert "DEADLINE b00000004:" in text and "b00000001" not in text, text


def test_the_request_joined_with_a_unit_finding_stays_within_400_characters(
        tmp_path, monkeypatch, capsys):
    long_finding = "x" * 200
    monkeypatch.setattr(hooks_cli, "_format_notify_lines", lambda notes, redactor: [long_finding])
    from types import SimpleNamespace
    monkeypatch.setattr(hooks_cli, "_process_units_for_session",
                        lambda data, key, session_dir, payload: [
                            SimpleNamespace(notify=SimpleNamespace(agent_ids=()),
                                            live_claims=[])])
    monkeypatch.setattr(hooks_cli, "_resolve_session_id", lambda payload: FAKE_UUID)
    text = _stop(tmp_path, monkeypatch, capsys, [CODEX], subagents=True)
    assert text.startswith(long_finding) and "DEADLINE b00000001" in text, text
    assert len(text) <= 400, len(text)


# --- review round 2 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,asked",
    [
        pytest.param('timeout 0.1 true "$(sleep 2)"', True, id="a substitution runs first"),
        pytest.param("timeout 60 run-it < /tmp/feed", True, id="stdin from a file that may block"),
        pytest.param("timeout 60 run-it < /dev/null > out.md 2> err.log", False,
                     id="stdin closed, output to files"),
        pytest.param("timeout -s CONT 0.1 sleep 2", True, id="a signal that ends nothing"),
        pytest.param("timeout -s CONT -k 5 60 run-it", False, id="a kill-after that ends it"),
        pytest.param("timeout --signal=KILL 60 run-it", False, id="KILL ends it"),
        pytest.param("timeout -s TERM 60 run-it", False, id="TERM ends it"),
    ],
)
def test_round_two_bounded_shapes(tmp_path, monkeypatch, capsys, command, asked):
    text = _stop(tmp_path, monkeypatch, capsys, [{**CODEX, "command": command}])
    assert ("DEADLINE b00000001" in text) is asked, text


def test_a_locked_unit_store_still_lets_the_deadline_through(tmp_path, monkeypatch, capsys):
    from agent_postmortem import state

    def locked(*args, **kwargs):
        raise state.StateLocked("busy")

    monkeypatch.setattr(state, "transaction", locked)
    monkeypatch.setattr(hooks_cli, "_resolve_session_id", lambda payload: FAKE_UUID)
    text = _stop(tmp_path, monkeypatch, capsys, [CODEX], subagents=True)
    assert "DEADLINE b00000001" in text, text


def test_the_request_names_only_the_tasks_this_call_won(tmp_path, monkeypatch, capsys):
    from agent_postmortem import live

    tasks = [{**CODEX, "id": f"b0000000{n}"} for n in range(1, 5)]
    main = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main, [user(None, T0, "start")])
    real = live.delivered_keys

    def stale(path, store_path=None):
        # the request is prepared from an empty record; another call wins three keys first
        keys = real(path, store_path)
        live.claim_deliveries(path, [f"cmd:b0000000{n}:stop" for n in range(1, 4)])
        return keys

    monkeypatch.setattr(live, "delivered_keys", stale)
    text = _stop(tmp_path, monkeypatch, capsys, tasks)
    assert "DEADLINE b00000004:" in text and "b00000001" not in text, text


# --- review round 3 (each with the reviewer's reproduction) -------------------------------------


@pytest.mark.parametrize(
    "command,bounded",
    [
        pytest.param("timeout -sCONT 0.1 sleep 2", False, id="an attached signal"),
        pytest.param("timeout --sig=CONT 0.1 sleep 2", False, id="an abbreviated long option"),
        pytest.param("timeout --frobnicate 60 run-it", False, id="an unknown option"),
        pytest.param("timeout -sKILL 60 run-it", True, id="an attached KILL"),
        pytest.param("timeout --foreground 60 run-it", True, id="a known flag"),
        pytest.param("timeout 60 run-it <<< 'prompt'", True, id="a literal here-string"),
        pytest.param('timeout 60 run-it <<< "$PROMPT"', False, id="an expanded here-string"),
    ],
)
def test_round_three_bounded_shapes(command, bounded):
    assert hooks_cli._is_bounded(command) is bounded


def test_an_output_redirect_to_a_fifo_is_not_bounded(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert hooks_cli._is_bounded(f"timeout 1 true > {fifo}") is False
    assert hooks_cli._is_bounded(f"timeout 1 true > {tmp_path / 'out.log'}") is True
    assert hooks_cli._is_bounded("timeout 1 true > /dev/tty") is False


def test_a_state_file_error_still_lets_the_deadline_through(tmp_path, monkeypatch, capsys):
    from agent_postmortem import state

    def unreadable(*args, **kwargs):
        raise PermissionError("unreadable state.json")

    monkeypatch.setattr(state, "transaction", unreadable)
    monkeypatch.setattr(hooks_cli, "_resolve_session_id", lambda payload: FAKE_UUID)
    assert "DEADLINE b00000001" in _stop(tmp_path, monkeypatch, capsys, [CODEX], subagents=True)
