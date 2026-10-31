"""N9 V3: the 0.3 docs say what the 0.3 tool does (.reviews/v0.3/N9-plan.md).

README "What changed in 0.3", the CHANGELOG entry, the DESIGN 4.13 status
marks, and the two plugin skills read against the 0.3 outputs.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from agent_postmortem.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
DESIGN = (REPO_ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
# skill prose wraps at 78 columns, so a needle must not depend on where a line breaks
POSTMORTEM_SKILL = " ".join(
    (REPO_ROOT / "plugin" / "skills" / "postmortem" / "SKILL.md").read_text(encoding="utf-8").split()
)
RESCUE_SKILL = " ".join(
    (REPO_ROOT / "plugin" / "skills" / "rescue" / "SKILL.md").read_text(encoding="utf-8").split()
)


def _section(text: str, heading: str, level: str = "## ") -> str:
    start = text.index(f"{level}{heading}")
    rest = text[start + len(level) :]
    nxt = re.search(rf"^{level}", rest, re.M)
    return text[start : start + len(level) + (nxt.start() if nxt else len(rest))]


# ---- README ----

WHAT_CHANGED = "What changed in 0.3"


def test_readme_section_sits_between_quick_start_and_commands():
    heads = re.findall(r"^## (.+)$", README, re.M)
    assert heads.index(WHAT_CHANGED) == heads.index("Quick start") + 1
    assert heads.index(WHAT_CHANGED) + 1 == heads.index("Commands")


def test_readme_section_is_at_most_forty_lines():
    section = _section(README, WHAT_CHANGED).rstrip("\n")
    assert len(section.splitlines()) <= 40


# one needle group per item N9-plan.md V3 lists; every group must hit
README_ITEMS = {
    "schema 3 json follows the share rules": ("schema: 3", "follows the share rules"),
    "severity from recorded inputs": ("recorded inputs", "FAILED comes only from a failed lifecycle"),
    "missing artifact is info with a pointer": ("expected file", "info", "--verbose"),
    "multi-line work at risk block": ("WORK AT RISK", "one line per attempt"),
    "why unknown is common": ("cannot be proven read-only", "unknown:"),
    "per-repository observation": ("REPO STATE", "checked: no"),
    "per-repository rescue status": ("rescue status", "per declared repository"),
    "run --here and scan --here": ("run --here", "scan --here"),
    "new RUN_ID forms": ("session UUID", "agent-<id>", "a:<alias>"),
    "watch START OK DONE": ("START", "OK", "DONE", "at once"),
    "rescue no killed or failed attempt": ("NO KILLED OR FAILED ATTEMPT", "exit 4"),
}


@pytest.mark.parametrize("item", sorted(README_ITEMS))
def test_readme_section_covers_every_listed_item(item):
    section = _section(README, WHAT_CHANGED)
    for needle in README_ITEMS[item]:
        assert needle in section, needle


def test_readme_section_numbers_come_from_the_demo():
    section = _section(README, WHAT_CHANGED)
    assert section.splitlines()[2].startswith("Verdict:")
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["demo"])
    demo = buf.getvalue()
    quoted = "2 attempts at risk: 0 paths named"
    assert quoted in section and quoted in demo


# ---- CHANGELOG ----


def test_changelog_unreleased_entry_is_first_and_grouped():
    assert CHANGELOG.index("## [0.3.0]") < CHANGELOG.index("## [0.2.0]")
    entry = _section(CHANGELOG, "[0.3.0]")
    groups = re.findall(r"^### (.+)$", entry, re.M)
    assert groups == ["Changed", "Added", "Fixed"]
    assert "http" not in entry


def test_the_package_version_is_0_3_6():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init = (REPO_ROOT / "src" / "agent_postmortem" / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r'^version = "0\.3\.6"', pyproject, re.M)
    assert '__version__ = "0.3.6"' in init


# ---- DESIGN ----

COMMITS = re.compile(r"\(commits [0-9a-f]{7}\.\.[0-9a-f]{7}\)")


def test_design_413_heading_is_marked_implemented():
    heading = re.search(r"^## 4\.13 .*$", DESIGN, re.M).group(0)
    assert "implemented" in heading and "N7" in heading and "N9" in heading


@pytest.mark.parametrize("number", [1, 2, 3, 4, 5, 6, 7, 8])
def test_design_section_headings_carry_implemented_and_a_commit_range(number):
    heading = re.search(rf"^### 4\.13\.{number} .*$", DESIGN, re.M).group(0)
    assert re.search(r"\(implemented in N\d", heading), heading
    assert COMMITS.search(heading), heading


def test_design_4139_and_41310_headings_are_marked():
    h9 = re.search(r"^### 4\.13\.9 .*$", DESIGN, re.M).group(0)
    h10 = re.search(r"^### 4\.13\.10 .*$", DESIGN, re.M).group(0)
    assert "implemented in N9" in h9 and "f58f8b2.." in h9
    assert "standing" in h10


def test_design_41311_items_are_marked_folded():
    heading = re.search(r"^### 4\.13\.11 .*$", DESIGN, re.M).group(0)
    assert "folded" in heading.lower()
    body = _section(DESIGN, "4.13.11", "### ")
    items = re.findall(r"^\*\*N([3-7]),[^\n]*", body, re.M)
    assert sorted(items) == ["3", "4", "5", "6", "7"]
    for number in ("3", "4", "5", "6", "7"):
        line = re.search(rf"^\*\*N{number},[^\n]*", body, re.M).group(0)
        assert "Folded into" in line, line


def test_the_existing_n6_heading_pin_still_holds():
    assert "### 4.13.6 Identity (N6) (implemented in N6)" in DESIGN


# ---- plugin skills ----


def test_postmortem_skill_describes_the_unfinished_tag_by_lifecycle():
    assert "started key with no result yet" not in POSTMORTEM_SKILL
    assert "no lifecycle terminal" in POSTMORTEM_SKILL
    assert "last activity" in POSTMORTEM_SKILL and "possibly running" in POSTMORTEM_SKILL
    assert "never carries the `unfinished` marker" in POSTMORTEM_SKILL


def test_postmortem_skill_names_the_verdict_words():
    assert "CLEAN / INCOMPLETE / a severity" not in POSTMORTEM_SKILL
    for word in ("CLEAN", "DEGRADED", "FAILED", "(INCOMPLETE)"):
        assert word in POSTMORTEM_SKILL, word


def test_postmortem_skill_explains_unknown_work_at_risk():
    assert "cannot be proven read-only" in POSTMORTEM_SKILL
    assert "never say nothing was lost" in POSTMORTEM_SKILL


def test_rescue_skill_runs_checks_per_repository():
    assert "from that repository's root" in RESCUE_SKILL
    assert "from the repo root" not in RESCUE_SKILL


def test_rescue_skill_handles_the_no_unit_sentences():
    assert "NO KILLED OR FAILED ATTEMPT" in RESCUE_SKILL
    assert "NOTHING TO RESCUE" in RESCUE_SKILL
    assert "WORK AT RISK" in RESCUE_SKILL
    assert "exit code 4" in RESCUE_SKILL


# ---- N9 fix 1 (.reviews/v0.3/N9-dispositions.md items 2 to 7) ----

def test_fix1_changelog_states_the_real_marker_boundary():
    entry = _section(CHANGELOG, "[0.3.0]")
    flat = " ".join(entry.split())
    assert "never on text a tool result quotes" not in flat
    assert "only in the position the registry anchors" in flat
    assert "ignored" in flat
    assert "first line matches the runtime notice still counts" in flat


def test_fix1_readme_work_at_risk_describes_the_real_branches():
    section = _section(README, WHAT_CHANGED)
    assert "then a `run yourself` command" not in section
    assert "repo state: run with --observe" in section
    assert "refused, unchecked, or unknown" in section
    assert "an unreturned subagent" in section
    assert "the list of attempts at risk may be incomplete" in section


def test_fix1_readme_does_not_tie_unknown_first_line_to_a_shell_command():
    section = " ".join(_section(README, WHAT_CHANGED).split())
    assert "says unknown often because a shell command" not in section
    assert "`unknown:` when it cannot establish which attempts were at risk" in section
    assert "cannot be proven read-only" in section  # still explains the may-have-written clause


def test_fix1_postmortem_skill_repeats_the_attempts_own_reason():
    assert "means a shell command ran that cannot be proven read-only" not in POSTMORTEM_SKILL
    assert "repeat each attempt line's own stated reason" in POSTMORTEM_SKILL
    assert "mention a shell command only when an attempt line says one ran" in POSTMORTEM_SKILL
    assert "never say nothing was lost" in POSTMORTEM_SKILL


def test_fix1_rescue_skill_documents_all_three_no_unit_sentences():
    assert "NOTHING TO RESCUE YET" in RESCUE_SKILL
    assert "says one of two things" not in RESCUE_SKILL
    assert "nothing failed" not in RESCUE_SKILL
    assert (
        "no attempt was recorded as killed, failed, or superseded; this does not make "
        "the workflow verdict CLEAN" in RESCUE_SKILL
    )


def test_fix1_design_marks_n9_verification_by_what_exists():
    h = re.search(r"^## 4\.13 .*$", DESIGN, re.M).group(0)
    assert "verified" not in h
    h9 = re.search(r"^### 4\.13\.9 .*$", DESIGN, re.M).group(0)
    assert "87b4cca.." not in h9 and "f58f8b2.." in h9
    assert "advisory corpus check run with the owner's authorization" in h9
    body = _section(DESIGN, "4.13.11", "### ")
    n7 = re.search(r"^\*\*N7,[^\n]*", body, re.M).group(0)
    assert "step 3 and its `RUN_CACHE_ENABLED` flag are deferred past v0.3 by the owner's decision C (N7-plan.md" in " ".join(
        body[body.index("**N7,"):].split()
    )
    assert "Folded into 4.13.7 in N7 step 1" in n7


def test_fix1_design_lifecycle_range_runs_to_the_last_lifecycle_fix():
    h3 = re.search(r"^### 4\.13\.3 .*$", DESIGN, re.M).group(0)
    assert "31a2c24..7f3a643" in h3 and "533acac" not in h3


def test_fix1_readme_detector_table_lists_workflow_failed():
    table = _section(README, "What it detects")
    rows = [line for line in table.splitlines() if line.startswith("| `workflow_failed`")]
    assert len(rows) == 1
    assert "| major |" in rows[0]
    assert "failed" in rows[0] and "parent" in rows[0]
