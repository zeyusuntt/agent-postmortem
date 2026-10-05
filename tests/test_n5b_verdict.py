"""N5b: the verdict word and the exit codes (docs/DESIGN.md 4.6, 4.13.4, S3).

FAILED when the run's own lifecycle terminal is `failed` or any finding is
critical; DEGRADED when any finding is major; CLEAN otherwise; ` (INCOMPLETE)`
appended when the status says so. Exit 3 means verdict FAILED, 4 means
INCOMPLETE without FAILED, 3 wins when both apply, for run, demo, and scan.
"""

from __future__ import annotations

import ast
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

from agent_postmortem import cli, detectors, hooks_cli, reader, report, watch
from agent_postmortem.models import Finding, Lifecycle
from tests.n5b_helpers import (
    BASE,
    Spec,
    build_session,
    build_workflow,
    model_attempt,
    model_session_run,
    model_workflow_run,
)
from tests.test_field_v03 import _assistant_text, _kill, _user

SRC = Path(__file__).resolve().parent.parent / "src" / "agent_postmortem"


def _finding(severity: str, detector: str = "silent_retry") -> Finding:
    return Finding(
        detector=detector, severity=severity, title="t", run_id="wf_00000000-b00",
        agent_id=None, evidence=[], metrics={}, rule_id=None,
    )


# --- the verdict function ---------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["completed", "stopped", "journal_resolved", "unknown", "none"])
def test_a_lifecycle_that_is_not_failed_does_not_make_the_verdict_failed(terminal):
    run = model_workflow_run([model_attempt(1)], terminal=terminal)
    assert report.verdict_of(run, []) == "CLEAN"
    assert report.verdict_of(run, [_finding("major")]) == "DEGRADED"


def test_a_failed_lifecycle_is_failed_with_no_findings_and_with_any_findings():
    run = model_workflow_run([model_attempt(1)], terminal="failed")
    assert report.verdict_of(run, []) == "FAILED"
    assert report.verdict_of(run, [_finding("info")]) == "FAILED"
    assert report.verdict_of(run, [_finding("major")]) == "FAILED"


def test_a_reserved_critical_finding_wins_without_a_failed_lifecycle():
    run = model_workflow_run([model_attempt(1)], terminal="completed")
    assert report.verdict_of(run, [_finding("critical")]) == "FAILED"
    assert report.verdict_of(run, [_finding("critical"), _finding("major")]) == "FAILED"


def test_major_is_degraded_and_minor_and_info_are_clean():
    run = model_workflow_run([model_attempt(1)], terminal="completed")
    assert report.verdict_of(run, [_finding("major"), _finding("minor")]) == "DEGRADED"
    assert report.verdict_of(run, [_finding("minor")]) == "CLEAN"
    assert report.verdict_of(run, [_finding("info"), _finding("minor")]) == "CLEAN"


def test_the_incomplete_suffix_follows_the_status_for_every_word():
    for terminal, findings, word in [
        ("completed", [], "CLEAN"), ("completed", [_finding("major")], "DEGRADED"),
        ("failed", [], "FAILED"),
    ]:
        run = model_workflow_run([model_attempt(1)], terminal=terminal, status="INCOMPLETE")
        assert report.verdict_of(run, findings) == f"{word} (INCOMPLETE)"


def test_a_session_run_has_no_lifecycle_and_never_reaches_failed_through_major_findings():
    run = model_session_run([model_attempt(1, key="agent")])
    assert run.lifecycle is None
    assert report.verdict_of(run, [_finding("major")]) == "DEGRADED"
    assert report.verdict_of(run, []) == "CLEAN"
    assert report.verdict_of(run, [_finding("critical")]) == "FAILED"


def test_is_failed_is_the_exit_three_condition():
    failed = model_workflow_run([model_attempt(1)], terminal="failed")
    fine = model_workflow_run([model_attempt(1)], terminal="completed")
    assert report.is_failed(failed, []) is True
    assert report.is_failed(fine, [_finding("major")]) is False
    assert report.is_failed(fine, [_finding("critical")]) is True


