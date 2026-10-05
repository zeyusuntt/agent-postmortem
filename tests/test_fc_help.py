"""Part FC, P11 (field milestone F): `--help` lists the exit codes of `run`, `rescue plan`,
`rescue status`, and `watch`, taken exactly from the contract that owns them (docs/DESIGN.md
4.6/4.8 for `run`, 4.11 for the rescue commands, 4.12 for `watch`; .reviews/v0.3/F-plan.md FC).

The field case (econ): the exit codes of `rescue` were surprising and nothing said what they
were; `scan --help` already ended in an `exit codes:` epilog. The expected texts below are the
ones DESIGN 4.8 prints in its "Exit codes in --help" block, copied verbatim; a test pins that
the block in DESIGN, this file, and the real `--help` output agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_postmortem import cli

DESIGN = (Path(__file__).parent.parent / "docs" / "DESIGN.md").read_text(encoding="utf-8")

RUN = """\
exit codes:
  exit 0  the status is complete and the verdict is CLEAN or DEGRADED
  exit 1  usage error
  exit 2  no such run, or not a run
  exit 3  the verdict is FAILED (3 wins when 4 also applies)
  exit 4  the status is INCOMPLETE and the verdict is not FAILED
"""

RESCUE_PLAN = """\
exit codes (the first condition that applies wins):
  exit 1  usage error, or a configuration error
  exit 2  no such run
  exit 4  the run may still be running, there is nothing to rescue yet, a finished
          run's risk population is not empty or not complete, the lifecycle
          terminal is unknown, or any UNKNOWN fact is in the packet
  exit 0  otherwise
"""

RESCUE_STATUS = """\
exit codes (the first condition that applies wins):
  exit 1  usage error, or a configuration error
  exit 2  no such run
  exit 3  any row is NONZERO_OBSERVED
  exit 4  the lifecycle terminal is unknown, any row is NOT_OBSERVED or
          WRITE_AFTER_CHECK, the parent transcript could not be read, any
          NEVER-list match, or no unit at all in a run that is not finished with
          every attempt completed
  exit 0  otherwise
"""

WATCH = """\
exit codes (the first condition that applies wins):
  exit 1  usage error
  exit 2  no journal appeared within 60 seconds
  exit 3  DONE with verdict FAILED
  exit 4  LIMIT (--max-hours, or a malformed journal), or DONE with the lifecycle
          terminal unknown and the verdict not FAILED
  exit 0  DONE otherwise (verdict CLEAN or DEGRADED, with or without the
          (INCOMPLETE) suffix)
"""

CASES = [
    pytest.param(["run", "--help"], RUN, id="run"),
    pytest.param(["rescue", "plan", "--help"], RESCUE_PLAN, id="rescue plan"),
    pytest.param(["rescue", "status", "--help"], RESCUE_STATUS, id="rescue status"),
    pytest.param(["watch", "--help"], WATCH, id="watch"),
]


def _help(capsys, argv) -> str:
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("argv,expected", CASES)
def test_p11_help_ends_with_the_exit_codes_verbatim(capsys, argv, expected):
    out = _help(capsys, argv)
    assert out.endswith("\n" + expected) or out.endswith(expected), out
    assert out.count("exit codes") == 1


@pytest.mark.parametrize("argv,expected", CASES)
def test_p11_the_text_is_in_the_contract_verbatim(argv, expected):
    assert expected in DESIGN


@pytest.mark.parametrize("argv,expected", CASES)
def test_p11_every_epilog_line_fits_in_88_columns(capsys, argv, expected):
    out = _help(capsys, argv)
    tail = out[out.index("exit codes"):]
    assert max(len(line) for line in tail.splitlines()) <= 88


def test_p11_scan_help_is_unchanged(capsys):
    out = _help(capsys, ["scan", "--help"])
    assert out.endswith(cli._SCAN_EXIT_CODES)


def test_p11_the_codes_match_the_contract_for_each_command():
    """Every code number each epilog names is a code the owning section of DESIGN names."""
    assert re.findall(r"exit (\d)", RUN) == ["0", "1", "2", "3", "4"]
    assert re.findall(r"exit (\d)", RESCUE_PLAN) == ["1", "2", "4", "0"]
    assert re.findall(r"exit (\d)", RESCUE_STATUS) == ["1", "2", "3", "4", "0"]
    assert re.findall(r"exit (\d)", WATCH) == ["1", "2", "3", "4", "0"]


def test_p11_help_text_of_the_other_options_is_still_there(capsys):
    out = _help(capsys, ["watch", "--help"])
    assert "--interval" in out and "--done-grace" in out
    out = _help(capsys, ["run", "--help"])
    assert "--here" in out and "--observe" in out
    out = _help(capsys, ["rescue", "plan", "--help"])
    assert "--evidence" in out and "--json" in out
    out = _help(capsys, ["rescue", "status", "--help"])
    assert "--evidence" in out and "--json" in out
