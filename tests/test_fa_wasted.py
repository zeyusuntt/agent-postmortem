"""Part FA5 (field milestone F, T5): wasted time counts every attempt that returned no
result (docs/DESIGN.md 4.3 wasted_time row, 4.7 WASTED column and header sentence;
.reviews/v0.3/F-plan.md FA5).

The field case: a session subagent killed after about 70 minutes showed WASTED 0s,
because WASTED summed every attempt but the LAST and a killed subagent (or a workflow
key with no retry) is its own last attempt. One rule now serves the AGENT column's
WASTED, `wasted_time`'s `wasted_seconds`, and the header sentence: the wall-clock of
the deduplicated union of every non-final attempt of a key and every attempt whose
outcome is killed, failed, or superseded at any position. A possibly-live open final
attempt is not counted.

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, detectors
from agent_postmortem.detectors.wasted_time import WastedTime
from agent_postmortem.models import Attempt, LogicalAgent, Run
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _kill,
    _no_config,
    _user,
    _wf_dir,
    _write_jsonl,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
SENTENCE = "was spent on retries and attempts that ended without a result."
OLD_SENTENCE = "was spent in attempts that returned no result."


def _span(agent, start_min, end_min, *, kill=False):
    """A transcript of one attempt from start to end (minutes after T0); a kill ends in the
    sentinel at `end_min`."""
    start, end = T0 + timedelta(minutes=start_min), T0 + timedelta(minutes=end_min)
    entries = [_user("work", start, agent), _assistant_text("working", start + timedelta(seconds=5), agent)]
    entries.append(_kill(end, agent) if kill else _assistant_text("done", end, agent))
    return entries


# --- workflow public fixtures ------------------------------------------------------------


def _workflow(tmp_path, monkeypatch, wf_id, journal, transcripts):
    _no_config(monkeypatch, tmp_path)
    run_dir = _wf_dir(tmp_path / "root", wf_id)
    _write_jsonl(run_dir / "journal.jsonl", journal)
    for agent, entries in transcripts.items():
        _write_jsonl(run_dir / f"agent-{agent}.jsonl", entries)
    return run_dir


def _started(key, agent, label="impl"):
    return {"type": "started", "key": key, "agentId": agent, "label": label, "phase": "Build"}


def _result(key, agent):
    return {"type": "result", "key": key, "agentId": agent, "result": {"ok": True}}


def _failed(key, agent):
    return {"type": "failed", "key": key, "agentId": agent, "error": "boom"}


def _run_text(run_dir, capsys, *flags):
    cli.main(["run", str(run_dir), "--no-redact", *flags])
    return capsys.readouterr().out


def _row(out, label):
    return next(line for line in out.splitlines() if line.startswith(label))


def _metric(run_dir, capsys, name="wasted_seconds"):
    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    (finding,) = [f for f in payload["findings"] if f["detector"] == "wasted_time"]
    return finding["metrics"][name]


def test_fa5_workflow_key_whose_only_attempt_was_killed_counts_it(tmp_path, monkeypatch, capsys):
    a = _aid(0x50)
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-fa7", [_started("v2:k", a)],
                        {a: _span(a, 0, 70, kill=True)})
    out = _run_text(run_dir, capsys)
    assert _row(out, "impl").split()[-1] == "1h10m"
    assert f"1h10m {SENTENCE}" in out
    assert OLD_SENTENCE not in out
    assert _metric(run_dir, capsys) == 4200.0


def test_fa5_a_completed_final_attempt_never_counts(tmp_path, monkeypatch, capsys):
    a = _aid(0x51)
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-fa8",
                        [_started("v2:k", a), _result("v2:k", a)], {a: _span(a, 0, 70)})
    out = _run_text(run_dir, capsys)
    assert _row(out, "impl").split()[-1] == "0s"
    assert SENTENCE not in out and OLD_SENTENCE not in out
    assert _metric(run_dir, capsys) == 0.0


def test_fa5_a_non_final_completed_attempt_counts_as_a_retry(tmp_path, monkeypatch, capsys):
    first, second = _aid(0x52), _aid(0x53)
    journal = [_started("v2:k", first), _result("v2:k", first), _started("v2:k", second),
               _result("v2:k", second)]
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-fa9", journal,
                        {first: _span(first, 0, 30), second: _span(second, 31, 50)})
    out = _run_text(run_dir, capsys)
    assert _row(out, "impl").split()[-1] == "30m"
    assert f"30m {SENTENCE}" in out
    assert _metric(run_dir, capsys) == 1800.0


def test_fa5_a_non_final_killed_attempt_counts_once(tmp_path, monkeypatch, capsys):
    first, second = _aid(0x54), _aid(0x55)
    journal = [_started("v2:k", first), _started("v2:k", second), _result("v2:k", second)]
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-faa", journal,
                        {first: _span(first, 0, 40, kill=True), second: _span(second, 41, 60)})
    out = _run_text(run_dir, capsys)
    assert _row(out, "impl").split()[-1] == "40m"
    assert _metric(run_dir, capsys) == 2400.0


def test_fa5_a_final_failed_attempt_counts(tmp_path, monkeypatch, capsys):
    a = _aid(0x56)
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-fab",
                        [_started("v2:k", a), _failed("v2:k", a)], {a: _span(a, 0, 25)})
    out = _run_text(run_dir, capsys)
    assert _row(out, "impl").split()[-1] == "25m"
    assert f"25m {SENTENCE}" in out
    assert _metric(run_dir, capsys) == 1500.0


def test_fa5_a_final_killed_after_a_failed_retry_counts_both_once(tmp_path, monkeypatch, capsys):
    first, second = _aid(0x57), _aid(0x58)
    journal = [_started("v2:k", first), _failed("v2:k", first), _started("v2:k", second)]
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-fac", journal,
                        {first: _span(first, 0, 20), second: _span(second, 21, 51, kill=True)})
    out = _run_text(run_dir, capsys)
    assert _row(out, "impl").split()[-1] == "50m"
    assert _metric(run_dir, capsys) == 3000.0


def test_fa5_a_possibly_live_open_final_attempt_is_not_counted(tmp_path, monkeypatch, capsys):
    a = _aid(0x59)
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-fad", [_started("v2:k", a)],
                        {a: _span(a, 0, 45)})
    out = _run_text(run_dir, capsys)
    assert _row(out, "impl").split()[-1] == "0s"
    assert SENTENCE not in out
    assert _metric(run_dir, capsys) == 0.0


def test_fa5_scan_workflow_totals_follow_wasted_time(tmp_path, monkeypatch, capsys):
    a = _aid(0x5A)
    run_dir = _workflow(tmp_path, monkeypatch, "wf_00000000-fae", [_started("v2:k", a)],
                        {a: _span(a, 0, 60, kill=True)})
    cli.main(["scan", "--root", str(tmp_path / "root"), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["workflow"]["wasted_hours"] == pytest.approx(1.0)
    assert run_dir.exists()


# --- session public fixture: the field case ----------------------------------------------


def _session(tmp_path, monkeypatch, children):
    """A session whose subagents' transcripts are `children` (agent id -> entries)."""
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    uuid = "00000000-0000-4000-8000-000000000000"
    main = root / "-home-user-project" / f"{uuid}.jsonl"
    _write_jsonl(main, [
        {**entry, "sessionId": uuid}
        for entry in (
            _user("Plan the work.", T0),
            _assistant_text("Delegating.", T0 + timedelta(minutes=1)),
            _assistant_text("Waiting.", T0 + timedelta(minutes=90)),
        )
    ])
    for agent, entries in children.items():
        _write_jsonl(main.parent / uuid / "subagents" / f"agent-{agent}.jsonl", entries)
    return main


