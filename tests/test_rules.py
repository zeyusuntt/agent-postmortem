"""Tests for rules.py: the RULES library, rules_for, and the managed-block
apply_rule writer. See docs/DESIGN.md sections 4.5 and 4.7.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from agent_postmortem import rules, state
from agent_postmortem.cli import main
from agent_postmortem.models import Finding

_ALL_IDS = (
    "R-RETRY-SAFE",
    "R-CAP-CONCURRENCY",
    "R-NO-LONG-BASH",
    "R-SMALL-QUANTUM",
    "R-ASSERT-ARTIFACT",
    "R-SCALAR-SCHEMA",
    "R-CODEX-CONTRACT",
    "R-MONITOR-RETRIES",
    "R-GIT-CWD",
    "R-MODEL-ALLOCATION",
)


def _finding(rule_id, detector="silent_retry"):
    return Finding(
        detector=detector,
        severity="major",
        title="t",
        run_id="wf_x",
        agent_id=None,
        evidence=[],
        metrics={},
        rule_id=rule_id,
    )


# --- RULES ---------------------------------------------------------------


def test_rules_library_has_exactly_ten_ids():
    assert set(rules.RULES) == set(_ALL_IDS)
    assert len(rules.RULES) == 10


def test_every_rule_has_id_title_text_and_valid_enforcement():
    for rule_id, rule in rules.RULES.items():
        assert rule.id == rule_id
        assert rule.title
        assert rule.text
        assert rule.enforcement in ("prompt", "hook")


def test_only_model_allocation_ships_a_hook_template():
    hooked = [rule.id for rule in rules.RULES.values() if rule.hook_template]
    assert hooked == ["R-MODEL-ALLOCATION"]


# --- rules_for -------------------------------------------------------------


def test_rules_for_counts_and_orders_by_backing_count_then_id():
    findings = [
        _finding("R-RETRY-SAFE"),
        _finding("R-RETRY-SAFE"),
        _finding("R-GIT-CWD", detector="git_cwd_mismatch"),
        _finding(None),
    ]
    rows = rules.rules_for(findings)
    assert [rule.id for rule, _ in rows] == ["R-RETRY-SAFE", "R-GIT-CWD"]
    assert {rule.id: count for rule, count in rows} == {"R-RETRY-SAFE": 2, "R-GIT-CWD": 1}


def test_rules_for_ignores_findings_without_a_rule_id():
    assert rules.rules_for([_finding(None)]) == []


# --- apply_rule ------------------------------------------------------------


def test_apply_dry_run_leaves_file_untouched_and_diff_mentions_rule(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My rules\n- existing rule\n", encoding="utf-8")

    diff = rules.apply_rule("R-RETRY-SAFE", target, write=False)

    assert target.read_text(encoding="utf-8") == "# My rules\n- existing rule\n"
    assert "R-RETRY-SAFE" in diff


def test_apply_dry_run_on_a_target_that_does_not_exist_does_not_create_it(tmp_path):
    target = tmp_path / "new.md"
    diff = rules.apply_rule("R-GIT-CWD", target, write=False)
    assert not target.exists()
    assert "R-GIT-CWD" in diff


def test_apply_write_creates_the_managed_block(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My rules\n- existing rule\n", encoding="utf-8")

    diff = rules.apply_rule("R-RETRY-SAFE", target, write=True)
    assert diff

    text = target.read_text(encoding="utf-8")
    assert rules.BEGIN_MARKER in text
    assert rules.END_MARKER in text
    assert f"- R-RETRY-SAFE: {rules.RULES['R-RETRY-SAFE'].text}" in text
    assert text.startswith("# My rules\n- existing rule\n")


def test_apply_second_write_is_a_noop_with_empty_diff(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My rules\n", encoding="utf-8")

    first_diff = rules.apply_rule("R-RETRY-SAFE", target, write=True)
    assert first_diff
    before_second = target.read_text(encoding="utf-8")

    second_diff = rules.apply_rule("R-RETRY-SAFE", target, write=True)
    assert second_diff == ""
    assert target.read_text(encoding="utf-8") == before_second


def test_apply_creates_a_target_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "new.md"
    assert not target.exists()

    rules.apply_rule("R-GIT-CWD", target, write=True)

    assert target.exists()
    assert "R-GIT-CWD" in target.read_text(encoding="utf-8")


def test_apply_preserves_existing_content_outside_the_block_byte_for_byte(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    original = "# Team rules\n\n- always write tests first\n- never force push\n"
    target.write_text(original, encoding="utf-8")

    rules.apply_rule("R-NO-LONG-BASH", target, write=True)

    text = target.read_text(encoding="utf-8")
    assert text.startswith(original.rstrip("\n"))


def test_apply_preserves_extra_trailing_blank_lines_in_existing_content(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    # Two blank lines at the end of the user's own content: everything
    # outside the managed block must survive byte for byte, not just its
    # non-whitespace prefix.
    original = "# Team rules\n\n- always write tests first\n\n\n"
    target.write_text(original, encoding="utf-8")

    rules.apply_rule("R-NO-LONG-BASH", target, write=True)

    text = target.read_text(encoding="utf-8")
    assert text.startswith(original)


def test_apply_second_rule_adds_a_second_bullet_and_keeps_the_first(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"

    rules.apply_rule("R-RETRY-SAFE", target, write=True)
    rules.apply_rule("R-GIT-CWD", target, write=True)

    text = target.read_text(encoding="utf-8")
    assert f"- R-RETRY-SAFE: {rules.RULES['R-RETRY-SAFE'].text}" in text
    assert f"- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}" in text
    assert text.count(rules.BEGIN_MARKER) == 1


def test_apply_records_state_on_write(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"

    rules.apply_rule("R-GIT-CWD", target, write=True)

    saved = state.load_state()
    assert len(saved["applied"]) == 1
    entry = saved["applied"][0]
    assert entry["rule_id"] == "R-GIT-CWD"
    assert entry["target"] == str(target)
    assert entry["applied_at"]


def test_apply_write_preserves_the_targets_existing_permission_bits(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Team rules\n", encoding="utf-8")
    target.chmod(0o640)

    rules.apply_rule("R-GIT-CWD", target, write=True)

    # os.replace() otherwise leaves the target with mkstemp's own 0600,
    # silently loosening or tightening permissions on every write.
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_apply_refuses_to_write_through_a_symlinked_target(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    real_target = tmp_path / "real.md"
    real_target.write_text("# Team rules\n", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(real_target)

    import pytest

    with pytest.raises(ValueError):
        rules.apply_rule("R-GIT-CWD", link, write=True)

    # os.replace() would otherwise swap the symlink itself for a plain
    # file instead of updating whatever it points to.
    assert link.is_symlink()
    assert real_target.read_text(encoding="utf-8") == "# Team rules\n"


def test_apply_write_fsyncs_the_temp_file_before_replacing_the_target(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"

    fsync_calls = []
    real_fsync = os.fsync

    def _tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _tracking_fsync)

    rules.apply_rule("R-GIT-CWD", target, write=True)

    assert fsync_calls


def test_apply_unknown_rule_id_raises():
    import pytest

    with pytest.raises(ValueError):
        rules.apply_rule("R-NOT-A-RULE", "/tmp/does-not-matter.md", write=False)


# --- apply_rule: one transaction, idempotent against drift ------------------


def test_apply_write_does_not_modify_target_when_the_state_transaction_fails(tmp_path, monkeypatch):
    """docs/DESIGN.md 9: apply_rule --write runs entirely inside one
    transaction -- read target, compute diff, write target, record state,
    commit. If the state side of that fails, the target must never have
    been touched, or a retry sees an empty diff and the application is
    lost for good."""
    import pytest

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state_dir = tmp_path / "state" / "agent-postmortem"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"version": 99, "min_reader": 99, "applied": [], "sessions": {}, "digest": {"pending": []}}),
        encoding="utf-8",
    )
    target = tmp_path / "CLAUDE.md"
    target.write_text("# rules\n", encoding="utf-8")

    with pytest.raises(state.StateTooNew):
        rules.apply_rule("R-GIT-CWD", target, write=True)

    assert target.read_text(encoding="utf-8") == "# rules\n"


def test_apply_write_records_drift_when_rule_already_present_but_unrecorded(tmp_path, monkeypatch):
    """docs/DESIGN.md 9: if the target already carries the rule (someone
    applied it by hand, or state.json was lost) but no applied entry names
    this (rule_id, target) pair, --write records one even though the diff
    is empty -- apply is idempotent against that drift."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n",
        encoding="utf-8",
    )

    diff = rules.apply_rule("R-GIT-CWD", target, write=True)

    assert diff == ""  # nothing changed in the target
    assert diff.already_present_unrecorded is True
    applied = state.load_state()["applied"]
    assert len(applied) == 1
    assert applied[0]["rule_id"] == "R-GIT-CWD"
    assert applied[0]["target"] == str(target)


