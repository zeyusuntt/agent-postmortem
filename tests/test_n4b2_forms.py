"""N4b-2 coverage group 9 (private forms and schema-2 exclusion, L1, L15) plus
the DESIGN wording the ledger depends on (docs/DESIGN.md 4.1, 4.13.1).

The `to_dict()` forms are exact objects; report.export_run() carries none of the
new keys (the byte goldens in tests/test_schema2_goldens.py are the other half).
"""

from __future__ import annotations

from pathlib import Path

from agent_postmortem import detectors, models, reader, report
from agent_postmortem.redact import Redactor
from tests.n4b2_helpers import (
    CWD,
    result,
    session_run,
    text,
    use,
    workflow_run,
    write_lines,
)
from tests.test_field_v03 import _aid

DESIGN = Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md"


# --- LedgerEntry and Ledger ------------------------------------------------------------


def test_ledger_entry_to_dict_is_exact():
    entry = models.LedgerEntry("claimed_target", "command_grammar", "success", "/tmp/ws/f",
                               "/tmp/ws", "agent-a.jsonl:7")
    assert entry.to_dict() == {
        "effect": "claimed_target", "basis": "command_grammar", "completion": "success",
        "path": "/tmp/ws/f", "repo": "/tmp/ws", "line": "agent-a.jsonl:7",
    }


def test_ledger_entry_to_dict_keeps_null_path_and_repo():
    entry = models.LedgerEntry("possible", "opaque_command", "no_result", None, None, "t:1")
    assert entry.to_dict() == {
        "effect": "possible", "basis": "opaque_command", "completion": "no_result",
        "path": None, "repo": None, "line": "t:1",
    }


def test_ledger_to_dict_is_exact():
    built = models.Ledger(
        [models.LedgerEntry("observed", "tool_result", "success", "/tmp/a", None, "t:2")],
        "partial", ["shell_context_unverified"],
    )
    assert built.to_dict() == {
        "entries": [{
            "effect": "observed", "basis": "tool_result", "completion": "success",
            "path": "/tmp/a", "repo": None, "line": "t:2",
        }],
        "completeness": "partial",
        "reasons": ["shell_context_unverified"],
    }


def test_an_empty_complete_ledger_to_dict():
    assert models.Ledger([], "complete", []).to_dict() == {
        "entries": [], "completeness": "complete", "reasons": []}


# --- Attempt and Run ---------------------------------------------------------------------


def test_attempt_to_dict_carries_the_ledger_and_the_journal_state(tmp_path):
    run = workflow_run(tmp_path, [use("t1", "Write", {"file_path": "/tmp/ws/a"}), result("t1"),
                                   text()])
    payload = run.attempts[0].to_dict()
    assert payload["ledger"] == {
        "entries": [{
            "effect": "observed", "basis": "tool_result", "completion": "success",
            "path": "/tmp/ws/a", "repo": CWD, "line": "agent-" + _aid(1) + ".jsonl:2",
        }],
        "completeness": "complete",
        "reasons": [],
    }
    assert payload["journal_state"] == "completed"


def test_a_directly_built_attempt_has_a_null_ledger_in_to_dict():
    attempt = models.Attempt(
        agent_id="a", key=None, label="", phase="", agent_type=None, meta={}, transcript_path=None,
        events=[], first_ts=None, last_ts=None, ordinal=1, outcome="completed", usage={},
    )
    assert attempt.ledger is None
    assert attempt.to_dict()["ledger"] is None


def test_run_to_dict_carries_the_completeness_and_its_reasons(tmp_path):
    run = workflow_run(tmp_path, [text()], parent=None)
    payload = run.to_dict()
    assert payload["risk_population_completeness"] == "partial"
    assert payload["risk_population_completeness_reasons"] == ["unassignable_attempt"]
    assert payload["discovery"] == {"journal": [], "parent": [], "transcripts": [], "meta": []}


def test_a_complete_run_to_dict(tmp_path):
    run = workflow_run(tmp_path, [text()])
    payload = run.to_dict()
    assert payload["risk_population_completeness"] == "complete"
    assert payload["risk_population_completeness_reasons"] == []


def test_a_directly_built_run_has_no_completeness_yet():
    run = models.Run(
        run_id="wf_x", path=Path("."), project_slug="s", session_id=None, claude_version=None,
        journal=[], agents=[], attempts=[], started_at=None, ended_at=None, diagnostics=[],
    )
    assert run.risk_population_completeness is None
    assert run.risk_population_completeness_reasons == []
    assert run.to_dict()["risk_population_completeness"] is None


