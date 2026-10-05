"""N3b fix B, C5 and S7: the grouped hook notification (several subagent units
of one session) carries the `agents a:<alias>, ...` clause like the
single-notification line, and any clause names at most three agents then
`+N more` (7.2). Only aliases appear, never a raw agent id."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent_postmortem import disclosure, hooks_cli
from agent_postmortem.cli import main
from tests import n3b_helpers as h

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "hook_session" / "-home-user-project"
SESSION_ID = "00000000-0000-4000-8000-000000000000"
MAIN_TRANSCRIPT = FIXTURE / f"{SESSION_ID}.jsonl"
RUN = "s_deadbeef00"
AGENTS = [f"a7e57ab1e0000000{n}" for n in range(1, 6)]
DIAGNOSTIC = disclosure.UNAVAILABLE_DIAGNOSTIC


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _notify(*agent_ids):
    return hooks_cli._NotifyInfo(
        run_id=RUN, verdict_word="DEGRADED", headline="done", is_incomplete=False,
        finding_count=1, agent_ids=tuple(agent_ids),
    )


def _check(monkeypatch, capsys, notifies, *, active):
    outcomes = [hooks_cli._UnitOutcome(entry={}, notify=n) for n in notifies]
    monkeypatch.setattr(hooks_cli, "_process_units_for_session", lambda *a, **k: outcomes)
    payload = {"hook_event_name": "Stop", "transcript_path": str(MAIN_TRANSCRIPT),
               "session_id": SESSION_ID, "stop_hook_active": active}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    main(["hook", "check"])
    out = capsys.readouterr().out
    return json.loads(out), out


def _form(agent_id):
    return f"a:{h.alias_of('agent', agent_id)}"


@pytest.mark.parametrize("active", [False, True])
def test_a_grouped_notification_names_its_agents_by_stable_alias(active, monkeypatch, capsys):
    output, raw = _check(monkeypatch, capsys, [_notify(AGENTS[0]), _notify(AGENTS[1])], active=active)
    message = output["systemMessage"]
    assert message == (
        f"DEGRADED  2 subagents finished, 2 findings  {RUN}  /postmortem {RUN}"
        f"  agents {_form(AGENTS[0])}, {_form(AGENTS[1])}"
    )
    assert ("hookSpecificOutput" in output) is (not active)
    if not active:
        assert output["hookSpecificOutput"]["additionalContext"] == message
    for real in AGENTS[:2]:
        assert real not in raw


@pytest.mark.parametrize("active", [False, True])
def test_a_grouped_notification_on_a_fallback_book_is_positional_with_the_diagnostic(
    active, monkeypatch, capsys, tmp_path,
):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    output, raw = _check(monkeypatch, capsys, [_notify(AGENTS[0]), _notify(AGENTS[1])], active=active)
    message = output["systemMessage"]
    assert f"/postmortem {RUN}  agents agent-1, agent-2" in message
    assert DIAGNOSTIC in message
    assert "a:" not in message
    if not active:
        assert DIAGNOSTIC in output["hookSpecificOutput"]["additionalContext"]
    for real in AGENTS[:2]:
        assert real not in raw


def test_a_grouped_notification_without_agent_ids_has_no_clause(monkeypatch, capsys):
    output, _raw = _check(monkeypatch, capsys, [_notify(), _notify()], active=False)
    assert "  agents " not in output["systemMessage"]


# --- S7: at most three agents, then +N more -----------------------------------


@pytest.mark.parametrize("active", [False, True])
def test_four_agents_name_three_aliases_and_plus_one_more(active, monkeypatch, capsys):
    output, raw = _check(monkeypatch, capsys, [_notify(*AGENTS[:4])], active=active)
    message = output["systemMessage"]
    clause = message.split("  agents ", 1)[1]
    assert clause == ", ".join(_form(a) for a in AGENTS[:3]) + ", +1 more"
    assert _form(AGENTS[3]) not in message
    for real in AGENTS[:4]:
        assert real not in raw


def test_five_agents_say_plus_two_more_and_exactly_three_is_not_capped(monkeypatch, capsys):
    output, _raw = _check(monkeypatch, capsys, [_notify(*AGENTS[:5])], active=False)
    assert output["systemMessage"].endswith(", +2 more")
    output, _raw = _check(monkeypatch, capsys, [_notify(*AGENTS[:3])], active=False)
    assert "more" not in output["systemMessage"]
    assert output["systemMessage"].count("a:") == 3


@pytest.mark.parametrize("active", [False, True])
def test_the_cap_counts_distinct_agents_across_a_group(active, monkeypatch, capsys):
    notifies = [_notify(AGENTS[0], AGENTS[1]), _notify(AGENTS[1], AGENTS[2], AGENTS[3])]
    output, _raw = _check(monkeypatch, capsys, notifies, active=active)
    message = output["systemMessage"]
    assert message.endswith("+1 more")
    assert message.count("a:") == 3
