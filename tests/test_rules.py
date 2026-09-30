"""Tests for rules.py: the RULES library, rules_for, and apply_rule's diff.

See docs/DESIGN.md sections 4.3 and 9. Unit M6: apply_rule() only ever
computes a diff, it never writes a target or the state store -- there is no
`write` argument any more (docs/DESIGN.md 9, R3).
"""

from __future__ import annotations

import os

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
    "R-USAGE-LIMIT",
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


def _finding_with_rule_ids(rule_ids, detector="silent_retry"):
    return Finding(
        detector=detector,
        severity="major",
        title="t",
        run_id="wf_x",
        agent_id=None,
        evidence=[],
        metrics={},
        rule_id=None,
        rule_ids=rule_ids,
    )


# --- RULES ---------------------------------------------------------------


def test_rules_library_has_exactly_eleven_ids():
    assert set(rules.RULES) == set(_ALL_IDS)
    assert len(rules.RULES) == 11


def test_every_rule_has_id_title_text_and_valid_enforcement():
    for rule_id, rule in rules.RULES.items():
        assert rule.id == rule_id
        assert rule.title
        assert rule.text
        assert rule.enforcement in ("prompt", "hook")


def test_only_model_allocation_ships_a_hook_template():
    hooked = [rule.id for rule in rules.RULES.values() if rule.hook_template]
    assert hooked == ["R-MODEL-ALLOCATION"]


def test_target_kind_matches_the_design_mapping():
    """docs/DESIGN.md 9: most rules belong in a workflow's own prompt;
    R-MODEL-ALLOCATION is a hook; R-GIT-CWD is project-wide instructions."""
    by_kind = {}
    for rule in rules.RULES.values():
        by_kind.setdefault(rule.target_kind, set()).add(rule.id)

    assert by_kind["hook"] == {"R-MODEL-ALLOCATION"}
    assert by_kind["project instructions"] == {"R-GIT-CWD"}
    assert by_kind["workflow prompt"] == {
        "R-RETRY-SAFE", "R-CAP-CONCURRENCY", "R-NO-LONG-BASH", "R-SMALL-QUANTUM",
        "R-ASSERT-ARTIFACT", "R-SCALAR-SCHEMA", "R-CODEX-CONTRACT",
        "R-MONITOR-RETRIES", "R-USAGE-LIMIT",
    }
    assert set(by_kind) == {"workflow prompt", "hook", "project instructions"}


def test_render_rule_show_and_rule_to_json_carry_target_kind():
    rule = rules.RULES["R-GIT-CWD"]
    assert "belongs in: project instructions" in rules.render_rule_show(rule)
    assert rules.rule_to_json(rule)["target_kind"] == "project instructions"


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


def test_rules_for_counts_a_finding_once_per_rule_in_rule_ids():
    """unit M1d: a single Finding carrying two rule ids (silent_retry's
    burst case) backs each rule exactly once -- the same arithmetic as if
    it had been two separate findings, without actually being two."""
    findings = [_finding_with_rule_ids(("R-RETRY-SAFE", "R-CAP-CONCURRENCY"))]
    rows = {rule.id: count for rule, count in rules.rules_for(findings)}
    assert rows == {"R-RETRY-SAFE": 1, "R-CAP-CONCURRENCY": 1}


def test_finding_rule_id_and_rule_ids_stay_in_sync():
    """unit M1d: passing only rule_id fills rule_ids in as (rule_id,);
    passing only rule_ids fills rule_id in as rule_ids[0]."""
    only_rule_id = _finding("R-RETRY-SAFE")
    assert only_rule_id.rule_ids == ("R-RETRY-SAFE",)

    only_rule_ids = _finding_with_rule_ids(("R-RETRY-SAFE", "R-CAP-CONCURRENCY"))
    assert only_rule_ids.rule_id == "R-RETRY-SAFE"

    neither = _finding(None)
    assert neither.rule_ids == ()
    assert neither.rule_id is None


# --- apply_rule: computes a diff, never writes (docs/DESIGN.md 9, R3) ------


