"""What the independent product review of 2026-11-05 found, each reproduced before it was fixed
(field reports of that date; docs/DESIGN.md 4.3, 4.7). Synthetic data only.
"""

from __future__ import annotations

import pytest

from agent_postmortem import reader
from agent_postmortem.detectors.claim_contradiction import _clauses


def _claims(result: dict) -> dict:
    return reader._reported_claims(result)


@pytest.mark.parametrize(
    "result,clause",
    [
        pytest.param({"tests_failed": 2}, "tests_failed", id="the shape it always read"),
        pytest.param({"tests": {"failed": 2}}, "tests_failed", id="tests.failed"),
        pytest.param({"tests": {"failures": 3}}, "tests_failed", id="tests.failures"),
        pytest.param({"status": "failed"}, "status_failed", id="status failed"),
        pytest.param({"status": "FAILURE"}, "status_failed", id="any case"),
        pytest.param({"status": "error"}, "status_failed", id="status error"),
    ],
)
def test_the_common_shapes_of_a_failed_result_are_read(result, clause):
    assert clause in [item[0] for item in _clauses(_claims(result))]


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"status": "done"}, id="done, 124 of 722 real results"),
        pytest.param({"status": "ok"}, id="ok"),
        pytest.param({"status": "done_with_concerns"}, id="a word that only contains no failure"),
        pytest.param({"status": "not failed"}, id="a phrase"),
        pytest.param({"status": "failed to find anything wrong"}, id="a sentence"),
        pytest.param({"tests": {"failed": 0, "passed": 9}}, id="none failed"),
        pytest.param({"tests": {"failed": True}}, id="a bool is no count"),
        pytest.param({"tests": 5}, id="not a dict"),
        pytest.param({"detail": {"tests": {"failed": 2}}}, id="nested any deeper"),
        pytest.param({"summary": {"status": "failed"}}, id="a nested status"),
    ],
)
def test_what_stays_unread_or_is_no_failure(result):
    assert [item[0] for item in _clauses(_claims(result))] == []


def test_the_explicit_count_wins_over_a_nested_one():
    assert _claims({"tests_failed": 0, "tests": {"failed": 2}})["tests_failed"] == 0


def test_a_text_result_is_still_not_read_since_only_its_length_is_kept():
    assert reader._build_reported({"result": "2 tests failed"}) == {
        "type": "text", "text_length": 14}


def test_the_suggested_rules_do_not_claim_they_would_have_prevented_it(capsys):
    from agent_postmortem.cli import main

    assert main(["demo"]) in (0, 3, 4)
    out = capsys.readouterr().out
    assert "would have prevented" not in out
    assert "rules that might have helped: agent-postmortem rules propose" in out


def test_the_wasted_sentence_says_it_is_agent_time(capsys):
    from agent_postmortem.cli import main

    assert main(["demo"]) in (0, 3, 4)
    assert "20m of agent time went to retries and attempts that ended without a result." \
        in capsys.readouterr().out


def test_the_readme_labels_its_figure():
    from pathlib import Path

    text = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    assert "10.2 hours of agent time" in text and "not wall-clock" in text
    assert "10.2 hours went" not in text


# --- further cases ----------------------------------------------------------------




def _fixture_with_result(tmp_path, result: dict):
    """The claim fixture copied, its `ok_false` agent now returning this result."""
    import json
    import shutil
    from pathlib import Path

    source = Path(__file__).parent / "fixtures" / "runs" / "claim_contradiction"
    target = tmp_path / "claim_contradiction"
    shutil.copytree(source, target)
    (journal,) = target.rglob("journal.jsonl")
    lines = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if isinstance(entry.get("result"), dict) and entry["result"].get("ok") is False:
            entry["result"] = result
        lines.append(json.dumps(entry))
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return journal.parent


def test_a_failed_status_is_a_finding_and_leaves_the_run_complete(tmp_path, capsys):
    """Through the reader, detector, severity table and report: an unregistered clause falls into
    the contract-error path, which marks the run INCOMPLETE and prints the clause id."""
    from agent_postmortem.cli import main

    main(["run", str(_fixture_with_result(tmp_path, {"status": "failed"})), "--raw"])
    out = capsys.readouterr().out
    assert "INCOMPLETE" not in out, out
    assert "its status reports failure" in out, out


def test_the_report_says_it_in_words():
    from agent_postmortem.detectors.claim_contradiction import _clauses

    (clause,) = _clauses(_claims({"status": "failed"}))
    assert clause[1] == "status is failed" and clause[2] == "none"
