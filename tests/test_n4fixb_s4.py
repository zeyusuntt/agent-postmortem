"""Fix B, S4: in every write form, options must precede the first operand (POSIX
getopt, what macOS runs); an option-looking word after an operand makes the
command opaque (docs/DESIGN.md 4.13.1; .reviews/v0.3/N4-dispositions.md S4).
The read-only grammar is unaffected: an operand there is only read.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands

R = "/tmp/ws/repo"


def match(command: str, cwd: str | None = R):
    return commands.match_write_grammar(commands.lex(command), cwd)


def targets(command: str):
    result = match(command)
    assert result is not None, command
    return [(t.path, t.kind) for t in result.targets]


TOUCH_OPAQUE_RED = [
    "touch a -t 202601010000 b",
    "touch a -c",
    "touch a b -m",
    "touch a -r ref",
    "touch a -d 2026-01-01",
    "touch a -ac",
    "touch a -- b",
    "touch a --",
    "touch -c a -m b",
]


@pytest.mark.parametrize("command", ["touch a -x", "touch -x a", "touch a -h"])
def test_an_unknown_touch_option_is_opaque_anywhere(command):
    assert match(command) is None


@pytest.mark.parametrize("command", TOUCH_OPAQUE_RED)
def test_a_touch_option_after_an_operand_is_opaque(command):
    assert match(command) is None
    assert commands.classify_call("Bash", command, cwd=R).row == 6


@pytest.mark.parametrize("command,expected", [
    ("touch -t 202601010000 a b", [(f"{R}/a", "touch"), (f"{R}/b", "touch")]),
    ("touch -c a b", [(f"{R}/a", "touch"), (f"{R}/b", "touch")]),
    ("touch -ac a b", [(f"{R}/a", "touch"), (f"{R}/b", "touch")]),
    ("touch -a -m a", [(f"{R}/a", "touch")]),
    ("touch -r ref a", [(f"{R}/a", "touch")]),
    ("touch -- -t", [(f"{R}/-t", "touch")]),
    ("touch -- a b", [(f"{R}/a", "touch"), (f"{R}/b", "touch")]),
    ("touch -- a -b", [(f"{R}/a", "touch"), (f"{R}/-b", "touch")]),
    ("touch -c -- a -b", [(f"{R}/a", "touch"), (f"{R}/-b", "touch")]),
    ("touch a -", [(f"{R}/a", "touch"), (f"{R}/-", "touch")]),
    ("touch a b c", [(f"{R}/a", "touch"), (f"{R}/b", "touch"), (f"{R}/c", "touch")]),
])
def test_a_touch_with_options_first_still_matches(command, expected):
    assert targets(command) == expected


@pytest.mark.parametrize("command", [
    "sed -i s/x/y/ f -n",
    "sed -i s/x/y/ -n f",
    "sed s/x/y/ -i f",
    "sed -i.bak s/x/y/ f -n",
    "sed -i.bak s/x/y/ -n f",
    "sed -i '' s/x/y/ f -n",
    "sed -i '' s/x/y/ -n f",
    "sed f -i s/x/y/",
    "sed -i s/x/y/ f -- g",
])
def test_a_sed_i_option_after_an_operand_is_opaque(command):
    assert match(command) is None


@pytest.mark.parametrize("command", [
    "cat f | tee f2 -a",
    "cat f | tee f2 -i",
    "cat f | tee f2 -- f3",
    "cat f | tee -a f2 -a",
])
def test_a_tee_option_after_an_operand_is_opaque(command):
    assert match(command) is None


@pytest.mark.parametrize("command", [
    "echo x > f -n",
    "echo x > f extra",
    "cat > f g",
    "cat > f -n",
])
def test_a_word_after_a_redirect_target_is_opaque(command):
    assert match(command) is None


def test_the_read_only_grammar_still_accepts_an_option_after_an_operand():
    assert commands.classify_call("Bash", "cat a -n", cwd=R).row == 4
    assert commands.classify_call("Bash", "grep x f -n", cwd=R).row == 4
    assert commands.classify_call("Bash", "ls d -la", cwd=R).row == 4
    assert match("cat a -n > out") is not None
