"""N3b P10 (C-5 rendering): an attempt whose journal `started` event has no
canonical agentId is named by a positional agent-N for the render: never by
its label, never by an HMAC of an invalid value, and its transcript pointer
is omitted. The run is INCOMPLETE (reader, 1a).
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import state
from agent_postmortem.cli import main
from tests import n3b_planted as plant

WEIRD = "zzWeird" + "Plant" + "7c6"


def _rewrite_first_started(planted, agent_id_field):
    """Make the killed attempt's journal entry carry a non-canonical agentId
    (and, for a string, move its transcript to the file name that id names)."""
    journal = planted.run_dir / "journal.jsonl"
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row.get("type") == "started" and row.get("agentId") == planted.agent1:
            if agent_id_field == "__missing__":
                del row["agentId"]
            else:
                row["agentId"] = agent_id_field
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if isinstance(agent_id_field, str) and agent_id_field != "__missing__":
        for suffix in (".jsonl", ".meta.json"):
            (planted.run_dir / f"agent-{planted.agent1}{suffix}").rename(
                planted.run_dir / f"agent-{agent_id_field}{suffix}"
            )


@pytest.fixture
def planted(tmp_path):
    return plant.build(tmp_path)


def _run(planted, capsys, *flags):
    capsys.readouterr()
    main(["run", str(planted.run_dir), *flags])
    captured = capsys.readouterr()
    return captured.out, captured.err


def _registry_agents() -> dict:
    registry = json.loads((state.state_dir() / "aliases.json").read_text(encoding="utf-8"))
    return registry["agent"]


@pytest.mark.parametrize("field", [WEIRD, None, "__missing__", ""])
@pytest.mark.parametrize("flags", [["--verbose", "--evidence"], ["--bug-report"]])
def test_attempt_without_a_canonical_agent_id_is_incomplete_unlabeled_and_unpointed(
    field, flags, planted, capsys,
):
    _rewrite_first_started(planted, field)
    out, _err = _run(planted, capsys, *flags)
    if flags == ["--verbose", "--evidence"]:
        assert "(INCOMPLETE)" in out
    assert planted.label not in out and planted.phase not in out
    assert WEIRD not in out
    assert planted.agent1 not in out
    # no pointer into that attempt's transcript: its marker lines are gone
    assert "[bash_timeout]" not in out
    # the other attempt keeps its pointer
    assert ".jsonl:1" in out


@pytest.mark.parametrize("field", [WEIRD, None, "__missing__"])
def test_a_non_canonical_value_is_never_registered_or_hmaced(field, planted, capsys):
    _rewrite_first_started(planted, field)
    _run(planted, capsys, "--verbose", "--evidence")
    agents = _registry_agents()
    assert len(agents) == 1  # only the canonical retry's id, never the invalid one


def test_the_journal_diagnostic_is_the_exact_c5_text(planted, capsys):
    _rewrite_first_started(planted, WEIRD)
    out, _err = _run(planted, capsys, "--json")
    diagnostics = json.loads(out)["run"]["diagnostics"]
    assert "journal.jsonl:2: started event has no canonical agentId" in diagnostics


def test_a_non_string_agent_id_keeps_the_existing_skip(planted, capsys):
    _rewrite_first_started(planted, 123)
    out, _err = _run(planted, capsys, "--json")
    diagnostics = json.loads(out)["run"]["diagnostics"]
    assert any("non-string agentId" in d for d in diagnostics)


def test_local_names_the_attempt_by_its_label_but_never_prints_the_invalid_id(planted, capsys):
    _rewrite_first_started(planted, WEIRD)
    out, _err = _run(planted, capsys, "--local", "--verbose")
    assert planted.label in out  # a label is real at local
    # N3b S14: no transcript is opened for a non-canonical id, so there is no
    # pointer into one (it printed the real file name before).
    assert f"agent-{WEIRD}.jsonl" not in out
