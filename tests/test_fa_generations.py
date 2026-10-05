"""Part FA2 (field milestone F, T1): generations are bounded by the launch
`tool_use` timestamp, not by the launch result line (docs/DESIGN.md 4.1, 4.13.1;
.reviews/v0.3/F-plan.md FA2).

Why: in the real econ runs the first agent writes its first entry 0.33 to 0.47 s
BEFORE the parent records the launch result, so the first attempt of nearly
every workflow was unassignable and so always "at risk". An attempt that starts
after the Workflow call is placed; one that starts before the call is not.

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, ledger, reader
from agent_postmortem.models import Attempt, Generation, Lifecycle, Run
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _envelope,
    _no_config,
    _notification_entry,
    _parent_path,
    _user,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
CLEAN = {"journal": [], "parent": [], "transcripts": [], "meta": []}


def _ms(base: datetime, seconds: float) -> str:
    """An ISO timestamp with milliseconds (the real transcripts carry them)."""
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _at(base: datetime, seconds: float, entry: dict) -> dict:
    entry = dict(entry)
    entry["timestamp"] = _ms(base, seconds)
    return entry


def _index(tmp_path, entries):
    path = tmp_path / "parent.jsonl"
    _write_jsonl(path, entries)
    return reader.ParentIndex._scan(path), path


# --- reader boundary: Generation.launch_ts ------------------------------------------------


def test_fa2_launch_ts_is_the_tool_use_entrys_exact_timestamp(tmp_path):
    index, _ = _index(tmp_path, [
        _at(T0, 0.250, _workflow_launch("toolu_A", T0)),
        _at(T0, 4.750, _workflow_launch_result("toolu_A", T0, "wf_00000000-fa1", "tA")),
    ])
    (generation,) = index.generations
    assert generation.launch_ts == T0 + timedelta(seconds=0.25)
    assert generation.result_ts == T0 + timedelta(seconds=4.75)
    assert generation.launch_ts.tzinfo is not None


def test_fa2_launch_ts_is_none_when_the_tool_use_entry_has_no_timestamp(tmp_path):
    launch = _workflow_launch("toolu_A", T0)
    del launch["timestamp"]
    index, _ = _index(tmp_path, [
        launch, _workflow_launch_result("toolu_A", T0 + timedelta(seconds=2), "wf_00000000-fa1", "tA"),
    ])
    (generation,) = index.generations
    assert generation.launch_ts is None
    assert generation.result_ts == T0 + timedelta(seconds=2)


@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-45T99:99:99Z", 12345, "", None, ["x"]])
def test_fa2_launch_ts_is_none_when_the_timestamp_does_not_parse(tmp_path, bad):
    launch = _workflow_launch("toolu_A", T0)
    launch["timestamp"] = bad
    index, _ = _index(tmp_path, [
        launch, _workflow_launch_result("toolu_A", T0 + timedelta(seconds=2), "wf_00000000-fa1", "tA"),
    ])
    (generation,) = index.generations
    assert generation.launch_ts is None


def test_fa2_launch_ts_comes_from_the_same_snapshot_as_the_lifecycle(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    wf_id = "wf_00000000-fa2"
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent = _aid(0x1A2)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent, "label": "impl", "phase": "Implement"},
        {"type": "result", "key": "v2:x", "agentId": agent, "result": {"ok": True}},
    ])
    _write_jsonl(run_dir / f"agent-{agent}.jsonl", [
        _user("go", T0 + timedelta(seconds=5), agent),
        _assistant_text("ok", T0 + timedelta(seconds=6), agent),
    ])
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=1), wf_id, "tA"),
    ])
    run = reader.load_run(run_dir)
    (snapshot,) = run.lifecycle._generations
    assert snapshot.launch_ts == T0
    with parent.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_workflow_launch("toolu_B", T0 + timedelta(minutes=1))) + "\n")
        handle.write(json.dumps(
            _workflow_launch_result("toolu_B", T0 + timedelta(minutes=2), wf_id, "tB")
        ) + "\n")
    refreshed = reader.ParentIndex.for_path(parent)
    assert [g.launch_ts for g in refreshed.generations_for(wf_id)] == [
        T0, T0 + timedelta(minutes=1),
    ]
    assert [g.launch_ts for g in run.lifecycle._generations] == [T0]


def test_fa2_generation_to_dict_carries_launch_ts_iso_or_null(tmp_path):
    index, _ = _index(tmp_path, [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=7), "wf_00000000-fa1", "tA"),
    ])
    assert index.generations[0].to_dict()["launch_ts"] == "2026-09-01T12:00:00+00:00"
    launch = _workflow_launch("toolu_B", T0, resume_from="wf_00000000-fa3")
    del launch["timestamp"]
    index, _ = _index(tmp_path, [launch])
    assert index.generations[0].to_dict()["launch_ts"] is None


def test_fa2_a_directly_built_generation_defaults_launch_ts_to_none():
    generation = Generation(
        launch_line=1, tool_use_id="t", resume_from=None, result_line=None, run_id="wf_x",
        task_id=None, launch_status=None, terminal=None, terminal_source=None,
        terminal_line=None, diagnostics=[],
    )
    assert generation.launch_ts is None


def test_fa2_a_result_line_physically_before_its_launch_keeps_both_timestamps(tmp_path):
    index, _ = _index(tmp_path, [
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=1), "wf_00000000-fa1", "tA"),
        _workflow_launch("toolu_A", T0 + timedelta(seconds=3)),
    ])
    (generation,) = index.generations
    assert generation.launch_line == 2 and generation.result_line == 1
    assert generation.launch_ts == T0 + timedelta(seconds=3)
    assert generation.result_ts == T0 + timedelta(seconds=1)


def test_fa2_a_result_less_resume_has_launch_ts_and_no_result_ts(tmp_path):
    index, _ = _index(tmp_path, [
        _at(T0, 2.5, _workflow_launch("toolu_A", T0, resume_from="wf_00000000-fa3")),
    ])
    (generation,) = index.generations
    assert generation.result_line is None and generation.result_ts is None
    assert generation.launch_ts == T0 + timedelta(seconds=2.5)


def test_fa2_a_malformed_resume_result_keeps_launch_ts_and_result_ts(tmp_path):
    bad = _workflow_launch_result("toolu_A", T0 + timedelta(seconds=9), "wf_00000000-fa3", "tA")
    del bad["toolUseResult"]["runId"]
    index, _ = _index(tmp_path, [
        _workflow_launch("toolu_A", T0 + timedelta(seconds=4), resume_from="wf_00000000-fa3"), bad,
    ])
    (generation,) = index.generations
    assert generation.terminal_source == "incomplete_generation"
    assert generation.launch_ts == T0 + timedelta(seconds=4)
    assert generation.result_ts == T0 + timedelta(seconds=9)


def test_fa2_lifecycle_ordering_stays_on_the_result_line(tmp_path):
    """Assignment moved to the launch call; lifecycle evidence did not. A
    notification between the launch call and the result line is not terminal
    evidence, one after the result line is."""
    before = [
        _workflow_launch("toolu_A", T0),
        _notification_entry(T0 + timedelta(seconds=1), _envelope("tA", "toolu_A", "failed")),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=2), "wf_00000000-fa1", "tA"),
    ]
    index, _ = _index(tmp_path, before)
    (generation,) = index.generations
    assert generation.terminal is None and generation.terminal_line is None
    after = [before[0], before[2], before[1]]
    index, _ = _index(tmp_path, after)
    (generation,) = index.generations
    assert generation.terminal == "failed" and generation.terminal_line == 3


# --- the model: ledger.generation_of / is_unassignable ------------------------------------


def _generation(n, launch_seconds, result_seconds=None, *, launch=True, launch_line=None):
    return Generation(
        launch_line=launch_line if launch_line is not None else n * 10,
        tool_use_id=f"toolu_{n}", resume_from=None, result_line=n * 10 + 1,
        run_id="wf_00000000-fa4", task_id="t", launch_status="async_launched",
        terminal="completed", terminal_source="notification", terminal_line=n * 10 + 2,
        diagnostics=[],
        result_ts=(
            T0 + timedelta(seconds=result_seconds) if result_seconds is not None else None
        ),
        launch_ts=T0 + timedelta(seconds=launch_seconds) if launch else None,
    )


def _attempt(first_seconds, name="a"):
    return Attempt(
        agent_id=name, key="k", label=name, phase="p", agent_type=None, meta={},
        transcript_path=Path(f"agent-{name}.jsonl"), events=[],
        first_ts=T0 + timedelta(seconds=first_seconds) if first_seconds is not None else None,
        last_ts=None, ordinal=1, outcome="completed", usage={}, interrupt=None,
        journal_state="completed",
    )


def _run(attempts, gens):
    lifecycle = Lifecycle(
        terminal="completed", source="notification", activity="stale",
        generations=len(gens), diagnostics=[],
    )
    lifecycle._generations = list(gens)
    return Run(
        run_id="wf_00000000-fa4", kind="workflow", path=Path("."), project_slug="s",
        session_id=None, claude_version=None, journal=[], agents=[], attempts=list(attempts),
        started_at=None, ended_at=None, diagnostics=[], lifecycle=lifecycle, discovery=dict(CLEAN),
    )


def test_fa2_an_attempt_between_the_launch_call_and_its_result_is_placed():
    run = _run([_attempt(0.6)], [_generation(1, 0.0, 1.0)])
    assert ledger.generation_of(run, run.attempts[0]).tool_use_id == "toolu_1"
    assert not ledger.is_unassignable(run, run.attempts[0])


def test_fa2_an_attempt_before_the_launch_call_is_unassignable():
    run = _run([_attempt(0.4)], [_generation(1, 0.5, 1.0)])
    assert ledger.generation_of(run, run.attempts[0]) is None
    assert ledger.is_unassignable(run, run.attempts[0])


def test_fa2_equality_with_the_launch_call_belongs():
    run = _run([_attempt(0.5)], [_generation(1, 0.5, 1.0)])
    assert ledger.generation_of(run, run.attempts[0]).tool_use_id == "toolu_1"


def test_fa2_two_generations_an_attempt_between_the_second_call_and_its_result_is_the_seconds():
    gens = [_generation(1, 0.0, 1.0), _generation(2, 100.0, 101.0)]
    run = _run([_attempt(100.3), _attempt(50.0, "b"), _attempt(99.9, "c")], gens)
    assert ledger.generation_of(run, run.attempts[0]).tool_use_id == "toolu_2"
    assert ledger.generation_of(run, run.attempts[1]).tool_use_id == "toolu_1"
    assert ledger.generation_of(run, run.attempts[2]).tool_use_id == "toolu_1"


def test_fa2_a_missing_launch_ts_makes_every_attempt_unassignable():
    gens = [_generation(1, 0.0, 1.0), _generation(2, 0.0, 101.0, launch=False)]
    run = _run([_attempt(10.0), _attempt(200.0, "b")], gens)
    assert all(ledger.is_unassignable(run, attempt) for attempt in run.attempts)


def test_fa2_a_non_increasing_launch_ts_makes_every_attempt_unassignable():
    gens = [_generation(1, 5.0, 6.0), _generation(2, 5.0, 7.0)]
    run = _run([_attempt(10.0), _attempt(200.0, "b")], gens)
    assert all(ledger.is_unassignable(run, attempt) for attempt in run.attempts)
    gens = [_generation(1, 9.0, 10.0), _generation(2, 5.0, 11.0)]
    run = _run([_attempt(20.0)], gens)
    assert ledger.is_unassignable(run, run.attempts[0])


def test_fa2_a_missing_result_ts_does_not_unassign_when_launch_ts_is_usable():
    """The missing-timestamp rule moved to launch_ts: a generation with no result
    timestamp (a result-less or malformed resume) no longer places nobody."""
    gens = [_generation(1, 0.0, None)]
    run = _run([_attempt(3.0)], gens)
    assert ledger.generation_of(run, run.attempts[0]).tool_use_id == "toolu_1"


def test_fa2_a_non_increasing_result_ts_is_irrelevant_when_launch_ts_increases():
    gens = [_generation(1, 0.0, 50.0), _generation(2, 10.0, 20.0)]
    run = _run([_attempt(12.0)], gens)
    assert ledger.generation_of(run, run.attempts[0]).tool_use_id == "toolu_2"


def test_fa2_an_attempt_with_no_first_ts_stays_unassignable():
    run = _run([_attempt(None)], [_generation(1, 0.0, 1.0)])
    assert ledger.is_unassignable(run, run.attempts[0])


# --- field_first_agent_before_launch_result (the real econ shape) -------------------------

FIELD_WF = "wf_00000000-fa5"


def _field_fixture(tmp_path, monkeypatch, *, first_offset=-0.33, committed_marker=False):
    """Four agents; the first writes its first entry 0.33 s before the launch
    result line and after the Workflow tool_use entry (the shape of all four
    real econ runs)."""
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    run_dir = _wf_dir(root, FIELD_WF)
    base = datetime.now(timezone.utc) - timedelta(minutes=20)
    launch_ts = base
    result_ts = base + timedelta(seconds=1.0)
    agents = [_aid(0x5A0 + n) for n in range(4)]
    journal = []
    for n, agent in enumerate(agents):
        journal.append({"type": "started", "key": f"v2:k{n}", "agentId": agent,
                        "label": f"card {n}", "phase": "Build"})
        journal.append({"type": "result", "key": f"v2:k{n}", "agentId": agent,
                        "result": {"ok": True}})
    _write_jsonl(run_dir / "journal.jsonl", journal)
    starts = [first_offset, 0.2, 0.3, 0.4]
    for n, agent in enumerate(agents):
        first = result_ts + timedelta(seconds=starts[n])
        _write_jsonl(run_dir / f"agent-{agent}.jsonl", [
            _at(first, 0.0, _user("Implement the card.", first, agent)),
            _at(first, 5.0, _assistant_text("Done.", first, agent)),
        ])
    _write_jsonl(_parent_path(root), [
        _at(launch_ts, 0.0, _workflow_launch("toolu_F", launch_ts)),
        _at(result_ts, 0.0, _workflow_launch_result("toolu_F", result_ts, FIELD_WF, "tF")),
        _notification_entry(base + timedelta(minutes=10), _envelope("tF", "toolu_F", "completed")),
    ])
    return run_dir


def test_field_first_agent_before_launch_result_model(tmp_path, monkeypatch):
    run_dir = _field_fixture(tmp_path, monkeypatch)
    run = reader.load_run(run_dir)
    (generation,) = run.lifecycle._generations
    first = run.attempts[0]
    assert generation.launch_ts < first.first_ts < generation.result_ts
    assert not ledger.is_unassignable(run, first)
    assert all(not ledger.is_unassignable(run, attempt) for attempt in run.attempts)
    assert run.risk_population_completeness == "complete"
    assert run.risk_population_completeness_reasons == []
    assert ledger.risk_population(run) == []


def test_field_first_agent_before_launch_result(tmp_path, monkeypatch, capsys):
    run_dir = _field_fixture(tmp_path, monkeypatch)
    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    risk_line = next(line for line in out.splitlines() if line.startswith("WORK AT RISK"))
    assert "none" in risk_line
    assert "could not be placed" not in out
    assert "unassignable" not in out
    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    ledgers = [attempt["ledger"] for attempt in payload["run"]["attempts"]]
    assert len(ledgers) == 4
    assert all(item["completeness"] == "complete" for item in ledgers)
    assert all(item["reasons"] == [] for item in ledgers)


def test_fa2_an_attempt_that_really_starts_before_the_launch_call_is_still_unassignable(
    tmp_path, monkeypatch, capsys
):
    run_dir = _field_fixture(tmp_path, monkeypatch, first_offset=-1.5)
    run = reader.load_run(run_dir)
    assert ledger.is_unassignable(run, run.attempts[0])
    assert run.risk_population_completeness == "partial"
    assert run.risk_population_completeness_reasons == ["unassignable_attempt"]
    assert [a.agent_id for a in ledger.risk_population(run)] == [run.attempts[0].agent_id]
