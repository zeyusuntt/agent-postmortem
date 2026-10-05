"""N5a-2: the words around schema 3 (plan S12): scan's exit codes in --help,
README's JSON paragraph, and the two skill texts. Public rendering, hence the
n5 label (D17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_postmortem import cli

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
POSTMORTEM_SKILL = (ROOT / "plugin" / "skills" / "postmortem" / "SKILL.md").read_text(encoding="utf-8")
RESCUE_SKILL = (ROOT / "plugin" / "skills" / "rescue" / "SKILL.md").read_text(encoding="utf-8")


def _scan_help(capsys) -> str:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["scan", "--help"])
    assert exc_info.value.code == 0
    return capsys.readouterr().out


def test_scan_help_documents_every_exit_code(capsys):
    out = _scan_help(capsys).lower()
    for line in ("exit 0", "exit 1", "exit 2", "exit 3", "exit 4"):
        assert line in out, line
    assert "verdict is failed" in out
    assert "incomplete" in out


def test_readme_json_paragraph_says_default_json_is_share_safe():
    assert "until JSON schema 3" not in README
    assert "raw agent ids" not in README
    assert "is refused until then" not in README
    assert "as safe to paste as the text" in README
    assert "`--local --json`" in README
    assert "`disclosure` field" in README


def test_the_skills_say_nothing_schema_3_changed():
    for text in (POSTMORTEM_SKILL, RESCUE_SKILL):
        lowered = text.lower()
        assert "schema 2" not in lowered
        assert "raw agent id" not in lowered
        assert "--json" not in lowered  # neither skill parses JSON, so no schema statement can go stale


def test_the_rescue_skill_still_prescribes_local_and_never_raw():
    assert "rescue plan <run-id> --local --evidence" in RESCUE_SKILL
    assert "rescue status <run-id> --local" in RESCUE_SKILL
    assert "Do not switch to the raw level" in RESCUE_SKILL
