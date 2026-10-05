"""N5 fix 1, C4: `mislabeled_kill` follows the literal 4.13.4 row.

`parent_stop`, `human`, or a short or unknown silence is info, at any position;
a long silence is major on the final attempt and minor on a non-final one.
The expectations below are written out by hand from the DESIGN row, not read
from `severity.py`.
"""

from __future__ import annotations

import itertools

import pytest

from agent_postmortem import detectors, reader
from tests.n5b_helpers import Spec, build_workflow, model_attempt, model_workflow_run, severity


def _expected(cause: str, silence: str, position: str) -> str:
    if cause in ("parent_stop", "human"):
        return "info"
    if silence in ("short", "unknown"):
        return "info"
    return "major" if position == "final" else "minor"


GRID = list(itertools.product(
    ("runtime", "unattributed", "parent_stop", "human"),
    ("long", "short", "unknown"),
    ("final", "non_final"),
))


@pytest.mark.parametrize("cause,silence,position", GRID, ids=["/".join(row) for row in GRID])
def test_mislabeled_kill_grid_follows_the_literal_row(cause, silence, position):
    run = model_workflow_run([model_attempt(1)])
    result = severity.resolve(
        run, "mislabeled_kill", cause=cause, silence=silence, position=position,
    )
    assert result == _expected(cause, silence, position)
    assert run.status == "COMPLETE"
    assert run.diagnostics == []


def _findings(run):
    return [f for f in detectors.run_detectors(run) if f.detector == "mislabeled_kill"]


def test_a_short_silence_on_a_non_final_attempt_is_info_through_the_detector(tmp_path, monkeypatch):
    specs = [Spec("v2:x", 1, journal="superseded", kill=True, silence=2), Spec("v2:x", 2)]
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-f1a", specs)
    run = reader.load_run(run_dir)
    (finding,) = _findings(run)
    assert finding.severity == "info"


def test_a_long_silence_on_a_non_final_attempt_stays_minor_through_the_detector(
    tmp_path, monkeypatch,
):
    specs = [Spec("v2:x", 1, journal="superseded", kill=True, silence=400), Spec("v2:x", 2)]
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-f1b", specs)
    run = reader.load_run(run_dir)
    (finding,) = _findings(run)
    assert finding.severity == "minor"


def test_a_long_silence_on_the_final_attempt_stays_major_through_the_detector(
    tmp_path, monkeypatch,
):
    specs = [Spec("v2:x", 1, journal="open", kill=True, silence=400)]
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-f1c", specs, parent="running")
    run = reader.load_run(run_dir)
    (finding,) = _findings(run)
    assert finding.severity == "major"
