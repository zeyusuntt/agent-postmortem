"""Tests for rescue.py: the read-only decision packet (`rescue plan`) and
observed check status (`rescue status`), docs/DESIGN.md 4.11, unit M4.

Every git repository is a throwaway one built under pytest's tmp_path with
the real system git via subprocess, the same approach tests/test_observe.py
uses -- never a checked-in fixture, never the project's own repository. A
scenario that does not need git behavior at all (a REPORTED variant, a
possibly-live packet, a nothing-to-rescue run) builds Run/Attempt/
AttemptFacts objects directly in memory instead, the same dual strategy
tests/test_observe.py already uses for its own pure-Python rules.

Every AGENT_POSTMORTEM_CONFIG points at a tmp file (or a deliberately
missing one), so config.load_config()'s process-level cache never collides
across tests and a real machine's own config file is never touched.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem import cli, config, observe, reader, rescue
from agent_postmortem.models import (
    Attempt,
    AttemptFacts,
    Interrupt,
    LogicalAgent,
    Run,
)
from agent_postmortem.redact import Redactor

RESCUE_PATH = Path(rescue.__file__)


# --- throwaway git repo builder (mirrors tests/test_observe.py) -------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    return repo


# --- transcript line builders ------------------------------------------------


def _iso(base: datetime, offset: float) -> str:
    return (base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user(content, ts: str, agent: str | None, cwd: Path | None = None) -> dict:
    entry = {"type": "user", "message": {"role": "user", "content": content}, "timestamp": ts}
    if agent is not None:
        entry["agentId"] = agent
    if cwd is not None:
        entry["cwd"] = str(cwd)
    return entry


def _assistant_tool(tool_id: str, name: str, input_obj: dict, ts: str, agent: str | None,
                     cwd: Path | None = None) -> dict:
    entry = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}
        ], "usage": {}},
        "timestamp": ts,
    }
    if agent is not None:
        entry["agentId"] = agent
    if cwd is not None:
        entry["cwd"] = str(cwd)
    return entry


def _tool_result(tool_id: str, content: str, ts: str, agent: str | None,
                  cwd: Path | None = None, is_error: bool = False) -> dict:
    entry = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}
        ]},
        "timestamp": ts,
    }
    if agent is not None:
        entry["agentId"] = agent
    if cwd is not None:
        entry["cwd"] = str(cwd)
    return entry


_KILL_SENTINEL = "[Request interrupted by user for tool use]"


def _kill(ts: str, agent: str | None, cwd: Path | None = None) -> dict:
    return _user(_KILL_SENTINEL, ts, agent, cwd)


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


# --- the shared "killed then completed" workflow fixture --------------------
#
# One unit (key v2:impl), two attempts: attempt 1 (a4c0000000000001) is
# killed mid-edit, having already written two dirty paths and one clean one,
# and reported one commit; attempt 2 (a4c0000000000002) completes, rewriting
# the first dirty path (the "later write" case) and reporting a dict result.
# Lives under a real throwaway git repo so observe.observe_run has something
# to say. wf_00000000-4c0 -- registered in tests/synthetic-ids.txt.


def _build_killed_then_completed(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tests").mkdir()
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("original a\n", encoding="utf-8")
    (repo / "src" / "pkg" / "b.py").write_text("original b\n", encoding="utf-8")
    (repo / "src" / "pkg" / "c.py").write_text("original c\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "initial"], repo)
    (repo / "tests" / "test_a.py").write_text("changed a\n", encoding="utf-8")
    (repo / "src" / "pkg" / "b.py").write_text("changed b\n", encoding="utf-8")

    slug = "-home-user-project"
    session = "00000000-0000-4000-8000-000000000000"
    run_dir = (
        tmp_path / slug / session / "subagents" / "workflows" / "wf_00000000-4c0"
    )
    run_dir.mkdir(parents=True)

    a1, a2 = "a4c0000000000001", "a4c0000000000002"
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    journal_lines = [
        {"type": "started", "key": "v2:impl", "agentId": a1, "label": "impl:paths",
         "phase": "Implement"},
        {"type": "started", "key": "v2:impl", "agentId": a2, "label": "impl:paths",
         "phase": "Implement"},
        {"type": "result", "key": "v2:impl", "agentId": a2,
         "result": {"ok": True, "tests_failed": 0, "commits": ["abcdef1"]}},
    ]
    _write_jsonl(run_dir / "journal.jsonl", journal_lines)

    path_a = str(repo / "tests" / "test_a.py")
    path_b = str(repo / "src" / "pkg" / "b.py")
    path_c = str(repo / "src" / "pkg" / "c.py")

    attempt1 = [
        _user("Fix the failing tests in tests/test_a.py and ship it.", _iso(base, 0), a1, repo),
        _assistant_tool("t1", "Edit", {"file_path": path_a}, _iso(base, 1), a1, repo),
        _tool_result("t1", "ok", _iso(base, 2), a1, repo),
        _assistant_tool("t2", "Edit", {"file_path": path_b}, _iso(base, 3), a1, repo),
        _tool_result("t2", "ok", _iso(base, 4), a1, repo),
        _assistant_tool("t3", "Edit", {"file_path": path_c}, _iso(base, 5), a1, repo),
        _tool_result("t3", "ok", _iso(base, 6), a1, repo),
        _assistant_tool("t4", "Bash", {"command": "git commit -am 'wip'"}, _iso(base, 7), a1, repo),
        _tool_result("t4", "[main 1234567] wip", _iso(base, 8), a1, repo),
        _assistant_tool("t5", "Edit", {"file_path": path_a}, _iso(base, 9), a1, repo),
        _kill(_iso(base, 10), a1, repo),
    ]
    _write_jsonl(run_dir / f"agent-{a1}.jsonl", attempt1)

    attempt2 = [
        _user("Fix the failing tests in tests/test_a.py and ship it.", _iso(base, 11), a2, repo),
        _assistant_tool("u1", "Edit", {"file_path": path_a}, _iso(base, 12), a2, repo),
        _tool_result("u1", "ok", _iso(base, 13), a2, repo),
    ]
    _write_jsonl(run_dir / f"agent-{a2}.jsonl", attempt2)

    return run_dir, repo, base


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _no_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "does-not-exist.toml"))


# --- rescue plan: the full packet, PATHS/redaction/REPORTED/COMMITS/-------
# --- IN FLIGHT/CHECKS/STEPS/exit code ---------------------------------------


def test_packet_shows_dirty_paths_states_and_later_write(tmp_path, monkeypatch):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    run = reader.load_run(run_dir)
    observation = observe.observe_run(run)
    plan = rescue.build_plan(run, observation, [], config.load_config())
    text = rescue.render_plan_text(run, plan, Redactor(enabled=False))

    assert "DIRTY_NOW" in text
    assert "CLEAN_NOW" in text
    path_rows = [
        line for line in text.splitlines()
        if ("DIRTY_NOW" in line or "CLEAN_NOW" in line) and "tests/test_a.py" in line
    ]
    a_line = [line for line in path_rows if "tests/test_a.py" in line][0]
    assert "DIRTY_NOW" in a_line and a_line.rstrip().endswith("yes")
    b_line = [
        line for line in text.splitlines()
        if ("DIRTY_NOW" in line or "CLEAN_NOW" in line) and "src/pkg/b.py" in line
    ][0]
    assert "DIRTY_NOW" in b_line and b_line.rstrip().endswith("no")
    c_line = [
        line for line in text.splitlines()
        if ("DIRTY_NOW" in line or "CLEAN_NOW" in line) and "src/pkg/c.py" in line
    ][0]
    assert "CLEAN_NOW" in c_line and c_line.rstrip().endswith("no")


def test_default_output_uses_path_pseudonyms_and_no_prompt_text(tmp_path, monkeypatch):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    code = cli.main(["rescue", "plan", str(run_dir)])
    assert code in (0, 4)


def test_default_output_no_redact_flag_absent(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir)])
    out = capsys.readouterr().out
    assert str(repo) not in out
    assert "tests/test_a.py" not in out
    assert "Fix the failing tests" not in out
    assert "path-1" in out
    assert "prompt length:" in out


def test_no_redact_shows_real_paths_and_prompt_text(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "tests/test_a.py" in out
    assert "src/pkg/b.py" in out
    assert "src/pkg/c.py" in out
    assert '"Fix the failing tests in tests/test_a.py and ship it."' in out


def test_reported_dict_shows_allowlisted_claims(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "REPORTED  ok: true, tests_failed: 0" in out
    assert "abcdef1" in out


def test_commits_line_lists_reported_commit_ids(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "COMMITS   reported by these attempts: 1234567" in out


def test_in_flight_line_names_the_tool_and_path(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "IN FLIGHT at the kill: Edit tests/test_a.py (no result recorded)" in out


def test_checks_declared_from_matching_repo_config(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _write_config(
        tmp_path,
        f'[[repos]]\npath = "{repo}"\nchecks = ["uv run pytest -q tests/test_a.py"]\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "config.toml"))

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "CHECKS    declared for this repo: uv run pytest -q tests/test_a.py" in out
    assert "3. [decide] run the declared check from the repo root:" in out


def test_default_output_redacts_a_path_embedded_in_declared_check_text(
    tmp_path, monkeypatch, capsys,
):
    """docs/DESIGN.md 5.1/4.11 (F7 hardening): a declared check's own text
    gets its own check-N pseudonym by default -- the whole command hidden,
    not merely the path embedded inside it. Changed from the old pinned
    partial-substitution expectation ("uv run pytest -q path-1"), which
    still showed the command's own flags and subcommand by default."""
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _write_config(
        tmp_path,
        f'[[repos]]\npath = "{repo}"\nchecks = ["uv run pytest -q tests/test_a.py"]\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "config.toml"))

    cli.main(["rescue", "plan", str(run_dir)])
    out = capsys.readouterr().out
    assert "tests/test_a.py" not in out
    assert "uv run pytest" not in out
    assert "CHECKS    declared for this repo: check-1" in out
    assert "3. [decide] run the declared check from the repo root: check-1" in out


def test_no_redact_shows_the_raw_check_text(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _write_config(
        tmp_path,
        f'[[repos]]\npath = "{repo}"\nchecks = ["uv run pytest -q tests/test_a.py"]\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "config.toml"))

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "CHECKS    declared for this repo: uv run pytest -q tests/test_a.py" in out


def test_checks_none_declared_without_a_matching_repo_config(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _write_config(
        tmp_path, '[[repos]]\npath = "/somewhere/else"\nchecks = ["make check"]\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "config.toml"))

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "no checks declared for" in out
    assert "add [[repos]] to the config" in out
    assert "3. [decide] declare a check first" in out


def test_compound_check_is_rejected_and_never_listed(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _write_config(
        tmp_path,
        f'[[repos]]\npath = "{repo}"\n'
        'checks = ["uv run pytest -q tests/test_a.py && echo done"]\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "config.toml"))

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out, err = capsys.readouterr()
    assert "&& echo done" not in out
    assert "no checks declared for" in out
    assert "rejected" in err.lower()


def test_steps_and_never_line(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "1. [look]   git status --short --" in out
    assert "2. [look]   git diff --" in out
    assert "commit, after reading the diff" in out
    assert "relaunch the remaining work" in out
    assert "NEVER  git checkout / restore / reset / stash / clean" in out


def test_json_allowlist_and_shape(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {
        "schema", "run", "nothing_to_rescue", "units", "brief", "exit_code",
    }
    assert payload["schema"] == 2
    assert set(payload["run"]) == {"run_id", "kind", "finished", "possibly_live"}
    assert len(payload["units"]) == 1
    unit = payload["units"][0]
    assert set(unit) == {
        "agent", "phase", "attempts", "end", "task", "reported", "paths",
        "commits", "in_flight", "unknown", "checks", "steps",
    }
    for path_entry in unit["paths"]:
        assert set(path_entry) == {"path", "state", "reason", "attempts", "later_write_observed"}
        assert path_entry["path"].startswith("path-")
    assert unit["reported"]["type"] == "dict"
    assert "commits" in unit["reported"]["claims"]


def test_json_no_redact_shows_real_paths(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)

    cli.main(["rescue", "plan", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    unit = payload["units"][0]
    paths = {entry["path"] for entry in unit["paths"]}
    assert "tests/test_a.py" in paths
    assert unit["task"]["text"] == "Fix the failing tests in tests/test_a.py and ship it."


def test_happy_path_exit_code_is_zero(tmp_path, monkeypatch):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _write_config(
        tmp_path,
        f'[[repos]]\npath = "{repo}"\nchecks = ["uv run pytest -q tests/test_a.py"]\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "config.toml"))
    code = cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    assert code == 0


def test_observation_not_checked_gives_exit_4_and_names_the_reason(tmp_path, monkeypatch, capsys):
    run_dir, repo, base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)
    # A run that ended more than 72 hours ago: observe.observe_run refuses
    # to start a git process at all (docs/DESIGN.md 4.9 rule 1). Rewrite the
    # kill line's own timestamp far enough in the past.
    a1 = "a4c0000000000001"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = (run_dir / f"agent-{a1}.jsonl").read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    for entry in entries:
        entry["timestamp"] = old_ts
    _write_jsonl(run_dir / f"agent-{a1}.jsonl", entries)
    a2 = "a4c0000000000002"
    lines2 = [json.loads(line) for line in (run_dir / f"agent-{a2}.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()]
    for entry in lines2:
        entry["timestamp"] = old_ts
    _write_jsonl(run_dir / f"agent-{a2}.jsonl", lines2)

    code = cli.main(["rescue", "plan", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert code == 4
    assert "UNKNOWN" in out
    assert "historical: run older than 72 hours" in out


# --- REPORTED variants and possibly-live / nothing-to-rescue, built in ------
# --- memory (no git behavior involved) --------------------------------------


def _facts(paths_written=(), in_flight=(), commits=(), cwds=()) -> AttemptFacts:
    return AttemptFacts(
        paths_written=list(paths_written), in_flight=list(in_flight),
        commits=list(commits), cwds=list(cwds), background_unfinished=0,
        last_tool=None, seconds_since_last_tool_at_end=None,
    )


def _attempt(agent_id, key, ordinal, outcome, *, facts=None, reported=None,
             interrupt=None, label="agent", phase="phase") -> Attempt:
    return Attempt(
        agent_id=agent_id, key=key, label=label, phase=phase, agent_type=None,
        meta={}, transcript_path=None, events=[], first_ts=None, last_ts=None,
        ordinal=ordinal, outcome=outcome, usage={}, interrupt=interrupt,
        reported=reported, facts=facts if facts is not None else _facts(),
    )


def _run(agents: list[LogicalAgent], *, finished=True) -> Run:
    attempts = [attempt for agent in agents for attempt in agent.attempts]
    return Run(
        run_id="wf_00000000-4c1", path=Path("/tmp/does-not-matter"),
        project_slug="-home-user-project", session_id=None, claude_version=None,
        journal=[], agents=agents, attempts=attempts, started_at=None, ended_at=None,
        diagnostics=[], finished=finished,
    )


def test_reported_text_result(tmp_path):
    killed = _attempt(
        "a0000000000000001", "v2:x", 1, "killed",
        interrupt=Interrupt(kill_ts=None, preceding=None, silence_seconds=12.0, cause="runtime",
                             cause_evidence=None),
    )
    completed = _attempt(
        "a0000000000000002", "v2:x", 2, "completed",
        reported={"type": "text", "text_length": 4321},
    )
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[killed, completed])
    run = _run([agent])
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config())
    text = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert "REPORTED  text result, 4321 chars, not kept" in text


def test_reported_status_claim_hidden_by_default_shown_with_no_redact(tmp_path):
    """docs/DESIGN.md 4.11 (F7 hardening): reported.status is arbitrary,
    up-to-32-char free text (docs/DESIGN.md section 3) and must not appear
    raw by default -- "status: set" only, the real value under --no-redact,
    text and JSON alike."""
    killed = _attempt(
        "a0000000000000001", "v2:x", 1, "killed",
        interrupt=Interrupt(kill_ts=None, preceding=None, silence_seconds=5.0, cause="runtime",
                             cause_evidence=None),
    )
    completed = _attempt(
        "a0000000000000002", "v2:x", 2, "completed",
        reported={"type": "dict", "claims": {"status": "blocked on migration"}},
    )
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[killed, completed])
    run = _run([agent])
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config())

    redacted_text = rescue.render_plan_text(run, plan, Redactor(enabled=True))
    raw_text = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert "blocked on migration" not in redacted_text
    assert "REPORTED  status: set" in redacted_text
    assert "REPORTED  status: blocked on migration" in raw_text

    redacted_json = rescue.plan_to_json(run, plan, Redactor(enabled=True))
    raw_json = rescue.plan_to_json(run, plan, Redactor(enabled=False))
    assert redacted_json["units"][0]["reported"]["claims"]["status"] == "set"
    assert raw_json["units"][0]["reported"]["claims"]["status"] == "blocked on migration"


def test_reported_none_when_last_attempt_was_killed(tmp_path):
    killed = _attempt(
        "a0000000000000001", "v2:x", 1, "killed",
        interrupt=Interrupt(kill_ts=None, preceding=None, silence_seconds=5.0, cause="runtime",
                             cause_evidence=None),
    )
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[killed])
    run = _run([agent])
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config())
    text = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert "REPORTED  no result event" in text


def test_nothing_to_rescue_when_every_attempt_completed(tmp_path):
    completed = _attempt("a0000000000000001", "v2:x", 1, "completed")
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[completed])
    run = _run([agent])
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config())
    text = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert text.strip() == "NOTHING TO RESCUE: every attempt completed."
    assert plan.exit_code == 0


# --- F6: a killed/failed/superseded attempt with no transcript at all -------
# --- (facts is None) counts as UNKNOWN; NOTHING TO RESCUE requires a -------
# --- finished run where every attempt is completed --------------------------


def test_killed_attempt_with_facts_none_counts_as_unknown(tmp_path):
    """docs/DESIGN.md 4.11 (F6 hardening): _unit_has_unknown_facts used to
    `continue` past a None facts value instead of treating it as unknown, so
    a killed attempt with no transcript at all silently dropped out of the
    UNKNOWN accounting. facts=None (not _facts()) is built directly here --
    the _attempt() helper always defaults to an empty AttemptFacts, never
    None, which is exactly the case this test needs."""
    killed = Attempt(
        agent_id="a0000000000000001", key="v2:x", label="agent", phase="phase",
        agent_type=None, meta={}, transcript_path=None, events=[], first_ts=None,
        last_ts=None, ordinal=1, outcome="killed", usage={},
        interrupt=Interrupt(kill_ts=None, preceding=None, silence_seconds=5.0,
                             cause="runtime", cause_evidence=None),
        reported=None, facts=None,
    )
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[killed])
    run = _run([agent])
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config())
    assert plan.exit_code == 4
    assert plan.has_unknown_fact is True
    text = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert "UNKNOWN facts: transcript missing" in text


def test_unfinished_run_with_no_qualifying_unit_prints_nothing_to_rescue_yet(tmp_path):
    completed = _attempt("a0000000000000001", "v2:x", 1, "completed")
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[completed])
    run = _run([agent], finished=False)
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config())
    text = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert text.strip() == (
        "NOTHING TO RESCUE YET: the run has not finished and no attempt was "
        "killed or failed."
    )
    assert plan.exit_code == 4


def test_possibly_live_suppresses_steps_3_to_6_and_the_brief(tmp_path):
    killed = _attempt(
        "a0000000000000001", "v2:x", 1, "killed",
        facts=_facts(paths_written=["src/a.py"], cwds=["/tmp/repo"]),
        interrupt=Interrupt(kill_ts=datetime.now(timezone.utc), preceding=None,
                             silence_seconds=3.0, cause="runtime", cause_evidence=None),
    )
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[killed])
    run = _run([agent], finished=False)
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config(), possibly_live=True)
    text = rescue.render_plan_text(run, plan, Redactor(enabled=False))

    assert text.startswith(
        "POSSIBLY LIVE: do not modify files in this repo. Confirm with the human "
        "that the run has stopped."
    )
    assert "BRIEF FOR A RELAUNCH" not in text
    assert "3. [decide]" not in text
    assert "6. [decide]" not in text
    assert "1. [look]" in text
    assert plan.exit_code == 4


# --- privacy: the word "verified" never appears -----------------------------


def test_the_word_verified_never_appears_in_output_or_source(tmp_path, monkeypatch, capsys):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)
    # Default (redacted) invocations: --no-redact would print this test's own
    # tmp_path, which pytest names after the test function itself and would
    # spuriously contain the substring "verified" -- a naming collision, not
    # a real word this tool ever prints, so this checks the product's own
    # vocabulary via the mode most users actually see.
    cli.main(["rescue", "plan", str(run_dir)])
    cli.main(["rescue", "status", str(run_dir)])
    out = capsys.readouterr().out
    assert "verified" not in out.lower()

    source = RESCUE_PATH.read_text(encoding="utf-8")
    assert "verified" not in source.lower()


