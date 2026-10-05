"""Fix round 2 of field milestone F: R2-4 (docs/DESIGN.md 4.7; .reviews/v0.3/F-dispositions.md,
Codex round 2).

`no_launch_record` ("no launch of this run was found in the parent transcript") is for a run whose
parent snapshot holds no relevant launch evidence. A launch that was found and deliberately not
used (an ambiguous duplicate tool-use id, a malformed or rejected result) gets its own reason,
"a launch of this run was found but could not be used".

Fixtures are synthetic; the wf_ ids are in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import re
from datetime import timedelta

from agent_postmortem import ledger, reader
from tests.n5c1_helpers import BASE, FAKE_SLUG, Att, build_run, init_repo, write_step
from tests.test_field_v03 import (
    _parent_path,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)
from tests.test_n5c1_gaps import _block, _run_text

NO_LAUNCH = "no launch of this run was found in the parent transcript"
UNUSABLE = "a launch of this run was found but could not be used"


def _run_with_parent(tmp_path, monkeypatch, suffix, parent_lines):
    repo = init_repo(tmp_path / "repo")
    wf_id = f"wf_00000000-{suffix}"
    run_dir = build_run(
        tmp_path, monkeypatch, wf_id,
        [Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)])], parent=None,
    )
    if parent_lines is not None:
        _write_jsonl(_parent_path(tmp_path / "root", slug=FAKE_SLUG), parent_lines(wf_id))
    return run_dir, reader.load_run(run_dir)


def _duplicate(wf_id):
    """Codex's exact input: lines 1 and 2 are duplicate launches with one tool-use id; line 3
    is a result naming the inspected run."""
    return [
        _workflow_launch("toolu_DUP", BASE),
        _workflow_launch("toolu_DUP", BASE + timedelta(seconds=1)),
        _workflow_launch_result("toolu_DUP", BASE + timedelta(seconds=2), wf_id, "tDUP"),
    ]


def _malformed(wf_id):
    result = _workflow_launch_result("toolu_M", BASE + timedelta(seconds=1), wf_id, "tM")
    del result["toolUseResult"]["taskId"]
    return [_workflow_launch("toolu_M", BASE), result]


def _other_run(wf_id):
    return [
        _workflow_launch("toolu_O", BASE),
        _workflow_launch_result("toolu_O", BASE + timedelta(seconds=1), "wf_00000000-b23", "tO"),
    ]


def test_fc2_an_ambiguous_launch_is_not_no_launch_record(tmp_path, monkeypatch):
    _, run = _run_with_parent(tmp_path, monkeypatch, "b21", _duplicate)
    assert run.lifecycle.source == "ambiguous_launch" and run.lifecycle.generations == 0
    (attempt,) = run.attempts
    assert ledger.unassignable_reason(run, attempt) == "launch_unusable"


def test_fc2_the_public_line_for_an_ambiguous_launch_says_it_was_found(
    tmp_path, monkeypatch, capsys
):
    run_dir, _ = _run_with_parent(tmp_path, monkeypatch, "b21", _duplicate)
    for flags in ([], ["--raw"], ["--local"]):
        block = _block(_run_text(capsys, run_dir, *flags))
        line = next(line for line in block if "could not be placed in a launch of this run" in line)
        assert re.search(
            r" could not be placed in a launch of this run \(" + re.escape(UNUSABLE) + r"\)", line
        ), line
        assert NO_LAUNCH not in line


def test_fc2_a_malformed_result_naming_the_run_is_found_but_unusable(tmp_path, monkeypatch):
    _, run = _run_with_parent(tmp_path, monkeypatch, "b22", _malformed)
    (attempt,) = run.attempts
    assert run.lifecycle.generations == 0
    assert ledger.unassignable_reason(run, attempt) == "launch_unusable"


def test_fc2_a_launch_of_another_run_leaves_no_launch_record(tmp_path, monkeypatch):
    _, run = _run_with_parent(tmp_path, monkeypatch, "b24", _other_run)
    (attempt,) = run.attempts
    assert ledger.unassignable_reason(run, attempt) == "no_launch_record"


def test_fc2_a_missing_parent_still_has_no_launch_record(tmp_path, monkeypatch):
    _, run = _run_with_parent(tmp_path, monkeypatch, "b25", None)
    (attempt,) = run.attempts
    assert ledger.unassignable_reason(run, attempt) == "no_launch_record"


def test_fc2_the_contract_names_the_fifth_reason():
    from pathlib import Path
    design = (Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md").read_text(
        encoding="utf-8"
    )
    assert UNUSABLE in " ".join(design.split())


# --- the two keys, each alone, and what must not count (model-level pins) ------------------------


def _model(source, parent_diagnostics):
    from tests.test_fa_generations import _attempt
    from tests.test_fa_generations import _run as model_run

    run = model_run([_attempt(3.0)], [])
    run.lifecycle.source = source
    run.discovery = {**run.discovery, "parent": list(parent_diagnostics)}
    return run


def test_fc2_the_ambiguous_launch_lifecycle_alone_is_found_but_unusable():
    run = _model("ambiguous_launch", [])
    assert ledger.unassignable_reason(run, run.attempts[0]) == "launch_unusable"


def test_fc2_each_launch_diagnostic_alone_is_found_but_unusable():
    for text in (
        "ambiguous launch 'toolu_X': lines 1, 2",
        "launch without result at line 4",
        "rejected launch result at line 5",
        "malformed launch result at line 6",
    ):
        run = _model("notification", [text])
        assert ledger.unassignable_reason(run, run.attempts[0]) == "launch_unusable", text


def test_fc2_a_file_level_parent_diagnostic_is_not_launch_evidence():
    run = _model("notification", ["x.jsonl: parent transcript unreadable", "line 3: undecodable"])
    assert ledger.unassignable_reason(run, run.attempts[0]) == "no_launch_record"
