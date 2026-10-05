"""N8a K2 (docs/DESIGN.md 4.7, 4.13.8): no generic `[look] see the findings
hidden here` item, and the WHAT TO DO heading (with its items and the blank
line after it) is printed only with at least one item."""

from __future__ import annotations

import json

import pytest

from agent_postmortem import cli
from tests.test_field_v03_more import _clean_run_fixture


def _run_text(run_dir, capsys, *extra: str) -> str:
    assert cli.main(["run", str(run_dir), *extra]) == 0
    return capsys.readouterr().out


def test_k2_no_look_item_at_default(tmp_path, monkeypatch, capsys):
    out = _run_text(_clean_run_fixture(tmp_path, monkeypatch), capsys)
    assert "[look]" not in out
    # the trailing pointer stays
    assert "1 info finding not shown (--verbose)" in out


def test_k2_no_look_item_with_verbose(tmp_path, monkeypatch, capsys):
    out = _run_text(_clean_run_fixture(tmp_path, monkeypatch), capsys, "--verbose")
    assert "[look]" not in out


def test_k2_heading_absent_with_zero_items(tmp_path, monkeypatch, capsys):
    out = _run_text(_clean_run_fixture(tmp_path, monkeypatch), capsys, "--verbose")
    assert "WHAT TO DO" not in out


@pytest.mark.parametrize("extra", [[], ["--verbose"]])
def test_k2_no_double_blank_line(tmp_path, monkeypatch, capsys, extra):
    out = _run_text(_clean_run_fixture(tmp_path, monkeypatch), capsys, *extra)
    assert "\n\n\n" not in out


def test_k2_json_next_is_an_empty_list_with_no_item(tmp_path, monkeypatch, capsys):
    out = _run_text(_clean_run_fixture(tmp_path, monkeypatch), capsys, "--json")
    assert json.loads(out)["next"] == []


def test_k2_a_run_with_items_still_prints_the_heading(capsys):
    assert cli.main(["demo"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "WHAT TO DO" in lines
    start = lines.index("WHAT TO DO")
    assert lines[start + 1].startswith("  1. [decide]")
    assert lines[start + 2] == ""
    assert not lines[start + 3].startswith("  ")


def test_k2_the_demo_prints_only_its_rules_item(capsys):
    assert cli.main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "see the findings hidden here" not in out
    assert "[look]" not in out
