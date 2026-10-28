"""Tests for `rules review` (docs/DESIGN.md 9, unit M6): the owner's adopted
rules' detector rate N weeks before and after adoption, reusing trend.py's
before_after aggregation (trend.review_rows, an extension of it, not a
duplicate), plus a closing "consider removing" list.

Two layers: the pure aggregation/rendering functions in trend.py against
hand-built (Run, [Finding]) pairs, and the CLI end to end -- a `[[adopted]]`
config entry, a legacy v0.1 state-store `applied` entry, and their dedup --
against a tmp root of minimal on-disk workflow runs built with
tests/rule_fixtures.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_postmortem import state, trend
from agent_postmortem.cli import main
from agent_postmortem.models import Finding, Run
from tests import rule_fixtures

ADOPTED_AT = datetime(2026, 8, 15, tzinfo=timezone.utc)
BEFORE_DATE = "2026-08-10"
AFTER_DATE = "2026-08-20"


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


def _finding(detector: str, rule_id: str | None) -> Finding:
    return Finding(
        detector=detector, severity="minor", title="t", run_id="wf_x", agent_id=None,
        evidence=[], metrics={}, rule_id=rule_id,
    )


BEFORE1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
BEFORE2 = datetime(2026, 8, 5, tzinfo=timezone.utc)
AFTER1 = datetime(2026, 8, 20, tzinfo=timezone.utc)


# --- trend.review_rows / render_review_text / review_to_json ---------------


def test_review_rows_adds_target_kind_and_detectors():
    adopted = [{"rule_id": "R-GIT-CWD", "target": "t.md", "applied_at": "2026-08-15"}]
    runs = [
        (_run("wf_1", BEFORE1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_2", AFTER1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
    ]
    rows = trend.review_rows(adopted, runs, weeks=4)
    assert len(rows) == 1
    row = rows[0]
    assert row["target_kind"] == "project instructions"
    assert row["detectors"] == ["git_cwd_mismatch"]
    assert row["before"] == {"runs": 1, "fired": 1, "rate": 1.0}
    assert row["after"] == {"runs": 1, "fired": 1, "rate": 1.0}


def test_review_rows_windows_to_weeks_on_each_side():
    """Unlike plain trend.before_after (unbounded), review_rows only counts
    a run within `weeks` of applied_at on either side."""
    adopted = [{"rule_id": "R-GIT-CWD", "target": "t.md", "applied_at": "2026-08-15"}]
    far_before = datetime(2026, 1, 1, tzinfo=timezone.utc)  # far more than 4 weeks earlier
    runs = [
        (_run("wf_far", far_before), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_near", BEFORE1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
    ]
    row = trend.review_rows(adopted, runs, weeks=4)[0]
    assert row["before"]["runs"] == 1  # only wf_near, wf_far is outside the window


def test_render_review_text_no_adopted_rule():
    assert trend.render_review_text([]) == "no adopted rule to review\n"


def test_render_review_text_shows_counts_and_consider_removing():
    adopted = [{"rule_id": "R-GIT-CWD", "target": "t.md", "applied_at": "2026-08-15"}]
    runs = [
        (_run("wf_1", BEFORE1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_2", BEFORE2), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_3", AFTER1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
    ]
    rows = trend.review_rows(adopted, runs, weeks=4)
    text = trend.render_review_text(rows)

    assert "R-GIT-CWD  adopted 2026-08-15  git_cwd_mismatch: 2 of 2 runs before, 1 of 1 runs after" in text
    assert "consider removing: R-GIT-CWD" in text


def test_render_review_text_does_not_flag_a_rule_that_improved():
    adopted = [{"rule_id": "R-GIT-CWD", "target": "t.md", "applied_at": "2026-08-15"}]
    runs = [
        (_run("wf_1", BEFORE1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_2", BEFORE2), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_3", AFTER1), []),  # after adoption: did not fire
    ]
    rows = trend.review_rows(adopted, runs, weeks=4)
    text = trend.render_review_text(rows)

    assert "consider removing: none" in text


def test_render_review_text_does_not_flag_a_rule_with_no_data_on_either_side():
    adopted = [{"rule_id": "R-GIT-CWD", "target": "t.md", "applied_at": "2026-08-15"}]
    rows = trend.review_rows(adopted, [], weeks=4)
    text = trend.render_review_text(rows)

    assert "R-GIT-CWD  adopted 2026-08-15  git_cwd_mismatch: 0 of 0 runs before, 0 of 0 runs after" in text
    assert "consider removing: none" in text


# --- F13: review's own target field is target-N only by default ------------


def test_review_to_json_target_is_pseudonym_only_by_default():
    """docs/DESIGN.md 9/5.1 (F13 hardening): rules review's own target field
    must never let the basename survive by default -- unlike trend's own
    before/after target (kept as 'target-N (basename)' by design, pinned by
    tests/test_trend.py), review's target is target-N alone, the raw target
    string reserved for --no-redact."""
    from agent_postmortem.redact import Redactor

    adopted = [{"rule_id": "R-GIT-CWD", "target": "/home/user/CLAUDE.md", "applied_at": "2026-08-15"}]
    runs = [
        (_run("wf_1", BEFORE1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_2", AFTER1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
    ]
    rows = trend.review_rows(adopted, runs, weeks=4)

    redacted = trend.review_to_json(rows, Redactor(enabled=True))
    assert redacted["reviews"][0]["target"].startswith("target-")
    assert "CLAUDE.md" not in redacted["reviews"][0]["target"]

    raw = trend.review_to_json(rows, Redactor(enabled=False))
    assert raw["reviews"][0]["target"] == "/home/user/CLAUDE.md"


def test_cli_review_json_target_hidden_by_default_shown_with_no_redact(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-e0f", datetime(2026, 8, 10, tzinfo=timezone.utc))

    code = main(["rules", "review", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["reviews"][0]["target"].startswith("target-")
    assert "workflow.md" not in payload["reviews"][0]["target"]

    code = main(["rules", "review", "--root", str(root), "--json", "--no-redact"])
    raw_payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert raw_payload["reviews"][0]["target"] == "workflow.md"


def test_review_to_json_shape_and_consider_removing_list():
    adopted = [{"rule_id": "R-GIT-CWD", "target": "/tmp/t.md", "applied_at": "2026-08-15"}]
    runs = [
        (_run("wf_1", BEFORE1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
        (_run("wf_2", AFTER1), [_finding("git_cwd_mismatch", "R-GIT-CWD")]),
    ]
    rows = trend.review_rows(adopted, runs, weeks=4)
    payload = trend.review_to_json(rows)

    assert payload["schema"] == 3
    assert payload["consider_removing"] == ["R-GIT-CWD"]
    entry = payload["reviews"][0]
    assert entry["rule_id"] == "R-GIT-CWD"
    assert entry["target_kind"] == "project instructions"
    assert entry["detectors"] == ["git_cwd_mismatch"]
    assert entry["adopted_at"] == "2026-08-15"
    assert entry["before"] == {"runs": 1, "fired": 1, "rate": 1.0}
    assert entry["after"] == {"runs": 1, "fired": 1, "rate": 1.0}


# --- CLI end to end -----------------------------------------------------------


def _write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cli_review_no_adopted_rule_prints_message_and_exits_zero(tmp_path, capsys):
    code = main(["rules", "review", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out == "no adopted rule to review\n"


def test_cli_review_config_adopted_rule_computes_before_after(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-e0f", datetime(2026, 8, 10, tzinfo=timezone.utc), agent_n=1)
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-e10", datetime(2026, 8, 20, tzinfo=timezone.utc), agent_n=2)

    code = main(["rules", "review", "--root", str(root)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "R-GIT-CWD  adopted 2026-08-15  git_cwd_mismatch: 1 of 1 runs before, 1 of 1 runs after" in captured.out
    assert "consider removing: R-GIT-CWD" in captured.out


def test_cli_review_legacy_applied_entry_counts_as_adopted(tmp_path, capsys, monkeypatch):
    """docs/DESIGN.md 7.5/9: a v0.1 state-store 'applied' entry counts as
    adopted too, with no [[adopted]] config row at all."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state.update(
        lambda s: s["applied"].append(
            {"rule_id": "R-GIT-CWD", "target": "/home/user/CLAUDE.md", "applied_at": "2026-08-15T00:00:00Z"}
        )
    )

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-e11", datetime(2026, 8, 10, tzinfo=timezone.utc))

    code = main(["rules", "review", "--root", str(root)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "R-GIT-CWD  adopted 2026-08-15" in captured.out


def test_cli_review_does_not_flag_a_rule_that_improved(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-e12", datetime(2026, 8, 10, tzinfo=timezone.utc), agent_n=1)
    rule_fixtures.build_clean_run(root, "wf_00000000-e13", datetime(2026, 8, 20, tzinfo=timezone.utc), agent_n=2)

    code = main(["rules", "review", "--root", str(root)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "1 of 1 runs before, 0 of 1 runs after" in captured.out
    assert "consider removing: none" in captured.out


def test_cli_review_dedup_between_config_and_identical_legacy_entry(tmp_path, capsys, monkeypatch):
    """The same (rule, date, target) declared in both [[adopted]] and a
    legacy state entry is one adoption, not two rows."""
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state.update(
        lambda s: s["applied"].append(
            {"rule_id": "R-GIT-CWD", "target": "workflow.md", "applied_at": "2026-08-15"}
        )
    )

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-e14", datetime(2026, 8, 10, tzinfo=timezone.utc))

    code = main(["rules", "review", "--root", str(root)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert captured.out.count("R-GIT-CWD  adopted") == 1


def test_cli_review_no_runs_under_root_exits_two_when_a_rule_is_adopted(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    code = main(["rules", "review", "--root", str(tmp_path / "empty-root")])
    captured = capsys.readouterr()

    assert code == 2
    assert "no runs found" in captured.err


def test_cli_review_json_shape(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(root, "wf_00000000-e0f", datetime(2026, 8, 10, tzinfo=timezone.utc))

    code = main(["rules", "review", "--root", str(root), "--json"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["schema"] == 3
    assert payload["reviews"][0]["rule_id"] == "R-GIT-CWD"
    # No "after" run at all here (0 runs on that side): not enough evidence
    # either way, so never flagged (docs/DESIGN.md 9).
    assert payload["consider_removing"] == []


def test_codex_unavailable_after_adoption_counts_as_codex_contract_recurrence():
    """B1: R-CODEX-CONTRACT is also backed by codex_unavailable (DESIGN 9, 4.3),
    so an unavailable-companion finding after adoption is a recurrence."""
    adopted = [{"rule_id": "R-CODEX-CONTRACT", "target": "t.md", "applied_at": "2026-08-15"}]
    runs = [
        (_run("wf_1", BEFORE1), [_finding("codex_write_denied", "R-CODEX-CONTRACT")]),
        (_run("wf_2", AFTER1), [_finding("codex_unavailable", "R-CODEX-CONTRACT")]),
    ]
    row = trend.review_rows(adopted, runs, weeks=4)[0]
    assert row["detectors"] == ["codex_write_denied", "codex_unavailable"]
    assert row["before"] == {"runs": 1, "fired": 1, "rate": 1.0}
    assert row["after"] == {"runs": 1, "fired": 1, "rate": 1.0}
    assert "1 of 1 runs after" in trend.render_review_text([row])
