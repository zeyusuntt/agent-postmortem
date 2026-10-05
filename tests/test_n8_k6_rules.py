"""N8a K6 (docs/DESIGN.md 9, 4.13.8): adopted rules in `rules propose`, the
R-NO-LONG-BASH macOS note, the `agent prompt` target kind and its own
actionable line, and `rules propose --run`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agent_postmortem import rules
from agent_postmortem.cli import main
from agent_postmortem.models import Finding, Run
from tests import rule_fixtures

DAY1 = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
DAY2 = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
DAY3 = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
ADOPTION = "2026-08-02"
TARGET = "/home/user/project/zz-n8-adopted-target.md"

AGENT_PROMPT_LINE = (
    "add this directive to the workflow's agent preamble or to the Agent-tool "
    "prompt that launches the subagent"
)


def _run(run_id: str, started_at: datetime | None) -> Run:
    return Run(
        run_id=run_id, kind="workflow", path=Path("/tmp") / run_id,
        project_slug="-home-user-project", session_id=None, claude_version=None, journal=[],
        agents=[], attempts=[], started_at=started_at, ended_at=started_at, diagnostics=[],
    )


def _finding(rule_id: str | None, detector: str = "git_cwd_mismatch") -> Finding:
    return Finding(
        detector=detector, severity="minor", title="t", run_id="wf_x", agent_id=None,
        evidence=[], metrics={}, rule_id=rule_id,
    )


def _adopted(rule: str, date: str, target: str = TARGET):
    return SimpleNamespace(rule=rule, date=date, target=target)


def _three_runs(rule_id: str = "R-GIT-CWD"):
    return [
        (_run("wf_1", DAY1), [_finding(rule_id)]),
        (_run("wf_2", DAY2), [_finding(rule_id)]),
        (_run("wf_3", DAY3), [_finding(rule_id)]),
    ]


def _propose(runs, adopted=(), **kwargs):
    return rules.propose_rules(runs, adopted, **kwargs)


# --- adopted rows, model level ---------------------------------------------------------------


def test_k6_adopted_rule_above_threshold_is_one_adopted_row_not_a_proposal():
    rows = _propose(_three_runs(), [_adopted("R-GIT-CWD", ADOPTION)])
    assert len(rows) == 1
    row = rows[0]
    assert row["adopted"] == ADOPTION
    # runs started on or after the adoption date: wf_2 and wf_3
    assert row["since"] == {"findings": 2, "runs": 2}
    text = rules.render_propose_text(rows)
    assert text == "R-GIT-CWD   adopted 2026-08-02; still violated 2 times in 2 runs since\n"
    assert "apply" not in text
    assert "belongs in" not in text


def test_k6_adopted_rule_below_threshold_is_still_shown():
    runs = [(_run("wf_1", DAY2), [_finding("R-GIT-CWD")])]
    rows = _propose(runs, [_adopted("R-GIT-CWD", ADOPTION)])
    assert [row["rule"].id for row in rows] == ["R-GIT-CWD"]
    assert rules.render_propose_text(rows) == (
        "R-GIT-CWD   adopted 2026-08-02; still violated 1 time in 1 run since\n"
    )


def test_k6_two_findings_in_one_run_count_twice_in_one_run():
    runs = [(_run("wf_1", DAY2), [_finding("R-GIT-CWD"), _finding("R-GIT-CWD")])]
    row = _propose(runs, [_adopted("R-GIT-CWD", ADOPTION)])[0]
    assert row["since"] == {"findings": 2, "runs": 1}
    assert rules.render_propose_text([row]).startswith(
        "R-GIT-CWD   adopted 2026-08-02; still violated 2 times in 1 run since"
    )


def test_k6_a_finding_of_a_backing_detector_without_the_rule_id_is_not_counted():
    runs = [(_run("wf_1", DAY2), [_finding(None, detector="git_cwd_mismatch")])]
    row = _propose(runs, [_adopted("R-GIT-CWD", ADOPTION)])[0]
    assert row["since"] == {"findings": 0, "runs": 0}


def test_k6_zero_violations_since_adoption_has_null_dates():
    runs = [(_run("wf_1", DAY1), [_finding("R-GIT-CWD")])]  # before the adoption date
    row = _propose(runs, [_adopted("R-GIT-CWD", ADOPTION)])[0]
    assert row["since"] == {"findings": 0, "runs": 0}
    entry = rules.propose_row_to_json(row)
    assert entry["findings"] == 0 and entry["runs"] == 0
    assert entry["first"] is None and entry["last"] is None
    assert rules.render_propose_text([row]) == (
        "R-GIT-CWD   adopted 2026-08-02; still violated 0 times in 0 runs since\n"
    )


def test_k6_an_adopted_rule_with_no_finding_anywhere_is_still_a_status_row():
    rows = _propose([(_run("wf_1", DAY2), [])], [_adopted("R-GIT-CWD", ADOPTION)])
    assert [row["rule"].id for row in rows] == ["R-GIT-CWD"]
    assert rows[0]["since"] == {"findings": 0, "runs": 0}


def test_k6_several_adoption_records_use_the_earliest_date_and_say_so():
    adopted = [_adopted("R-GIT-CWD", "2026-08-03"), _adopted("R-GIT-CWD", "2026-08-01")]
    rows = _propose(_three_runs(), adopted)
    assert len(rows) == 1
    assert rows[0]["adopted"] == "2026-08-01"
    assert rows[0]["since"] == {"findings": 3, "runs": 3}
    assert rules.render_propose_text(rows) == (
        "R-GIT-CWD   adopted 2026-08-01 (2 records); still violated 3 times in 3 runs since\n"
    )


def test_k6_unadopted_rows_carry_adopted_and_since_as_null():
    rows = _propose(_three_runs())
    assert len(rows) == 1
    assert rows[0]["adopted"] is None and rows[0]["since"] is None
    entry = rules.propose_row_to_json(rows[0])
    assert entry["adopted"] is None and entry["since"] is None


def test_k6_the_complete_json_shape_of_an_adopted_and_an_unadopted_row():
    adopted_row = _propose(_three_runs(), [_adopted("R-GIT-CWD", ADOPTION)])[0]
    entry = rules.propose_row_to_json(adopted_row)
    assert list(entry) == [
        "rule_id", "target_kind", "findings", "runs", "first", "last", "adopted", "since", "text",
    ]
    assert entry["adopted"] == ADOPTION
    assert entry["since"] == {"findings": 2, "runs": 2}
    assert (entry["findings"], entry["runs"]) == (2, 2)
    assert (entry["first"], entry["last"]) == ("2026-08-02", "2026-08-03")
    assert TARGET not in json.dumps(entry)
    plain = rules.propose_row_to_json(_propose(_three_runs())[0])
    assert list(plain) == list(entry)
    assert (plain["findings"], plain["runs"]) == (3, 3)


def test_k6_an_undated_single_run_lists_the_rule_with_null_dates():
    runs = [(_run("wf_u", None), [_finding("R-GIT-CWD")])]
    rows = _propose(runs, single_run=True)
    assert len(rows) == 1
    entry = rules.propose_row_to_json(rows[0])
    assert (entry["findings"], entry["runs"]) == (1, 1)
    assert entry["first"] is None and entry["last"] is None
    text = rules.render_propose_text(rows)
    assert "backed by 1 finding in 1 run" in text
    assert "first" not in text.splitlines()[0]


def test_k6_an_undated_run_cannot_be_placed_after_the_adoption_date():
    runs = [(_run("wf_u", None), [_finding("R-GIT-CWD")])]
    row = _propose(runs, [_adopted("R-GIT-CWD", ADOPTION)], single_run=True)[0]
    assert row["since"] == {"findings": 0, "runs": 0}


def test_k6_a_single_run_bypasses_the_three_runs_two_days_threshold():
    runs = [(_run("wf_1", DAY1), [_finding("R-GIT-CWD")])]
    assert _propose(runs) == []
    rows = _propose(runs, single_run=True)
    assert [row["rule"].id for row in rows] == ["R-GIT-CWD"]
    assert (rows[0]["findings"], rows[0]["runs"]) == (1, 1)
    assert (rows[0]["first"], rows[0]["last"]) == ("2026-08-01", "2026-08-01")


def test_k6_a_single_run_view_lists_an_adopted_rule_only_when_the_run_backs_it():
    runs = [(_run("wf_1", DAY2), [_finding("R-GIT-CWD")])]
    adopted = [_adopted("R-GIT-CWD", ADOPTION), _adopted("R-RETRY-SAFE", ADOPTION)]
    rows = _propose(runs, adopted, single_run=True)
    assert [row["rule"].id for row in rows] == ["R-GIT-CWD"]
    # the fleet view keeps an adopted rule the scanned runs never back
    assert [row["rule"].id for row in _propose(runs, adopted)] == ["R-GIT-CWD", "R-RETRY-SAFE"]


def test_k6_a_single_run_view_has_its_own_heading():
    runs = [(_run("wf_1", DAY1), [_finding("R-GIT-CWD")])]
    text = rules.render_propose_text(_propose(runs, single_run=True), run_id="wf_1")
    assert text.splitlines()[0] == "rules backed by findings in run wf_1"


# --- the agent prompt kind and the macOS note --------------------------------------------------


def test_k6_only_r_no_long_bash_moves_to_the_agent_prompt_kind():
    by_kind: dict[str, set[str]] = {}
    for rule in rules.RULES.values():
        by_kind.setdefault(rule.target_kind, set()).add(rule.id)
    assert by_kind["agent prompt"] == {"R-NO-LONG-BASH"}
    assert by_kind["hook"] == {"R-MODEL-ALLOCATION"}
    assert by_kind["project instructions"] == {"R-GIT-CWD"}
    assert "R-NO-LONG-BASH" not in by_kind["workflow prompt"]
    assert len(by_kind["workflow prompt"]) == 8
    assert set(by_kind) == {"workflow prompt", "agent prompt", "hook", "project instructions"}


def test_k6_the_rule_text_names_the_macos_gap_and_the_subagent_advice():
    text = rules.RULES["R-NO-LONG-BASH"].text
    assert "macOS has no timeout(1)" in text
    assert "python3 -c 'import subprocess; subprocess.run([...], timeout=N)'" in text
    assert "perl -e 'alarm N; exec @ARGV'" in text
    assert "In a subagent, run long jobs from the main session; never background and wait." in text
    assert "—" not in text


def test_k6_a_proposal_for_an_agent_prompt_rule_has_its_own_line_and_no_apply_line():
    runs = [(_run(f"wf_{n}", day), [_finding("R-NO-LONG-BASH", "bash_timeout")])
            for n, day in ((1, DAY1), (2, DAY1), (3, DAY2))]
    text = rules.render_propose_text(_propose(runs))
    assert "belongs in: agent prompt" in text
    assert f"  {AGENT_PROMPT_LINE}" in text
    assert "apply" not in text


def test_k6_a_proposal_for_a_file_rule_keeps_its_apply_line():
    text = rules.render_propose_text(rules.propose_rules(_three_runs()))
    assert "show the diff for a file: agent-postmortem apply R-GIT-CWD --target <file>" in text
    assert AGENT_PROMPT_LINE not in text


def test_k6_rules_show_carries_the_new_kind_the_line_and_the_note(capsys):
    assert main(["rules", "--show", "R-NO-LONG-BASH"]) == 0
    out = capsys.readouterr().out
    assert "belongs in: agent prompt" in out
    assert AGENT_PROMPT_LINE in out
    assert "macOS has no timeout(1)" in out
    assert "apply" not in out


def test_k6_rules_show_of_a_file_rule_has_no_agent_prompt_line(capsys):
    assert main(["rules", "--show", "R-GIT-CWD"]) == 0
    assert AGENT_PROMPT_LINE not in capsys.readouterr().out


def test_k6_rules_show_json_carries_the_kind_and_the_note(capsys):
    assert main(["rules", "--show", "R-NO-LONG-BASH", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_kind"] == "agent prompt"
    assert "macOS has no timeout(1)" in payload["text"]


# --- the CLI end to end -------------------------------------------------------------------------


def _config(tmp_path: Path, monkeypatch, *records: tuple[str, str]) -> None:
    body = "".join(
        f'[[adopted]]\nrule = "{rule}"\ndate = "{date}"\ntarget = "{TARGET}"\n\n'
        for rule, date in records
    )
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))


def _fleet(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-8d1", DAY1, agent_n=1)
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-8d2", DAY2, agent_n=2)
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-8d3", DAY3, agent_n=3)
    return root


def test_k6_cli_text_marks_the_adopted_rule_and_never_prints_its_target(
    tmp_path, monkeypatch, capsys,
):
    _config(tmp_path, monkeypatch, ("R-GIT-CWD", ADOPTION))
    root = _fleet(tmp_path)
    for flags in ([], ["--local"], ["--raw"]):
        assert main(["rules", "propose", "--root", str(root), *flags]) == 0
        out = capsys.readouterr().out
        assert "R-GIT-CWD   adopted 2026-08-02; still violated 2 times in 2 runs since" in out
        assert "backed by" not in out
        assert TARGET not in out
        assert "zz-n8-adopted-target" not in out


def test_k6_cli_json_carries_adopted_and_since_and_never_the_target(
    tmp_path, monkeypatch, capsys,
):
    _config(tmp_path, monkeypatch, ("R-GIT-CWD", ADOPTION))
    root = _fleet(tmp_path)
    assert main(["rules", "propose", "--root", str(root), "--json"]) == 0
    raw = capsys.readouterr().out
    assert TARGET not in raw and "zz-n8-adopted-target" not in raw
    (row,) = json.loads(raw)["proposals"]
    assert row["adopted"] == ADOPTION
    assert row["since"] == {"findings": 2, "runs": 2}


def test_k6_cli_since_filters_the_adopted_count_but_keeps_the_row(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch, ("R-GIT-CWD", ADOPTION))
    root = _fleet(tmp_path)
    assert main(["rules", "propose", "--root", str(root), "--since", "2026-08-03"]) == 0
    out = capsys.readouterr().out
    assert "R-GIT-CWD   adopted 2026-08-02; still violated 1 time in 1 run since" in out


def test_k6_cli_unadopted_proposals_are_unchanged_in_text(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    root = _fleet(tmp_path)
    assert main(["rules", "propose", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "R-GIT-CWD   backed by 3 findings in 3 runs, first 2026-08-01, last 2026-08-03" in out


def test_k6_cli_run_is_a_single_run_view(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    root = _fleet(tmp_path)
    assert main(["rules", "propose", "--root", str(root), "--run", "wf_00000000-8d2"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "rules backed by findings in run wf_00000000-8d2"
    assert "R-GIT-CWD   backed by 1 finding in 1 run, first 2026-08-02, last 2026-08-02" in out


def test_k6_cli_run_json_lists_only_that_runs_counts(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    root = _fleet(tmp_path)
    assert main(["rules", "propose", "--root", str(root), "--run", "wf_00000000-8d2", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["proposals"]
    assert (row["findings"], row["runs"], row["first"], row["last"]) == (1, 1, "2026-08-02", "2026-08-02")
    assert row["adopted"] is None and row["since"] is None


def test_k6_cli_run_with_an_ambiguous_id_is_exit_1(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    root = _fleet(tmp_path)
    assert main(["rules", "propose", "--root", str(root), "--run", "wf_00000000-8d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "wf_00000000-8d1" in captured.err and "wf_00000000-8d2" in captured.err


def test_k6_cli_run_with_since_is_a_usage_error(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    root = _fleet(tmp_path)
    code = main([
        "rules", "propose", "--root", str(root), "--run", "wf_00000000-8d2", "--since", "2026-08-01",
    ])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--run" in captured.err and "--since" in captured.err


def test_k6_cli_run_with_an_unknown_id_is_exit_2(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    root = _fleet(tmp_path)
    assert main(["rules", "propose", "--root", str(root), "--run", "wf_00000000-8ff"]) == 2
    assert capsys.readouterr().out == ""
