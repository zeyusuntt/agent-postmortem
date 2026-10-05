"""N3b fix B, C1 (supersedes S3): `rescue status` never prints a transcript
token for a NEVER-list command without --evidence. The text and the share
JSON carry only the matched rule's own canonical spelling (for example
`git checkout`), taken from code. Raw JSON keeps the 60-character excerpt;
--evidence at local and raw keeps v0.2's excerpt in text."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests import n3b_helpers as h
from tests import n3b_planted as plant

CHECK_TAIL = "zzCheck" + "Plant" + "7c6"
SECRET = "CLIENT_SECRET"

COMMANDS = [
    (f"{SECRET} git checkout -- src/module.py", "git checkout"),
    (f"{SECRET}=sk-live-abc123 git restore src/module.py", "git restore"),
    ("/opt/zzclient/bin/git reset --hard", "git reset"),
    ("cd /opt/zzclient/repo && git stash", "git stash"),
    (f"{SECRET} git clean -fd", "git clean"),
]


@pytest.fixture
def planted(tmp_path):
    # N5c-1: the observable variant (the observed repository root is what a
    # declared check is matched against and what paths render relative to).
    return plant.build(tmp_path, observable=True)


def _configure(planted, tmp_path, monkeypatch, never_command, extra_command=None):
    check = f"pytest -q {planted.repo}/tests/{CHECK_TAIL}"
    config = tmp_path / "rescue-config.toml"
    config.write_text(
        f'[[repos]]\npath = "{planted.repo}"\nchecks = ["{check}"]\n', encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    after = datetime.now(timezone.utc) - timedelta(minutes=5)

    def stamp(offset):
        return (after + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def call(tool_id, command, offset):
        return {"type": "assistant", "timestamp": stamp(offset), "cwd": str(planted.repo),
                "sessionId": planted.session, "message": {"role": "assistant", "usage": {},
                "content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                             "input": {"command": command}}]}}

    def result(tool_id, offset):
        return {"type": "user", "timestamp": stamp(offset), "cwd": str(planted.repo),
                "sessionId": planted.session, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": "ok",
                     "is_error": False}]}}

    rows = [call("c1", check, 0), result("c1", 1), call("c2", never_command, 2), result("c2", 3)]
    if extra_command:
        rows += [call("c3", extra_command, 4), result("c3", 5)]
    path = planted.root / planted.slug / f"{planted.session}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _status(planted, capsys, *flags):
    return h.cli(capsys, "rescue", "status", planted.run_dir, *flags)


@pytest.mark.parametrize("command,category", COMMANDS)
@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_text_prints_the_rule_category_never_a_transcript_token(
    command, category, flags, planted, tmp_path, monkeypatch, capsys,
):
    _configure(planted, tmp_path, monkeypatch, command)
    _code, out, _err = _status(planted, capsys, *flags)
    assert re.search(rf"commands matching the NEVER list: line \d+: {category}$", out, re.MULTILINE)
    assert SECRET not in out
    assert "sk-live" not in out
    assert "/opt/zzclient" not in out


@pytest.mark.parametrize("command,category", COMMANDS)
def test_share_evidence_text_prints_the_category_too(
    command, category, planted, tmp_path, monkeypatch, capsys,
):
    _configure(planted, tmp_path, monkeypatch, command)
    _code, out, _err = _status(planted, capsys, "--evidence")
    assert SECRET not in out
    assert re.search(rf"commands matching the NEVER list: line \d+: {category}", out)


@pytest.mark.parametrize("command,category", COMMANDS)
def test_share_json_carries_only_the_category(
    command, category, planted, tmp_path, monkeypatch, capsys,
):
    _configure(planted, tmp_path, monkeypatch, command)
    _code, out, _err = _status(planted, capsys, "--json")
    assert SECRET not in out
    assert "sk-live" not in out
    assert "/opt/zzclient" not in out
    payload = json.loads(out)
    # N5a-1 (5.3b): the category is its own key and no excerpt key exists at share.
    assert [row["category"] for row in payload["never_matches"]] == [category]
    assert all("excerpt" not in row for row in payload["never_matches"])


def test_share_json_not_counted_carries_no_transcript_token(planted, tmp_path, monkeypatch, capsys):
    _configure(
        planted, tmp_path, monkeypatch, "git checkout -- src/module.py",
        extra_command=f"{SECRET} touch src/module.py",
    )
    _code, out, _err = _status(planted, capsys, "--json")
    payload = json.loads(out)
    assert payload["not_counted"]
    assert SECRET not in out


@pytest.mark.parametrize("command,category", COMMANDS)
def test_raw_json_keeps_the_sixty_character_excerpt(
    command, category, planted, tmp_path, monkeypatch, capsys,
):
    _configure(planted, tmp_path, monkeypatch, command)
    # N5a-1 (5.3b): the excerpt key exists only with --evidence at local and raw.
    _code, out, _err = _status(planted, capsys, "--raw", "--json", "--evidence")
    payload = json.loads(out)
    assert payload["never_matches"][0]["excerpt"] == command[:60]
    _code, out, _err = _status(planted, capsys, "--raw", "--json")
    assert "excerpt" not in json.loads(out)["never_matches"][0]
