"""Smoke tests for the postmortem CLI: right exit codes, right header, valid JSON."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import agent_postmortem
from agent_postmortem import __version__, rules, state, trend
from agent_postmortem.cli import _build_parser, _top_detector, main
from agent_postmortem.detectors import codex_write_denied
from agent_postmortem.models import Finding

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs" / "reader_minimal"
MAIN_RUN = (
    FIXTURES_ROOT
    / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000"
    / "subagents"
    / "workflows"
    / "wf_b0000000-002"
)
DEMO_RUN = Path(agent_postmortem.__file__).parent / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"


def test_run_on_fixture_path_exits_four_and_prints_run_header(capsys):
    # wf_b0000000-002's agent-a1111111111111111.jsonl carries one
    # undecodable line (NOT-JSON-AT-ALL, docs/DESIGN.md 4.6) out of five, a
    # 20% rate well past the 5% threshold: this run's status is INCOMPLETE
    # (docs/DESIGN.md 4.6), and since it has no critical finding, exit code
    # 4 (INCOMPLETE without FAILED), not 0.
    code = main(["run", str(MAIN_RUN)])
    captured = capsys.readouterr()
    assert code == 4, captured.err
    assert captured.out.splitlines()[0].startswith("RUN wf_b0000000-002")
    assert "(INCOMPLETE)" in captured.out.splitlines()[0]


def test_run_json_flag_emits_valid_json(capsys):
    code = main(["run", str(MAIN_RUN), "--json"])
    captured = capsys.readouterr()
    # See test_run_on_fixture_path_exits_four_and_prints_run_header: this
    # fixture's status is INCOMPLETE, so exit code 4, not 0.
    assert code == 4, captured.err
    payload = json.loads(captured.out)
    assert payload["run"]["run_id"] == "wf_b0000000-002"
    assert "findings" in payload
    assert "verdict" in payload
    assert payload["status"] == "INCOMPLETE"


# --- unit M3c: user config (docs/DESIGN.md 4.10) at the CLI boundary -------


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_run_exits_one_with_a_clear_message_when_off_names_a_critical_detector(
    tmp_path, monkeypatch, capsys,
):
    # N5b (4.13.4): no shipped detector is critical any more, so the refusal
    # is exercised by a detector that declares it.
    monkeypatch.setattr(codex_write_denied.DETECTOR, "severity_default", "critical")
    config_path = _write_config(tmp_path, '[detectors]\noff = ["codex_write_denied"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    code = main(["run", str(MAIN_RUN)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "codex_write_denied" in captured.err
    assert "critical" in captured.err.lower()


def test_run_warns_on_stderr_for_an_unknown_detector_id_but_still_runs(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path, '[detectors]\noff = ["not_a_real_detector"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    code = main(["run", str(MAIN_RUN)])
    captured = capsys.readouterr()
    assert code == 4, captured.err  # unchanged from the no-config baseline
    assert "not_a_real_detector" in captured.err
    assert captured.out.splitlines()[0].startswith("RUN wf_b0000000-002")


def test_run_warns_on_stderr_for_malformed_toml_and_silences_nothing(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path, "not [valid toml")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    code = main(["run", str(MAIN_RUN)])
    captured = capsys.readouterr()
    assert code == 4, captured.err
    assert "could not parse" in captured.err
    assert "silenced by config" not in captured.out


def test_run_with_a_missing_config_file_silences_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "does-not-exist.toml"))
    code = main(["run", str(MAIN_RUN)])
    captured = capsys.readouterr()
    assert code == 4, captured.err
    assert captured.err == ""
    assert "silenced by config" not in captured.out


INCOMPLETE_JOURNAL_RUN = Path(__file__).parent / "fixtures" / "runs" / "incomplete_journal"


def test_run_on_incomplete_journal_fixture_exits_four_without_a_critical_finding(capsys):
    """docs/DESIGN.md 4.6: a journal with an undecodable line makes
    Run.status INCOMPLETE; with no critical finding (this run is CLEAN
    otherwise) that is exit code 4, not 0."""
    code = main(["run", str(INCOMPLETE_JOURNAL_RUN)])
    captured = capsys.readouterr()
    assert code == 4, captured.err
    assert "(INCOMPLETE)" in captured.out.splitlines()[0]
    assert "CLEAN" in captured.out.splitlines()[0]


def _finding(severity, detector):
    return Finding(
        detector=detector, severity=severity, title="t", run_id="wf_x", agent_id=None,
        evidence=[], metrics={}, rule_id=None,
    )


def test_top_detector_returns_none_for_info_only_findings():
    """unit M1d: an info-only finding (wasted_time, present on every run)
    must never be reported as the 'top detector' of an otherwise clean run."""
    assert _top_detector([]) is None
    assert _top_detector([_finding("info", "wasted_time")]) is None
    assert _top_detector([_finding("minor", "bash_timeout")]) is None


def test_top_detector_returns_the_major_or_critical_detector_with_the_most_findings():
    findings = [
        _finding("info", "wasted_time"),
        _finding("minor", "bash_timeout"),
        _finding("major", "silent_retry"),
        _finding("major", "silent_retry"),
        _finding("critical", "codex_write_denied"),
    ]
    assert _top_detector(findings) == "silent_retry"


def test_scan_with_no_runs_under_root_exits_two(tmp_path, capsys):
    code = main(["scan", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "no runs found" in captured.err


def test_run_with_unknown_run_id_exits_two(capsys):
    code = main(["run", "wf_does-not-exist", "--root", str(FIXTURES_ROOT)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""


def test_run_with_ambiguous_prefix_exits_one_and_lists_candidates(tmp_path, capsys):
    """docs/DESIGN.md 4.8: RUN_ID may be a prefix of either kind; a prefix
    matching more than one run is a usage error listing every candidate."""
    project = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    for suffix in ("001", "002"):
        run_dir = project / "subagents" / "workflows" / f"wf_dup00000-{suffix}"
        run_dir.mkdir(parents=True)
        (run_dir / "journal.jsonl").write_text("", encoding="utf-8")

    code = main(["run", "wf_dup", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "matches more than one run" in captured.err
    # N3b P13: at share a candidate is listed by its id only when it has a
    # run id's shape (wf_ plus 8 hex, dash, 3 hex); these two directory names
    # do not, so they are spans (value-N), never echoed. The canonical case is
    # in tests/test_n3b_handler_errors.py.
    assert "wf_dup00000" not in captured.err
    assert "value-2" in captured.err

    code = main(["run", "wf_dup", "--root", str(tmp_path), "--raw"])
    captured = capsys.readouterr()
    assert code == 1
    assert "wf_dup00000-001" in captured.err
    assert "wf_dup00000-002" in captured.err


def test_run_on_a_session_jsonl_path_exits_zero(capsys):
    session_path = (
        Path(__file__).parent / "fixtures" / "runs" / "session_clean" / "-home-user-project"
        / "00000000-0000-4000-8000-000000000000.jsonl"
    )
    code = main(["run", str(session_path)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    # unit M3a: a session run's header word is "SESSION", not "RUN ... (session)".
    assert captured.out.splitlines()[0].startswith("SESSION ")


def test_run_on_an_arbitrary_file_is_refused_not_reported_clean(tmp_path, capsys):
    """A file that happens to exist (for example README.md) is not a run
    directory and must never be silently scored CLEAN with zero attempts."""
    readme = tmp_path / "README.md"
    readme.write_text("not a run\n", encoding="utf-8")
    code = main(["run", str(readme)])
    captured = capsys.readouterr()
    assert code == 2
    assert "CLEAN" not in captured.out
    assert captured.out == ""


def test_run_on_a_directory_without_a_journal_is_refused_not_reported_clean(tmp_path, capsys):
    empty_dir = tmp_path / "not-a-run"
    empty_dir.mkdir()
    code = main(["run", str(empty_dir)])
    captured = capsys.readouterr()
    assert code == 2
    assert "CLEAN" not in captured.out
    assert captured.out == ""


def test_rules_all_lists_the_full_library_even_with_no_runs(tmp_path, capsys):
    code = main(["rules", "--root", str(tmp_path), "--all"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "R-RETRY-SAFE" in captured.out
    assert "R-MODEL-ALLOCATION" in captured.out


def test_rules_json_flag_emits_valid_json(tmp_path, capsys):
    """M8 review D2/D6: rules gains --json like the other commands.

    unit M3c (docs/DESIGN.md 5.3): the bare list became {schema, rules} so
    this command's JSON carries the same schema-2 marker as run/scan/
    trend/demo's."""
    code = main(["rules", "--root", str(tmp_path), "--all", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["schema"] == 3
    assert any(row["rule_id"] == "R-RETRY-SAFE" for row in payload["rules"])


def test_rules_show_json_flag_emits_valid_json(capsys):
    code = main(["rules", "--show", "R-RETRY-SAFE", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["rule_id"] == "R-RETRY-SAFE"


def test_rules_no_runs_error_is_redacted_by_default_and_raw_with_no_redact(capsys):
    """M8 review D2: rules' own error messages route through the active
    Redactor exactly like run/scan/apply/trend's do -- a --root under the
    user's home is exactly the kind of path redaction exists for."""
    missing_root = Path.home() / "agent-postmortem-fixture-missing-root"
    assert not missing_root.exists()

    code = main(["rules", "--root", str(missing_root)])
    captured = capsys.readouterr()
    assert code == 2
    assert str(Path.home()) not in captured.err
    # N3b P13: the root is a span (it was "~/.../name" before).
    assert "path-1" in captured.err

    code = main(["rules", "--root", str(missing_root), "--no-redact"])
    captured = capsys.readouterr()
    assert code == 2
    assert str(missing_root) in captured.err


def test_apply_dry_run_exits_zero_and_prints_a_diff(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"
    code = main(["apply", "R-RETRY-SAFE", "--target", str(target)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert not target.exists()
    assert "R-RETRY-SAFE" in captured.out


def test_apply_dry_run_diff_is_redacted_by_default_and_raw_with_no_redact(capsys):
    """M8 review D2: apply's own diff (a unified diff quotes its target path
    twice, in the --- and +++ headers) routes through the active Redactor
    exactly like its error messages already do."""
    target = Path.home() / "agent-postmortem-fixture-apply-target.md"
    assert not target.exists()

    code = main(["apply", "R-RETRY-SAFE", "--target", str(target)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert not target.exists()
    assert str(Path.home()) not in captured.out
    # N3b P18: at share the target is path-N (it was "~/.../name" before).
    assert "path-1" in captured.out

    code = main(["apply", "R-RETRY-SAFE", "--target", str(target), "--no-redact"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert str(target) in captured.out


def test_apply_reports_already_present_message_and_bytes_are_unchanged(tmp_path, capsys, monkeypatch):
    """Changed from testing --write's drift-recording message ("already
    present, recorded", removed with --write and the state entry it wrote,
    docs/DESIGN.md 9): the only remaining fact is the plain "already
    present" message, and that the target's bytes never move."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    original = f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n"
    target.write_text(original, encoding="utf-8")

    # --no-redact: the target path itself is what this test checks, and a
    # tmp_path can legitimately embed the machine's own account name, which
    # --no-redact is exactly the flag that stops from being replaced.
    code = main(["apply", "R-GIT-CWD", "--target", str(target), "--no-redact"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert f"already present in {target}; nothing to add" in captured.out
    assert "Apply it yourself" in captured.out
    assert target.read_text(encoding="utf-8") == original
    assert not state.state_path().exists()


def test_apply_never_records_state_even_when_the_rule_is_already_present(tmp_path, capsys, monkeypatch):
    """Changed from asserting a drift entry gets recorded (removed along
    with --write, docs/DESIGN.md 9): apply never touches the state store,
    present or absent, so no state.json is ever created."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"{rules.BEGIN_MARKER}\n- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text}\n{rules.END_MARKER}\n",
        encoding="utf-8",
    )

    code = main(["apply", "R-GIT-CWD", "--target", str(target)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert not state.state_path().exists()


def test_apply_repeated_calls_show_the_same_diff_since_nothing_persists(tmp_path, capsys):
    """Changed from testing that a second --write reports plain "nothing to
    do" once the first write already applied the rule (removed, docs/
    DESIGN.md 9): since apply never writes, target never exists after
    either call, so both calls show the identical, still non-empty diff."""
    target = tmp_path / "CLAUDE.md"

    main(["apply", "R-GIT-CWD", "--target", str(target)])
    first = capsys.readouterr().out
    code = main(["apply", "R-GIT-CWD", "--target", str(target)])
    second = capsys.readouterr().out

    assert code == 0
    assert "R-GIT-CWD" in first
    assert first == second
    assert not target.exists()


def test_apply_error_message_is_redacted_by_default_and_raw_with_no_redact(tmp_path, capsys, monkeypatch):
    """docs/DESIGN.md 5: every CLI error message that may carry a path
    passes through the active Redactor before printing."""
    target = tmp_path / "CLAUDE.md"
    session_id = "00000000-0000-4000-8000-000000000000"

    def _boom(rule_id, target):
        raise OSError(f"could not lock state.lock (session {session_id})")

    monkeypatch.setattr(rules, "apply_rule", _boom)

    code = main(["apply", "R-GIT-CWD", "--target", str(target)])
    captured = capsys.readouterr()
    assert code == 1
    assert session_id not in captured.err
    # N3b P18: at share an apply error is a fixed sentence naming the target
    # as path-N; the exception text (which may carry a path or target bytes)
    # is no longer echoed, so the redacted form "uuid:0000" is gone as well.
    assert "uuid:0000" not in captured.err
    assert "path-1" in captured.err

    code = main(["apply", "R-GIT-CWD", "--target", str(target), "--no-redact"])
    captured = capsys.readouterr()
    assert code == 1
    assert session_id in captured.err


def test_apply_on_a_symlinked_target_exits_zero_and_reads_through_it(tmp_path, capsys):
    """Changed from expecting exit 1 with a symlink refusal (removed, docs/
    DESIGN.md 9): that refusal was specific to the write path apply_rule no
    longer has. Reading through a symlink is now the ordinary case."""
    real_target = tmp_path / "real.md"
    real_target.write_text("# rules\n", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(real_target)

    code = main(["apply", "R-RETRY-SAFE", "--target", str(link)])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "R-RETRY-SAFE" in captured.out
    assert real_target.read_text(encoding="utf-8") == "# rules\n"


def test_apply_write_is_an_unrecognized_argument(tmp_path, capsys):
    """docs/DESIGN.md 9, R3: --write is removed entirely -- argparse itself
    must refuse it, which raises SystemExit(1) (cli._Parser.error), the
    same as --version does (test_version_flag_prints_version_and_exits_zero)."""
    target = tmp_path / "CLAUDE.md"

    try:
        code = main(["apply", "R-RETRY-SAFE", "--target", str(target), "--write"])
    except SystemExit as exc:
        code = exc.code
    capsys.readouterr()

    assert code == 1
    assert not target.exists()


def test_apply_closing_sentence_never_claims_it_wrote_the_target(tmp_path, capsys):
    target = tmp_path / "CLAUDE.md"

    # --no-redact: tmp_path can embed the machine's own account name, which
    # would otherwise be replaced and break the exact-text comparison below.
    code = main(["apply", "R-RETRY-SAFE", "--target", str(target), "--no-redact"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    last_line = captured.out.strip().splitlines()[-1]
    assert last_line == (
        "Apply it yourself: paste the block into the target file, or save this "
        "output and run git apply."
    )
    assert not target.exists()


def test_scan_wasted_hours_reflects_the_wasted_time_metric(capsys):
    """wf_b0000000-002 has one killed, non-final attempt (5 minutes); the
    fleet total must reflect it, not stay 0.0h. This fixture root also
    contains that same run's INCOMPLETE status (docs/DESIGN.md 4.6), and no
    FAILED run, so scan's own exit code is 4, matching 'run' on that run
    alone (see test_run_on_fixture_path_exits_four_and_prints_run_header)."""
    code = main(["scan", "--root", str(FIXTURES_ROOT), "--json"])
    captured = capsys.readouterr()
    assert code == 4, captured.err
    payload = json.loads(captured.out)
    assert payload["totals"]["workflow"]["wasted_hours"] > 0.0



def _write_failed_notification(root: Path, wf_id: str) -> None:
    """A parent transcript whose Workflow launch ends in a `failed`
    notification (docs/DESIGN.md 4.13.3), beside the 00000000-... session
    directory the tests above use."""
    from datetime import datetime, timedelta, timezone

    from tests.test_field_v03 import (
        _envelope,
        _notification_entry,
        _workflow_launch,
        _workflow_launch_result,
        _write_jsonl,
    )

    base = datetime(2026, 9, 1, 9, 59, tzinfo=timezone.utc)
    _write_jsonl(root / "-home-user-project" / "00000000-0000-4000-8000-000000000000.jsonl", [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(base + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed")),
    ])


def test_scan_exits_three_when_any_run_is_failed(tmp_path, capsys):
    """docs/DESIGN.md 4.6/4.7: scan's exit code must reflect the worst
    verdict across the fleet, the same as 'run' does for a single run
    (exit code 3, verdict FAILED, whenever any critical finding fires)."""
    project = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    run_dir = project / "subagents" / "workflows" / "wf_00000000-930"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(
        '{"type":"started","key":"v2:a","agentId":"a1111111111111111",'
        '"label":"impl:x","phase":"Implement"}\n',
        encoding="utf-8",
    )
    (run_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":'
        '"write the summary to /tmp/agent-postmortem-scan-exit-930.md"},'
        '"timestamp":"2026-09-01T10:00:00Z"}\n',
        encoding="utf-8",
    )
    # N5b (4.6, 4.13.4): FAILED is reached through the run's own lifecycle
    # terminal (no detector is critical any more, and a missing artifact is
    # info), so the harness reports this workflow failed in the parent
    # transcript.
    _write_failed_notification(tmp_path, "wf_00000000-930")
    code = main(["scan", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 3, captured.err
    assert "FAILED" in captured.out


def test_scan_exits_four_when_a_run_is_incomplete_without_any_failed(tmp_path, capsys):
    """docs/DESIGN.md 4.6/4.7: exit code 4 (INCOMPLETE without FAILED) when
    no scanned run is FAILED but at least one is INCOMPLETE."""
    project = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    run_dir = project / "subagents" / "workflows" / "wf_00000000-940"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(
        "NOT-VALID-JSON\n"
        '{"type":"started","key":"v2:a","agentId":"a1111111111111111",'
        '"label":"impl:x","phase":"Implement"}\n',
        encoding="utf-8",
    )
    code = main(["scan", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 4, captured.err
    assert "INCOMPLETE" in captured.out


def test_scan_exits_zero_when_every_run_is_clean_and_complete(tmp_path, capsys):
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    code = main(["scan", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0, captured.err


def test_scan_shows_dash_for_a_clean_runs_top_detector(tmp_path, capsys):
    """unit M1d: this session is CLEAN but still carries an info-only
    wasted_time finding; the row must show '-', never 'wasted_time'.

    unit M3b: a 0-subagent CLEAN session is never a NEEDS ATTENTION
    candidate, so this row only ever shows up in the full --all list.
    """
    session_path = _write_minimal_session(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000",
    )
    # the file's own mtime matches its one entry (2026-09-01), as a finished session's would; a
    # fresh mtime would make the session possibly running, which the --all row now says
    # (release review of 0.3.0)
    old = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc).timestamp()
    os.utime(session_path, (old, old))

    code = main(["scan", "--root", str(tmp_path), "--all"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    row_line = next(line for line in captured.out.splitlines() if line.startswith("s_"))
    assert row_line.rstrip().endswith("CLEAN  -")

    code = main(["scan", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["rows"][0]["verdict"] == "CLEAN"
    assert payload["rows"][0]["top_detector"] is None


def test_scan_runs_with_retries_ignores_an_unkeyed_attempt(tmp_path, capsys):
    """A run whose only 'started' journal event carries no key must not be
    counted as a run with retries: it has one agent, one attempt, zero
    actual restarts."""
    project = tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    run_dir = project / "subagents" / "workflows" / "wf_00000000-999"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(
        '{"type":"started","agentId":"a9999999999999999","label":"impl:x","phase":"Implement"}\n',
        encoding="utf-8",
    )
    code = main(["scan", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["totals"]["workflow"]["runs_with_retries"] == 0


def test_trend_with_no_runs_under_root_exits_two(tmp_path, capsys):
    code = main(["trend", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "no runs found" in captured.err


def test_trend_still_counts_a_detector_silenced_by_config(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 4.10: 'a silenced detector still runs ... and still
    counts in trend' -- unlike run/scan/demo, trend is never wired to
    config.partition_silenced at all, so silencing silent_retry here must
    not change its weekly count. wf_b0000000-002 (started 2026-09-01) fires
    silent_retry; --weeks defaults to 8, so the clock is pinned to
    2026-09-30, inside that window. Left on the real clock, the 2026-09-01
    week fell out of the window on 2026-10-26 and the test failed."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[detectors]\noff = ["silent_retry"]\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    pinned_now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    real_weekly_counts = trend.weekly_counts
    monkeypatch.setattr(
        trend, "weekly_counts",
        lambda runs, weeks, now=None: real_weekly_counts(runs, weeks, now=now or pinned_now),
    )

    code = main(["trend", "--root", str(FIXTURES_ROOT), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["schema"] == 3
    silent_retry_weeks = payload["weekly_counts"].get("silent_retry", {})
    assert sum(silent_retry_weeks.values()) == 1


def test_trend_state_read_error_message_is_redacted_by_default_and_raw_with_no_redact(
    tmp_path, capsys, monkeypatch
):
    """docs/DESIGN.md 5: every CLI error message that may carry a path
    passes through the active Redactor before printing, including a
    filesystem error reading the state file."""
    session_id = "00000000-0000-4000-8000-000000000000"

    def _boom(path=None):
        raise OSError(f"permission denied reading state (session {session_id})")

    monkeypatch.setattr(state, "load_state", _boom)

    code = main(["trend", "--root", str(FIXTURES_ROOT)])
    captured = capsys.readouterr()
    assert code == 1
    assert session_id not in captured.err
    assert "uuid:0000" in captured.err

    code = main(["trend", "--root", str(FIXTURES_ROOT), "--no-redact"])
    captured = capsys.readouterr()
    assert code == 1
    assert session_id in captured.err


def test_trend_redacts_applied_rule_target_by_default(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 5.1: by default a trend target renders as
    'target-N (basename)', never the raw path (structural redact() alone
    would leave everything past the home directory, such as a client name
    in the path, untouched); the raw path only shows with --no-redact."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    real_home_target = Path.home() / ".claude" / "CLAUDE.md"
    entry = {"rule_id": "R-RETRY-SAFE", "target": str(real_home_target), "applied_at": state.now_iso()}
    state.update(lambda s: s["applied"].append(entry))

    code = main(["trend", "--root", str(FIXTURES_ROOT)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert str(Path.home()) not in captured.out
    assert "target-1 (CLAUDE.md)" in captured.out

    code = main(["trend", "--root", str(FIXTURES_ROOT), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert str(Path.home()) not in captured.out
    payload = json.loads(captured.out)
    assert payload["before_after"][0]["target"] == "target-1 (CLAUDE.md)"

    code = main(["trend", "--root", str(FIXTURES_ROOT), "--no-redact"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert str(real_home_target) in captured.out


def test_scan_tolerates_a_stray_workflow_dir_without_journal(tmp_path, capsys):
    """docs/DESIGN.md 4.2: discover_runs finds every wf_* directory
    regardless of contents; a stray one with no journal.jsonl must not
    abort the whole fleet scan with an uncaught NotARun. It still loads (with
    a missing-journal diagnostic, docs/DESIGN.md 4.6), so its status is
    INCOMPLETE and scan's own exit code reflects that (4, not 0)."""
    stray = (
        tmp_path / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
        / "subagents" / "workflows" / "wf_00000000-777"
    )
    stray.mkdir(parents=True)
    code = main(["scan", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 4, captured.err
    payload = json.loads(captured.out)
    assert payload["totals"]["workflow"]["runs"] == 1


def _write_minimal_session(root: Path, slug: str, uuid: str) -> Path:
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    session_path = slug_dir / f"{uuid}.jsonl"
    session_path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},'
        f'"sessionId":"{uuid}","timestamp":"2026-09-01T10:00:00Z"}}\n',
        encoding="utf-8",
    )
    return session_path


def _write_minimal_workflow(root: Path, slug: str, uuid: str, wf_id: str) -> Path:
    run_dir = root / slug / uuid / "subagents" / "workflows" / wf_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "journal.jsonl").write_text("", encoding="utf-8")
    return run_dir


def test_scan_kind_filter_session_only_in_text_and_json(tmp_path, capsys):
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-820"
    )

    # unit M3b: the default triage view lists only NEEDS ATTENTION
    # candidates, and its own totals lines always mention "workflow runs"
    # even at zero (the word this test's row-only check needs to rule out
    # of); --all restores the plain row-per-run list this test pins.
    code = main(["scan", "--root", str(tmp_path), "--kind", "session", "--all"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    # Row lines carry each run's own kind and must reflect the filter; the
    # stratified totals section (docs/DESIGN.md 4.7, M3b) always shows a
    # "workflow runs" line, even at zero, so only the row lines are checked
    # for the literal word "workflow".
    row_lines = captured.out.split("\n\n", 1)[0]
    assert "session" in row_lines
    assert "workflow" not in row_lines
    assert "0 workflow runs" in captured.out

    code = main(["scan", "--root", str(tmp_path), "--kind", "session", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["kind"] == "session"


def test_scan_kind_filter_workflow_only_in_json(tmp_path, capsys):
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-820"
    )
    code = main(["scan", "--root", str(tmp_path), "--kind", "workflow", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["kind"] == "workflow"


def test_scan_text_output_shows_the_kind_column(tmp_path, capsys):
    # unit M3b: this 0-subagent CLEAN session is never a NEEDS ATTENTION
    # candidate, so the kind column is only visible in the --all row list.
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    code = main(["scan", "--root", str(tmp_path), "--all"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "session" in captured.out.splitlines()[0]


def test_scan_totals_are_stratified_by_kind(tmp_path, capsys):
    """docs/DESIGN.md 4.7: scan's totals are stratified by kind, not one
    combined count -- a fleet with both kinds must report workflow and
    session numbers separately, each over its own runs."""
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-820"
    )
    code = main(["scan", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    totals = payload["totals"]
    assert totals["workflow"]["runs"] == 1
    assert totals["session"]["runs"] == 1
    assert set(totals["workflow"]) == {"runs", "runs_with_retries", "attempts", "wasted_hours"}
    assert set(totals["session"]) == {"runs", "subagents", "interrupted"}

    code = main(["scan", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "workflow runs" in captured.out
    assert "session runs" in captured.out


def test_scan_marks_an_unfinished_workflow_row_unfinished(tmp_path, capsys):
    """docs/DESIGN.md 12's D-C: a workflow whose journal has a 'started'
    key with no matching 'result' (Run.finished is False) gets its verdict
    followed by an 'unfinished' marker on its scan row (not replaced by
    one, docs/DESIGN.md 4.7), so `latest` can skip it."""
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    workflow_dir = _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-820"
    )
    (workflow_dir / "journal.jsonl").write_text(
        '{"type":"started","timestamp":"2026-09-01T10:00:00Z","key":"v2:a",'
        '"agentId":"a1111111111111111","label":"impl:a","phase":"Implement"}\n',
        encoding="utf-8",
    )
    # unit M3b: this 0-subagent CLEAN session is never a NEEDS ATTENTION
    # candidate (and the workflow's own row-with-marker format below is the
    # --all list's shape), so --all restores the row-per-run list this test
    # pins.
    code = main(["scan", "--root", str(tmp_path), "--all"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    lines = {line.split("  ")[0]: line for line in captured.out.splitlines() if line.startswith("wf_") or line.startswith("s_")}
    workflow_line = next(line for run_id, line in lines.items() if run_id.startswith("wf_"))
    session_line = next(line for run_id, line in lines.items() if run_id.startswith("s_"))
    assert "CLEAN unfinished" in workflow_line
    assert "unfinished" not in session_line

    code = main(["scan", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    rows_by_kind = {row["kind"]: row for row in payload["rows"]}
    assert rows_by_kind["workflow"]["finished"] is False
    # docs/DESIGN.md 4.13.3 (v0.3, N2, D11): a session's own Run.finished is
    # null, not true -- it has no lifecycle model to report at all.
    assert rows_by_kind["session"]["finished"] is None


def test_run_on_an_unfinished_workflow_still_computes_and_prints_findings(tmp_path, capsys):
    """docs/DESIGN.md 12's D-C: `run` on a workflow that has not finished
    still computes and prints its real findings and verdict -- an
    unfinished, retried, or abandoned run is exactly the run a user most
    wants to inspect, so hiding it behind a bare "no verdict yet" line
    would hide the most interesting runs. The header just adds an
    '(unfinished)' tag; a critical finding still exits 3, same as any
    other run (docs/DESIGN.md 4.6's exit codes are unaffected by
    finished-ness)."""
    workflow_dir = _write_minimal_workflow(
        tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000", "wf_00000000-960",
    )
    (workflow_dir / "journal.jsonl").write_text(
        '{"type":"started","timestamp":"2026-09-01T10:00:00Z","key":"v2:a",'
        '"agentId":"a1111111111111111","label":"impl:a","phase":"Implement"}\n',
        encoding="utf-8",
    )
    (workflow_dir / "agent-a1111111111111111.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":'
        '"write the summary to /tmp/agent-postmortem-scan-exit-960.md"},'
        '"timestamp":"2026-09-01T10:00:01Z"}\n',
        encoding="utf-8",
    )
    code = main(["run", str(workflow_dir)])
    captured = capsys.readouterr()
    # missing_artifact fires: the message names a path that was never
    # actually created -- exactly the kind of real finding the old
    # short-circuit hid. N5b (D2): it is an info finding now (the check is
    # made later, so it proves nothing about the end of the run), which
    # moves the verdict to CLEAN and the exit code to 0; --verbose shows it.
    assert code == 0, captured.err
    # unit M3a: the header line now continues past the verdict tag with
    # "ended ...", so it is a prefix check, not an exact first-line match;
    # FINDINGS is gone from the default report (--verbose only).
    assert captured.out.startswith("RUN wf_00000000-960  CLEAN (unfinished)  ")
    assert "missing_artifact" not in captured.out  # detector id: verbose-only
    assert "STORIES (0)" in captured.out
    assert "2 info findings not shown (--verbose)" in captured.out
    code = main(["run", str(workflow_dir), "--verbose"])
    verbose = capsys.readouterr().out
    assert code == 0
    assert "; the tool cannot tell whether it existed when the run ended" in verbose


def test_run_resolves_a_session_by_a_unique_s_prefix(tmp_path, capsys):
    _write_minimal_session(tmp_path, "-home-user-project", "00000000-0000-4000-8000-000000000000")
    code = main(["scan", "--root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["rows"][0]["run_id"]
    assert run_id.startswith("s_")

    code = main(["run", run_id[:5], "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    # unit M3a: a session run's header word is "SESSION", not "RUN".
    assert captured.out.splitlines()[0].startswith(f"SESSION {run_id}")


def test_json_export_omits_event_text(capsys):
    # docs/DESIGN.md 5.3 (M3b): the export allowlist carries event_count, not
    # an event list at all -- no per-event dict to check "text" against
    # (findings carry curated, redacted evidence instead; the JSON export
    # must not dump every tool-output excerpt, for privacy and size).
    fixture = DEMO_RUN
    rc = main(["run", str(fixture), "--json"])
    assert rc in (0, 3)
    data = json.loads(capsys.readouterr().out)
    attempt = data["run"]["attempts"][0]
    assert "events" not in attempt
    assert "text" not in attempt
    assert attempt["event_count"] > 0


# --- CLAUDE_CONFIG_DIR ------------------------------------------------------


def test_default_root_honors_claude_config_dir(tmp_path, monkeypatch):
    """docs/DESIGN.md 4.8: default --root is ~/.claude/projects, overridable
    by CLAUDE_CONFIG_DIR (first entry when comma-separated), for parity
    with ccusage."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    parser = _build_parser()
    args = parser.parse_args(["scan"])
    assert args.root == tmp_path / "projects"


def test_default_root_uses_the_first_of_several_comma_separated_dirs(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{first},{second}")
    parser = _build_parser()
    args = parser.parse_args(["scan"])
    assert args.root == first / "projects"


def test_default_root_falls_back_to_dot_claude_without_the_env_var(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["scan"])
    assert args.root == Path("~/.claude/projects").expanduser()


def test_claude_config_dir_actually_changes_where_scan_looks(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "custom-config"
    _write_minimal_session(config_dir / "projects", "-home-user-project", "00000000-0000-4000-8000-000000000000")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    code = main(["scan"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "session" in captured.out


# --- --version ---------------------------------------------------------


def test_version_flag_prints_version_and_exits_zero(capsys):
    try:
        code = main(["--version"])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == f"agent-postmortem {__version__}"


# --- demo (docs/DESIGN.md 8.2) -----------------------------------------


def test_demo_renders_the_packaged_run_through_the_normal_report_path(capsys):
    code = main(["demo"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out.startswith("RUN ")
    # docs/DESIGN.md 5.1: the packaged run's own label is pseudonymized by
    # default, same as any other run's -- demo is not a redaction exemption.
    assert "impl:silent-alpha" not in captured.out


def test_demo_json_flag_emits_valid_json_for_the_packaged_run(capsys):
    code = main(["demo", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["run"]["attempts"]


def test_demo_loads_the_run_that_actually_lives_in_the_package(capsys):
    assert DEMO_RUN.is_dir()
    assert (DEMO_RUN / "journal.jsonl").exists()


def test_demo_exit_code_matches_run_exit_code_on_the_packaged_run(capsys):
    """docs/DESIGN.md 8.2's D5: demo is rendered through the normal run
    path end to end, exit code included -- it must never diverge from
    what `run` itself reports for the exact same directory."""
    demo_code = main(["demo"])
    capsys.readouterr()
    run_code = main(["run", str(DEMO_RUN)])
    capsys.readouterr()
    assert demo_code == run_code


def test_demo_exit_code_reflects_a_critical_finding_not_hardcoded_zero(monkeypatch, capsys):
    """docs/DESIGN.md 8.2's D5: demo derives its exit code exactly like run
    (3 critical, 4 INCOMPLETE, else 0); it must not hardcode 0 regardless
    of what the packaged run's own findings turn out to be. The comparison
    test above cannot expose this on its own, since the packaged demo run
    happens to be DEGRADED (not FAILED/INCOMPLETE) today -- 0 either way."""
    from agent_postmortem import cli as cli_module
    from agent_postmortem.models import Finding

    def _fake_findings(run):
        return [
            Finding(
                detector="fake", severity="critical", title="synthetic",
                run_id=run.run_id, agent_id=None, evidence=[], metrics={}, rule_id=None,
            )
        ]

    monkeypatch.setattr(cli_module, "_run_findings", _fake_findings)
    code = main(["demo"])
    capsys.readouterr()
    assert code == 3
