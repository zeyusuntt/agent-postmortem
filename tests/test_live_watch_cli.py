"""docs/DESIGN.md 4.14.3: `agent-postmortem watch --session [SESSION_ID|PATH]`, the command a
Monitor runs. The poll loop's sleep and clock are replaced so no test waits."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from live_helpers import FAKE_UUID, cost_state, user, write_jsonl

from agent_postmortem import live
from agent_postmortem.cli import main

SLUG = "-home-user-project"
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def fast(monkeypatch):
    clock = {"t": 1_000_000.0}

    def sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(live, "_sleep", sleep)
    monkeypatch.setattr(live, "_clock", lambda: clock["t"])
    return clock


def _ended_session(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start"), cost_state(), cost_state()])
    return main_path


def test_watch_session_by_path_runs_until_ended_and_exits_0(tmp_path, fast, capsys):
    main_path = _ended_session(tmp_path)
    code = main(["watch", "--session", str(main_path), "--interval", "0.2"])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0].startswith("START  session s_")
    assert out[-1].startswith("ENDED  session s_")


def test_watch_session_by_uuid_under_root(tmp_path, fast, capsys):
    _ended_session(tmp_path)
    code = main(["watch", "--session", FAKE_UUID, "--root", str(tmp_path / "projects")])
    assert code == 0
    assert capsys.readouterr().out.startswith("START")


def test_watch_session_with_no_value_follows_the_callers_session(tmp_path, fast, capsys, monkeypatch):
    _ended_session(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", FAKE_UUID)
    code = main(["watch", "--session", "--root", str(tmp_path / "projects")])
    assert code == 0
    assert capsys.readouterr().out.startswith("START")


def test_watch_session_with_no_value_and_no_caller_is_a_usage_error(tmp_path, fast, capsys):
    code = main(["watch", "--session", "--root", str(tmp_path)])
    assert code == 1
    assert "CLAUDE_CODE_SESSION_ID" in capsys.readouterr().err


def test_watch_session_not_found_exits_2(tmp_path, fast, capsys):
    (tmp_path / "projects").mkdir()
    code = main(["watch", "--session", FAKE_UUID, "--root", str(tmp_path / "projects")])
    assert code == 2


def test_watch_needs_a_run_or_a_session(tmp_path, capsys):
    assert main(["watch"]) == 1
    assert "--session" in capsys.readouterr().err


def test_watch_session_limit_exits_4(tmp_path, fast, capsys):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    code = main(["watch", "--session", str(main_path), "--max-hours", "0.01", "--interval", "5"])
    out = capsys.readouterr().out.splitlines()
    assert code == 4
    assert out[-1] == "LIMIT  0.01h reached."


def test_watch_session_json_prints_one_object_per_line(tmp_path, fast, capsys):
    main_path = _ended_session(tmp_path)
    code = main(["watch", "--session", str(main_path), "--json"])
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert [json.loads(line)["event"] for line in lines] == ["START", "ENDED"]


def test_watch_session_releases_its_claim_on_exit(tmp_path, fast, capsys):
    main_path = _ended_session(tmp_path)
    assert main(["watch", "--session", str(main_path)]) == 0
    capsys.readouterr()
    assert main(["watch", "--session", str(main_path)]) == 0
    assert "already watched" not in capsys.readouterr().out


def test_watch_run_and_session_together_is_a_usage_error(tmp_path, capsys):
    code = main(["watch", "wf_00000000-750", "--session", FAKE_UUID])
    assert code == 1


def test_watch_help_names_the_session_form(capsys):
    with pytest.raises(SystemExit):
        main(["watch", "--help"])
    out = capsys.readouterr().out
    assert "--session" in out

