"""The N4b-2 counters of the private measurement script, asserted against a small
hand-counted corpus (`measure_population`: the REAL reader over every run).

The script lives in the private, untracked .reviews/tooling/n4/ directory; this
test is skipped where it is absent. Fixtures are synthetic.

The hand count (6 risk-population attempts, 4 with a partial ledger):
- session: the main thread (a shell call, ends in an interrupt: partial), a
  subagent that wrote a file and was interrupted (complete), a subagent that ran
  `make` and was interrupted (partial: shell, possible_opaque_command);
- wf_d02 (a parent transcript with one completed generation): the failed attempt
  wrote a file (complete); the completed one is not at risk;
- wf_d03 (no parent transcript, so every attempt is unassignable): a file-tool
  attempt (partial: unassignable_attempt) and a read-only shell attempt (partial:
  shell_context_unverified, unassignable_attempt).
"""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from tests.n4b2_helpers import T0, result, stamp, text, use, write_lines
from tests.test_field_v03 import (
    FAKE_UUID,
    _aid,
    _envelope,
    _notification_entry,
    _workflow_launch,
    _workflow_launch_result,
)

SCRIPT = Path(__file__).resolve().parents[1] / ".reviews" / "tooling" / "n4" / "measure.py"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="private measure.py is not present")

KILL = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "[Request interrupted by user]"}]}}


def _load():
    spec = importlib.util.spec_from_file_location("n4b2_measure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow(root, session, wf_id, attempts):
    run_dir = root / "-tmp-proj" / session / "subagents" / "workflows" / wf_id
    journal = []
    for number, (outcome, entries) in enumerate(attempts, start=1):
        agent = _aid(number)
        key = f"v2:{number}"
        journal.append({"type": "started", "key": key, "agentId": agent, "label": "l", "phase": "p"})
        journal.append({"type": "failed" if outcome == "failed" else "result", "key": key,
                        "agentId": agent, "result": {}})
        write_lines(run_dir / f"agent-{agent}.jsonl", stamp(entries))
    write_lines(run_dir / "journal.jsonl", journal)


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "projects"
    session_dir = root / "-tmp-proj"
    main = stamp([
        text(who="user", body="go"), use("m1", "Bash", {"command": "cat f"}), result("m1"),
        text(), KILL,
    ])
    main[0] = {**main[0], "sessionId": FAKE_UUID}
    write_lines(session_dir / f"{FAKE_UUID}.jsonl", main)
    children = session_dir / FAKE_UUID / "subagents"
    write_lines(children / f"agent-{_aid(11)}.jsonl", stamp([
        text(who="user", body="x"), use("c1", "Write", {"file_path": "/tmp/ws/a"}), result("c1"),
        KILL,
    ]))
    write_lines(children / f"agent-{_aid(12)}.jsonl", stamp([
        text(who="user", body="x"), use("c2", "Bash", {"command": "make"}), result("c2"), KILL,
    ]))
    write_lines(children / f"agent-{_aid(13)}.jsonl", stamp([text(who="user", body="x"), text()]))
    # wf_d02: a parent transcript with one generation that completed
    write_lines(session_dir / "sess2.jsonl", [
        _workflow_launch("toolu_A", T0),
        _workflow_launch_result("toolu_A", T0 + timedelta(seconds=1), "wf_00000000-d02", "tA"),
        _notification_entry(T0 + timedelta(seconds=2), _envelope("tA", "toolu_A", "completed")),
    ])
    _workflow(root, "sess2", "wf_00000000-d02", [
        ("completed", [use("a", "Write", {"file_path": "/tmp/ws/ok"}), result("a"), text()]),
        ("failed", [use("b", "Write", {"file_path": "/tmp/ws/bad"}), result("b"), text()]),
    ])
    # wf_d03: no parent transcript, so every attempt is unassignable
    _workflow(root, "sess3", "wf_00000000-d03", [
        ("completed", [use("a", "Write", {"file_path": "/tmp/ws/x"}), result("a"), text()]),
        ("completed", [use("b", "Bash", {"command": "cat f"}), result("b"), text()]),
    ])
    return root


EXPECTED = {
    "population.runs_loaded": 3,
    "population.runs_failed_to_load": 0,
    "population.runs_by_kind.session": 1,
    "population.runs_by_kind.workflow": 2,
    "population.runs_partial": 1,
    "population.run_reason.unassignable_attempt": 1,
    "population.run_reason.discovery_incomplete": 0,
    "population.runs_with_empty_population": 0,
    "population.attempts_total": 4 + 2 + 2,
    "population.attempts_partial": 4,
    "population.members": 6,
    "population.members_by_kind.session": 3,
    "population.members_by_kind.workflow": 3,
    "population.members_partial": 4,
    "population.reason.shell_context_unverified": 3,
    "population.reason.possible_opaque_command": 1,
    "population.reason.unassignable_attempt": 2,
    "population.reason.transcript_missing": 0,
    "population.reason.discovery_incomplete": 0,
    "population.partial_only_by_run_level_reasons": 1,
    "population.partial_only_by_shell_context": 1,
    "risk_population_partial_ledger_share": 0.6667,
    "population.attempts_partial_share": 0.5,
}


def test_the_population_counters_match_the_hand_count(corpus):
    measured = _load().measure_population(corpus)
    for key, value in EXPECTED.items():
        assert measured.get(key) == value, (key, measured.get(key), value)


def test_every_unassignable_member_has_one_cause(corpus):
    measured = _load().measure_population(corpus)
    causes = {
        key: value for key, value in measured.items()
        if key.startswith("population.unassignable_cause.")
    }
    assert causes == {
        "population.unassignable_cause.no_generation": 2,
        "population.unassignable_cause.generation_timestamps_unusable": 0,
        "population.unassignable_cause.no_first_ts": 0,
        "population.unassignable_cause.unplaceable_timestamp": 0,
        "population.unassignable_cause.before_first_launch_result": 0,
    }
    assert sum(causes.values()) == measured["population.reason.unassignable_attempt"]


def test_every_reason_has_a_row_even_at_zero(corpus):
    from agent_postmortem import ledger
    measured = _load().measure_population(corpus)
    for reason in ledger.REASON_ORDER:
        assert f"population.reason.{reason}" in measured, reason


def test_an_empty_corpus_has_a_null_share(tmp_path):
    (tmp_path / "projects").mkdir()
    measured = _load().measure_population(tmp_path / "projects")
    assert measured["risk_population_partial_ledger_share"] is None
    assert measured["population.members"] == 0
    assert measured["population.runs_loaded"] == 0


def test_the_script_prints_only_counters(corpus, capsys):
    module = _load()
    module.main(["measure.py", str(corpus), "--population"])
    out = capsys.readouterr().out
    assert "risk_population_partial_ledger_share: 0.6667" in out
    assert "/tmp" not in out and "ws" not in out.replace("workflow", "")
