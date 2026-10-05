"""Fix round 4 (W4, W5) of field milestone F: the one free-text path scanner reaches every output
that can carry a path (docs/DESIGN.md 5.1, 4.11; .reviews/v0.3/F-W-spec.md W4).

Rows: rescue excerpts (F-codex-r3 item 4) through text and JSON at local, the direct
`Pseudonyms.check()` values at local (spec review round 2 item 4), the direct Pseudonyms name
values at local, diagnostics, metrics, evidence lines, a hook debug traceback and a handler error.
Share keeps `check-N` and no excerpt; raw is unchanged.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest

from agent_postmortem import reader, report, rescue
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.models import Finding
from agent_postmortem.redact import Redactor
from tests import n3b_helpers as h

HOME = Path("/home/user")
FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "clean_run"
PRIVATE = ("Client", "Secret", "deep", "notes.md")


def _redactor(level):
    return Redactor(context=DisclosureContext(level=level, home=HOME))


def _status(excerpt, listed=()):
    run = reader.load_run(FIXTURE)
    unit = rescue.UnitStatus(
        key="impl", label="impl", checks_declared=0, last_counted=None, word="NOT_OBSERVED",
        writes_after=0, cycles=0,
    )
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[unit], not_counted=[],
        never_matches=[rescue.NeverRow(9, excerpt, "git checkout")],
        transcript_unknown=False, checked=False, repo_root=None, exit_code=0,
        listed_paths=tuple(listed),
    )
    return run, status


def _status_outputs(excerpt, listed):
    """(text, json text) of `rescue status --evidence` at local for one NEVER-list excerpt."""
    run, status = _status(excerpt, listed)
    text = rescue.render_status_text(run, status, _redactor("local"), evidence=True)
    payload = rescue.status_to_json(run, status, _redactor("local"), evidence=True)
    return text, json.dumps(payload)


# --- F-codex-r3 item 4: rescue excerpts at local ---------------------------------------------------

EXCERPTS = [
    pytest.param(("/srv/data/A",), "cat /srv/data/A\\ Client/deep/notes.md now",
                 "cat path-2 [text hidden]", id="item4_escaped_space"),  # F fix 6 (W6-2)
    pytest.param(("/srv/data/A",), "cat /srv/data/A\\\"Client\\ Secret/deep/notes.md now",
                 None, id="escaped_double_quote"),
    pytest.param(("/srv/data/A",), "cat /srv/data/A/'Client Secret'/deep/notes.md now",
                 "cat path-2 [text hidden]", id="quoted_segment"),  # F fix 6 (W6-2)
    pytest.param(("/srv/data/A", "/srv/data/A/Client"), "cat /srv/data/A/Client\\ Secret/deep/x",
                 None, id="prefix_related_listed_paths"),
    pytest.param(("/srv/data/A", "/srv/data/B"), "cp /srv/data/A\\ Client/p /srv/data/B\\ Secret/q",
                 "cp path-3 [text hidden]",  # F fix 6 (W6-2): the second path is in the span
                 id="two_listed_paths_in_one_excerpt"),
    pytest.param(("/srv/data/A",), "cat '/srv/data/A'\"'\"'Client Secret/deep/notes.md' now",
                 None, id="posix_splice"),
]


@pytest.mark.parametrize("listed,excerpt,expected", EXCERPTS)
def test_fc4_a_local_rescue_excerpt_shows_no_private_tail_in_text_and_json(
    listed, excerpt, expected
):
    text, payload = _status_outputs(excerpt, listed)
    for word in PRIVATE:
        assert word not in text, (word, text)
        assert word not in payload, (word, payload)
    if expected is not None:
        assert f"line 9: {expected}" in text, text
        assert expected in json.loads(payload)["never_matches"][0]["excerpt"], payload


def test_fc4_share_rescue_prints_no_excerpt_and_raw_prints_it_unchanged():
    excerpt = "cat /srv/data/A\\ Client/deep/notes.md now"
    run, status = _status(excerpt, ("/srv/data/A",))
    text = rescue.render_status_text(run, status, _redactor("share"), evidence=True)
    assert "Client" not in text and "notes.md" not in text
    payload = rescue.status_to_json(run, status, _redactor("share"), evidence=True)
    assert "excerpt" not in payload["never_matches"][0]
    raw = Redactor(context=DisclosureContext(level="raw", home=HOME))
    payload = rescue.status_to_json(run, status, raw, evidence=True)
    assert payload["never_matches"][0]["excerpt"] == excerpt[:60]


# --- direct Pseudonyms.check() values at local ----------------------------------------------------

CHECK = "pytest /srv/data/Client\\ Secret/deep/notes.md -k x"


def _plan(declared_checks):
    run = reader.load_run(FIXTURE)
    unit = rescue.UnitPlan(
        key="impl", label="impl", phase="build", attempts_count=1, last_end_word="KILLED",
        task=None, reported=None, paths=[], commits=[], in_flight=[],
        repos=["/srv/data/repo"], declared_checks=list(declared_checks),
    )
    plan = rescue.RescuePlan(
        run_id=run.run_id, kind="workflow", finished=True, possibly_live=False,
        nothing_to_rescue=False, nothing_to_rescue_ok=False, units=[unit], checked=False,
        observation_reason=None, repo_root=None, config_warnings=[], killed_brief=[],
        failed_brief=[], already_exists_paths=[], already_exists_commits=[],
        has_unknown_fact=False,
    )
    return run, plan


def test_fc4_local_plan_json_scans_checks_steps_text_and_steps_command():
    run, plan = _plan([CHECK])
    payload = rescue.plan_to_json(run, plan, _redactor("local"))
    unit = payload["units"][0]
    blob = json.dumps(unit)
    for word in PRIVATE:
        assert word not in blob, (word, blob)
    assert unit["checks"] and "pytest" in unit["checks"][0]
    commands = [step["command"] for step in unit["steps"] if step["command"]]
    assert any("pytest" in command for command in commands)


def test_fc4_local_status_json_scans_last_counted_run_command():
    run = reader.load_run(FIXTURE)
    unit = rescue.UnitStatus(
        key="impl", label="impl", checks_declared=1,
        last_counted=rescue.CountedRun(3, None, CHECK, "completed_ok"),
        word="EXIT_0_OBSERVED", writes_after=0, cycles=1,
    )
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[unit], not_counted=[], never_matches=[],
        transcript_unknown=False, checked=False, repo_root=None, exit_code=0,
    )
    payload = rescue.status_to_json(run, status, _redactor("local"))
    blob = json.dumps(payload["units"][0]["last_counted_run"])
    for word in PRIVATE:
        assert word not in blob, (word, blob)
    assert "pytest" in blob
    text = rescue.render_status_text(run, status, _redactor("local"))
    for word in PRIVATE:
        assert word not in text, (word, text)


def test_fc4_share_checks_stay_check_n_and_raw_checks_stay_raw():
    run, plan = _plan([CHECK])
    payload = rescue.plan_to_json(run, plan, _redactor("share"))
    unit = payload["units"][0]
    assert unit["checks"] == ["check-1"]
    assert {step["command"] for step in unit["steps"] if step["command"]} >= {"check-1"}
    raw = Redactor(context=DisclosureContext(level="raw", home=HOME))
    payload = rescue.plan_to_json(run, plan, raw)
    assert payload["units"][0]["checks"] == [CHECK]


# --- direct Pseudonyms name values at local ---------------------------------------------------------


def test_fc4_a_local_name_with_an_escaped_path_shows_no_tail():
    r = _redactor("local")
    names = r.fresh_names(detailed=True)
    for shown in (
        names.agent("fix /srv/data/Client\\ Secret/deep/notes.md"),
        names.description("run '/srv/data/Client Secret/deep/notes.md'"),
        names.phase("/srv/data/'Client Secret'/deep/notes.md"),
        names.local_text("/srv/data/Client\\ Secret/deep/notes.md"),
    ):
        for word in PRIVATE:
            assert word not in shown, (word, shown)


# --- report sinks: diagnostics, metrics, evidence ------------------------------------------------------

HOSTILE = "see /srv/data/Client\\ Secret/'deep dir'/notes.md now"


@pytest.mark.parametrize("level", ["share", "local"])
def test_fc4_diagnostics_metrics_and_evidence_scan_a_hostile_word(level):
    r = _redactor(level)
    r.fresh_names(detailed=(level == "local"))
    shown = report._diagnostic_for_export(HOSTILE, r)
    for word in PRIVATE:
        assert word not in shown, (word, shown)
    finding = Finding(
        detector="silent_retry", severity="major", title=HOSTILE, run_id="wf_00000000-a1",
        agent_id=None, evidence=[HOSTILE], metrics={"command": HOSTILE, "nested": [HOSTILE]},
        rule_id=None,
    )
    exported = report._export_finding(finding, r, frozenset())
    blob = json.dumps(exported)
    for word in PRIVATE:
        assert word not in blob, (word, blob)


def test_fc4_the_local_evidence_view_scans_an_excerpt():
    run = reader.load_run(FIXTURE)
    r = _redactor("local")
    r.fresh_names(detailed=True)
    view = report.EvidenceView(run, r, show_excerpts=True)
    shown = view.line(f"agent-ac100000000000001.jsonl:1: {HOSTILE}")
    for word in PRIVATE:
        assert word not in shown, (word, shown)


# --- hook debug traceback and handler errors ---------------------------------------------------------


@pytest.mark.parametrize("level", ["share", "local"])
def test_fc4_a_debug_traceback_names_no_private_segment(level):
    try:
        raise RuntimeError("cannot open /srv/data/Client\\ Secret/deep/notes.md now")
    except RuntimeError:
        scrubbed = _redactor(level).scrub_leftover_paths(traceback.format_exc())
    for word in PRIVATE:
        assert word not in scrubbed, (word, scrubbed)


@pytest.mark.parametrize("flag", [[], ["--local"]])
def test_fc4_a_handler_error_echoes_no_private_segment(flag, tmp_path, capsys):
    hostile = tmp_path / "Client Secret" / "deep" / "notes.md"
    code, out, err = h.cli(capsys, "run", str(hostile), "--root", str(tmp_path), *flag)
    assert code == 2 and out == ""
    for word in PRIVATE:
        assert word not in err, (word, err)


def test_fc4_the_hook_dispatcher_debug_traceback_names_no_private_segment(monkeypatch, capsys):
    import io

    from agent_postmortem import hooks_cli
    from agent_postmortem.cli import main

    def boom(_payload):
        raise RuntimeError("cannot open /srv/data/Client\\ Secret/deep/notes.md and '/srv/a/b c'")

    monkeypatch.setattr(hooks_cli, "_cmd_check", boom)
    monkeypatch.setenv("AGENT_POSTMORTEM_DEBUG", "1")
    payload = {"hook_event_name": "Stop", "transcript_path": "x", "session_id": "s",
               "stop_hook_active": False}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    assert main(["hook", "check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err and "cannot open" in captured.err
    for word in PRIVATE:
        assert word not in captured.err, (word, captured.err)


def test_fc4_rescue_prints_the_absolute_and_the_repo_relative_spelling_as_one_path():
    run = reader.load_run(FIXTURE)
    unit = rescue.UnitStatus(
        key="impl", label="impl", checks_declared=0, last_counted=None, word="NOT_OBSERVED",
        writes_after=0, cycles=0,
    )
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[unit], not_counted=[],
        never_matches=[rescue.NeverRow(9, "python /repo/src/a.py src/a.py", "git checkout")],
        transcript_unknown=False, checked=True, repo_root="/repo", exit_code=0,
        listed_paths=("src/a.py",),
    )
    payload = rescue.status_to_json(run, status, _redactor("local"), evidence=True)
    shown = payload["never_matches"][0]["excerpt"]
    assert "/repo" not in shown
    # F fix 6 (W6-2): the absolute spelling hides the rest of its line, the relative one with it
    # (the two spellings as one path are pinned in test_fc_fix6_sinks.py)
    assert shown == "python path-1 [text hidden]", shown


def test_fc4_local_plan_text_scans_the_checks_line_and_the_run_check_step():
    run, plan = _plan([CHECK])
    text = rescue.render_plan_text(run, plan, _redactor("local"))
    for word in PRIVATE:
        assert word not in text, (word, text)
    assert "pytest" in text
    share = rescue.render_plan_text(run, plan, _redactor("share"))
    assert "check-1" in share and "pytest" not in share


def test_fc4_status_last_counted_run_is_check_n_at_share_and_raw_at_raw():
    run = reader.load_run(FIXTURE)
    unit = rescue.UnitStatus(
        key="impl", label="impl", checks_declared=1,
        last_counted=rescue.CountedRun(3, None, CHECK, "completed_ok"),
        word="EXIT_0_OBSERVED", writes_after=0, cycles=1,
    )
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[unit], not_counted=[], never_matches=[],
        transcript_unknown=False, checked=False, repo_root=None, exit_code=0,
    )
    shared = rescue.status_to_json(run, status, _redactor("share"))
    assert shared["units"][0]["last_counted_run"]["command"] == "check-1"
    raw = Redactor(context=DisclosureContext(level="raw", home=HOME))
    assert rescue.status_to_json(run, status, raw)["units"][0]["last_counted_run"]["command"] == CHECK