def test_fa5_a_session_subagent_killed_after_70_minutes_shows_1h10m(tmp_path, monkeypatch, capsys):
    a = _aid(0x5B)
    main = _session(tmp_path, monkeypatch, {a: _span(a, 2, 72, kill=True)})
    out = _run_text(main, capsys)
    assert f"1h10m {SENTENCE}" in out
    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("AGENT"))
    assert lines[header + 1].split()[-1] == "1h10m"
    assert _metric(main, capsys) == 4200.0


def test_fa5_a_session_subagent_that_finished_does_not_count(tmp_path, monkeypatch, capsys):
    a = _aid(0x5C)
    main = _session(tmp_path, monkeypatch, {a: _span(a, 2, 72)})
    out = _run_text(main, capsys)
    assert SENTENCE not in out
    assert _metric(main, capsys) == 0.0


def test_fa5_session_wasted_time_metrics_keep_the_others_unchanged(tmp_path, monkeypatch, capsys):
    a = _aid(0x5D)
    main = _session(tmp_path, monkeypatch, {a: _span(a, 2, 72, kill=True)})
    cli.main(["run", str(main), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    (finding,) = [f for f in payload["findings"] if f["detector"] == "wasted_time"]
    assert list(finding["metrics"]) == ["run_seconds", "child_seconds", "child_count", "wasted_seconds"]
    assert finding["metrics"]["child_count"] == 1
    assert finding["metrics"]["child_seconds"] == 4200.0


# --- model level: the detector over hand-built runs, every outcome at every position ------


def _attempt(name, minutes, outcome, ordinal=1, tokens=0):
    start = T0 + timedelta(minutes=ordinal * 1000)
    return Attempt(
        agent_id=name, key="k", label="l", phase="p", agent_type=None, meta={},
        transcript_path=Path(f"agent-{name}.jsonl"), events=[], first_ts=start,
        last_ts=start + timedelta(minutes=minutes), ordinal=ordinal, outcome=outcome,
        usage={"output_tokens": tokens},
    )


def _hand_run(kind, groups):
    agents = [
        LogicalAgent(key=f"k{i}", label=f"l{i}", phase="p", attempts=attempts)
        for i, attempts in enumerate(groups)
    ]
    return Run(
        run_id="wf_00000000-faf", kind=kind, path=Path("."), project_slug="s", session_id=None,
        claude_version=None, journal=[], agents=agents,
        attempts=[a for attempts in groups for a in attempts],
        started_at=T0, ended_at=T0 + timedelta(hours=10), diagnostics=[],
    )


def _wasted(run):
    (finding,) = WastedTime().run(run)
    return finding.metrics["wasted_seconds"]


def _final_rows():
    rows = []
    for kind in ("workflow", "session"):
        for outcome, counted in (
            ("killed", True), ("failed", True), ("superseded", True),
            ("completed", False), ("unknown", False),
        ):
            rows.append(pytest.param(kind, outcome, counted))
    return rows


@pytest.mark.parametrize("kind,outcome,counted", _final_rows())
def test_fa5_a_final_attempt_counts_by_its_outcome(kind, outcome, counted):
    run = _hand_run(kind, [[_attempt("a", 10, outcome)]])
    assert _wasted(run) == (600.0 if counted else 0.0)


@pytest.mark.parametrize("outcome", ["completed", "unknown", "killed", "failed", "superseded"])
def test_fa5_a_non_final_attempt_counts_at_any_outcome(outcome):
    run = _hand_run("workflow", [[_attempt("a", 10, outcome), _attempt("b", 5, "completed", 2)]])
    assert _wasted(run) == 600.0


def test_fa5_each_attempt_is_counted_once_across_both_rules():
    """A non-final killed attempt is in both rules, and a final killed is only in the second."""
    run = _hand_run("workflow", [
        [_attempt("a", 10, "killed"), _attempt("b", 7, "killed", 2)],
        [_attempt("c", 3, "failed"), _attempt("d", 4, "completed", 2)],
    ])
    assert _wasted(run) == (10 + 7 + 3) * 60.0


def test_fa5_the_workflow_title_and_tokens_follow_the_same_set():
    run = _hand_run("workflow", [[_attempt("a", 10, "completed", tokens=100),
                                   _attempt("b", 5, "killed", 2, tokens=40)]])
    (finding,) = WastedTime().run(run)
    assert finding.metrics["wasted_seconds"] == 900.0
    assert finding.metrics["wasted_tokens"] == 140
    assert finding.title == (
        "run spent 10h00m wall-clock; 15m wasted across 1 retried attempts and "
        "1 final attempt that ended without a result"
    )


def test_fa5_the_workflow_title_is_unchanged_when_no_final_attempt_is_extra():
    run = _hand_run("workflow", [[_attempt("a", 10, "completed"), _attempt("b", 5, "completed", 2)]])
    (finding,) = WastedTime().run(run)
    assert finding.title == "run spent 10h00m wall-clock; 10m wasted across 1 retried attempts"


def test_fa5_session_metrics_gain_wasted_seconds_last():
    run = _hand_run("session", [[_attempt("a", 10, "killed")], [_attempt("b", 4, "completed")]])
    (finding,) = WastedTime().run(run)
    assert finding.metrics == {
        "run_seconds": 36000.0, "child_seconds": 840.0, "child_count": 2, "wasted_seconds": 600.0,
    }


def test_fa5_detector_registry_still_has_wasted_time():
    assert "wasted_time" in {d.id for d in detectors.all_detectors()}