def test_apply_write_is_a_true_noop_once_the_drift_entry_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n",
        encoding="utf-8",
    )

    rules.apply_rule("R-GIT-CWD", target, write=True)  # records the drift entry
    second = rules.apply_rule("R-GIT-CWD", target, write=True)

    assert second == ""
    assert second.already_present_unrecorded is False
    assert len(state.load_state()["applied"]) == 1  # not recorded twice


def test_apply_dry_run_reports_drift_without_writing_target_or_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    original = f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n"
    target.write_text(original, encoding="utf-8")

    diff = rules.apply_rule("R-GIT-CWD", target, write=False)

    assert diff == ""
    assert diff.already_present_unrecorded is True
    assert target.read_text(encoding="utf-8") == original
    assert not state.state_path().exists()  # dry run never touches state


def test_apply_dry_run_reports_no_drift_once_already_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    rules.apply_rule("R-GIT-CWD", target, write=True)

    diff = rules.apply_rule("R-GIT-CWD", target, write=False)

    assert diff == ""
    assert diff.already_present_unrecorded is False


def test_apply_write_to_the_same_target_from_two_processes_loses_no_rule(tmp_path):
    """docs/DESIGN.md 9: the state lock also serializes concurrent applies
    to the same target file (not only concurrent state.json writers). Two
    processes apply two different rules to the same target at once; both
    must end up present in the target and both recorded in state -- never
    a last-writer-wins target with only one rule and two state records."""
    state_home = tmp_path / "xdg-state"
    target = tmp_path / "CLAUDE.md"
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    go_file = tmp_path / "go"

    worker_script = textwrap.dedent(
        f"""
        import os
        os.environ["XDG_STATE_HOME"] = {str(state_home)!r}
        import sys
        sys.path.insert(0, {src_dir!r})
        import time
        from agent_postmortem import rules

        # A start barrier: every worker waits for the parent's 'go' file so
        # both processes are racing for the lock at (as close to) the same
        # time, then a short sleep while holding the transaction widens the
        # window a broken implementation would need to interleave in.
        go_file = {str(go_file)!r}
        while not os.path.exists(go_file):
            time.sleep(0.005)

        real_atomic_write = rules._atomic_write

        def _slow_atomic_write(path, content):
            time.sleep(0.005)
            return real_atomic_write(path, content)

        rules._atomic_write = _slow_atomic_write
        rules.apply_rule(sys.argv[1], {str(target)!r}, write=True)
        """
    )

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker_script, rule_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for rule_id in ("R-GIT-CWD", "R-RETRY-SAFE")
    ]
    go_file.write_text("go", encoding="utf-8")
    outputs = [proc.communicate(timeout=30) for proc in procs]
    for proc, (_out, err) in zip(procs, outputs):
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    text = target.read_text(encoding="utf-8")
    assert f"- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}" in text
    assert f"- R-RETRY-SAFE: {rules.RULES['R-RETRY-SAFE'].text}" in text
    assert text.count(rules.BEGIN_MARKER) == 1

    applied = state.load_state(state_home / "agent-postmortem" / "state.json")["applied"]
    assert {entry["rule_id"] for entry in applied} == {"R-GIT-CWD", "R-RETRY-SAFE"}
    assert len(applied) == 2


# --- cli apply refusal on a run directory ----------------------------------


def test_apply_refuses_to_write_inside_a_workflows_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run_dir = tmp_path / "project" / "session" / "subagents" / "workflows" / "wf_x"
    run_dir.mkdir(parents=True)
    target = run_dir / "notes.md"

    code = main(["apply", "R-RETRY-SAFE", "--target", str(target), "--write"])

    assert code == 1
    assert not target.exists()
