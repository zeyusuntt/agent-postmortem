"""Part FC, P9 and P12 (field milestone F): scan's wording (docs/DESIGN.md 4.7;
.reviews/v0.3/F-plan.md FC).

- P9: the per-detector block of the full list is introduced by "findings per detector (one
  finding can cover several events):", because "bash_backgrounded: 2" next to a run that
  reports 6 commands read as an event count.
- P12 (found by the main session's real smoke test after FB2): a NEEDS ATTENTION group with
  no candidate says "nothing needs attention" instead of printing a bare header; a LIVE NOW
  group with no run is omitted entirely.

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from agent_postmortem import report
from agent_postmortem.redact import Redactor
from tests.test_fb_live_now import (
    _freeze_now,
    _group,
    _id,
    _live_degraded_workflow,
    _scan,
    _session,
)

DESIGN = (Path(__file__).parent.parent / "docs" / "DESIGN.md").read_text(encoding="utf-8")
LABEL = "findings per detector (one finding can cover several events):"

TOTALS = {
    "workflow": {"runs": 2, "runs_with_retries": 1, "attempts": 4, "wasted_hours": 0.5},
    "session": {"runs": 1, "subagents": 3, "interrupted": 1},
    "findings_per_detector": {"bash_backgrounded": 2, "wasted_time": 3},
}


def _render(totals):
    return report.render_scan_text([], totals, Redactor("share"))


# --- P9 -------------------------------------------------------------------------------------


def test_p9_the_per_detector_block_is_introduced_by_the_label():
    lines = _render(TOTALS).splitlines()
    index = lines.index(LABEL)
    assert lines[index + 1:index + 3] == ["  bash_backgrounded: 2", "  wasted_time: 3"]
    assert lines[index - 1].endswith("interrupted")


def test_p9_an_empty_block_prints_no_label():
    totals = {**TOTALS, "findings_per_detector": {}}
    out = _render(totals)
    assert LABEL not in out
    assert "findings per detector" not in out


def test_p9_the_label_is_printed_once_and_the_rows_keep_their_sorted_order():
    totals = {**TOTALS, "findings_per_detector": {"z_last": 1, "a_first": 2, "m_mid": 3}}
    lines = _render(totals).splitlines()
    assert lines.count(LABEL) == 1
    index = lines.index(LABEL)
    assert lines[index + 1:index + 4] == ["  a_first: 2", "  m_mid: 3", "  z_last: 1"]


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_p9_scan_all_prints_the_label_at_every_level(tmp_path, monkeypatch, capsys, flags):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _live_degraded_workflow(root, now)
    _session(root, 1, now - timedelta(hours=3), live=False)
    _code, out, _err = _scan(capsys, root, "--all", *flags)
    lines = out.splitlines()
    assert lines.count(LABEL) == 1
    index = lines.index(LABEL)
    assert lines[index + 1].startswith("  ") and ": " in lines[index + 1]


def test_p9_the_default_triage_view_has_no_per_detector_block(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _live_degraded_workflow(root, now)
    out = _scan(capsys, root)[1]
    assert "findings per detector" not in out


def test_p9_json_totals_are_unchanged(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _live_degraded_workflow(root, now)
    out = _scan(capsys, root, "--all", "--json")[1]
    payload = json.loads(out)
    assert "findings_per_detector" in payload["totals"]
    assert "findings per detector" not in out


def test_p9_the_contract_states_the_label():
    assert LABEL in " ".join(DESIGN.split())


# --- P12 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_p12_an_empty_needs_attention_group_says_nothing_needs_attention(
    tmp_path, monkeypatch, capsys, flags,
):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(hours=2), live=False)
    code, out, _err = _scan(capsys, root, *flags)
    assert code == 0
    lines = out.splitlines()
    index = lines.index("NEEDS ATTENTION (0 of 1 runs)")
    assert lines[index + 1] == "  nothing needs attention"
    assert lines[index + 2].startswith("LAST 7 DAYS")
    assert _group(out, "NEEDS ATTENTION") == [
        "NEEDS ATTENTION (0 of 1 runs)", "  nothing needs attention",
    ]


def test_p12_a_run_that_is_only_live_leaves_needs_attention_empty_and_live_now_listed(
    tmp_path, monkeypatch, capsys,
):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    live = _session(root, 1, now - timedelta(minutes=1), live=True)
    out = _scan(capsys, root)[1]
    assert _group(out, "NEEDS ATTENTION") == [
        "NEEDS ATTENTION (0 of 1 runs)", "  nothing needs attention",
    ]
    assert _group(out, "LIVE NOW") == [
        "LIVE NOW (1 of 1 runs)",
        f"  {_id(live)}  possibly live  CLEAN  agent-postmortem run {_id(live)}",
    ]


def test_p12_a_group_with_a_candidate_never_says_nothing_needs_attention(
    tmp_path, monkeypatch, capsys,
):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _live_degraded_workflow(root, now)
    out = _scan(capsys, root)[1]
    assert "nothing needs attention" not in out
    assert _group(out, "NEEDS ATTENTION")[0] == "NEEDS ATTENTION (1 of 1 runs)"


def test_p12_a_truncated_group_never_says_nothing_needs_attention(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    for index in range(3):
        _live_degraded_workflow(root, now, wf_id=f"wf_00000000-fb{index}")
    out = _scan(capsys, root, "--limit", "1")[1]
    assert "nothing needs attention" not in out
    assert _group(out, "NEEDS ATTENTION")[0].startswith("NEEDS ATTENTION (3 of 3 runs; showing 1")


def test_p12_with_no_live_run_the_live_now_group_is_omitted_entirely(
    tmp_path, monkeypatch, capsys,
):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(hours=2), live=False)
    out = _scan(capsys, root)[1]
    assert "LIVE NOW" not in out
    lines = out.splitlines()
    index = lines.index("NEEDS ATTENTION (0 of 1 runs)")
    assert lines[index + 1] == "  nothing needs attention"
    assert lines[index + 2].startswith("LAST 7 DAYS")


def test_p12_the_empty_line_is_text_only_json_rows_are_unchanged(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(hours=2), live=False)
    out = _scan(capsys, root, "--json")[1]
    assert "nothing needs attention" not in out
    assert json.loads(out)["rows"]


def test_p12_scan_all_does_not_print_the_line(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(hours=2), live=False)
    out = _scan(capsys, root, "--all")[1]
    assert "nothing needs attention" not in out


def test_p12_the_renderer_alone_follows_the_same_rule():
    redactor = Redactor("share")
    out = report.render_scan_triage_text(
        [], 0, 0, TOTALS, TOTALS, redactor,
    )
    assert out.splitlines()[:2] == ["NEEDS ATTENTION (0 of 0 runs)", "  nothing needs attention"]
    assert "LIVE NOW" not in out


def test_p12_the_contract_states_both_empty_cases():
    assert "  nothing needs attention" in DESIGN
    assert "LIVE NOW group is omitted entirely" in " ".join(DESIGN.split())


def test_p12_limit_zero_with_candidates_is_not_nothing_needs_attention(
    tmp_path, monkeypatch, capsys,
):
    """The line is for a group with no candidate at all, not for one --limit hides."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _live_degraded_workflow(root, now)
    code, out, _err = _scan(capsys, root, "--limit", "0")
    assert "nothing needs attention" not in out
    assert out.splitlines()[0].startswith("NEEDS ATTENTION (1 of 1 runs; showing 0")