def test_apply_leaves_file_untouched_and_diff_mentions_rule(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My rules\n- existing rule\n", encoding="utf-8")

    diff = rules.apply_rule("R-RETRY-SAFE", target)

    assert target.read_text(encoding="utf-8") == "# My rules\n- existing rule\n"
    assert "R-RETRY-SAFE" in diff


def test_apply_on_a_target_that_does_not_exist_does_not_create_it(tmp_path):
    target = tmp_path / "new.md"
    diff = rules.apply_rule("R-GIT-CWD", target)
    assert not target.exists()
    assert "R-GIT-CWD" in diff


def test_apply_never_creates_the_managed_block_on_disk(tmp_path):
    """Changed from testing --write's on-disk result (removed, docs/
    DESIGN.md 9): the managed block now only ever appears in the printed
    diff, never written to target."""
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My rules\n- existing rule\n", encoding="utf-8")

    diff = rules.apply_rule("R-RETRY-SAFE", target)

    assert rules.BEGIN_MARKER in diff
    assert rules.END_MARKER in diff
    assert f"- R-RETRY-SAFE: {rules.RULES['R-RETRY-SAFE'].text}" in diff
    assert target.read_text(encoding="utf-8") == "# My rules\n- existing rule\n"


def test_apply_is_idempotent_across_repeated_calls_since_nothing_persists(tmp_path):
    """Changed from testing that a second --write sees an empty diff
    (removed, docs/DESIGN.md 9): since apply never writes, a second call
    against the same untouched target recomputes the identical, still
    non-empty diff -- there is no "already applied" state to reach."""
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My rules\n", encoding="utf-8")

    first = rules.apply_rule("R-RETRY-SAFE", target)
    second = rules.apply_rule("R-RETRY-SAFE", target)

    assert first
    assert first == second
    assert target.read_text(encoding="utf-8") == "# My rules\n"


def test_apply_never_creates_a_target_that_does_not_exist(tmp_path):
    target = tmp_path / "new.md"
    assert not target.exists()

    diff = rules.apply_rule("R-GIT-CWD", target)

    assert not target.exists()
    assert "R-GIT-CWD" in diff


def test_with_rule_applied_preserves_existing_content_outside_the_block_byte_for_byte():
    """Changed from checking target on disk after --write (removed, docs/
    DESIGN.md 9): the byte-preservation guarantee now lives in
    _with_rule_applied, the pure text transform apply_rule's diff is
    computed from -- there is no file write left to check it against."""
    original = "# Team rules\n\n- always write tests first\n- never force push\n"

    new_text = rules._with_rule_applied(original, "R-NO-LONG-BASH")

    assert new_text.startswith(original.rstrip("\n"))


def test_with_rule_applied_preserves_extra_trailing_blank_lines_in_existing_content():
    # Two blank lines at the end of the user's own content: everything
    # outside the managed block must survive byte for byte, not just its
    # non-whitespace prefix.
    original = "# Team rules\n\n- always write tests first\n\n\n"

    new_text = rules._with_rule_applied(original, "R-NO-LONG-BASH")

    assert new_text.startswith(original)


def test_with_rule_applied_composes_across_two_rules_keeping_both_bullets():
    """Changed from applying two rules to the same file with two --write
    calls (removed, docs/DESIGN.md 9): apply_rule has nothing to compose
    across calls any more, since nothing persists between them. The
    composition guarantee (two rules, one block, one BEGIN marker) now
    lives in _with_rule_applied, chained the way a human pasting two
    printed diffs into the same file, one after the other, effectively
    would."""
    text = rules._with_rule_applied("", "R-RETRY-SAFE")
    text = rules._with_rule_applied(text, "R-GIT-CWD")

    assert f"- R-RETRY-SAFE: {rules.RULES['R-RETRY-SAFE'].text}" in text
    assert f"- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}" in text
    assert text.count(rules.BEGIN_MARKER) == 1


def test_apply_never_writes_the_state_store(tmp_path, monkeypatch):
    """Changed from checking a recorded applied entry (removed, docs/
    DESIGN.md 9, R3): apply_rule no longer touches the state store at
    all, so no state file is ever created by calling it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"

    rules.apply_rule("R-GIT-CWD", target)

    assert not state.state_path().exists()


def test_apply_never_touches_the_targets_existing_permission_bits(tmp_path):
    """Changed from checking permission bits survive a write (removed,
    docs/DESIGN.md 9): apply_rule never opens target for writing at all
    now, so its mode can never change."""
    import stat

    target = tmp_path / "CLAUDE.md"
    target.write_text("# Team rules\n", encoding="utf-8")
    target.chmod(0o640)

    rules.apply_rule("R-GIT-CWD", target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_apply_reads_transparently_through_a_symlinked_target(tmp_path):
    """Changed from expecting a ValueError (removed, docs/DESIGN.md 9): the
    old refusal was specific to the write path (os.replace() would have
    swapped the symlink itself for a plain file). apply_rule only ever
    reads target now, so a symlink is the ordinary case, not a refusal."""
    real_target = tmp_path / "real.md"
    real_target.write_text("# Team rules\n", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(real_target)

    diff = rules.apply_rule("R-GIT-CWD", link)

    assert "R-GIT-CWD" in diff
    assert link.is_symlink()
    assert real_target.read_text(encoding="utf-8") == "# Team rules\n"


def test_apply_never_calls_os_replace_or_fsync(tmp_path, monkeypatch):
    """Changed from tracking a real fsync call during --write (removed,
    docs/DESIGN.md 9): the atomic-write machinery (_atomic_write, a temp
    file, fsync, os.replace) is deleted along with --write, so apply_rule
    has no filesystem-write primitive left to call at all."""

    def _boom(*args, **kwargs):
        raise AssertionError("apply_rule must never call this")

    monkeypatch.setattr(os, "replace", _boom)
    monkeypatch.setattr(os, "fsync", _boom)

    target = tmp_path / "CLAUDE.md"
    target.write_text("# Team rules\n", encoding="utf-8")

    diff = rules.apply_rule("R-GIT-CWD", target)

    assert "R-GIT-CWD" in diff


def test_apply_unknown_rule_id_raises():
    import pytest

    with pytest.raises(ValueError):
        rules.apply_rule("R-NOT-A-RULE", "/tmp/does-not-matter.md")


# --- apply_rule never reads or writes the state store -----------------------


def test_apply_ignores_the_state_store_even_when_it_is_too_new(tmp_path, monkeypatch):
    """Changed from expecting StateTooNew to propagate out of a --write
    transaction (removed, docs/DESIGN.md 9): apply_rule no longer opens a
    state.transaction() at all, so a state.json this build cannot even
    read has no effect on it, one way or the other."""
    import json

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state_dir = tmp_path / "state" / "agent-postmortem"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"version": 99, "min_reader": 99, "applied": [], "sessions": {}, "digest": {"pending": []}}),
        encoding="utf-8",
    )
    target = tmp_path / "CLAUDE.md"
    target.write_text("# rules\n", encoding="utf-8")

    diff = rules.apply_rule("R-GIT-CWD", target)

    assert "R-GIT-CWD" in diff
    assert target.read_text(encoding="utf-8") == "# rules\n"


def test_apply_already_present_in_target_returns_empty_diff(tmp_path):
    """Changed from a "drift" ApplyDiff carrying already_present_unrecorded
    (removed along with the state-recording it existed for, docs/DESIGN.md
    9): the only remaining fact is that an already-satisfied target yields
    an empty diff."""
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n",
        encoding="utf-8",
    )

    diff = rules.apply_rule("R-GIT-CWD", target)

    assert diff == ""


def test_apply_already_present_in_target_is_idempotent_across_repeated_calls(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n",
        encoding="utf-8",
    )

    first = rules.apply_rule("R-GIT-CWD", target)
    second = rules.apply_rule("R-GIT-CWD", target)

    assert first == ""
    assert second == ""


def test_apply_already_present_never_touches_target_or_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    original = f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n"
    target.write_text(original, encoding="utf-8")

    diff = rules.apply_rule("R-GIT-CWD", target)

    assert diff == ""
    assert target.read_text(encoding="utf-8") == original
    assert not state.state_path().exists()


def test_apply_returns_empty_diff_regardless_of_state_store_contents(tmp_path, monkeypatch):
    """Changed from checking the drift flag flips once an applied entry
    exists (removed, docs/DESIGN.md 9): apply_rule never reads the state
    store any more, so seeding one with a matching entry changes nothing
    about its result -- target's own content is the only input."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n",
        encoding="utf-8",
    )
    state.update(
        lambda s: s["applied"].append(
            {"rule_id": "R-GIT-CWD", "target": str(target), "applied_at": state.now_iso()}
        )
    )

    diff = rules.apply_rule("R-GIT-CWD", target)

    assert diff == ""