def test_verdict_word_takes_the_failed_terminal_as_an_explicit_argument():
    assert report.verdict_word([], "COMPLETE", terminal_failed=True) == "FAILED"
    assert report.verdict_word([], "INCOMPLETE", terminal_failed=True) == "FAILED (INCOMPLETE)"
    assert report.verdict_word([_finding("major")], "COMPLETE", terminal_failed=False) == "DEGRADED"
    assert report.verdict_word([_finding("major")], "COMPLETE") == "DEGRADED"


def test_verdict_word_still_reads_findings_and_status_alone():
    assert report.verdict_word([]) == "CLEAN"
    assert report.verdict_word([_finding("critical")]) == "FAILED"
    assert report.verdict_word([_finding("major")], "INCOMPLETE") == "DEGRADED (INCOMPLETE)"


def _verdict_word_calls():
    calls = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "verdict_word":
                calls.append((path.name, node))
    return calls


def test_no_caller_reads_the_verdict_from_findings_and_status_alone_when_it_has_a_run():
    # Every call to verdict_word passes the failed terminal explicitly, except
    # the two that have no run to ask (a unit that could not be analyzed at
    # all: no findings, an empty list literal).
    calls = _verdict_word_calls()
    assert calls, "no verdict_word call found"
    for filename, node in calls:
        keywords = {keyword.arg for keyword in node.keywords}
        first = node.args[0] if node.args else None
        empty_literal = isinstance(first, ast.List) and not first.elts
        assert "terminal_failed" in keywords or empty_literal, (
            f"{filename}:{node.lineno} verdict_word without terminal_failed"
        )


# --- run: text, JSON, exit codes ----------------------------------------------------------------------


def _failed_run(tmp_path, monkeypatch, suffix="b50", *, incomplete=False, slug=None):
    kwargs = {} if slug is None else {"slug": slug}
    run_dir = build_workflow(
        tmp_path, monkeypatch, f"wf_00000000-{suffix}", [Spec("v2:x", 1)], parent="failed",
        **kwargs,
    )
    if incomplete:
        journal = run_dir / "journal.jsonl"
        journal.write_text(journal.read_text() + "{not json\n", encoding="utf-8")
    return run_dir


def _clean_run(tmp_path, monkeypatch, suffix="b51", *, incomplete=False, slug=None):
    kwargs = {} if slug is None else {"slug": slug}
    run_dir = build_workflow(
        tmp_path, monkeypatch, f"wf_00000000-{suffix}", [Spec("v2:x", 1)], parent="completed",
        **kwargs,
    )
    if incomplete:
        journal = run_dir / "journal.jsonl"
        journal.write_text(journal.read_text() + "{not json\n", encoding="utf-8")
    return run_dir


def _run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_run_of_a_failed_lifecycle_is_failed_in_the_header_and_exits_3(tmp_path, monkeypatch, capsys):
    run_dir = _failed_run(tmp_path, monkeypatch)
    code, out = _run_cli(capsys, "run", str(run_dir))
    assert out.splitlines()[0].split("  ")[1] == "FAILED"
    assert code == 3


def test_run_json_verdict_is_failed_for_a_failed_lifecycle(tmp_path, monkeypatch, capsys):
    run_dir = _failed_run(tmp_path, monkeypatch)
    code, out = _run_cli(capsys, "run", str(run_dir), "--json")
    payload = json.loads(out)
    assert payload["verdict"] == "FAILED"
    assert payload["status"] == "COMPLETE"
    assert code == 3


def test_run_failed_and_incomplete_exits_3_not_4(tmp_path, monkeypatch, capsys):
    run_dir = _failed_run(tmp_path, monkeypatch, incomplete=True)
    code, out = _run_cli(capsys, "run", str(run_dir))
    assert out.splitlines()[0].split("  ")[1] == "FAILED (INCOMPLETE)"
    assert code == 3


def test_run_incomplete_only_exits_4(tmp_path, monkeypatch, capsys):
    run_dir = _clean_run(tmp_path, monkeypatch, incomplete=True)
    code, out = _run_cli(capsys, "run", str(run_dir))
    assert out.splitlines()[0].split("  ")[1] == "CLEAN (INCOMPLETE)"
    assert code == 4


def test_run_clean_exits_0(tmp_path, monkeypatch, capsys):
    run_dir = _clean_run(tmp_path, monkeypatch)
    code, out = _run_cli(capsys, "run", str(run_dir))
    assert out.splitlines()[0].split("  ")[1] == "CLEAN"
    assert code == 0


