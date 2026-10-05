"""N3b Part 3: `watch` text and --json on stable agent aliases (P8, P14, C-1,
C-5, C-6; docs/DESIGN.md 4.12).

The agent named on a line, and the `agent` field of every JSON event, is the
event attempt's own `a:<alias>`, the one `run` shows for it. `attempt` and
`restarted_as` keep their ordinals. Agents known at start register in one
call; one first named by a later event registers then. A failed registration
makes that event and every later one positional (`agent-N`, numbered within
this watch) while the earlier lines stand, and prints one stderr line. An
attempt with no canonical agentId is named positionally, never by its label.
watch takes no level flag.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import disclosure, state
from agent_postmortem.cli import main
from tests import n3b_helpers as h
from tests import test_watch as tw

LABEL = "zzLabel" + "Plant" + "7c1"
A1 = "a" + "7e57ab1e" + "0000" + "00a1"
B1 = "a" + "7e57ab1e" + "0000" + "00b1"
C1 = "a" + "7e57ab1e" + "0000" + "00c1"
DIAGNOSTIC_LINE = "agent-postmortem watch: stable aliases unavailable"


def _form(agent_id: str) -> str:
    return f"a:{h.alias_of('agent', agent_id)}"


def _run(monkeypatch, capsys, run_dir, actions, *flags):
    clock = tw._Clock()
    tw._patch_clock(monkeypatch, clock)
    tw._patch_scripted_sleep(monkeypatch, actions, clock)
    capsys.readouterr()
    code = main(["watch", str(run_dir), "--interval", "1", *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _failed_journal(run_dir, *pairs, label=LABEL):
    """A launched journal plus one started/failed pair per (key, agent id)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [tw._journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z")]
    for key, agent in pairs:
        lines.append(tw._started("2026-02-01T00:00:00.000Z", key, agent, label))
        lines.append(tw._failed("2026-02-01T00:05:00.000Z", key, agent))
    tw._write_lines(run_dir / "journal.jsonl", lines)


def _append_failed(journal, key, agent, label=LABEL):
    return lambda: tw._append_lines(journal, [
        tw._started("2026-02-01T00:10:00.000Z", key, agent, label),
        tw._failed("2026-02-01T00:15:00.000Z", key, agent),
    ])


def _register_recorder(monkeypatch):
    books: list[disclosure.AliasBook] = []
    calls: list[list] = []
    real = disclosure.register

    def recording(identities, **kwargs):
        calls.append(list(identities))
        book = real(identities, **kwargs)
        books.append(book)
        return book

    monkeypatch.setattr(disclosure, "register", recording)
    return books, calls


# --- stable aliases ----------------------------------------------------------


def test_kill_text_names_the_attempt_by_its_stable_alias(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa1"
    key, agent1, agent2 = tw._build_kill_scenario(run_dir)
    journal = run_dir / "journal.jsonl"
    actions = [
        lambda: tw._append_lines(journal, [tw._started("2026-02-01T00:10:00.000Z", key, agent2, "impl:a")]),
        lambda: tw._append_lines(journal, [tw._result("2026-02-01T00:10:06.000Z", key, agent2)]),
    ]
    code, out, err = _run(monkeypatch, capsys, run_dir, actions)
    lines = out.splitlines()
    assert lines[0].startswith("START  ")  # N8b K5.1: START is the first line
    assert lines[1] == (
        f"KILL   {_form(agent1)} attempt 1 after 416s of silence; restarted as attempt 2."
    )
    assert err == "" and code == 0
    for real in (agent1, agent2, "impl:a", "Implement the watch-a module"):
        assert real not in out


def test_kill_json_agent_field_is_the_stable_alias_and_ordinals_stay(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa5"
    key, agent1, agent2 = tw._build_kill_scenario(run_dir)
    journal = run_dir / "journal.jsonl"
    actions = [
        lambda: tw._append_lines(journal, [tw._started("2026-02-01T00:10:00.000Z", key, agent2, "impl:a")]),
    ]
    _code, out, err = _run(monkeypatch, capsys, run_dir, actions, "--json")
    records = [json.loads(line) for line in out.splitlines()]
    assert records[0]["event"] == "START"  # N8b K5.1: START is the first event
    kill = records[1]
    assert kill["event"] == "KILL"
    assert kill["agent"] == _form(agent1)
    assert kill["attempt"] == 1 and kill["restarted_as"] == 2
    assert "schema" not in kill
    assert err == ""
    assert agent1 not in out and "impl:a" not in out


def test_failed_and_quiet_lines_carry_the_alias_and_never_the_label(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-fa7"
    run_dir.mkdir()
    key = "v2:quiet"
    tw._write_lines(run_dir / "journal.jsonl", [
        tw._journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
        tw._started("2026-02-01T00:00:00.000Z", key, A1, LABEL),
    ])
    tw._write_transcript(
        tw._transcript_path(run_dir, A1), [tw._user_text("2026-02-01T00:00:00.000Z", "Work.")],
    )
    clock = tw._Clock()
    tw._patch_clock(monkeypatch, clock)
    # the transcript's mtime is "now"; advance the fake clock past 300 s
    tw._patch_scripted_sleep(monkeypatch, [lambda: clock.advance(400)], clock)
    capsys.readouterr()
    main(["watch", str(run_dir), "--interval", "1", "--max-hours", "0.2"])
    out = capsys.readouterr().out
    assert f"QUIET  {_form(A1)} attempt 1 has produced nothing for" in out
    assert LABEL not in out and A1 not in out


# --- registration ------------------------------------------------------------


def test_agents_known_at_start_register_in_one_call_and_a_late_agent_once(
    monkeypatch, capsys, tmp_path,
):
    run_dir = tmp_path / "wf_00000000-fa8"
    _failed_journal(run_dir, ("v2:a", A1), ("v2:a2", A1[:-1] + "2"))
    journal = run_dir / "journal.jsonl"
    books, calls = _register_recorder(monkeypatch)
    actions = [_append_failed(journal, "v2:b", B1)]
    code, out, err = _run(monkeypatch, capsys, run_dir, actions)
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted([("agent", A1), ("agent", A1[:-1] + "2")])
    assert books[0].slow_path_count == 1  # the late agent B1, once
    failed = [line for line in out.splitlines() if line.startswith("FAILED")]
    assert len(failed) == 3
    assert f"FAILED {_form(B1)} attempt 1: cause not recorded." in failed
    assert err == "" and code == 0
    for real in (A1, B1, LABEL):
        assert real not in out


def test_a_late_agent_registers_stably_when_nothing_was_known_at_start(
    monkeypatch, capsys, tmp_path,
):
    run_dir = tmp_path / "wf_00000000-fa9"
    run_dir.mkdir()
    tw._write_lines(run_dir / "journal.jsonl", [
        tw._journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
    ])
    _books, calls = _register_recorder(monkeypatch)
    _code, out, err = _run(
        monkeypatch, capsys, run_dir, [_append_failed(run_dir / "journal.jsonl", "v2:b", B1)],
    )
    registrations = list(calls)  # before _form() registers on its own
    assert f"FAILED {_form(B1)} attempt 1: cause not recorded." in out
    assert registrations == [] and err == ""


# --- fallback ----------------------------------------------------------------


@pytest.mark.parametrize("json_mode", [False, True])
def test_a_late_agent_whose_registration_fails_is_positional_with_one_diagnostic(
    json_mode, monkeypatch, capsys, tmp_path,
):
    run_dir = tmp_path / "wf_00000000-fb1"
    _failed_journal(run_dir, ("v2:a", A1))
    journal = run_dir / "journal.jsonl"
    stable_a1 = _form(A1)  # registered now, before the lock is made to fail
    real_lock = state.lock_state_dir
    calls = {"n": 0}

    def flaky_lock(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated lock failure after the start registration")
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(state, "lock_state_dir", flaky_lock)
    actions = [_append_failed(journal, "v2:b", B1), _append_failed(journal, "v2:c", C1)]
    flags = ["--json"] if json_mode else []
    code, out, err = _run(monkeypatch, capsys, run_dir, actions, *flags)

    assert err == DIAGNOSTIC_LINE + "\n"  # exactly one stderr line
    if json_mode:
        records = [json.loads(line) for line in out.splitlines()]  # valid JSONL
        agents = [r["agent"] for r in records if r["event"] == "FAILED"]
        assert [r["event"] for r in records].count("DONE") == 1
        assert all("diagnostic" not in r and r["event"] != "DIAGNOSTIC" for r in records)
    else:
        agents = [line.split()[1] for line in out.splitlines() if line.startswith("FAILED")]
    # earlier line stands stable; the failing event and every later one are positional,
    # numbered within this watch (the earlier agent takes agent-1)
    assert agents == [stable_a1, "agent-2", "agent-3"]
    for real in (A1, B1, C1, LABEL):
        assert real not in out
    assert code == 0


@pytest.mark.parametrize("json_mode", [False, True])
def test_unavailable_registry_at_start_is_positional_with_one_stderr_line(
    json_mode, monkeypatch, capsys, tmp_path,
):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    run_dir = tmp_path / "wf_00000000-fb2"
    _failed_journal(run_dir, ("v2:a", A1), ("v2:b", B1))
    flags = ["--json"] if json_mode else []
    _code, out, err = _run(monkeypatch, capsys, run_dir, [], *flags)
    assert err == DIAGNOSTIC_LINE + "\n"
    if json_mode:
        records = [json.loads(line) for line in out.splitlines()]
        agents = sorted(r["agent"] for r in records if r["event"] == "FAILED")
    else:
        agents = sorted(line.split()[1] for line in out.splitlines() if line.startswith("FAILED"))
    assert agents == ["agent-1", "agent-2"]
    assert A1 not in out and B1 not in out and LABEL not in out


# --- attempts without a canonical agent id (C-5) -----------------------------


@pytest.mark.parametrize("agent_value", ["missing", None, "not-a-canonical-id"])
def test_an_attempt_without_a_canonical_agent_id_is_positional_never_the_label(
    agent_value, monkeypatch, capsys, tmp_path,
):
    run_dir = tmp_path / "wf_00000000-fb3"
    run_dir.mkdir()
    lines = [tw._journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z")]
    for key in ("v2:x", "v2:y"):  # two attempts, one shared label
        started = {"type": "started", "timestamp": "2026-02-01T00:00:00.000Z", "key": key,
                   "label": LABEL, "phase": "Implement"}
        failed = {"type": "failed", "timestamp": "2026-02-01T00:05:00.000Z", "key": key}
        if agent_value != "missing":
            started["agentId"] = agent_value
            failed["agentId"] = agent_value
        lines += [json.dumps(started), json.dumps(failed)]
    tw._write_lines(run_dir / "journal.jsonl", lines)
    _books, calls = _register_recorder(monkeypatch)
    _code, out, err = _run(monkeypatch, capsys, run_dir, [])
    failed_lines = [line for line in out.splitlines() if line.startswith("FAILED")]
    # one positional name per attempt, numbered within this watch
    assert sorted(failed_lines) == [
        "FAILED agent-1 attempt 1: cause not recorded.",
        "FAILED agent-2 attempt 1: cause not recorded.",
    ]
    assert LABEL not in out
    assert "agent-1.jsonl" not in out and ".jsonl" not in out  # no transcript pointer
    assert calls == [] and err == ""