# =============================================================================
# rescue status: reads the parent session transcript, docs/DESIGN.md 4.11
# =============================================================================
#
# One unit (key v2:x), one failed attempt that wrote tests/test_a.py, in a
# real throwaway git repo, plus a parent session .jsonl (the file the
# workflow directory sits under) that each test fills in with its own Bash
# calls after the run's own ended_at. wf_00000000-4c2 -- registered in
# tests/synthetic-ids.txt.


def _build_status_fixture(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("original\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "initial"], repo)

    slug = "-home-user-project"
    session = "00000000-0000-4000-8000-000000000000"
    run_dir = tmp_path / slug / session / "subagents" / "workflows" / "wf_00000000-4c2"
    run_dir.mkdir(parents=True)

    agent_id = "a4c2000000000001"
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    journal_lines = [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": ""},
        {"type": "failed", "key": "v2:x", "agentId": agent_id},
    ]
    _write_jsonl(run_dir / "journal.jsonl", journal_lines)

    path_a = str(repo / "tests" / "test_a.py")
    attempt = [
        _user("do the thing", _iso(base, 0), agent_id, repo),
        _assistant_tool("t1", "Edit", {"file_path": path_a}, _iso(base, 1), agent_id, repo),
        _tool_result("t1", "ok", _iso(base, 2), agent_id, repo),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", attempt)

    session_path = tmp_path / slug / f"{session}.jsonl"
    return run_dir, repo, session_path, base, path_a


def _bash_call(tool_id: str, command: str, ts: str, cwd: Path | None = None) -> dict:
    return _assistant_tool(tool_id, "Bash", {"command": command}, ts, None, cwd)


def _bash_result(tool_id: str, content: str, ts: str, cwd: Path | None = None,
                  is_error: bool = False) -> dict:
    return _tool_result(tool_id, content, ts, None, cwd, is_error=is_error)


_CHECK = "uv run pytest -q tests/test_a.py"


def _configure_check(tmp_path: Path, monkeypatch, repo: Path, check: str = _CHECK) -> None:
    _write_config(tmp_path, f'[[repos]]\npath = "{repo}"\nchecks = ["{check}"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "config.toml"))


def test_status_counted_run_exit_0_gives_exit_0_observed(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "EXIT_0_OBSERVED" in out
    assert code == 0


def test_status_default_output_redacts_the_path_in_last_counted_run(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 5.1/4.11 (F7 hardening): the declared check text
    rescue status echoes back on the 'last counted run' line now gets its
    own check-N pseudonym, the whole command hidden -- changed from the old
    pinned expectation, which only substituted the path embedded inside the
    command ("uv run pytest -q path-1") and left the rest of the command
    (flags, subcommand) visible by default."""
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
    ])

    cli.main(["rescue", "status", str(run_dir)])
    out = capsys.readouterr().out
    assert "tests/test_a.py" not in out
    assert "uv run pytest" not in out
    assert "last counted run: check-1" in out


def test_status_counted_run_nonzero_gives_exit_3(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "1 failed", _iso(after, 1), repo, is_error=True),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "NONZERO_OBSERVED" in out
    assert code == 3


def test_status_write_after_check_gives_write_after_check_and_exit_4(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _assistant_tool("w1", "Edit", {"file_path": path_a}, _iso(after, 2), None, repo),
        _tool_result("w1", "ok", _iso(after, 3), None, repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "WRITE_AFTER_CHECK" in out
    assert code == 4


def test_status_printf_redirect_after_check_is_write_after_check(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 4.11 (F3 hardening): any post-check Bash command not
    proven read-only is a possible write, not only Edit/Write/NotebookEdit
    to a listed path."""
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c2", f"printf x > {path_a}", _iso(after, 2), repo),
        _bash_result("c2", "", _iso(after, 3), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "WRITE_AFTER_CHECK" in out
    assert "a Bash command that may write ran after the check" in out
    assert code == 4


def test_status_sed_in_place_after_check_is_write_after_check(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c2", f"sed -i '' 's/a/b/' {path_a}", _iso(after, 2), repo),
        _bash_result("c2", "", _iso(after, 3), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "WRITE_AFTER_CHECK" in out
    assert code == 4


def test_status_git_checkout_after_check_forces_exit_4(tmp_path, monkeypatch, capsys):
    """The NEVER list forces exit 4 regardless of everything else (F3): even
    though the check itself passed and nothing else looked wrong, a NEVER-
    list command anywhere after the run ended is disqualifying."""
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c2", f"git checkout -- {path_a}", _iso(after, 2), repo),
        _bash_result("c2", "", _iso(after, 3), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert code == 4
    assert "NEVER_COMMAND_OBSERVED" in out


def test_status_read_only_git_status_after_check_does_not_change_result(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c2", "git status", _iso(after, 2), repo),
        _bash_result("c2", "clean", _iso(after, 3), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "WRITE_AFTER_CHECK" not in out
    assert "NEVER_COMMAND_OBSERVED" not in out
    assert "EXIT_0_OBSERVED" in out
    assert code == 0


def test_status_same_command_another_cwd_is_not_counted(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), other_dir),
        _bash_result("c1", "5 passed", _iso(after, 1), other_dir),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "not counted: line" in out
    assert "other cwd" in out
    assert "NOT_OBSERVED" in out
    assert code == 4


def test_status_compound_command_is_not_counted(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", f"cd {repo} && {_CHECK}", _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "compound command" in out
    assert "NOT_OBSERVED" in out
    assert code == 4


def test_status_backgrounded_call_is_not_counted(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result(
            "c1",
            "Command did not complete within its 30s timeout and was moved to the "
            "background (ID: 1).",
            _iso(after, 1), repo,
        ),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "backgrounded" in out
    assert "NOT_OBSERVED" in out
    assert code == 4


def test_status_call_with_no_result_is_not_counted(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "no result" in out
    assert "NOT_OBSERVED" in out
    assert code == 4


def test_status_ambiguous_tool_id_is_not_counted(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 4.2/4.11 (F5 hardening): a duplicated tool_use_id makes
    a call's own completion untrustworthy, so rescue status must never count
    it as a genuine check run -- it goes to not_counted with reason
    'ambiguous tool id' instead."""
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c1", _CHECK, _iso(after, 2), repo),
        _bash_result("c1", "5 passed", _iso(after, 3), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "ambiguous tool id" in out
    assert "NOT_OBSERVED" in out
    assert code == 4


def test_status_two_cycles_observed(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _assistant_tool("w1", "Edit", {"file_path": path_a}, _iso(after, 0), None, repo),
        _tool_result("w1", "ok", _iso(after, 1), None, repo),
        _bash_call("c1", _CHECK, _iso(after, 2), repo),
        _bash_result("c1", "5 passed", _iso(after, 3), repo),
        _assistant_tool("w2", "Edit", {"file_path": path_a}, _iso(after, 4), None, repo),
        _tool_result("w2", "ok", _iso(after, 5), None, repo),
        _bash_call("c2", _CHECK, _iso(after, 6), repo),
        _bash_result("c2", "5 passed", _iso(after, 7), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "check cycles observed: 2 of 2" in out
    assert "EXIT_0_OBSERVED" in out
    assert code == 0


def test_status_never_list_git_checkout_is_flagged(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c2", f"git checkout -- {path_a}", _iso(after, 2), repo),
        _bash_result("c2", "", _iso(after, 3), repo),
    ])

    code = cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "commands matching the NEVER list" in out
    assert "git checkout" in out
    # F3: a NEVER-list match now forces exit 4 regardless of everything else
    # (it used to leave the exit code at 0 when nothing else was wrong).
    assert code == 4


def test_status_freshness_sentence_always_printed(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
    ])

    cli.main(["rescue", "status", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert (
        "Writes made outside this session after the check cannot be seen; "
        "freshness beyond the transcript is UNKNOWN." in out
    )


def test_status_json_shape(tmp_path, monkeypatch, capsys):
    run_dir, repo, session_path, base, _path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
    ])

    cli.main(["rescue", "status", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "schema", "run", "units", "not_counted", "never_matches",
        "transcript_unknown", "exit_code",
    }
    unit = payload["units"][0]
    assert set(unit) == {
        "agent", "checks_declared", "last_counted_run", "word", "writes_after",
        "write_after_check", "cycles",
    }
    assert unit["word"] == "EXIT_0_OBSERVED"


# --- review fixes after M4 -------------------------------------------------


def test_steps_collapse_when_no_path_is_dirty_now(tmp_path, monkeypatch, capsys):
    """When observation ran and nothing the killed attempts wrote is dirty
    now, there is nothing to inspect, keep, or commit: the steps say so
    instead of printing `git diff -- <none>`."""
    run_dir, repo, base = _build_killed_then_completed(tmp_path)
    # Make every written path clean again: commit the working tree.
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "everything committed"], repo)
    _no_config(monkeypatch, tmp_path)
    cli.main(["rescue", "plan", str(run_dir)])
    out = capsys.readouterr().out
    assert "<none>" not in out
    assert "no path is dirty now" in out
    assert "git diff --" not in out
    assert "relaunch the remaining work" in out


def test_paths_table_keeps_columns_apart_for_a_long_state(tmp_path, monkeypatch, capsys):
    """An UNKNOWN state with its reason is wider than the state column; the
    attempts column must still be separated from it by whitespace."""
    run_dir, repo, base = _build_killed_then_completed(tmp_path)
    # Add a write outside the work tree to the killed attempt's transcript.
    a1 = "a4c0000000000001"
    lines = (run_dir / f"agent-{a1}.jsonl").read_text(encoding="utf-8").splitlines()
    outside = str(tmp_path / "elsewhere" / "note.md")
    extra = [
        _assistant_tool("t9", "Write", {"file_path": outside, "content": "x"}, _iso(base, 9.5), a1, repo),
        _tool_result("t9", "ok", _iso(base, 9.6), a1, repo),
    ]
    rows = lines[:-1] + [json.dumps(e) for e in extra] + [lines[-1]]
    (run_dir / f"agent-{a1}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    _no_config(monkeypatch, tmp_path)
    cli.main(["rescue", "plan", str(run_dir)])
    out = capsys.readouterr().out
    unknown_lines = [line for line in out.splitlines() if "UNKNOWN (outside the work tree)" in line]
    assert unknown_lines
    for line in unknown_lines:
        assert re.search(r"UNKNOWN \(outside the work tree\)\s+\d", line), line


def test_status_never_line_uses_only_the_first_word_by_default(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 4.11/5.1 (F7 hardening): a NEVER-list excerpt shows
    only the command's own first word by default (git, sed, printf) --
    changed from the old pinned "git checkout -- path-N" expectation, which
    still showed the rest of the command (the exact subcommand and flags)
    once its path was substituted."""
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c2", f"git checkout -- {path_a}", _iso(after, 2), repo),
        _bash_result("c2", "", _iso(after, 3), repo),
    ])
    cli.main(["rescue", "status", str(run_dir)])
    out = capsys.readouterr().out
    never = [line for line in out.splitlines() if "NEVER list" in line]
    assert never
    assert path_a not in out
    assert "tests/test_a.py" not in out
    assert "checkout" not in never[0]
    assert never[0].rstrip().endswith("git")


def test_status_excerpts_are_first_word_by_default_and_full_with_no_redact(
    tmp_path, monkeypatch, capsys,
):
    run_dir, repo, session_path, base, path_a = _build_status_fixture(tmp_path)
    _configure_check(tmp_path, monkeypatch, repo)
    after = base + timedelta(seconds=30)
    _write_jsonl(session_path, [
        _bash_call("c1", _CHECK, _iso(after, 0), repo),
        _bash_result("c1", "5 passed", _iso(after, 1), repo),
        _bash_call("c2", f"git checkout -- {path_a}", _iso(after, 2), repo),
        _bash_result("c2", "", _iso(after, 3), repo),
    ])

    cli.main(["rescue", "status", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["never_matches"][0]["excerpt"] == "git"

    cli.main(["rescue", "status", str(run_dir), "--json", "--no-redact"])
    raw_payload = json.loads(capsys.readouterr().out)
    assert raw_payload["never_matches"][0]["excerpt"].startswith("git checkout --")
