"""Tests for `rules propose` (docs/DESIGN.md 9, unit M6): a rule is proposed
only when its backing detector(s) fired in at least 3 distinct runs on at
least 2 distinct days.

Two layers: rules.propose_rules() itself against hand-built (Run, [Finding])
pairs (fast, precise on the threshold boundary), and the CLI end to end
against a tmp root of minimal on-disk workflow runs built with
tests/rule_fixtures.py (proves discover_runs/--since/--json wiring, not
just the counting function).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_postmortem import rules
from agent_postmortem.cli import main
from agent_postmortem.models import Finding, Run
from tests import rule_fixtures

DAY1 = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
DAY2 = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
DAY3 = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def _run(run_id: str, started_at: datetime | None, kind: str = "workflow") -> Run:
    return Run(
        run_id=run_id,
        kind=kind,
        path=Path("/tmp") / run_id,
        project_slug="-home-user-project",
        session_id=None,
        claude_version=None,
        journal=[],
        agents=[],
        attempts=[],
        started_at=started_at,
        ended_at=started_at,
        diagnostics=[],
    )


def _finding(rule_id: str | None, detector: str = "git_cwd_mismatch") -> Finding:
    return Finding(
        detector=detector, severity="minor", title="t", run_id="wf_x", agent_id=None,
        evidence=[], metrics={}, rule_id=rule_id,
    )


# --- rules.propose_rules(): the threshold ------------------------------------


def test_propose_rules_two_runs_two_days_not_proposed():
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_2", DAY2), [_finding("R-GIT-CWD")]),
    ]
    assert rules.propose_rules(runs) == []


def test_propose_rules_three_runs_one_day_not_proposed():
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_2", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_3", DAY1), [_finding("R-GIT-CWD")]),
    ]
    assert rules.propose_rules(runs) == []


def test_propose_rules_three_runs_two_days_proposed():
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_2", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_3", DAY2), [_finding("R-GIT-CWD")]),
    ]
    rows = rules.propose_rules(runs)
    assert len(rows) == 1
    row = rows[0]
    assert row["rule"].id == "R-GIT-CWD"
    assert row["findings"] == 3
    assert row["runs"] == 3
    assert row["first"] == "2026-08-01"
    assert row["last"] == "2026-08-02"


def test_propose_rules_counts_total_findings_not_just_runs():
    """Two qualifying findings on the same run still count as one run, but
    two findings toward the total (docs/DESIGN.md 9)."""
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD"), _finding("R-GIT-CWD")]),
        (_run("wf_2", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_3", DAY2), [_finding("R-GIT-CWD")]),
    ]
    row = rules.propose_rules(runs)[0]
    assert row["runs"] == 3
    assert row["findings"] == 4


def test_propose_rules_orders_by_runs_descending_then_rule_id():
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_2", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_3", DAY2), [_finding("R-GIT-CWD")]),
        (_run("wf_4", DAY1), [_finding("R-NO-LONG-BASH", detector="bash_timeout")]),
        (_run("wf_5", DAY1), [_finding("R-NO-LONG-BASH", detector="bash_timeout")]),
        (_run("wf_6", DAY2), [_finding("R-NO-LONG-BASH", detector="bash_timeout")]),
        (_run("wf_7", DAY3), [_finding("R-NO-LONG-BASH", detector="bash_timeout")]),
    ]
    rows = rules.propose_rules(runs)
    assert [row["rule"].id for row in rows] == ["R-NO-LONG-BASH", "R-GIT-CWD"]


def test_propose_rules_run_with_no_started_at_never_supplies_a_day():
    """A run with no started_at still adds to the finding/run counts, but
    can never be one of the 2 required distinct days (docs/DESIGN.md 9)."""
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_2", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_3", None), [_finding("R-GIT-CWD")]),
    ]
    assert rules.propose_rules(runs) == []


def test_propose_rules_ignores_a_rule_id_not_in_the_library():
    runs = [(_run("wf_1", DAY1), [_finding("R-NOT-A-RULE")])]
    assert rules.propose_rules(runs) == []


def test_propose_rules_ignores_findings_with_no_rule_id():
    runs = [(_run("wf_1", DAY1), [_finding(None)])]
    assert rules.propose_rules(runs) == []


# --- rendering ---------------------------------------------------------------


def test_render_propose_text_no_rule_message():
    assert rules.render_propose_text([]) == "no rule is backed by 3 runs on 2 days yet\n"


def test_render_propose_text_shows_belongs_in_and_apply_command():
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_2", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_3", DAY2), [_finding("R-GIT-CWD")]),
    ]
    text = rules.render_propose_text(rules.propose_rules(runs))

    assert text.startswith("R-GIT-CWD")
    assert "backed by 3 findings in 3 runs, first 2026-08-01, last 2026-08-02" in text
    assert "belongs in: project instructions" in text
    normalized = " ".join(text.split())
    assert rules.RULES["R-GIT-CWD"].text in normalized
    assert "show the diff for a file: agent-postmortem apply R-GIT-CWD --target <file>" in text


def test_propose_row_to_json_shape():
    runs = [
        (_run("wf_1", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_2", DAY1), [_finding("R-GIT-CWD")]),
        (_run("wf_3", DAY2), [_finding("R-GIT-CWD")]),
    ]
    entry = rules.propose_row_to_json(rules.propose_rules(runs)[0])
    assert entry == {
        "rule_id": "R-GIT-CWD",
        "target_kind": "project instructions",
        "findings": 3,
        "runs": 3,
        "first": "2026-08-01",
        "last": "2026-08-02",
        "text": rules.RULES["R-GIT-CWD"].text,
    }


# --- CLI end to end, on a tmp root of real workflow runs ---------------------


def test_cli_rules_propose_two_runs_two_days_not_proposed(tmp_path, capsys):
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e01", DAY1)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e02", DAY2)

    code = main(["rules", "propose", "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert captured.out == "no rule is backed by 3 runs on 2 days yet\n"


def test_cli_rules_propose_three_runs_one_day_not_proposed(tmp_path, capsys):
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e03", DAY1, agent_n=1)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e04", DAY1, agent_n=2)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e05", DAY1, agent_n=3)

    code = main(["rules", "propose", "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert captured.out == "no rule is backed by 3 runs on 2 days yet\n"


def test_cli_rules_propose_three_runs_two_days_proposed(tmp_path, capsys):
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e06", DAY1, agent_n=1)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e07", DAY1, agent_n=2)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e08", DAY2, agent_n=3)

    code = main(["rules", "propose", "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "R-GIT-CWD" in captured.out
    assert "backed by 3 findings in 3 runs" in captured.out
    assert "belongs in: project instructions" in captured.out


def test_cli_rules_propose_since_filters_older_runs(tmp_path, capsys):
    # Three runs, but --since drops the oldest below threshold.
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e09", DAY1, agent_n=1)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e0a", DAY2, agent_n=2)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e0b", DAY3, agent_n=3)

    code = main(["rules", "propose", "--root", str(tmp_path), "--since", "2026-08-02"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert captured.out == "no rule is backed by 3 runs on 2 days yet\n"


def test_cli_rules_propose_no_runs_under_root_exits_two(tmp_path, capsys):
    code = main(["rules", "propose", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "no runs found" in captured.err


def test_cli_rules_propose_bad_since_exits_one(tmp_path, capsys):
    code = main(["rules", "propose", "--root", str(tmp_path), "--since", "not-a-date"])
    captured = capsys.readouterr()
    assert code == 1
    assert "--since" in captured.err


def test_cli_rules_propose_json_shape(tmp_path, capsys):
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e0c", DAY1, agent_n=1)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e0d", DAY1, agent_n=2)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e0e", DAY2, agent_n=3)

    code = main(["rules", "propose", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["schema"] == 2
    assert payload["proposals"] == [
        {
            "rule_id": "R-GIT-CWD",
            "target_kind": "project instructions",
            "findings": 3,
            "runs": 3,
            "first": "2026-08-01",
            "last": "2026-08-02",
            "text": rules.RULES["R-GIT-CWD"].text,
        }
    ]
