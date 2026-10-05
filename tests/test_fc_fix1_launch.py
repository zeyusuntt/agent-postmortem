"""Fix round 1 of field milestone F: S-m4 (docs/DESIGN.md 4.7; .reviews/v0.3/F-dispositions.md).

The unassignable reason "the launch times are missing or out of order" was misleading when
no launch exists at all (a run whose parent transcript is absent, the packaged demo): there is
no launch record, not launch times that are missing. A run with no generation now has its own
reason, "no launch of this run was found in the parent transcript"; a generation whose launch
timestamp is missing or not strictly increasing keeps the old words.

Fixtures are synthetic; the wf_ id is already in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_postmortem import ledger
from tests.n5c1_helpers import Att, init_repo, write_step
from tests.test_n5c1_gaps import _block, _load, _run_text

NO_LAUNCH = "no launch of this run was found in the parent transcript"
DESIGN = (Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md").read_text(encoding="utf-8")


def _no_parent_run(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    return _load(tmp_path, monkeypatch, "aa7", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ], parent=None)


def test_fc1_a_run_with_no_generation_has_the_no_launch_record_reason(tmp_path, monkeypatch):
    _, run = _no_parent_run(tmp_path, monkeypatch)
    (attempt,) = run.attempts
    assert ledger.is_unassignable(run, attempt)
    assert ledger.unassignable_reason(run, attempt) == "no_launch_record"


@pytest.mark.parametrize("flags", [[], ["--raw"], ["--local"]])
def test_fc1_the_work_at_risk_line_says_no_launch_was_found(
    tmp_path, monkeypatch, capsys, flags
):
    run_dir, _ = _no_parent_run(tmp_path, monkeypatch)
    block = _block(_run_text(capsys, run_dir, *flags))
    line = next(line for line in block if "could not be placed in a launch of this run" in line)
    assert re.search(r" could not be placed in a launch of this run \(" + re.escape(NO_LAUNCH) + r"\)", line)
    assert "launch times" not in line


def test_fc1_the_contract_names_the_fourth_reason():
    assert NO_LAUNCH in " ".join(DESIGN.split())


def test_fc1_a_missing_launch_timestamp_keeps_the_launch_times_words():
    """The old words stay for a generation that exists but cannot be ordered."""
    from agent_postmortem import report
    assert report._UNASSIGNABLE_REASON_WORDS["launch_times_unusable"] == (
        "the launch times are missing or out of order"
    )



def test_fc1_launch_times_are_still_checked_before_the_attempts_own_timestamp():
    """A generation whose launch timestamp is missing wins over an attempt with no first
    timestamp: the reason stays launch_times_unusable, not no_usable_timestamp."""
    from tests.test_fc_wording import _attempt, _generation, _model_run

    run = _model_run([_attempt(None)], [_generation(1, 0.0, 1.0, launch=False)])
    assert ledger.unassignable_reason(run, run.attempts[0]) == "launch_times_unusable"
    run = _model_run([_attempt(None)], [])
    assert ledger.unassignable_reason(run, run.attempts[0]) == "no_launch_record"
