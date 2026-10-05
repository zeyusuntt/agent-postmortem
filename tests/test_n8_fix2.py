"""N8 fix round 2 (.reviews/v0.3/N8-dispositions.md, "Codex round 2"): R2-1.

R2-1  a finished run whose every attempt completed and whose risk population is EMPTY but not
      complete prints NO KILLED OR FAILED ATTEMPT: the tool could not establish which attempts
      were at risk; see WORK AT RISK in agent-postmortem run <run id>. (never "0 attempt(s)",
      never the brief, never a NOTHING sentence), exit 4, in rescue plan and rescue status.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import ledger
from tests.n5c1_helpers import Att, build_run, write_step
from tests.n5c2_helpers import age_files, run_cli
from tests.test_n8_k4_rescue import _ws

RUN = "wf_00000000-9f1"
SENTENCE = (
    "NO KILLED OR FAILED ATTEMPT: the tool could not establish which attempts were at risk; "
    f"see WORK AT RISK in agent-postmortem run {RUN}."
)


def _empty_partial_run(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    run_dir = build_run(
        tmp_path, monkeypatch, RUN,
        [Att(1, journal="completed", steps=[write_step("t1", ws / "a.txt", ws)])],
    )
    age_files(tmp_path / "root")
    monkeypatch.setattr(ledger, "risk_population", lambda run: [])
    monkeypatch.setattr(
        ledger, "risk_population_completeness", lambda run: ("partial", ["discovery_incomplete"]),
    )
    return run_dir


@pytest.mark.parametrize("command", ["plan", "status"])
def test_r2_1_text_says_the_tool_could_not_establish_the_population(
    tmp_path, monkeypatch, capsys, command,
):
    run_dir = _empty_partial_run(tmp_path, monkeypatch)
    code, out, _err = run_cli(capsys, ["rescue", command, str(run_dir), "--no-redact"])
    assert SENTENCE in out
    assert "attempt(s)" not in out
    assert "BRIEF FOR A RELAUNCH" not in out
    assert "NOTHING TO RESCUE" not in out
    assert code == 4


@pytest.mark.parametrize("command", ["plan", "status"])
def test_r2_1_json_exits_4_and_the_plan_has_no_brief(tmp_path, monkeypatch, capsys, command):
    run_dir = _empty_partial_run(tmp_path, monkeypatch)
    code, out, _err = run_cli(capsys, ["rescue", command, str(run_dir), "--json"])
    payload = json.loads(out)
    assert payload["units"] == []
    assert payload["exit_code"] == code == 4
    if command == "plan":
        assert payload["nothing_to_rescue"] is False
        assert payload["brief"] is None
