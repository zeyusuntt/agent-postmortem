"""N6 (plan I9): the help text, README, and DESIGN name `--here` and the new RUN_ID forms."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_postmortem import cli

REPO_ROOT = Path(__file__).resolve().parent.parent


def _help(capsys, *argv) -> str:
    with pytest.raises(SystemExit):
        cli.main([*argv, "--help"])
    return " ".join(capsys.readouterr().out.split())


def test_run_help_lists_the_run_id_forms_and_here(capsys):
    text = _help(capsys, "run")
    for needle in ("--here", "session UUID", "agent-<id>", "a:<alias>", "path to a run directory",
                   "[RUN_ID|PATH]"):
        assert needle in text, needle


def test_scan_help_describes_here(capsys):
    text = _help(capsys, "scan")
    assert "--here" in text and "this directory or a parent of it" in text


@pytest.mark.parametrize("argv", [["rescue", "plan"], ["rescue", "status"], ["rules"]])
def test_the_other_run_resolving_commands_list_the_forms(argv, capsys):
    text = _help(capsys, *argv)
    assert "session UUID" in text and "a:<alias>" in text


def test_readme_and_design_carry_the_new_usage_lines():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "agent-postmortem run --here" in readme
    assert "agent transcript stem" in readme and "a:<alias>" in readme
    design = (REPO_ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "run    [RUN_ID|PATH] [--here]" in design
    assert "[--kind workflow|session] [--here]" in design
    assert "### 4.13.6 Identity (N6) (implemented in N6)" in design