def test_apply_sequential_calls_against_the_same_target_are_independent(tmp_path):
    """Changed from a two-process race against a shared state.transaction()
    lock (removed along with the write path it protected, docs/DESIGN.md
    9): apply_rule is a pure read now, so two calls against the same
    target for two different rules need no synchronization at all -- each
    returns its own correct diff, and the target itself is never touched
    by either one."""
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Team rules\n", encoding="utf-8")

    first = rules.apply_rule("R-GIT-CWD", target)
    second = rules.apply_rule("R-RETRY-SAFE", target)

    assert "R-GIT-CWD" in first
    assert "R-RETRY-SAFE" in second
    assert target.read_text(encoding="utf-8") == "# Team rules\n"


# --- cli apply refusal on a run directory ----------------------------------


def test_apply_refuses_a_target_inside_a_workflows_path(tmp_path):
    run_dir = tmp_path / "project" / "session" / "subagents" / "workflows" / "wf_x"
    run_dir.mkdir(parents=True)
    target = run_dir / "notes.md"

    code = main(["apply", "R-RETRY-SAFE", "--target", str(target)])

    assert code == 1
    assert not target.exists()


def test_apply_write_is_an_unrecognized_argument(tmp_path, capsys):
    """docs/DESIGN.md 9, R3: --write is removed entirely, not merely
    disconnected -- argparse itself must refuse it as a usage error, which
    raises SystemExit(1) (cli._Parser.error), the same as --version does."""
    target = tmp_path / "CLAUDE.md"

    try:
        code = main(["apply", "R-RETRY-SAFE", "--target", str(target), "--write"])
    except SystemExit as exc:
        code = exc.code
    capsys.readouterr()

    assert code == 1
    assert not target.exists()
