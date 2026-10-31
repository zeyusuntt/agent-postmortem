"""A user-authored name that starts with a lowercase letter must not reach share output
capitalized (docs/DESIGN.md 1 and 5.1: pseudonyms for every user-authored name in every
output; 4.13.5 share invariant).

A story sentence starts with a capital. When its first clause is a finding title that starts
with the agent's label (`mislabeled_kill`: "<label> interrupted after Ns of silence ..."), 0.3.5
capitalized the label before the case-sensitive name substitution ran, so "impl:x" printed as
"Impl:x". From 0.3.6 a story clause is capitalized after the sink substituted names
(`report._render_pieces`), so the tool's own capitalized words never meet a name either. The
planted names here start lowercase, unlike the sentinels of test_field_v03_more.py, and every
check is case-insensitive.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, reader, report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests.test_field_v03 import (
    FAKE_SLUG,
    FAKE_UUID,
    _assistant_tool,
    _kill,
    _no_config,
    _parent_path,
    _tool_result,
    _user,
    _wf_dir,
    _write_jsonl,
)

_LABEL = "zz-lower-agent-nickname"
_PHASE = "zz-lower-phase-title"
_WF_ID = "wf_00000000-b36"
_AGENT = "ab36" + "0" * 12 + "1"


def _backdate(paths: list[Path], when: datetime) -> None:
    for path in paths:
        os.utime(path, (when.timestamp(), when.timestamp()))


def _killed_after_silence(agent_id: str, base: datetime) -> list[dict]:
    """One Bash call, then 400 s of silence and the interrupt marker: an unattributed
    long-silence mislabeled_kill, whose title starts with the label and is the agent's
    whole story (no path, no commit, no restart)."""
    return [
        _user("Investigate.", base, agent_id),
        _assistant_tool("t1", "Bash", {"command": "ls"}, base + timedelta(seconds=1), agent_id),
        _tool_result("t1", "ok", base + timedelta(seconds=2), agent_id),
        _kill(base + timedelta(seconds=402), agent_id),
    ]


def _workflow(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    run_dir = _wf_dir(root, _WF_ID)
    base = datetime.now(timezone.utc) - timedelta(days=1)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": _AGENT, "label": _LABEL, "phase": _PHASE},
    ])
    transcript = run_dir / f"agent-{_AGENT}.jsonl"
    _write_jsonl(transcript, _killed_after_silence(_AGENT, base))
    main = _parent_path(root)
    _write_jsonl(main, [{
        "type": "user", "message": {"role": "user", "content": "start"},
        "sessionId": FAKE_UUID, "timestamp": base.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }])
    _backdate([run_dir / "journal.jsonl", transcript, main], base + timedelta(seconds=403))
    return root, run_dir, main


def _session(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    main = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    base = datetime.now(timezone.utc) - timedelta(days=1)
    _write_jsonl(main, [{
        "type": "user", "message": {"role": "user", "content": "start"},
        "sessionId": FAKE_UUID, "timestamp": base.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }])
    subagents = root / FAKE_SLUG / FAKE_UUID / "subagents"
    transcript = subagents / f"agent-{_AGENT}.jsonl"
    _write_jsonl(transcript, _killed_after_silence(_AGENT, base))
    meta = subagents / f"agent-{_AGENT}.meta.json"
    meta.write_text(json.dumps({"agentType": "general-purpose", "description": _LABEL}),
                    encoding="utf-8")
    _backdate([main, transcript, meta], base + timedelta(seconds=403))
    return root, main, main


_KINDS = {"workflow": _workflow, "session": _session}


def _outputs(capsys, monkeypatch, tmp_path, root: Path, run: Path, main: Path) -> dict[str, str]:
    """Every share sink that can carry a label, by command."""
    # (argv, expected exit code): run and scan succeed; rescue exits 4 for a run whose risk
    # population it cannot complete (docs/DESIGN.md 4.11)
    commands = {
        "run": (["run", str(run)], 0),
        "run --json": (["run", str(run), "--json"], 0),
        "run --verbose": (["run", str(run), "--verbose"], 0),
        "run --bug-report": (["run", str(run), "--bug-report"], 0),
        "scan": (["scan", "--root", str(root)], 0),
        "scan --all": (["scan", "--root", str(root), "--all"], 0),
        "scan --json": (["scan", "--root", str(root), "--all", "--json"], 0),
        "rescue plan": (["rescue", "plan", str(run)], 4),
        "rescue plan --json": (["rescue", "plan", str(run), "--json"], 4),
        "rescue status": (["rescue", "status", str(run)], 4),
    }
    run_id = reader.load_run(run).run_id
    out: dict[str, str] = {}
    for name, (argv, expected) in commands.items():
        code = cli.main(argv)
        captured = capsys.readouterr()
        assert code == expected and captured.out.strip(), (name, code, captured.err)
        if name.endswith("--json"):
            json.loads(captured.out)
        record = {"rescue plan": "UNIT agent-1", "rescue plan --json": '"agent-1"',
                  "rescue status": None}.get(name, run_id)
        if record is not None:
            assert record in captured.out, (name, record)
        out[name] = captured.out + captured.err
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "hookstate"))
    payload = {"hook_event_name": "Stop", "transcript_path": str(main),
               "session_id": FAKE_UUID, "stop_hook_active": False}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "check"]) == 0
    captured = capsys.readouterr()
    out["hook check"] = captured.out + captured.err
    return out


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_lowercase_label_never_reaches_share_output_in_any_case(
    kind, tmp_path, monkeypatch, capsys,
):
    _no_config(monkeypatch, tmp_path)
    root, run, main = _KINDS[kind](tmp_path)
    outputs = _outputs(capsys, monkeypatch, tmp_path, root, run, main)
    # the planted case is exercised: the story that used to carry the label is there, aliased
    assert "Agent-1 interrupted after 400s" in outputs["run"]
    stories = [entry["text"] for entry in json.loads(outputs["run --json"])["stories"]]
    assert any(text.startswith("Agent-1 interrupted after 400s") for text in stories), stories
    leaks = {
        name: text for name, text in outputs.items()
        if _LABEL in text.lower() or _PHASE in text.lower()
    }
    assert not leaks, {name: [line for line in text.splitlines()
                              if _LABEL in line.lower() or _PHASE in line.lower()]
                       for name, text in leaks.items()}


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_the_story_still_starts_with_a_capital_after_the_label_is_replaced(
    kind, tmp_path, monkeypatch, capsys,
):
    _no_config(monkeypatch, tmp_path)
    _root, run, _main = _KINDS[kind](tmp_path)
    cli.main(["run", str(run)])
    out = capsys.readouterr().out
    story = [line for line in out.splitlines() if "interrupted after 400s" in line]
    assert len(story) == 1, out
    sentence = story[0].strip().split("  ", 1)[1]
    assert sentence.startswith("Agent-1 interrupted after 400s"), story[0]


def test_local_still_prints_the_real_label_capitalized_where_the_sentence_starts(
    tmp_path, monkeypatch, capsys,
):
    _no_config(monkeypatch, tmp_path)
    _root, run, _main = _workflow(tmp_path)
    cli.main(["run", str(run), "--local"])
    out = capsys.readouterr().out
    assert "Zz-lower-agent-nickname interrupted after 400s" in out


def _share_redactor(**names: str) -> Redactor:
    redactor = Redactor(context=DisclosureContext(level="share", home="/home/user"))
    registry = redactor.fresh_names(detailed=False)
    for category, value in names.items():
        getattr(registry, category)(value)
    return redactor


def _story(redactor: Redactor, *clauses) -> str:
    return report._render_pieces(report._sentences_pieces(list(clauses)), redactor)


def test_a_clause_that_starts_with_a_lowercase_label_is_aliased_then_capitalized():
    redactor = _share_redactor(agent="zz-lower")
    assert _story(redactor, "zz-lower interrupted after 400s", "Wrote no paths.") == (
        "Agent-1 interrupted after 400s. Wrote no paths."
    )


def _demo_copy(tmp_path, label: str) -> Path:
    """The packaged demo workflow (two agents, each killed once by the runtime and restarted),
    with every journal label replaced by `label`."""
    import shutil
    from importlib import resources
    source = resources.files("agent_postmortem") / "demo" / "-home-user-project"
    target = tmp_path / "root" / "-home-user-project"
    with resources.as_file(source) as path:
        shutil.copytree(path, target)
    run_dir = next(target.glob("*/subagents/workflows/wf_*"))
    journal = run_dir / "journal.jsonl"
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if "label" in row:
            row["label"] = label
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return run_dir


def test_a_label_that_is_the_first_word_of_the_tools_own_clause_leaves_it_alone(
    tmp_path, monkeypatch, capsys,
):
    # Review round 2: a workflow agent labelled `killed` with a runtime retry. 0.3.5 printed
    # "Killed 1 time by the runtime (416s of silence)." and so must 0.3.6, in text and JSON.
    _no_config(monkeypatch, tmp_path)
    run_dir = _demo_copy(tmp_path, "killed")
    assert cli.main(["run", str(run_dir)]) == 0
    text = capsys.readouterr().out
    assert "Killed 1 time by the runtime (416s of silence)." in text, text
    assert cli.main(["run", str(run_dir), "--json"]) == 0
    stories = [entry["text"] for entry in json.loads(capsys.readouterr().out)["stories"]]
    assert stories and all(
        story.startswith("Killed 1 time by the runtime") for story in stories
    ), stories


@pytest.mark.parametrize(("label", "clause"), [
    ("restarted", "restarted 1 time"),
    ("attempt", "attempt 2 failed: usage limit"),
    ("stopped", "stopped by the parent"),
])
def test_the_tools_own_lowercase_clause_words_are_capitalized_before_any_name_meets_them(
    label, clause,
):
    shown = _story(_share_redactor(agent=label), report._own_clause(clause))
    assert shown == clause[:1].upper() + clause[1:] + ".", shown


@pytest.mark.parametrize(("category", "name", "clause"), [
    ("agent", "wrote", "Wrote no paths."),
    ("type", "codex", "1 Codex call could not start"),
    ("agent", "killed", "Killed 1 time by the runtime (400s of silence)"),
])
def test_the_tools_own_capitalized_words_never_meet_a_name_of_another_case(
    category, name, clause,
):
    # A label or agent type that is a plain word only matches that word as written; the
    # tool's own capitalized words around it stay as they are (no sentence-case matching).
    assert _story(_share_redactor(**{category: name}), clause) == clause.rstrip(".") + "."


def test_a_run_title_in_the_tools_own_words_keeps_its_capital_whatever_the_labels(
    tmp_path, monkeypatch, capsys,
):
    # Review round 3: a run-level story is a list of finding titles in the tool's own words; a
    # label that is their first word (here "the") must not rewrite them. 0.3.5 printed
    # "The harness reported this workflow failed." and so must 0.3.6.
    from tests.test_field_v03 import _case5_fixture
    run_dir, _parent = _case5_fixture(tmp_path, monkeypatch)
    journal = run_dir / "journal.jsonl"
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if "label" in row:
            row["label"] = "the"
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert cli.main(["run", str(run_dir)]) == 3  # a FAILED verdict
    text = capsys.readouterr().out
    assert "The harness reported this workflow failed." in text, text
    assert cli.main(["run", str(run_dir), "--json"]) == 3
    stories = [entry["text"] for entry in json.loads(capsys.readouterr().out)["stories"]]
    assert "The harness reported this workflow failed." in stories, stories