def test_run_degraded_and_complete_exits_0(tmp_path, monkeypatch, capsys):
    specs = [Spec("v2:x", 1, journal="failed")]
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-b52", specs)
    code, out = _run_cli(capsys, "run", str(run_dir))
    assert out.splitlines()[0].split("  ")[1] == "DEGRADED"
    assert code == 0


def test_silencing_workflow_failed_in_the_config_cannot_unfail_the_run(
    tmp_path, monkeypatch, capsys,
):
    run_dir = _failed_run(tmp_path, monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[detectors]\noff = ["workflow_failed"]\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    code, out = _run_cli(capsys, "run", str(run_dir))
    assert out.splitlines()[0].split("  ")[1] == "FAILED"
    assert code == 3


def test_a_loaded_session_with_a_major_finding_is_degraded_never_failed(tmp_path):
    def events(aid):
        return [
            _user("work", BASE + timedelta(seconds=5), aid),
            _assistant_text("working", BASE + timedelta(seconds=6), aid),
            _kill(BASE + timedelta(seconds=700), aid),
        ]

    main, _ = build_session(tmp_path, events)
    run = reader.load_session(main)
    findings = detectors.run_detectors(run)
    assert any(f.severity == "major" for f in findings)
    assert report.verdict_of(run, findings) == "DEGRADED"
    assert report.is_failed(run, findings) is False


# --- demo ------------------------------------------------------------------------------------------------


def _patch_demo_run(monkeypatch, *, terminal=None, status=None):
    real = reader.load_run

    def load(path, *args, **kwargs):
        run = real(path, *args, **kwargs)
        if terminal is not None:
            run.lifecycle = Lifecycle(
                terminal=terminal, source="notification", activity="stale", generations=1,
                diagnostics=[],
            )
        if status is not None:
            run.status = status
        return run

    monkeypatch.setattr(reader, "load_run", load)


def test_demo_exits_0_when_complete_and_not_failed(monkeypatch, capsys):
    _patch_demo_run(monkeypatch, terminal="completed", status="COMPLETE")
    code, _out = _run_cli(capsys, "demo")
    assert code == 0


def test_demo_incomplete_only_exits_4(monkeypatch, capsys):
    _patch_demo_run(monkeypatch, terminal="completed", status="INCOMPLETE")
    code, _out = _run_cli(capsys, "demo")
    assert code == 4


def test_demo_failed_exits_3_and_wins_over_incomplete(monkeypatch, capsys):
    _patch_demo_run(monkeypatch, terminal="failed", status="COMPLETE")
    code, out = _run_cli(capsys, "demo")
    assert out.splitlines()[0].split("  ")[1] == "FAILED"
    assert code == 3
    _patch_demo_run(monkeypatch, terminal="failed", status="INCOMPLETE")
    code, out = _run_cli(capsys, "demo")
    assert code == 3
    assert out.splitlines()[0].split("  ")[1] == "FAILED (INCOMPLETE)"


# --- scan -----------------------------------------------------------------------------------------------


SECOND = "-home-user-second"


def _scan(capsys, tmp_path):
    code, out = _run_cli(capsys, "scan", "--root", str(tmp_path / "root"), "--json", "--all")
    rows = json.loads(out)["rows"]
    return code, {row["run_id"]: row["verdict"] for row in rows if row["run_id"].startswith("wf_")}


def test_scan_failed_row_exits_3_even_beside_an_incomplete_row(tmp_path, monkeypatch, capsys):
    _failed_run(tmp_path, monkeypatch, "b60")
    _clean_run(tmp_path, monkeypatch, "b61", incomplete=True, slug=SECOND)
    code, verdicts = _scan(capsys, tmp_path)
    assert verdicts == {"wf_00000000-b60": "FAILED", "wf_00000000-b61": "CLEAN (INCOMPLETE)"}
    assert code == 3


def test_scan_failed_and_incomplete_in_one_row_exits_3(tmp_path, monkeypatch, capsys):
    _failed_run(tmp_path, monkeypatch, "b62", incomplete=True)
    code, verdicts = _scan(capsys, tmp_path)
    assert verdicts == {"wf_00000000-b62": "FAILED (INCOMPLETE)"}
    assert code == 3


def test_scan_incomplete_without_failed_exits_4(tmp_path, monkeypatch, capsys):
    _clean_run(tmp_path, monkeypatch, "b63", incomplete=True)
    code, verdicts = _scan(capsys, tmp_path)
    assert verdicts == {"wf_00000000-b63": "CLEAN (INCOMPLETE)"}
    assert code == 4


def test_scan_clean_fleet_exits_0(tmp_path, monkeypatch, capsys):
    _clean_run(tmp_path, monkeypatch, "b64")
    code, verdicts = _scan(capsys, tmp_path)
    assert verdicts == {"wf_00000000-b64": "CLEAN"}
    assert code == 0


def test_scan_ranks_and_flags_a_failed_lifecycle_run_as_needing_attention(
    tmp_path, monkeypatch, capsys,
):
    _failed_run(tmp_path, monkeypatch, "b65")
    code, out = _run_cli(capsys, "scan", "--root", str(tmp_path / "root"), "--json")
    rows = json.loads(out)["rows"]
    # rows are ordered by day and start time (release review of 0.3.0); the parent session starts
    # before the workflow it launched, so the workflow row is found by kind, not by position
    workflow_rows = [row for row in rows if row["kind"] == "workflow"]
    assert len(workflow_rows) == 1
    assert workflow_rows[0]["verdict"] == "FAILED"
    assert workflow_rows[0]["attention"] is True
    assert code == 3


# --- watch and the hooks follow the same verdict -------------------------------------------------------------


def test_watch_done_reports_failed_for_a_failed_lifecycle_and_exits_3(tmp_path, monkeypatch):
    run_dir = _failed_run(tmp_path, monkeypatch, "b70")
    out = io.StringIO()
    code = watch._emit_done(run_dir, True, out)
    assert json.loads(out.getvalue())["verdict"] == "FAILED"
    assert code == 3


def test_watch_done_reports_clean_for_a_completed_run(tmp_path, monkeypatch):
    run_dir = _clean_run(tmp_path, monkeypatch, "b71")
    out = io.StringIO()
    code = watch._emit_done(run_dir, True, out)
    assert json.loads(out.getvalue())["verdict"] == "CLEAN"
    assert code == 0


def test_the_hook_analysis_of_a_failed_workflow_carries_the_failed_terminal(
    tmp_path, monkeypatch,
):
    run_dir = _failed_run(tmp_path, monkeypatch, "b72")
    *_rest, failed = hooks_cli._analyze_finished_workflow(run_dir)
    assert failed is True
    clean_dir = _clean_run(tmp_path / "c", monkeypatch, "b73")
    *_rest, failed = hooks_cli._analyze_finished_workflow(clean_dir)
    assert failed is False


def test_hook_check_stores_and_announces_failed_for_a_failed_workflow(
    tmp_path, monkeypatch, capsys,
):
    from tests.test_hooks_cli import _run as hook_run

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run_dir = _failed_run(tmp_path, monkeypatch, "b74")
    root = tmp_path / "root"
    parent = root / "-home-user-project" / "00000000-0000-4000-8000-000000000000.jsonl"
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(parent),
        "session_id": "00000000-0000-4000-8000-000000000000", "stop_hook_active": False,
    }
    code, out = hook_run(monkeypatch, capsys, "check", payload)
    assert code == 0
    message = json.loads(out)["systemMessage"]
    assert message.startswith("FAILED") or " FAILED " in f" {message} "
    assert run_dir.name in message



def test_the_default_hook_message_names_only_the_agent_whose_finding_is_major(
    tmp_path, monkeypatch, capsys,
):
    # wf_00000000-710: the first attempt's runtime kill is minor now, below the
    # default notify threshold (major); the silent_retry finding of the second
    # agent still notifies. The message names that one agent and says DEGRADED.
    from tests import test_n3b_hooks as hooks

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("AGENT_POSTMORTEM_NOTIFY", raising=False)
    _code, out, _err = hooks._hook(monkeypatch, capsys, "check", hooks._payload())
    message = json.loads(out)["systemMessage"]
    assert message.startswith("DEGRADED")
    assert f"agents a:{hooks._alias('agent', hooks.AGENT_RETRY)}" in message
    assert hooks._alias("agent", hooks.AGENT_KILLED) not in message
