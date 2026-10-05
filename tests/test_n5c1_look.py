"""N5c-1: the WHAT TO DO `[look]` item for absent expected files (DESIGN 4.13.4,
4.7 item 3). A rendering hint driven by recency, never a severity change: one
item however many info-severity `missing_artifact` and `claim_contradiction`
findings exist, no path in its text, counted toward the cap of three, ahead of
the rules item.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import cli, report
from agent_postmortem.models import AttemptFacts, Finding
from tests.n5b_helpers import (
    BASE,
    Spec,
    build_workflow,
    ledger_of,
    model_attempt,
    model_workflow_run,
)

GONE = "/tmp/agent-postmortem-fixture/n5c1-never-written.md"
COMMAND = "agent-postmortem run wf_00000000-b00 --verbose"
HOURS = timedelta(hours=72)


def _finding(detector: str, severity: str = "info", **extra) -> Finding:
    return Finding(
        detector=detector, severity=severity, title="t", run_id="wf_00000000-b00",
        agent_id=None, evidence=[], metrics={}, **({"rule_id": None} | extra),
    )


def _run(ended_at):
    run = model_workflow_run([model_attempt(1)])
    run.ended_at = ended_at
    return run


def _items(monkeypatch, run, findings, *, now):
    monkeypatch.setattr(report, "_now", lambda: now)
    return report._what_to_do_items(run, findings, None, False)


def _look(items):
    return [item for item in items if "expected file" in item["text"]]


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_within_72_hours_the_item_is_present(monkeypatch):
    run = _run(NOW - timedelta(hours=71, minutes=59))
    items = _items(monkeypatch, run, [_finding("missing_artifact")], now=NOW)
    assert _look(items) == [{
        "grade": "look",
        "text": "1 expected file absent now: check whether the run wrote it",
        "command": COMMAND,
    }]


def test_exactly_72_hours_still_counts_as_within(monkeypatch):
    run = _run(NOW - HOURS)
    items = _items(monkeypatch, run, [_finding("missing_artifact")], now=NOW)
    assert len(_look(items)) == 1


def test_one_second_beyond_72_hours_the_item_is_absent(monkeypatch):
    run = _run(NOW - HOURS - timedelta(seconds=1))
    items = _items(monkeypatch, run, [_finding("missing_artifact")], now=NOW)
    assert _look(items) == []


def test_beyond_72_hours_nothing_remains(monkeypatch):
    run = _run(NOW - timedelta(days=5))
    items = _items(monkeypatch, run, [_finding("missing_artifact")], now=NOW)
    assert items == []


def test_an_unknown_last_activity_gets_no_item(monkeypatch):
    items = _items(monkeypatch, _run(None), [_finding("missing_artifact")], now=NOW)
    assert _look(items) == []


@pytest.mark.parametrize("detector", ["missing_artifact", "claim_contradiction"])
def test_one_finding_of_either_detector_reads_singular(monkeypatch, detector):
    run = _run(NOW - timedelta(hours=1))
    items = _items(monkeypatch, run, [_finding(detector)], now=NOW)
    assert _look(items)[0]["text"] == (
        "1 expected file absent now: check whether the run wrote it"
    )


def test_several_findings_are_one_item_with_the_count_and_plural_words(monkeypatch):
    run = _run(NOW - timedelta(hours=1))
    findings = [
        _finding("missing_artifact"), _finding("missing_artifact"),
        _finding("claim_contradiction"),
    ]
    items = _items(monkeypatch, run, findings, now=NOW)
    looks = _look(items)
    assert len(looks) == 1
    assert looks[0]["text"] == "3 expected files absent now: check whether the run wrote them"
    assert looks[0]["grade"] == "look"
    assert looks[0]["command"] == COMMAND


@pytest.mark.parametrize("finding", [
    _finding("claim_contradiction", "major"),  # the mixed clause finding stays major
    _finding("missing_artifact", "minor"),
    _finding("wasted_time"),  # info, but not an absent file
    _finding("bash_timeout", "minor"),
])
def test_only_info_findings_of_the_two_detectors_count(monkeypatch, finding):
    run = _run(NOW - timedelta(hours=1))
    items = _items(monkeypatch, run, [finding], now=NOW)
    assert _look(items) == []


def test_the_item_sits_before_the_rules_item_and_the_generic_item_is_left_out(monkeypatch):
    run = _run(NOW - timedelta(hours=1))
    backed = _finding("silent_retry", "major", rule_id="R-RETRY-SAFE")
    items = _items(monkeypatch, run, [backed, _finding("missing_artifact")], now=NOW)
    texts = [item["text"] for item in items]
    assert texts == [
        "1 expected file absent now: check whether the run wrote it",
        "rules that would have prevented this",
    ]


def test_the_item_counts_toward_the_cap_of_three_and_displaces_the_rules_item(monkeypatch):
    attempt = model_attempt(
        1, outcome="killed", ledger=ledger_of("observed"),
        facts=AttemptFacts(
            paths_written=["/tmp/ws/a.txt"], in_flight=[], commits=[], cwds=["/tmp/ws"],
            background_unfinished=0, last_tool="Write", seconds_since_last_tool_at_end=1.0,
        ),
    )
    run = model_workflow_run([attempt])
    run.ended_at = NOW - timedelta(hours=1)
    failed = _finding(
        "attempt_failed", "major", rule_id="R-RETRY-SAFE",
    )
    failed.metrics = {"retry_now": "yes", "cause": "unattributed"}
    items = _items(monkeypatch, run, [failed, _finding("missing_artifact")], now=NOW)
    assert [item["grade"] for item in items] == ["look", "decide", "look"]
    assert items[0]["command"].endswith("--observe")
    assert items[1]["text"].startswith("relaunch is safe now")
    assert "expected file" in items[2]["text"]
    assert all("rules that would" not in item["text"] for item in items)


def _prompt_run(tmp_path, monkeypatch, suffix, prompt):
    spec = Spec("v2:x", 1, prompt=prompt)
    return build_workflow(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [spec])


def _write_prompt(path=GONE):
    return f"Write the final report to {path} when done."


def _what_to_do_block(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line == "WHAT TO DO")
    block = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        block.append(line)
    return block


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_text_at_every_level_names_no_path(tmp_path, monkeypatch, capsys, flags):
    run_dir = _prompt_run(tmp_path, monkeypatch, "a10", _write_prompt())
    cli.main(["run", str(run_dir), *flags])
    out = capsys.readouterr().out
    block = _what_to_do_block(out)
    assert any(
        "agent-postmortem run wf_00000000-a10 --verbose" in line
        and "1 expected file absent now: check whether the run wrote it" in line
        for line in block
    ), block
    assert "n5c1-never-written" not in out  # info findings are hidden by default


def test_the_item_is_present_in_verbose_text_without_leaking_the_path_in_it(
    tmp_path, monkeypatch, capsys,
):
    run_dir = _prompt_run(tmp_path, monkeypatch, "a11", _write_prompt())
    cli.main(["run", str(run_dir), "--verbose"])
    block = _what_to_do_block(capsys.readouterr().out)
    assert any("1 expected file absent now" in line for line in block)
    assert all("n5c1-never-written" not in line for line in block)


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_json_next_carries_the_same_item(tmp_path, monkeypatch, capsys, flags):
    run_dir = _prompt_run(tmp_path, monkeypatch, "a12", _write_prompt())
    cli.main(["run", str(run_dir), "--json", *flags])
    payload = json.loads(capsys.readouterr().out)
    looks = [item for item in payload["next"] if "expected file" in item["text"]]
    assert looks == [{
        "grade": "look",
        "text": "1 expected file absent now: check whether the run wrote it",
        "command": "agent-postmortem run wf_00000000-a12 --verbose",
    }]
    assert "n5c1-never-written" not in json.dumps(payload["next"])


def test_two_absent_files_in_one_run_make_one_item(tmp_path, monkeypatch, capsys):
    prompt = (
        "Write the notes to /tmp/agent-postmortem-fixture/n5c1-a.md and then "
        "save the summary to /tmp/agent-postmortem-fixture/n5c1-b.txt please"
    )
    run_dir = _prompt_run(tmp_path, monkeypatch, "a13", prompt)
    cli.main(["run", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    looks = [item for item in payload["next"] if "expected file" in item["text"]]
    assert len(looks) == 1
    assert looks[0]["text"] == "2 expected files absent now: check whether the run wrote them"


def test_a_run_with_no_absent_file_has_no_item(tmp_path, monkeypatch, capsys):
    here = tmp_path / "present.md"
    here.write_text("x", encoding="utf-8")
    run_dir = _prompt_run(tmp_path, monkeypatch, "a14", _write_prompt(str(here)))
    cli.main(["run", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert all("expected file" not in item["text"] for item in payload["next"])


def test_a_run_older_than_72_hours_through_the_cli_has_no_item(tmp_path, monkeypatch, capsys):
    run_dir = _prompt_run(tmp_path, monkeypatch, "a15", _write_prompt())
    later = BASE + timedelta(hours=100)
    monkeypatch.setattr(report, "_now", lambda: later)
    cli.main(["run", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert all("expected file" not in item["text"] for item in payload["next"])
