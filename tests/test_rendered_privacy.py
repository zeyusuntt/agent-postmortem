"""Rendered-output privacy fixtures (release finding F14, docs/DESIGN.md
5.4/9): one distinctive marker string, planted in a path, a prompt, a
declared check/command, and a status claim, rendered through five public
commands (`run --json`, `rescue plan`, `rescue status`, `rules review`,
`scan`) and asserted absent by default, present under `--no-redact`.

Every underlying per-field redaction (path-N, check-N, "status: set",
target-N) already has its own dedicated test elsewhere (tests/test_rescue.py,
tests/test_rules_review.py); this file's job is different: prove that one
end-to-end render of each of the five sinks named above never lets the same
marker slip through by some path those narrower unit tests do not cover
(a renderer that forgets to redact one field while getting its neighbor
right, for instance), the same "sink-specific rendered privacy fixture"
gap the release review's finding 14 named.

MARKER is deliberately not a contiguous literal anywhere it is planted (see
_marker() below): assembled at run time so this tracked source file itself
never contains the raw value tests/test_privacy.py would otherwise flag.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem import config, observe, rescue
from agent_postmortem.cli import main
from agent_postmortem.models import Attempt, AttemptFacts, Interrupt, LogicalAgent, Run
from agent_postmortem.redact import Redactor
from tests import rule_fixtures


def _marker() -> str:
    return "zzMarker" + "PrivacyFx" + "9f3c1"


MARKER = _marker()


# --- shared on-disk fixture: path / prompt / declared check ----------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    return repo


def _iso(base: datetime, offset: float) -> str:
    return (base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user(content, ts: str, agent: str, cwd: Path) -> dict:
    return {
        "type": "user", "message": {"role": "user", "content": content}, "timestamp": ts,
        "agentId": agent, "cwd": str(cwd),
    }


def _assistant_tool(tool_id: str, name: str, input_obj: dict, ts: str, agent: str, cwd: Path) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}],
            "usage": {},
        },
        "timestamp": ts, "agentId": agent, "cwd": str(cwd),
    }


def _tool_result(tool_id: str, content: str, ts: str, agent: str, cwd: Path) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False}],
        },
        "timestamp": ts, "agentId": agent, "cwd": str(cwd),
    }


def _bash_call(tool_id: str, command: str, ts: str, cwd: Path) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}],
            "usage": {},
        },
        "timestamp": ts, "cwd": str(cwd),
    }


def _bash_result(tool_id: str, content: str, ts: str, cwd: Path) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False}],
        },
        "timestamp": ts, "cwd": str(cwd),
    }


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


CHECK_COMMAND = f"pytest -k {MARKER}"


def _build_marker_run(tmp_path: Path, monkeypatch):
    """One failed attempt, under a real throwaway git repo (mirrors tests/
    test_rescue.py's _build_status_fixture): writes a repo-relative path
    that embeds MARKER, its first user message embeds MARKER, and a
    config.toml declares a check command that embeds MARKER for this
    repo -- plus a session-level Bash call/result running that exact check,
    for rescue status's own 'last counted run' line."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    marker_file = repo / "src" / f"{MARKER}_module.py"
    marker_file.write_text("original\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "initial"], repo)
    marker_file.write_text("changed\n", encoding="utf-8")  # dirty at observe time

    slug = "-home-user-project"
    session = "00000000-0000-4000-8000-000000000000"
    run_dir = tmp_path / slug / session / "subagents" / "workflows" / "wf_00000000-caf"
    run_dir.mkdir(parents=True)

    agent_id = "acaf000000000001"
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(
        run_dir / "journal.jsonl",
        [
            {"type": "started", "key": "v2:mkr", "agentId": agent_id, "label": "impl", "phase": "Implement"},
            {"type": "failed", "key": "v2:mkr", "agentId": agent_id},
        ],
    )

    path_marker = str(marker_file)
    _write_jsonl(
        run_dir / f"agent-{agent_id}.jsonl",
        [
            _user(f"{MARKER} please fix the module and ship it.", _iso(base, 0), agent_id, repo),
            _assistant_tool("t1", "Edit", {"file_path": path_marker}, _iso(base, 1), agent_id, repo),
            _tool_result("t1", "ok", _iso(base, 2), agent_id, repo),
        ],
    )

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[[repos]]\npath = "{repo}"\nchecks = ["{CHECK_COMMAND}"]\n', encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    session_path = tmp_path / slug / f"{session}.jsonl"
    after = base + timedelta(seconds=30)
    _write_jsonl(
        session_path,
        [
            _bash_call("c1", CHECK_COMMAND, _iso(after, 0), repo),
            _bash_result("c1", "5 passed", _iso(after, 1), repo),
        ],
    )

    return run_dir, repo, tmp_path


# --- run --json: the marker embedded in a path (facts.paths_written) ------


def test_run_json_hides_the_marker_path_by_default_and_shows_it_with_no_redact(
    tmp_path, monkeypatch, capsys,
):
    run_dir, _repo, _root = _build_marker_run(tmp_path, monkeypatch)

    code = main(["run", str(run_dir), "--json"])
    default_out = capsys.readouterr().out
    assert code in (0, 3, 4)
    assert MARKER not in default_out
    assert "path-1" in default_out

    code = main(["run", str(run_dir), "--json", "--no-redact"])
    raw_out = capsys.readouterr().out
    assert code in (0, 3, 4)
    assert MARKER in raw_out


# --- rescue plan: the marker in a path, a prompt, and a declared check ----


def test_rescue_plan_hides_every_marker_field_by_default_and_shows_them_with_no_redact(
    tmp_path, monkeypatch, capsys,
):
    run_dir, _repo, _root = _build_marker_run(tmp_path, monkeypatch)

    main(["rescue", "plan", str(run_dir)])
    default_out = capsys.readouterr().out
    assert MARKER not in default_out
    assert "path-1" in default_out
    assert "prompt length:" in default_out
    assert "check-1" in default_out

    main(["rescue", "plan", str(run_dir), "--no-redact"])
    raw_out = capsys.readouterr().out
    # The path, the prompt (quoted, first 300 chars), and the declared
    # check command all carry MARKER in --no-redact text.
    assert raw_out.count(MARKER) >= 3

    main(["rescue", "plan", str(run_dir), "--json"])
    default_json = capsys.readouterr().out
    assert MARKER not in default_json

    main(["rescue", "plan", str(run_dir), "--json", "--no-redact"])
    raw_json = capsys.readouterr().out
    assert MARKER in raw_json


# --- rescue status: the marker in the declared check / counted command ----


def test_rescue_status_hides_the_marker_command_by_default_and_shows_it_with_no_redact(
    tmp_path, monkeypatch, capsys,
):
    run_dir, _repo, _root = _build_marker_run(tmp_path, monkeypatch)

    main(["rescue", "status", str(run_dir)])
    default_out = capsys.readouterr().out
    assert MARKER not in default_out
    assert "check-1" in default_out

    main(["rescue", "status", str(run_dir), "--no-redact"])
    raw_out = capsys.readouterr().out
    assert MARKER in raw_out

    main(["rescue", "status", str(run_dir), "--json"])
    default_json = capsys.readouterr().out
    assert MARKER not in default_json

    main(["rescue", "status", str(run_dir), "--json", "--no-redact"])
    raw_json = capsys.readouterr().out
    assert MARKER in raw_json


# --- rescue plan: the marker as a reported.status claim (in-memory run) ---
#
# Mirrors tests/test_rescue.py's test_reported_status_claim_hidden_by_
# default_shown_with_no_redact, with MARKER as the claim value instead of a
# fixed pinned string, so this fixture's own marker -- not just any
# free-form status text -- is what is proven hidden/shown.


def _facts() -> AttemptFacts:
    return AttemptFacts(
        paths_written=[], in_flight=[], commits=[], cwds=[], background_unfinished=0,
        last_tool=None, seconds_since_last_tool_at_end=None,
    )


def test_rescue_plan_hides_the_marker_status_claim_by_default_shows_it_with_no_redact(tmp_path):
    killed = Attempt(
        agent_id="a0000000000000001", key="v2:x", label="agent", phase="phase", agent_type=None,
        meta={}, transcript_path=None, events=[], first_ts=None, last_ts=None, ordinal=1,
        outcome="killed", usage={}, facts=_facts(),
        interrupt=Interrupt(kill_ts=None, preceding=None, silence_seconds=5.0, cause="runtime",
                             cause_evidence=None),
    )
    completed = Attempt(
        agent_id="a0000000000000002", key="v2:x", label="agent", phase="phase", agent_type=None,
        meta={}, transcript_path=None, events=[], first_ts=None, last_ts=None, ordinal=2,
        outcome="completed", usage={}, facts=_facts(),
        reported={"type": "dict", "claims": {"status": MARKER}},
    )
    agent = LogicalAgent(key="v2:x", label="impl", phase="", attempts=[killed, completed])
    run = Run(
        run_id="wf_00000000-caf", path=Path("/tmp/does-not-matter"),
        project_slug="-home-user-project", session_id=None, claude_version=None,
        journal=[], agents=[agent], attempts=[killed, completed], started_at=None, ended_at=None,
        diagnostics=[], finished=True,
    )
    observation = observe.observe_run(run, now=datetime.now(timezone.utc))
    plan = rescue.build_plan(run, observation, [], config.load_config())

    redacted_text = rescue.render_plan_text(run, plan, Redactor(enabled=True))
    raw_text = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert MARKER not in redacted_text
    assert "REPORTED  status: set" in redacted_text
    assert f"REPORTED  status: {MARKER}" in raw_text

    redacted_json = rescue.plan_to_json(run, plan, Redactor(enabled=True))
    raw_json = rescue.plan_to_json(run, plan, Redactor(enabled=False))
    assert redacted_json["units"][0]["reported"]["claims"]["status"] == "set"
    assert raw_json["units"][0]["reported"]["claims"]["status"] == MARKER


# --- rules review: the marker in an adopted rule's target path ------------


def _write_review_config(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_rules_review_hides_the_marker_target_by_default_shows_it_with_no_redact(
    tmp_path, capsys, monkeypatch,
):
    config_path = tmp_path / "config.toml"
    _write_review_config(
        config_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\n'
        f'target = "{MARKER}/CLAUDE.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(
        root, "wf_00000000-e0f", datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    code = main(["rules", "review", "--root", str(root), "--json"])
    default_payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert MARKER not in json.dumps(default_payload)
    assert default_payload["reviews"][0]["target"].startswith("target-")

    code = main(["rules", "review", "--root", str(root), "--json", "--no-redact"])
    raw_payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert raw_payload["reviews"][0]["target"] == f"{MARKER}/CLAUDE.md"

    # trend.render_review_text never prints the target path at all (only
    # the rule id, adoption date, and detector counts) -- --json is the
    # only rendering that carries the target field, redacted or not, so
    # there is no text-mode "shown under --no-redact" counterpart here.
    code = main(["rules", "review", "--root", str(root)])
    default_text = capsys.readouterr().out
    assert MARKER not in default_text

    code = main(["rules", "review", "--root", str(root), "--no-redact"])
    raw_text = capsys.readouterr().out
    assert MARKER not in raw_text


# --- scan: the marker never surfaces at all, by construction --------------
#
# scan's own default (triage) and --all row renderers never carry a prompt,
# a raw path, a declared check, or a reported claim into their output at
# all (report.attention_reason/_scan_row_line build their text from fixed
# vocabulary, counts, and detector ids only) -- so there is no --no-redact
# counterpart to check here the way there is for the other four commands;
# this pins that structural absence down as a regression test.


def test_scan_never_shows_the_marker_in_default_or_no_redact_output(tmp_path, monkeypatch, capsys):
    _run_dir, _repo, root = _build_marker_run(tmp_path, monkeypatch)

    main(["scan", "--root", str(root)])
    assert MARKER not in capsys.readouterr().out

    main(["scan", "--root", str(root), "--no-redact"])
    assert MARKER not in capsys.readouterr().out

    main(["scan", "--root", str(root), "--all", "--json"])
    assert MARKER not in capsys.readouterr().out

    main(["scan", "--root", str(root), "--all", "--json", "--no-redact"])
    assert MARKER not in capsys.readouterr().out
