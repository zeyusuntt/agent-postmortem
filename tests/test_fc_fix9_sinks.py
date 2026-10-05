"""Fix round 9 (W9-7) of field milestone F: the output argparse writes by itself.

`.reviews/tooling/f/inventory_fix9.py` classifies the sites no `print` call shows: the usage line and
the error line of `_Parser.error`, `--help`, and `--version`. Each is proven here: whatever the
command line holds, none of these outputs echoes it, at share, at local and in the usage and help
texts at every level. (`tests/test_n3b_parser_errors.py` proves the error line value by value.)

Every fixture is synthetic.
"""

from __future__ import annotations

import pytest

from agent_postmortem import __version__
from agent_postmortem.cli import main

MARKERS = ("Zq9a", "Zq9b", "Zq9c")

CASES = [
    pytest.param(["scan", "--root", "/srv/Zq9a Zq9b", "--bogus-Zq9c"], 1, id="unrecognized_option"),
    pytest.param(["run", "wf_Zq9a Zq9b", "--bogus", "Zq9c"], 1, id="unrecognized_after_a_run_id"),
    pytest.param(["scan", "--root", "/srv/Zq9a Zq9b", "--help"], 0, id="subcommand_help"),
    pytest.param(["--help"], 0, id="top_level_help"),
    pytest.param(["--version"], 0, id="version"),
    pytest.param(["Zq9a"], 1, id="unknown_subcommand"),
    pytest.param([], 1, id="no_subcommand"),
]


@pytest.mark.parametrize("level_flag", [[], ["--local"]], ids=["share", "local"])
@pytest.mark.parametrize("argv,code", CASES)
def test_fc9_w7_argparse_usage_error_help_and_version_never_echo_the_command_line(
    argv, code, level_flag, capsys,
):
    capsys.readouterr()
    tokens = [*argv, *level_flag] if argv and argv[0] != "--version" and "--help" not in argv else argv
    with pytest.raises(SystemExit) as raised:
        main(tokens)
    captured = capsys.readouterr()
    assert raised.value.code == code
    for marker in MARKERS:
        assert marker not in captured.out and marker not in captured.err, (marker, captured)
    if code == 1:
        assert "usage: postmortem" in captured.err, captured.err
    if argv == ["--version"]:
        assert captured.out.strip() == f"agent-postmortem {__version__}"
