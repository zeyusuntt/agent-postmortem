"""Fix round 6 (W6-2, W6-6, W6-8) of field milestone F through the rescue, check, name, scan, hook,
apply, reader and run sinks the reviewers list (docs/DESIGN.md 5.1, 4.11;
.reviews/v0.3/F-dispositions.md, "Fix round 5 review").

Every fixture is synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_postmortem import reader, report, rescue, rules
from tests import n3b_helpers as h
from tests.test_fc_fix4_sinks import _plan, _redactor, _status_outputs

PRIVATE = ("Client", "Secret", "Neil", "deep", "notes.md", "Important", "Annual", "Holdings", "Acme",
           "Private", "Zqa", "Zqb", "Zqc")
HIDDEN = " [text hidden]"
FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "clean_run"



# --- rescue excerpts, text and JSON, at local -----------------------------------------------------------

EXCERPTS = [
    pytest.param("cannot open /srv/data/Client Secret/deep/notes.md now",
                 f"cannot open path-1{HIDDEN}", id="unquoted_space"),
    pytest.param("cat /srv/Client Secret/deep More Private/notes.md now",
                 f"cat path-1{HIDDEN}", id="codex2_two_spaced_segments"),
    pytest.param("open /srv/data/Client (Acme Holdings)/deep/notes.md",
                 f"open path-1{HIDDEN}", id="codex2_parenthesized_directory"),
    pytest.param("see /srv/data/R&D Plans/notes.md now", f"see path-1{HIDDEN}", id="ampersand"),
    pytest.param("see /srv/data/Client Secret.md now", f"see path-1{HIDDEN}",
                 id="space_in_the_last_segment"),
    pytest.param("cat /srv/data/Client” Secret.txt” now", f"cat path-1{HIDDEN}",
                 id="codex3_smart_quote_half_quoted"),
    pytest.param('{"path":"\\u002fsrv\\u002fClient Secret\\u002fdeep\\u002fnotes.md"',
                 None, id="codex4_truncated_json"),
    pytest.param("PosixPath('/srv/O\\'Neil \"Client\" Secret/deep/f')",
                 f"PosixPath('path-1{HIDDEN}')", id="python_repr_both_quote_kinds"),
]


@pytest.mark.parametrize("excerpt,expected", EXCERPTS)
def test_fc6_a_local_rescue_excerpt_shows_no_private_tail_and_the_marker(excerpt, expected):
    text, payload = _status_outputs(excerpt, ())
    for word in PRIVATE:
        assert word not in text, (word, text)
        assert word not in payload, (word, payload)
    if expected is not None:
        assert f"line 9: {expected}" in text, text
        assert json.loads(payload)["never_matches"][0]["excerpt"] == expected, payload


CHECKS = [
    pytest.param("pytest /srv/data/Client (Acme Holdings)/deep/notes.md -k x", id="parentheses"),
    pytest.param("pytest /srv/data/AT&T/Client/notes.md -k x", id="ampersand"),
    pytest.param("pytest /srv/data/Client Secret.md -k x", id="space_in_the_last_segment"),
]


@pytest.mark.parametrize("check", CHECKS)
def test_fc6_a_local_declared_check_shows_no_private_tail(check):
    run, plan = _plan([check])
    text = rescue.render_plan_text(run, plan, _redactor("local"))
    payload = json.dumps(rescue.plan_to_json(run, plan, _redactor("local")))
    for word in PRIVATE + ("AT&T",):
        assert word not in text, (word, text)
        assert word not in payload, (word, payload)
    assert "pytest" in text and "pytest" in payload
    share = rescue.render_plan_text(run, plan, _redactor("share"))
    assert "check-1" in share and "pytest" not in share


@pytest.mark.parametrize("value", [
    pytest.param("fix /srv/data/Client (Acme Holdings)/deep/notes.md now", id="parentheses"),
    pytest.param("fix /srv/data/Client Secret.md now", id="space_in_the_last_segment"),
    pytest.param("fix /srv/data/R&D Client/deep/notes.md now", id="ampersand"),
])
def test_fc6_a_local_name_value_shows_no_private_tail(value):
    r = _redactor("local")
    names = r.fresh_names(detailed=True)
    for shown in (names.agent(value), names.description(value), names.phase(value),
                  names.local_text(value), r.title(value)):
        for word in PRIVATE + ("R&D",):
            assert word not in shown, (word, shown)


# --- scan triage and the hook notification -------------------------------------------------------------------

TRIAGE_ROWS = [
    pytest.param("cannot open /srv/data/Client Secret/deep/notes.md now",
                 f"cannot open path-1{HIDDEN}", id="unquoted_space"),
    pytest.param("open /srv/data/Client (copy)/deep/notes.md failed",
                 f"open path-1{HIDDEN}", id="parenthesized_directory"),
    pytest.param("cat /srv/The Important Client Annual Report 2026/deep/notes.md now",
                 f"cat path-1{HIDDEN}", id="long_name"),
]


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("text,expected", TRIAGE_ROWS)
def test_fc6_scan_triage_scans_the_reason_and_the_command(level, text, expected):
    from types import SimpleNamespace

    from tests.test_fc_scan import TOTALS

    candidate = {
        "run": SimpleNamespace(run_id="wf_00000000-a1"), "age": "1d", "verdict": "DEGRADED",
        "reason": text, "command": f"cat {text}",
    }
    out = report.render_scan_triage_text([candidate], 1, 1, TOTALS, TOTALS, _redactor(level))
    for word in PRIVATE + ("copy", "Report"):
        assert word not in out, (word, out)
    assert f"DEGRADED  {expected}\n" in out, out
    assert f"-> cat {expected}\n" in out, out


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("text,expected", TRIAGE_ROWS)
def test_fc6_a_hook_notification_headline_is_scanned_piece_by_piece(level, text, expected):
    from agent_postmortem import hooks_cli

    notify = hooks_cli._NotifyInfo(
        run_id="wf_00000000-a1", verdict_word="DEGRADED", headline=text,
        is_incomplete=False, quiet_unfinished=False, finding_count=1, agent_ids=[],
    )
    lines = hooks_cli._format_notify_lines([notify], _redactor(level))
    for word in PRIVATE + ("copy", "Report"):
        assert word not in lines[0], (word, lines[0])
    # the run id is its own piece: a path in the headline does not hide it
    assert lines[0] == f"DEGRADED  {expected}  wf_00000000-a1  /postmortem wf_00000000-a1", lines[0]
    assert lines[0].endswith("/postmortem wf_00000000-a1"), lines[0]


# --- W6-6: a Python repr reaches every named sink -----------------------------------------------------------

QUOTED = "O'Neil \"Client\" Secret"


def test_fc6_apply_at_local_prints_no_private_part_of_an_unreadable_target(tmp_path, capsys,
                                                                            monkeypatch):
    target = tmp_path / QUOTED / "deep" / "CLAUDE.md"

    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr(rules, "apply_rule", refuse)
    code, out, err = h.cli(capsys, "apply", "R-RETRY-SAFE", "--local", "--target", str(target))
    assert code == 1 and out == ""
    assert "Permission denied" in err, err
    for word in ("Client", "Secret", "Neil", "deep", "CLAUDE.md", str(tmp_path)):
        assert word not in err, (word, err)


def test_fc6_apply_at_share_prints_no_private_part_of_an_unreadable_target(tmp_path, capsys,
                                                                            monkeypatch):
    target = tmp_path / QUOTED / "deep" / "CLAUDE.md"

    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr(rules, "apply_rule", refuse)
    code, out, err = h.cli(capsys, "apply", "R-RETRY-SAFE", "--target", str(target))
    assert code == 1
    for word in ("Client", "Secret", "Neil", "deep", "CLAUDE.md", str(tmp_path)):
        assert word not in err, (word, err)


def _hook_payload():
    return {"hook_event_name": "Stop", "transcript_path": "x", "session_id": "s",
            "stop_hook_active": False}


def test_fc6_the_hook_check_debug_traceback_decodes_a_python_repr(monkeypatch, capsys, tmp_path):
    import io

    from agent_postmortem import reader as reader_module
    from agent_postmortem.cli import main
    from tests import test_hooks_cli as hooks_tests

    def boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied", "/srv/O'Neil \"Client\" Secret/deep/f.jsonl")

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(reader_module, "load_session_view", boom)
    monkeypatch.setenv("AGENT_POSTMORTEM_DEBUG", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hooks_tests._base_payload())))
    capsys.readouterr()
    assert main(["hook", "check"]) == 0
    err = capsys.readouterr().err
    assert "Permission denied" in err, err
    for word in ("Client", "Secret", "Neil", "deep", "f.jsonl"):
        assert word not in err, (word, err)


def test_fc6_the_hook_dispatcher_debug_traceback_decodes_a_python_repr(monkeypatch, capsys):
    import io

    from agent_postmortem import hooks_cli
    from agent_postmortem.cli import main

    def boom(_payload):
        raise OSError(13, "Permission denied", "/srv/O'Neil \"Client\" Secret/deep/f.jsonl")

    monkeypatch.setattr(hooks_cli, "_cmd_check", boom)
    monkeypatch.setenv("AGENT_POSTMORTEM_DEBUG", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_hook_payload())))
    capsys.readouterr()
    assert main(["hook", "check"]) == 0
    err = capsys.readouterr().err
    assert "Permission denied" in err, err
    for word in ("Client", "Secret", "Neil", "deep", "f.jsonl"):
        assert word not in err, (word, err)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
@pytest.mark.parametrize("level", ["share", "local"])
def test_fc6_a_reader_diagnostic_decodes_a_python_repr(level, tmp_path):
    run_dir = tmp_path / QUOTED / "wf_00000000-a1"
    shutil.copytree(FIXTURE, run_dir)
    journal = run_dir / "journal.jsonl"
    journal.chmod(0)
    try:
        run = reader.load_run(run_dir)
    finally:
        journal.chmod(0o600)
    unreadable = [d for d in run.diagnostics if "unreadable" in d]
    assert unreadable, run.diagnostics
    r = _redactor(level)
    r.fresh_names(detailed=(level == "local"))
    for diagnostic in unreadable:
        for shown in (report._diagnostic_for_export(diagnostic, r), r(diagnostic)):
            for word in ("Client", "Secret", "Neil", "wf_00000000-a1"):
                assert word not in shown, (word, shown)


# --- an end-to-end run whose label names a path with &, parentheses and a space ---------------------------


def _run_with_label(tmp_path, label):
    run_dir = tmp_path / "wf_00000000-a1"
    shutil.copytree(FIXTURE, run_dir)
    journal = run_dir / "journal.jsonl"
    journal.write_text(journal.read_text().replace("impl:alpha", label), encoding="utf-8")
    return run_dir


LABELS = [
    pytest.param("fix /srv/data/Zqa9&Zqb9/Zqc9.md", id="ampersand"),
    pytest.param("fix /srv/data/Zqa8 (Zqb8)/Zqc8/Zqd8.md", id="parentheses"),
    pytest.param("fix /srv/data/Zqa11 Zqb11.md", id="space_in_the_last_segment"),
]


def _assert_label_parts_hidden(label, flags, tmp_path, capsys):
    run_dir = _run_with_label(tmp_path, label)
    code, out, err = h.cli(capsys, "run", str(run_dir), *flags)
    assert code in (0, 1), err
    for word in ("Zqa", "Zqb", "Zqc", "Zqd"):
        assert word not in out, (word, out)
        assert word not in err, (word, err)
    assert "Traceback" not in err
    if "--local" in flags and "--json" not in flags:
        assert "fix path-" in out, out


@pytest.mark.parametrize("flags", [[], ["--json"]], ids=["share", "share_json"])
@pytest.mark.parametrize("label", LABELS)
def test_fc6_a_share_run_shows_none_of_the_parts_of_a_label_path(label, flags, tmp_path, capsys):
    _assert_label_parts_hidden(label, flags, tmp_path, capsys)


@pytest.mark.parametrize("flags", [["--local"], ["--local", "--json"]], ids=["local", "local_json"])
@pytest.mark.parametrize("label", LABELS)
def test_fc6_a_local_run_shows_none_of_the_parts_of_a_label_path(label, flags, tmp_path, capsys):
    _assert_label_parts_hidden(label, flags, tmp_path, capsys)


@pytest.mark.parametrize("flags", [["rescue", "plan"], ["rescue", "status"]])
@pytest.mark.parametrize("level", [[], ["--local"], ["--local", "--json"]])
def test_fc6_rescue_prints_none_of_the_parts_of_a_label_path(flags, level, tmp_path, capsys):
    run_dir = _run_with_label(tmp_path, "fix /srv/data/Zqa9&Zqb9/Zqc9.md")
    code, out, err = h.cli(capsys, *flags, str(run_dir), *level)
    for word in ("Zqa", "Zqb", "Zqc"):
        assert word not in out, (word, out)
        assert word not in err, (word, err)



# --- a path in one column does not hide the columns after it ----------------------------------------


def test_fc6_the_local_rescue_plan_keeps_every_column_of_a_paths_row(tmp_path, capsys):
    from tests import n3b_planted as plant

    planted = plant.build(tmp_path, observable=True)
    _code, out, _err = h.cli(capsys, "rescue", "plan", planted.run_dir, "--local")
    assert HIDDEN not in out, out
    row = next(line for line in out.splitlines() if line.strip().startswith("src/module.py"))
    assert row.split() == ["src/module.py", "DIRTY_NOW", "1", "no"], row
    assert "git status --short -- src/module.py" in out
    assert "already exists: 1 paths (src/module.py); commits reported: 0" in out


# --- the rescue plan scans each free-text piece where it enters the line ---------------------------------


def _unit(**fields):
    base = dict(
        key="impl", label="impl", phase="build", attempts_count=1, last_end_word="KILLED",
        task=None, reported=None, paths=[], commits=[], in_flight=[],
    )
    base.update(fields)
    return rescue.UnitPlan(**base)


def test_fc6_the_task_quote_the_claim_values_and_the_tool_name_are_scanned_as_pieces():
    from types import SimpleNamespace

    r = _redactor("local")
    r.fresh_names(detailed=True)
    view = SimpleNamespace(
        pointer=lambda name, line: f"{name}:{line}", show_excerpts=True, redactor=r,
    )
    task = rescue.TaskInfo("agent-x.jsonl", 1, "Fix /srv/data/Zqa7 Zqb7/Zqc7.md now", 40)
    lines = rescue._task_lines(_unit(task=task), view, r)
    assert lines[0] == f'  TASK      "Fix path-1{HIDDEN}"', lines
    shown = rescue._format_claim_value("note", "wrote /srv/data/Zqa8 Zqb8/Zqc8.md ok", r)
    assert shown == f"wrote path-2{HIDDEN}", shown
    flight = rescue.InFlightRow("/srv/data/Zqa9 Zqb9", None, None)
    lines = rescue._in_flight_lines(_unit(in_flight=[flight]), r)
    assert "Zq" not in "\n".join(lines), lines
    # share keeps the claim value scanned too
    share = _redactor("share")
    share.fresh_names()
    assert "Zq" not in rescue._format_claim_value("note", "x /srv/data/Zqa8 Zqb8/Zqc8.md", share)


def test_fc6_the_rescue_brief_and_status_keep_the_text_after_a_listed_path(monkeypatch):
    from types import SimpleNamespace

    from agent_postmortem.disclosure import DisclosureContext
    from agent_postmortem.redact import Redactor

    r = Redactor(context=DisclosureContext(level="local", home=Path("/home/user"),
                                           repo_root="/repo"))
    r.fresh_names(detailed=True)
    r.names.path("src/a.py", "/repo")
    r.names.path("src/b.py", "/repo")
    plan = SimpleNamespace(
        killed_brief=[], failed_brief=[], already_exists_paths=["src/a.py", "src/b.py"],
        already_exists_commits=[], fan_out_overlap=False,
    )
    brief = rescue._brief_text(plan, r)
    assert "already exists: 2 paths (src/a.py, src/b.py); commits reported: 0" in brief, brief
    # a declared check that names a listed relative path keeps the words after it on the status line
    run = reader.load_run(FIXTURE)
    unit = rescue.UnitStatus(
        key="impl", label="impl", checks_declared=1,
        last_counted=rescue.CountedRun(3, None, "pytest src/a.py -k x", "completed_ok"),
        word="EXIT_0_OBSERVED", writes_after=0, cycles=1,
    )
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[unit], not_counted=[], never_matches=[],
        transcript_unknown=False, checked=False, repo_root=None, exit_code=0,
    )
    monkeypatch.setattr(
        rescue, "_register_status_paths",
        lambda _run, _status, red: red.names.path("src/a.py", "/repo"),
    )
    local = Redactor(context=DisclosureContext(level="local", home=Path("/home/user"),
                                               repo_root="/repo"))
    text = rescue.render_status_text(run, status, local)
    assert "-> EXIT_0_OBSERVED" in text and "exit 0" in text, text


def test_fc6_rescue_prints_the_absolute_and_the_repo_relative_spelling_as_one_path_on_two_lines():
    run = reader.load_run(FIXTURE)
    unit = rescue.UnitStatus(
        key="impl", label="impl", checks_declared=0, last_counted=None, word="NOT_OBSERVED",
        writes_after=0, cycles=0,
    )
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[unit], not_counted=[],
        never_matches=[
            rescue.NeverRow(9, "python /repo/src/a.py", "git checkout"),
            rescue.NeverRow(10, "python src/a.py", "git checkout"),
        ],
        transcript_unknown=False, checked=True, repo_root="/repo", exit_code=0,
        listed_paths=("src/a.py",),
    )
    payload = rescue.status_to_json(run, status, _redactor("local"), evidence=True)
    shown = [row["excerpt"] for row in payload["never_matches"]]
    assert "/repo" not in shown[0]
    assert shown[0].split()[1] == shown[1].split()[1], shown
