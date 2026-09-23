"""README contract (docs/DESIGN.md 8.4).

Three things must stay true or the README silently drifts from the tool:
every command named in its command table exists in the argparse parser
(and every parser command is named in the table); its demo code block is
byte-for-byte what `agent-postmortem demo` prints right now, so the two can
never fall out of sync; and no user-facing prose file uses an em-dash
(CLAUDE.md's plain-English rule).
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_postmortem.cli import _build_parser, main
from tests import privacy_rules

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# CLAUDE.md: user-facing text is plain English, no em-dashes. These four
# files are the project's prose surface; source code comments and
# DESIGN.md use a plain "--" instead where a dash reads naturally, and are
# not covered here -- only the em-dash character itself is checked, and
# only in the files a reader is actually meant to read as prose.
PROSE_FILES = (
    README,
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "CHANGELOG.md",
    REPO_ROOT / "plugin" / "README.md",
)

EM_DASH = "—"

COMMAND_CELL_RE = re.compile(r"`agent-postmortem (\w+)")


def _flag_support() -> dict[str, tuple[bool, bool]]:
    """{command: (has --json, has --no-redact)}, read straight off the live
    argparse parser, in the order its subparsers were added."""
    parser = _build_parser()
    subparsers_action = next(
        action for action in parser._subparsers._group_actions if hasattr(action, "choices")
    )
    support = {}
    for name, subparser in subparsers_action.choices.items():
        options = set(subparser._option_string_actions)
        support[name] = ("--json" in options, "--no-redact" in options)
    return support


def _join_command_names(names: list[str]) -> str:
    quoted = [f"`{name}`" for name in names]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return ", ".join(quoted[:-1]) + f", and {quoted[-1]}"


def _expected_flag_sentence() -> str:
    """docs/DESIGN.md 8.4 / M8 review D6: the exact sentence README.md's
    Commands section must carry, reconstructed from the parser itself so a
    future flag change that is not also reflected in the README fails this
    test instead of silently going stale."""
    support = _flag_support()
    order = list(support)  # insertion order == subparser add order in cli.py
    seen_signatures: list[tuple[bool, bool]] = []
    for name in order:
        sig = support[name]
        if sig not in seen_signatures:
            seen_signatures.append(sig)

    clause_templates = {
        (True, True): "{names} take `--json` and `--no-redact`",
        (True, False): "{names} take{s} `--json` only",
        (False, True): "{names} take{s} `--no-redact` only",
        (False, False): "{names} take{s} neither",
    }
    clauses = []
    for sig in seen_signatures:
        names = [name for name in order if support[name] == sig]
        clauses.append(
            clause_templates[sig].format(
                names=_join_command_names(names), s="" if len(names) > 1 else "s"
            )
        )
    return "; ".join(clauses) + "."


def _known_commands() -> set[str]:
    parser = _build_parser()
    subparsers_action = next(
        action for action in parser._subparsers._group_actions if hasattr(action, "choices")
    )
    return set(subparsers_action.choices)


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_exists():
    assert README.exists(), "README.md is missing"


def test_readme_command_table_matches_the_parser_exactly():
    text = _readme_text()
    heading_match = re.search(r"(?m)^## Commands$", text)
    assert heading_match, "README is missing a '## Commands' heading"
    table_text = text[heading_match.end():]
    next_heading = re.search(r"(?m)^## ", table_text)
    if next_heading:
        table_text = table_text[: next_heading.start()]

    documented = set(COMMAND_CELL_RE.findall(table_text))
    assert documented, "no 'agent-postmortem <command>' cells found in the command table"

    known = _known_commands()
    undocumented = known - documented
    unknown = documented - known
    assert not undocumented, f"README command table is missing: {sorted(undocumented)}"
    assert not unknown, f"README command table lists commands the parser does not have: {sorted(unknown)}"


def test_readme_demo_block_is_the_live_demo_output(capsys):
    exit_code = main(["demo"])
    live_output = capsys.readouterr().out
    assert exit_code == 0, "agent-postmortem demo must exit 0 on the packaged run"

    text = _readme_text()
    block_match = re.search(r"```\n(RUN wf_.*?\n)```", text, re.S)
    assert block_match, "README is missing the fenced demo output block"
    assert block_match.group(1) == live_output, (
        "README's demo block has drifted from the live 'agent-postmortem demo' output"
    )


def test_no_em_dash_in_user_facing_prose():
    offenders = [str(path) for path in PROSE_FILES if EM_DASH in path.read_text(encoding="utf-8")]
    assert not offenders, f"em-dash found in: {offenders}"


def test_readme_flag_sentence_matches_the_parser_exactly():
    """M8 review D6: which commands take --json and --no-redact is stated
    once, in prose, and must exactly match the parser -- not a guess a
    reader has to verify by running --help on every subcommand."""
    expected = _expected_flag_sentence()
    assert expected in _readme_text(), (
        f"README's flag sentence does not match the parser; expected:\n{expected}"
    )


def test_readme_passes_the_repository_privacy_rules():
    """M8 review D-B: README.md is already swept by tests/test_privacy.py
    (every tracked file), but its demo block is regenerated by hand each
    time the packaged run changes -- check it directly too, so a new wf_
    id, UUID, or real path riding along with a regenerated block is caught
    right here, not only by a whole-tree sweep."""
    text = _readme_text()
    violations = (
        privacy_rules.rule_uuid("README.md", text)
        + privacy_rules.rule_users_path("README.md", text)
        + privacy_rules.rule_home_path("README.md", text)
        + privacy_rules.rule_email("README.md", text)
        + privacy_rules.rule_wf_id("README.md", text)
    )
    assert not violations, violations
