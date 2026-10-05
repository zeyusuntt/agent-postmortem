"""Fix round 1 of field milestone F: C1 (docs/DESIGN.md 4.13.1, 4.7, 5.3b;
.reviews/v0.3/F-dispositions.md).

The finding: FA6 counted a commit command the shell may never have reached
(`git diff --quiet || git commit -q -m x` in a clean repository; `false && git commit -q -m x;
true`) and said it "printed no id". One call's exit status cannot prove reachability, so the
claim is softened to what the transcript proves: the call CONTAINS a commit command, completed OK,
and reported no id. The internal fact is `commit_command_present`; the public clause is
"(<k> call(s) with a commit command reported no id)"; the `N+` lower bound stays (a commit may
have happened); the schema key `commit_commands_without_id` stays.

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import cli, commands
from tests.n4b2_helpers import result, text, use, workflow_run

CWD = "/tmp/ws"
IDIOMS = [
    pytest.param("git diff --quiet || git commit -q -m x", id="short_circuited_or"),
    pytest.param("false && git commit -q -m x; true", id="skipped_and_then_a_later_list"),
    pytest.param("git add -A; git commit -q -m x", id="plain_quiet_commit"),
]


def _killed_run(tmp_path, command, *, result_text=""):
    entries = [
        use("t1", "Bash", {"command": command}),
        result("t1", result_text),
        text(who="user", body="[Request interrupted by user]"),
    ]
    return workflow_run(tmp_path, entries, journal="open")


def _at_risk_line(run, capsys, *flags):
    cli.main(["run", str(run.path), *flags])
    out = capsys.readouterr().out
    return out, next(line for line in out.splitlines() if line.startswith("WORK AT RISK"))


@pytest.mark.parametrize("command", IDIOMS)
def test_fc1_the_classification_says_present_never_ran(command):
    call = commands.classify_call("Bash", command, cwd=CWD)
    assert call.commit_command_present is True
    assert not hasattr(call, "commit_ran")


def test_fc1_the_present_fact_is_false_for_a_background_dry_run_or_non_bash_call():
    assert commands.classify_call("Bash", "git commit -q -m x &", cwd=CWD).commit_command_present is False
    assert commands.classify_call("Bash", "git commit --dry-run", cwd=CWD).commit_command_present is False
    assert commands.classify_call("Monitor", "git commit -q -m x", cwd=CWD).commit_command_present is False


@pytest.mark.parametrize("command", IDIOMS)
def test_fc1_both_idioms_are_counted_as_one_call(tmp_path, command):
    run = _killed_run(tmp_path, command)
    assert run.attempts[0].facts.commit_commands_without_id == 1


@pytest.mark.parametrize("flags", [[], ["--no-redact"], ["--local"]])
@pytest.mark.parametrize("command", IDIOMS)
def test_fc1_the_work_at_risk_sentence_says_a_call_with_a_commit_command_reported_no_id(
    tmp_path, capsys, command, flags
):
    run = _killed_run(tmp_path, command)
    out, line = _at_risk_line(run, capsys, *flags)
    assert "0 commits reported (1 call with a commit command reported no id)" in line
    assert "printed no id" not in out
    assert "ran a commit" not in out


def test_fc1_the_sentence_pluralizes_calls(tmp_path, capsys):
    entries = [
        use("t1", "Bash", {"command": "git diff --quiet || git commit -q -m a"}),
        result("t1", ""),
        use("t2", "Bash", {"command": "git commit -q -m b"}),
        result("t2", ""),
        text(who="user", body="[Request interrupted by user]"),
    ]
    run = workflow_run(tmp_path, entries, journal="open")
    _, line = _at_risk_line(run, capsys)
    assert "(2 calls with a commit command reported no id)" in line


def _agent_table_commits(out):
    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("AGENT"))
    column = lines[header].index("COMMITS")
    return lines[header + 1][column:].split()[0]


@pytest.mark.parametrize("command", IDIOMS)
def test_fc1_the_commits_cell_keeps_its_lower_bound_mark(tmp_path, capsys, command):
    run = _killed_run(tmp_path, command)
    out, _ = _at_risk_line(run, capsys)
    assert _agent_table_commits(out) == "0+"


def test_fc1_a_call_that_printed_its_id_adds_no_clause(tmp_path, capsys):
    run = _killed_run(tmp_path, "git commit -m x", result_text="[main abc1234] x")
    _, line = _at_risk_line(run, capsys)
    assert "reported no id" not in line
    assert "1 commit reported" in line


def test_fc1_the_schema_key_is_unchanged_in_json(tmp_path, capsys):
    run = _killed_run(tmp_path, "git diff --quiet || git commit -q -m x")
    cli.main(["run", str(run.path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    (attempt,) = payload["run"]["attempts"]
    assert attempt["facts"]["commit_commands_without_id"] == 1
    assert "commit_command_present" not in json.dumps(payload)
