"""`hook pre-bash` warns before a background `codex exec` with no stdin redirect runs (item 5 of
the field report 2026-11-03 "no silent failures"; docs/DESIGN.md 4.14.5).

A warning beside the call, never a deny: over 30 days on the reference machine, 276 foreground
and 51 of 60 background `codex exec` runs with no redirect finished, and one background run hung
26 hours on the stdin nobody closed.

Synthetic commands only.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_postmortem import entry

SRC = Path(__file__).resolve().parent.parent / "src"
FIELD = ('cd /tmp/w; cat > p.txt <<\'EOF\'\nreview this\nEOF\n'
         'codex exec --sandbox read-only "$(cat p.txt)" > review.md 2> review.log; echo "exit=$?"')


def _pre(monkeypatch, capsys, command, *, background=True, tool="Bash", **extra) -> str:
    tool_input = {"command": command, "description": "x"}
    if background:
        tool_input["run_in_background"] = True
    payload = {"hook_event_name": "PreToolUse", "session_id": "x", "tool_name": tool,
               "tool_input": tool_input, **extra}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert entry.main(["hook", "pre-bash"]) == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return ""
    data = json.loads(out)["hookSpecificOutput"]
    assert "permissionDecision" not in data  # a warning, never a deny or an ask
    return data["additionalContext"]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(FIELD, id="the field command"),
        pytest.param("codex exec 'review the diff' > out.md", id="a plain prompt"),
        pytest.param("timeout 900 codex exec x > out.md 2> err.log", id="under timeout"),
        pytest.param("cd /tmp/w && codex e x", id="the short form"),
    ],
)
def test_a_background_codex_exec_without_stdin_redirect_is_warned(monkeypatch, capsys, command):
    text = _pre(monkeypatch, capsys, command)
    assert text.startswith("STDIN") and "</dev/null" in text, text
    assert len(text) <= 400


@pytest.mark.parametrize(
    "command,background",
    [
        pytest.param("codex exec x < /dev/null > out.md", True, id="stdin from /dev/null"),
        pytest.param("codex exec - < prompt.txt > out.md", True, id="stdin from a file"),
        pytest.param("cat prompt.txt | codex exec - > out.md", True, id="fed by a pipe"),
        pytest.param("codex exec - <<'EOF'\nreview\nEOF", True, id="a heredoc"),
        pytest.param("codex exec x <<< 'review'", True, id="a here-string"),
        pytest.param("codex exec x > out.md", False, id="in the foreground"),
        pytest.param("echo codex exec is a tool", True, id="codex only in an echo's words"),
        pytest.param("ls -la", True, id="no codex at all"),
    ],
)
def test_no_warning(monkeypatch, capsys, command, background):
    assert _pre(monkeypatch, capsys, command, background=background) == ""


def test_a_subagents_own_call_is_warned_too(monkeypatch, capsys):
    # the warning is about the caller's own call, so it goes to whoever makes it
    assert _pre(monkeypatch, capsys, "codex exec x > out.md", agent_id="a0000000000000001")


def test_a_call_with_no_codex_imports_nothing_past_json():
    code = ("import io, json, sys; sys.stdin = io.StringIO(json.dumps({'hook_event_name': "
            "'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': 'ls', "
            "'run_in_background': True}})); from agent_postmortem import entry; "
            "entry.main(['hook', 'pre-bash']); "
            "print(sorted(m for m in sys.modules if m.startswith('agent_postmortem')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}, check=True)
    loaded = json.loads(out.stdout.strip().replace("'", '"'))
    assert loaded == ["agent_postmortem", "agent_postmortem.entry", "agent_postmortem.pre_bash"]


# --- further cases ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("env -u SOME_VAR codex exec x", id="env with -u"),
        pytest.param("time -p codex exec x", id="time -p"),
        pytest.param("command -- codex exec x", id="command --"),
        pytest.param("timeout -s TERM 900 codex exec x", id="timeout with a signal"),
        pytest.param("timeout --kill-after=5 900 codex exec x", id="timeout --kill-after="),
        pytest.param("codex exec review $(cat </dev/null)", id="a redirect inside a substitution"),
        pytest.param('co"dex" exec x', id="a quoted command word"),
    ],
)
def test_round_one_missed_warnings(monkeypatch, capsys, command):
    assert _pre(monkeypatch, capsys, command).startswith("STDIN"), command


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("{ echo ready; codex exec x; } </dev/null", id="a group given stdin"),
        pytest.param("printf review | { echo ready; codex exec -; }", id="a group fed by a pipe"),
        pytest.param("codex exec x 3</dev/null 0<&3", id="stdin duplicated from another fd"),
        pytest.param("codex exec x 0<&-", id="stdin closed"),
    ],
)
def test_round_one_false_warnings(monkeypatch, capsys, command):
    assert _pre(monkeypatch, capsys, command) == ""


# --- further cases ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("codex --sandbox read-only exec review", id="a global option first"),
        pytest.param("codex -c model=x -m gpt exec review", id="global options with values"),
        pytest.param("codex exec review >$(printf /dev/null </dev/null)",
                     id="a substitution in a redirect target"),
        pytest.param('env -S "codex exec review"', id="env -S"),
        pytest.param('env --split-string="codex exec review"', id="env --split-string="),
        pytest.param("env -iu HOME codex exec review", id="clustered env options"),
        pytest.param("co\\\ndex exec review", id="a line continuation in the word"),
    ],
)
def test_round_two_missed_warnings(monkeypatch, capsys, command):
    assert _pre(monkeypatch, capsys, command).startswith("STDIN"), command


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("codex exec $(printf review) </dev/null", id="a substitution beside a real redirect"),
        pytest.param("exec </dev/null; codex exec review", id="stdin closed for the whole shell"),
        pytest.param("codex exec review 0>&-", id="fd 0 closed by an output form"),
        pytest.param("command -v codex exec", id="command -v only looks it up"),
        pytest.param("command -V codex exec", id="command -V only describes it"),
    ],
)
def test_round_two_false_warnings(monkeypatch, capsys, command):
    assert _pre(monkeypatch, capsys, command) == ""


# --- further cases -------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("codex exec review $(cat </dev/null )", id="a spaced substitution"),
        pytest.param("codex exec review >$(printf /dev/null </dev/null )",
                     id="a spaced substitution in a redirect operand"),
        pytest.param("codex --add-dir /tmp exec review", id="--add-dir takes a value"),
        pytest.param("env -S \"co'dex' exec review\"", id="quoting inside env -S"),
        pytest.param('env -S "env codex exec review"', id="a wrapper inside env -S"),
        pytest.param("false && exec </dev/null; codex exec review", id="an exec that may not run"),
        pytest.param("printf x | exec </dev/null; codex exec review", id="an exec in a pipe"),
        pytest.param("exec </dev/null & codex exec review", id="an exec in the background"),
    ],
)
def test_round_three_missed_warnings(monkeypatch, capsys, command):
    assert _pre(monkeypatch, capsys, command).startswith("STDIN"), command


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("codex exec review <<< 'review foo()'", id="a literal paren in a here-string"),
        pytest.param("codex exec $(printf review) </dev/null", id="a substitution and a real redirect"),
        pytest.param('codex exec "$(cat p.txt)" < /dev/null > out.md', id="the safe field shape"),
    ],
)
def test_round_three_false_warnings(monkeypatch, capsys, command):
    assert _pre(monkeypatch, capsys, command) == ""
