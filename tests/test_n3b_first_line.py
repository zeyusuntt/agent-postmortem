"""N3b P15: the first line of every text output at local and raw.

"disclosure: local (real names and paths; do not paste)" or "disclosure: raw
(real names, absolute paths; do not paste)" is the very first line written to
stdout. Share prints no such line; JSON never. A command that prints nothing
to stdout (an error that only writes to stderr) prints no label either: the
label comes first *when anything is printed to stdout*, never alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "runs"
ROOT = FIXTURES / "reader_minimal"
RUN = (
    ROOT / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
    / "subagents" / "workflows" / "wf_b0000000-002"
)
LABEL = {
    "local": "disclosure: local (real names and paths; do not paste)",
    "raw": "disclosure: raw (real names, absolute paths; do not paste)",
}
FLAG = {"local": "--local", "raw": "--raw"}

TEXT_COMMANDS = {
    "scan": ["scan", "--root", str(ROOT)],
    "scan --all": ["scan", "--all", "--root", str(ROOT)],
    "run": ["run", str(RUN)],
    "run --verbose": ["run", str(RUN), "--verbose", "--evidence"],
    "run --bug-report": ["run", str(RUN), "--bug-report"],
    "rules": ["rules", "--root", str(ROOT)],
    "rules --all": ["rules", "--all", "--root", str(ROOT)],
    "rules --run": ["rules", "--run", str(RUN)],
    "rules --show": ["rules", "--show", "R-RETRY-SAFE"],
    "rules propose": ["rules", "propose", "--root", str(ROOT)],
    "rules review": ["rules", "review", "--root", str(ROOT)],
    "trend": ["trend", "--root", str(ROOT)],
    "rescue plan": ["rescue", "plan", str(RUN)],
    "rescue status": ["rescue", "status", str(RUN)],
}


def _run(argv, capsys):
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("level", ["local", "raw"])
@pytest.mark.parametrize("name", sorted(TEXT_COMMANDS))
def test_text_output_starts_with_the_label(name, level, capsys):
    _code, out, err = _run([*TEXT_COMMANDS[name], FLAG[level]], capsys)
    assert out.splitlines()[0] == LABEL[level]
    assert out.count("disclosure:") == 1
    assert LABEL[level] not in err


def test_no_redact_is_raw_and_prints_the_raw_label(capsys):
    _code, out, _err = _run(["run", str(RUN), "--no-redact"], capsys)
    assert out.splitlines()[0] == LABEL["raw"]


@pytest.mark.parametrize("name", sorted(TEXT_COMMANDS))
def test_share_text_has_no_label(name, capsys):
    _code, out, _err = _run(TEXT_COMMANDS[name], capsys)
    assert "disclosure:" not in out


@pytest.mark.parametrize(
    "argv",
    [
        ["scan", "--json", "--root", str(ROOT)],
        ["run", str(RUN), "--json"],
        ["rules", "--json", "--root", str(ROOT)],
        ["rules", "--show", "R-RETRY-SAFE", "--json"],
        ["rules", "propose", "--json", "--root", str(ROOT)],
        ["rules", "review", "--json", "--root", str(ROOT)],
        ["trend", "--json", "--root", str(ROOT)],
        ["rescue", "plan", str(RUN), "--json"],
        ["rescue", "status", str(RUN), "--json"],
    ],
)
def test_json_never_carries_the_label(argv, capsys):
    _code, out, _err = _run([*argv, "--raw"], capsys)
    assert "disclosure:" not in out
    json.loads(out)  # still one valid JSON document, first byte "{"


@pytest.mark.parametrize("level", ["local", "raw"])
def test_a_command_that_prints_nothing_to_stdout_prints_no_label(level, tmp_path, capsys):
    code, out, err = _run(["scan", "--root", str(tmp_path / "no-runs"), FLAG[level]], capsys)
    assert code == 2
    assert out == ""
    assert "disclosure:" not in err


@pytest.mark.parametrize("level", ["local", "raw"])
def test_apply_output_starts_with_the_label(level, tmp_path, capsys):
    target = tmp_path / "instructions.md"
    target.write_text("# notes\n", encoding="utf-8")
    code, out, _err = _run(
        ["apply", "R-RETRY-SAFE", "--target", str(target), FLAG[level]], capsys
    )
    assert code == 0
    assert out.splitlines()[0] == LABEL[level]
