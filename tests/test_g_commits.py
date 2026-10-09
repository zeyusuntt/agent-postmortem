"""G-B, G4 (v0.3.1): the COMMITS cell of the AGENT table and its legends (docs/DESIGN.md 4.7).

No known commit id and a completed shell call that contained a commit command reads `?` (it was
`0+`); at least one id and more id-less calls reads `N+`. The legends say what the call
contained, never that a commit ran or that anything was printed.
"""

from __future__ import annotations

import re

import pytest

from agent_postmortem import cli
from tests.n4b2_helpers import result, session_run, use

PLUS_LEGEND = "+ is a lower bound: incomplete evidence may hide more paths or commits."
QUESTION_LEGEND = (
    "COMMITS ? a completed shell call contained a commit command but reported no commit id"
)
CWD = "/tmp/ws"
X = "a" + "1" * 16
Y = "a" + "2" * 16


def _call(command, result_text="", tool_id="t1"):
    return [use(tool_id, "Bash", {"command": command}), result(tool_id, result_text)]


def _render(tmp_path, capsys, children):
    run = session_run(tmp_path, [{"type": "user", "message": {"role": "user", "content": "go"}}],
                      children)
    cli.main(["run", str(run.path), "--no-redact"])
    return capsys.readouterr().out


def _table(out):
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("AGENT"))
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return lines[start:end]


def _cells(table):
    column = table[0].index("COMMITS")
    cells = [row[column:].split()[0] for row in table[1:] if row[column:].strip()]
    return [cell for cell in cells if re.fullmatch(r"\d+\+?|\?", cell)]


def test_g4_no_id_reads_question_mark_and_its_legend(tmp_path, capsys):
    out = _render(tmp_path, capsys, {X: _call("git add -A; git commit -q -m x")})
    table = _table(out)
    assert "?" in _cells(table)
    assert QUESTION_LEGEND in table


def test_g4_one_id_and_an_idless_call_reads_one_plus(tmp_path, capsys):
    entries = _call("git commit -m x", "[main abc1234] x", "t1") + _call(
        "git commit -q -m y", "", "t2")
    table = _table(_render(tmp_path, capsys, {X: entries}))
    assert _cells(table) == ["1+"]
    assert PLUS_LEGEND in table
    assert QUESTION_LEGEND not in table


def test_g4_both_legends_in_one_table_plus_first(tmp_path, capsys):
    one = _call("git commit -m x", "[main abc1234] x", "t1") + _call("git commit -q -m y", "", "t2")
    two = _call("git commit -q -m z")
    table = _table(_render(tmp_path, capsys, {X: one, Y: two}))
    assert sorted(_cells(table)) == ["1+", "?"]
    assert PLUS_LEGEND in table and QUESTION_LEGEND in table
    assert table.index(PLUS_LEGEND) < table.index(QUESTION_LEGEND)


def test_g4_a_plain_count_has_no_legend(tmp_path, capsys):
    table = _table(_render(tmp_path, capsys, {X: _call("git commit -m x", "[main abc1234] x")}))
    assert _cells(table) == ["1"]
    assert QUESTION_LEGEND not in table


@pytest.mark.parametrize(
    "command",
    ["false && git commit -q -m x; true", "git diff --quiet || git commit -q -m x"],
)
def test_g4_short_circuit_commands_get_the_contained_wording(tmp_path, capsys, command):
    table = _table(_render(tmp_path, capsys, {X: _call(command)}))
    assert "?" in _cells(table)
    legend = next(line for line in table if line.startswith("COMMITS ?"))
    assert "contained" in legend and "reported no commit id" in legend
    assert "ran" not in legend.split() and "printed" not in legend
