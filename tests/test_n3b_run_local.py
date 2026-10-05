"""N3b Part 1b, local and raw levels: `run` text and `run --bug-report`
(P10, P16, P19, D10). These replace 1a's interim "local equals share" tests
for run and the bug report.

local: real label, phase title, agent id, project directory name; no
absolute path (repo-relative, ~/.../name, or path-N); excerpts only with
--evidence. raw: today's output plus the first line; excerpts only with
--evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_postmortem.cli import main
from tests import n3b_planted as plant

FIXTURES = Path(__file__).parent / "fixtures" / "runs"
LOCAL_LABEL = "disclosure: local (real names and paths; do not paste)"
RAW_LABEL = "disclosure: raw (real names, absolute paths; do not paste)"


@pytest.fixture
def planted(tmp_path):
    return plant.build(tmp_path)


def _run(planted, capsys, *flags):
    capsys.readouterr()
    code = main(["run", str(planted.run_dir), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _no_absolute_path(out: str, planted):
    # An excerpt is cut at 160 characters, so a long temporary path can appear
    # truncated: check the leading part of the directory, never just the whole.
    tmp = str(planted.repo.parent)
    for fragment in (tmp, tmp[:45], str(planted.repo), str(planted.repo_file), planted.repo.name):
        assert fragment not in out, fragment


# --- local, run text -------------------------------------------------------


def test_local_text_shows_real_label_phase_and_agent_id_and_no_absolute_path(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--local", "--verbose", "--observe")
    assert out.splitlines()[0] == LOCAL_LABEL
    assert planted.label in out
    assert planted.phase in out
    assert f"agent-{planted.agent1}.jsonl:5 [bash_timeout]" in out
    assert f"agent-{planted.agent2}.jsonl:1" in out
    _no_absolute_path(out, planted)


def test_local_repo_state_is_repo_relative_with_an_observation(tmp_path, capsys):
    # N5c-1: the observable variant (a shell call in the killed attempt would make
    # its ledger partial, its group refused, and the repository unobserved).
    observable = plant.build(tmp_path, observable=True)
    _code, out, _err = _run(observable, capsys, "--local", "--observe")
    assert "src/module.py  DIRTY_NOW" in out
    _no_absolute_path(out, observable)


def test_local_without_an_observation_has_no_repo_state(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--local")
    assert "REPO STATE" not in out
    _no_absolute_path(out, planted)


def test_local_excerpt_only_with_evidence_and_never_an_absolute_path(planted, capsys):
    _code, without, _err = _run(planted, capsys, "--local", "--verbose", "--observe")
    assert planted.excerpt not in without
    _code, with_evidence, _err = _run(
        planted, capsys, "--local", "--verbose", "--evidence", "--observe",
    )
    assert planted.excerpt in with_evidence
    assert f"agent-{planted.agent1}.jsonl:5: Exit code 143" in with_evidence
    _no_absolute_path(with_evidence, planted)


def test_local_excerpt_path_without_an_observation_is_path_n(planted, capsys):
    """D-n: no observation, no repository root; every path counts as outside."""
    _code, out, _err = _run(planted, capsys, "--local", "--verbose", "--evidence")
    assert planted.excerpt in out
    assert "path-1" in out
    _no_absolute_path(out, planted)


def test_local_is_never_raw(planted, capsys):
    _code, local, _err = _run(planted, capsys, "--local", "--verbose", "--evidence", "--observe")
    _code, raw, _err = _run(planted, capsys, "--raw", "--verbose", "--evidence", "--observe")
    assert str(planted.repo.parent)[:45] in raw
    assert str(planted.repo.parent)[:45] not in local
    assert local != raw


def test_local_session_id_is_real_in_a_session_pointer(capsys):
    root = FIXTURES / "session_human_interrupt"
    capsys.readouterr()
    main(["run", str(next(root.glob("*/*.jsonl"))), "--local", "--verbose"])
    out = capsys.readouterr().out
    assert "00000000-0000-4000-8000-000000000000.jsonl:3" in out
    assert "uuid:0000" not in out


# --- local, bug report -----------------------------------------------------


def test_local_bug_report_shows_real_names_and_project_directory(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--local", "--bug-report")
    assert out.splitlines()[0] == LOCAL_LABEL
    assert f"project {planted.slug}, 1 logical agent(s):" in out
    assert planted.label in out
    assert planted.phase in out
    assert f"agent-{planted.agent1}.jsonl:5 [bash_timeout]" in out
    assert planted.excerpt not in out
    _no_absolute_path(out, planted)


def test_local_bug_report_excerpt_only_with_evidence(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--local", "--bug-report", "--evidence")
    assert planted.excerpt in out
    _no_absolute_path(out, planted)
    assert "Evidence lines include transcript excerpts (--evidence)" in out


def test_local_bug_report_footer_says_what_local_prints(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--local", "--bug-report")
    assert out.rstrip("\n").endswith(
        "Local level: real names, ids, and the project directory name are shown; "
        "paths are repo-relative or numbered. Evidence lines are pointers and marker "
        "codes only; add --evidence for transcript excerpts. Do not paste this report."
    )


# --- raw -------------------------------------------------------------------


def test_raw_text_is_no_redact_with_the_first_line(planted, capsys):
    _code, raw, _err = _run(planted, capsys, "--raw", "--verbose", "--evidence", "--observe")
    _code, legacy, _err = _run(planted, capsys, "--no-redact", "--verbose", "--evidence", "--observe")
    assert raw == legacy
    assert raw.splitlines()[0] == RAW_LABEL
    assert planted.label in raw and planted.phase in raw
    assert f"agent-{planted.agent1}.jsonl:5: Exit code 143" in raw
    assert str(planted.repo.parent)[:45] in raw  # the absolute path, as today


def test_raw_prints_an_excerpt_only_with_evidence(planted, capsys):
    _code, without, _err = _run(planted, capsys, "--raw", "--verbose", "--observe")
    assert planted.excerpt not in without
    assert f"agent-{planted.agent1}.jsonl:5 [bash_timeout]" in without
    _code, bug, _err = _run(planted, capsys, "--raw", "--bug-report")
    assert planted.excerpt not in bug
    _code, bug_evidence, _err = _run(planted, capsys, "--raw", "--bug-report", "--evidence")
    assert planted.excerpt in bug_evidence


def test_raw_bug_report_footer(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--raw", "--bug-report")
    assert out.splitlines()[0] == RAW_LABEL
    assert out.rstrip("\n").endswith(
        "Raw level: real names, ids, and paths are shown; nothing is redacted. "
        "Evidence lines are pointers and marker codes only; add --evidence for transcript "
        "excerpts. Do not paste this report."
    )
    assert f"project {planted.slug}, 1 logical agent(s):" in out


# --- raw is today's output plus the first line -------------------------------


@pytest.mark.parametrize("evidence", [True, False])
def test_raw_text_is_the_legacy_renderer_output_after_the_first_line(
    planted, evidence, capsys, monkeypatch,
):
    """P15: raw prints what a Redactor(enabled=False) render prints today,
    behind the first line. (The legacy render is the same code path v0.2 used
    for --no-redact, so this pins that raw adds nothing but the label.)"""
    from datetime import datetime, timezone

    from agent_postmortem import detectors, reader, report
    from agent_postmortem.redact import Redactor

    monkeypatch.setattr(report, "_now", lambda: datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc))
    run = reader.load_run(planted.run_dir)
    findings = detectors.run_detectors(run)
    legacy = report.render_run_text(
        run, findings, Redactor(enabled=False), verbose=True, evidence=evidence,
    )
    flags = ["--raw", "--verbose"] + (["--evidence"] if evidence else [])
    _code, out, _err = _run(planted, capsys, *flags)
    assert out == RAW_LABEL + "\n" + legacy

    legacy_bug = report.render_bug_report(run, findings, Redactor(enabled=False), evidence)
    flags = ["--raw", "--bug-report"] + (["--evidence"] if evidence else [])
    _code, out, _err = _run(planted, capsys, *flags)
    assert out == RAW_LABEL + "\n" + legacy_bug