def test_the_ledger_never_reaches_the_dataclass_equality_of_a_tool_call(tmp_path):
    # a private reader field that is not part of ToolCall's own form
    run_path = tmp_path / "t.jsonl"
    write_lines(run_path, [use("t1", "Write", {"file_path": "/tmp/ws/a"}), result("t1")])
    (call,) = reader._read_transcript(run_path, [])[5]
    assert set(call.to_dict()) == {
        "tool_use_id", "tool_name", "line_no", "ts", "path", "command", "command_sha256", "cwd",
        "result_line", "is_error", "backgrounded", "exit_code", "commit_ids", "ambiguous",
        "completion", "run_in_background", "multiedit_path", "classification",
    }


# --- schema 2 ---------------------------------------------------------------------------

# N5a-1: the attempt ledger (`ledger`, `completeness`, `reasons`, `entries`) is public in
# schema 3 (docs/DESIGN.md 5.3b); what stays private is the run-level population state.
PRIVATE = {"risk_population_completeness", "risk_population_completeness_reasons",
           "journal_state", "result_ts", "discovery"}


def _keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


def test_the_export_carries_the_public_ledger_and_no_private_population_key(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "none.toml"))
    run = workflow_run(tmp_path, [use("t1", "Write", {"file_path": "/tmp/ws/a"}), result("t1"),
                                   use("t2", "Bash", {"command": "make"}), result("t2"), text()])
    assert run.attempts[0].ledger.entries  # the ledger is really there
    findings = detectors.run_detectors(run)
    payload = report.export_run(run, findings, Redactor(enabled=False))
    assert not (_keys(payload) & PRIVATE)
    ledger = payload["run"]["attempts"][0]["ledger"]
    assert set(ledger) == {"completeness", "reasons", "entries"}
    assert ledger["entries"]
    # the closed reason codes are public ledger fields in schema 3
    assert "possible_opaque_command" in ledger["reasons"]
    assert "shell_context_unverified" in ledger["reasons"]


# --- the main attempt of a cached view is never shared ----------------------------------


def test_a_cached_main_attempt_is_not_mutated_by_a_view(tmp_path):
    child = _aid(4)
    run = session_run(
        tmp_path,
        [text(who="user", body="go"), use("m1", "Bash", {"command": "cat f"}), result("m1")],
        {child: [text(who="user", body="x"), text()]},
    )
    main_path, child_path = run.path, run.attempts[0].transcript_path
    cache: dict = {}
    clean = reader.load_session_view(main_path, [child_path], main_cache=cache)
    assert clean.main.ledger.reasons == ["shell_context_unverified"]
    bad = child_path.parent / "agent-weird name.jsonl"
    bad.write_text(child_path.read_text(), encoding="utf-8")
    dirty = reader.load_session_view(main_path, [bad], main_cache=cache)
    assert dirty.discovery["transcripts"] != []
    assert dirty.main.ledger.reasons == ["shell_context_unverified", "discovery_incomplete"]
    assert clean.main.ledger.reasons == ["shell_context_unverified"]


# --- DESIGN wording (pins; the wording lands with the first tests) ---------------------


def _design():
    return " ".join(DESIGN.read_text(encoding="utf-8").split())


def test_the_design_says_the_ledger_is_never_none_from_n4_on():
    design = _design()
    assert "the reader never leaves it None" in design
    assert "Ledger([], 'partial', ['transcript_missing'])" in design
    assert "None on the same terms `facts` is (no transcript at all" not in design


def test_the_design_names_the_grouping_and_moves_observation_to_n5():
    design = _design()
    assert "`ledger.group_by_repo(run)`" in design
    assert "observation moves to N5, C-14" in design
    assert "run status yourself" in design


def test_the_design_says_discovery_incomplete_applies_whatever_the_population_size():
    design = _design()
    assert "whatever the size of the risk population" in design
    assert "whenever journal, parent, attempt-transcript, or meta discovery" in design


def test_the_design_scopes_the_outcome_branches_to_workflow_runs():
    design = _design()
    assert "(workflow runs) its outcome (4.1/4.2)" in design
    assert "(session runs) every subagent with an `interrupt` set" in design


def test_attempt_and_run_to_dict_have_exactly_these_keys(tmp_path):
    run = workflow_run(tmp_path, [text()])
    assert set(run.attempts[0].to_dict()) == {
        "agent_id", "key", "label", "phase", "agent_type", "meta", "transcript_path", "events",
        "first_ts", "last_ts", "ordinal", "outcome", "usage", "interrupt", "reported",
        "reported_line", "tool_calls", "facts", "marker_hits", "journal_state", "ledger",
        "return_state", "return_reason", "resumes",
    }
    assert set(run.to_dict()) == {
        "run_id", "kind", "path", "project_slug", "session_id", "claude_version", "journal",
        "main", "agents", "attempts", "started_at", "ended_at", "diagnostics", "status",
        "finished", "lifecycle", "discovery", "risk_population_completeness",
        "risk_population_completeness_reasons",
    }
