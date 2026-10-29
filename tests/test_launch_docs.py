"""README first screen, coverage table and issue templates for the public launch (DESIGN 8.4).

The first screen asks the question the tool answers and gives the one-command PyPI run (0.3.5); "What it covers" states coverage honestly (covered, partial, not yet) before Quick
start; every issue template asks for default-level `--bug-report` output and the Claude Code
version, and never for a raw transcript.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
TEMPLATES = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
NEW_TEMPLATES = ("wrong_verdict.yml", "confusing_output.yml", "run_shape_not_covered.yml")
GIT_SOURCE = "git+https://github.com/zeyusuntt/agent-postmortem"
VERSION = re.search(r'^version = "([^"]+)"', (REPO_ROOT / "pyproject.toml").read_text(), re.M).group(1)
# Files a stranger reads for install instructions (history in CHANGELOG and DESIGN is exempt).
USER_FACING = [REPO_ROOT / "README.md", REPO_ROOT / "plugin" / "README.md", REPO_ROOT / "CONTRIBUTING.md",
               REPO_ROOT / "plugin" / "hooks" / "run.sh",
               *sorted((REPO_ROOT / "plugin" / "skills").glob("*/SKILL.md")),
               *sorted(TEMPLATES.glob("*.yml"))]


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"README has no '## {heading}' section"
    return match.group(1)


def test_first_screen_asks_the_question_and_gives_the_pypi_command():
    head = "\n".join(README.splitlines()[:16])
    assert 'Which "done" in your last Claude Code run can\'t you trust?' in head
    assert "\nuvx agent-postmortem scan\n" in head
    assert GIT_SOURCE not in head
    assert "coming" not in head
    assert "13 of 166 workflow runs" in head


def test_what_it_covers_comes_before_quick_start_and_is_honest():
    heads = re.findall(r"^## (.+)$", README, re.M)
    assert heads.index("What it covers") + 1 == heads.index("Quick start")
    section = _section(README, "What it covers")
    rows = [line for line in section.splitlines() if line.startswith("| ") and "---" not in line]
    coverage = {cells[0]: cells[1] for cells in (
        [cell.strip() for cell in row.strip("|").split("|")] for row in rows[1:]
    )}
    assert set(coverage.values()) == {"covered", "partial", "not yet"}
    for shape, expected in (
        ("`claude -p` (headless)", "partial"),
        ("Background subagents (`run_in_background`)", "partial"),
        ("`claude --bg` sessions", "not yet"),
        ("Agent teams", "not yet"),
        ("Cross-session messages (`SendMessage`)", "not yet"),
    ):
        assert coverage.get(shape) == expected, shape
    assert "issues/new?template=run_shape_not_covered.yml" in section
    assert "--bug-report" in section


@pytest.mark.parametrize("name", NEW_TEMPLATES)
def test_each_new_issue_template_asks_for_the_bug_report_and_never_for_a_transcript(name):
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    flat = " ".join(text.split())  # block scalars wrap freely
    assert "agent-postmortem run <run-id> --bug-report" in flat
    assert "claude --version" in flat
    assert "never paste a raw transcript" in flat
    assert "Do not add `--local`, `--raw` or `--evidence`" in flat
    assert re.search(r"^\s+required: true$", text, re.M)


def test_no_issue_template_asks_for_transcript_lines():
    for path in sorted(TEMPLATES.glob("*.yml")):
        text = path.read_text(encoding="utf-8").lower()
        assert "excerpt from `journal.jsonl`" not in text, path.name
        assert "transcript line that shows" not in text, path.name


def test_user_facing_install_lines_use_pypi():
    """0.3.5: PyPI works, so no install line a stranger reads points at the GitHub tag.
    The plugin's own fallback to the tag is code in run.sh, tested in test_plugin.py."""
    for path in USER_FACING:
        if path.name == "run.sh":
            continue
        text = path.read_text(encoding="utf-8")
        assert f"{GIT_SOURCE}@" not in text, f"{path.name} still installs from the GitHub tag"
    quick_start = _section(README, "Quick start")
    assert "\nuv tool install agent-postmortem\n" in quick_start
    for path in (REPO_ROOT / "plugin" / "README.md", REPO_ROOT / "plugin" / "skills" / "postmortem" / "SKILL.md"):
        assert "uv tool install agent-postmortem" in path.read_text(encoding="utf-8"), path.name


def test_every_github_tag_named_in_user_facing_text_is_the_package_version():
    for path in USER_FACING:
        for tag in re.findall(r"agent-postmortem@v([0-9][0-9.]*[0-9])", path.read_text(encoding="utf-8")):
            assert tag == VERSION, f"{path.name} names v{tag}, the package is {VERSION}"
