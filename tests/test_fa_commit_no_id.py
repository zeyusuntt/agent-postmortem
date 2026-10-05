"""Part FA6 (field milestone F, T6): commit commands that printed no id
(docs/DESIGN.md 4.13.1 commit grammar, 4.1 AttemptFacts, 4.7 COMMITS and the
WORK AT RISK count line, 5.3b `facts`; .reviews/v0.3/F-plan.md FA6).

The field case: `git commit -q` prints no summary line, so A1's id grammar (which
deliberately rejects -q and --quiet, because a quiet segment poisons the call's id
count) found no id, and the agent's COMMITS column read `0`, an exact-looking zero for
agents that did commit. A separate grammar fact, `commit_command_ran`, recognizes the
command itself; the attempt fact `commit_commands_without_id` counts shell CALLS that
completed_ok, ran a commit command, and yielded no id; COMMITS then reads `N+`.

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import cli, commands, reader
from agent_postmortem.models import FACTS_UNKNOWN
from tests.n4b2_helpers import result, text, use, workflow_run

FIELD = "cd $WT && WT=$PWD zsh run_quick.sh x | tail -4; git add a b; git commit -q -m msg"
CWD = "/tmp/ws"


def _ran(command, cwd=CWD):
    return commands.classify_call("Bash", command, cwd=cwd).commit_command_present


# --- the grammar fact: one segment ---------------------------------------------------------


@pytest.mark.parametrize(
    "words,expected",
    [
        (["git", "commit", "-q", "-m", "msg"], True),
        (["git", "commit", "--quiet", "-m", "msg"], True),
        (["git", "commit", "-qm", "msg"], True),
        (["git", "commit", "--qui"], True),
        (["git", "commit", "-m", "msg"], True),
        (["git", "commit"], True),
        (["git", "-C", "/r", "commit", "-q"], True),
        (["git", "-c", "user.name=x", "commit", "--quiet"], True),
        (["git", "commit", "--dry-run"], False),
        (["git", "commit", "-q", "--dry-run"], False),
        (["git", "commit", "--dry-run", "--quiet"], False),
        (["git", "commit", "--porcelain"], False),
        (["git", "commit", "--short"], False),
        (["git", "commit", "-z"], False),
        (["git", "commit", "-q", "--", "--dry-run"], True),
        (["git", "status"], False),
        (["git", "log", "-q"], False),
        (["echo", "git", "commit", "-q"], False),
        (["git", "--no-pager", "commit", "-q"], False),
        ([], False),
    ],
)
def test_fa6_commit_command_ran_recognizes_the_segment(words, expected):
    assert commands.commit_command_ran(words) is expected


def test_fa6_a1s_id_grammar_still_rejects_the_quiet_forms():
    for command in ("git commit -q -m x", "git commit --quiet -m x", "git add a; git commit -qm x"):
        assert commands.commit_facts(command, CWD).segments == ()
    assert len(commands.commit_facts("git commit -m x", CWD).segments) == 1


# --- the call: placement judged per candidate segment --------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param(FIELD, True, id="the field shape"),
        pytest.param("git add -A; git commit -q -m x", True, id="quiet"),
        pytest.param("git add -A; git commit --quiet -m x", True, id="--quiet"),
        pytest.param("git commit -q -m a; git commit -q -m b", True, id="two quiet segments"),
        pytest.param("git commit -m x", True, id="a plain commit"),
        pytest.param("cd /abs && git commit -q -m x", True, id="after a placed cd"),
        pytest.param("cd $WT && git commit -q -m x", True, id="after a cd with an expansion"),
        pytest.param("echo $(date) && git commit -q -m x", True, id="after a substitution"),
        pytest.param("echo *.md; git commit -q -m x", True, id="after a glob"),
        pytest.param("WT=$PWD true; git commit -q -m x", True, id="after an assignment"),
        pytest.param("echo hi & git commit -q -m x", True, id="a separate foreground list"),
        pytest.param("bash -c 'git commit -q -m x'", True, id="inside one bash -c"),
        pytest.param('git commit -q -m "$(cat <<\'EOF\'\nmsg\nEOF\n)"', True,
                     id="the literal message substitution"),
        pytest.param(f"{FIELD.rsplit(';', 1)[0]}; echo \"git commit -q -m msg\"", False,
                     id="quoted data, double quotes"),
        pytest.param(f"{FIELD.rsplit(';', 1)[0]}; echo 'git commit -q -m msg'", False,
                     id="quoted data, single quotes"),
        pytest.param(f"{FIELD.rsplit(';', 1)[0]}; cat <<'EOF'\ngit commit -q -m msg\nEOF", False,
                     id="heredoc data, quoted delimiter"),
        pytest.param(f"{FIELD.rsplit(';', 1)[0]}; cat <<EOF\ngit commit -q -m $x\nEOF", False,
                     id="heredoc data, expanding"),
        pytest.param("git commit -q -m x --dry-run", False, id="--dry-run"),
        pytest.param("git commit --dry-run", False, id="plain --dry-run"),
        pytest.param("git commit -q -m x &", False, id="backgrounded by a trailing &"),
        pytest.param("cd /abs && git commit -q -m x &", False, id="a backgrounded list"),
        pytest.param("git commit -q -m x & echo hi", False, id="backgrounded and followed"),
        pytest.param("nohup git commit -q -m x", False, id="nohup"),
        pytest.param('git commit -q -m "$MSG"', False, id="an expansion in the candidate"),
        pytest.param('echo hi; git commit -q -m "$MSG"', False, id="an expansion in a later candidate"),
        pytest.param("echo hi; git commit -q -m $(date)", False,
                     id="a substitution in a later candidate"),
        pytest.param("git commit -q -m $(date)", False, id="a substitution in the candidate"),
        pytest.param('git commit -q -m "unterminated', False, id="an unbalanced quote"),
        pytest.param("git commit -q -m *.md", False, id="a glob in the candidate"),
        pytest.param("GIT_X=1 git commit -q -m x", False, id="an assignment prefix"),
        pytest.param("git commit -q -m x &&", False, id="a dangling operator"),
        pytest.param("bash -c 'bash -c \"git commit -q\"'", False, id="a wrapper two deep"),
        pytest.param("bash -c 'git commit -q $X'", False, id="an unplaced wrapper body"),
        pytest.param("git status", False, id="not a commit"),
        pytest.param("echo git commit -q", False, id="the words in an echo"),
    ],
)
def test_fa6_call_ran_a_commit_command(command, expected):
    assert _ran(command) is expected


def test_fa6_run_in_background_is_not_a_ran_commit():
    call = commands.classify_call("Bash", "git commit -q -m x", run_in_background=True, cwd=CWD)
    assert call.commit_command_present is False


def test_fa6_a_non_bash_tool_never_ran_a_commit():
    assert commands.classify_call("Monitor", "git commit -q -m x", cwd=CWD).commit_command_present is False


def test_fa6_the_reader_classifies_the_full_command_not_the_stored_copy(tmp_path):
    pad = "a" * 3000
    path = tmp_path / "t.jsonl"
    entries = [use("t1", "Bash", {"command": f"echo {pad}; git commit -q -m x"}), result("t1", "")]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    (call,) = reader._read_transcript(path, [])[5]
    assert call.command is None
    assert call.classification.commit_command_present is True


# --- the attempt fact -------------------------------------------------------------------


def _attempt_facts(tmp_path, entries, **kwargs):
    run = workflow_run(tmp_path, entries, **kwargs)
    return run, run.attempts[0].facts


def _call(command, result_text="", *, tool_id="t1", **extra):
    return [use(tool_id, "Bash", {"command": command, **extra}), result(tool_id, result_text)]


def test_fa6_the_field_shape_counts_one_call(tmp_path):
    _, facts = _attempt_facts(tmp_path, _call(FIELD))
    assert facts.commit_commands_without_id == 1
    assert facts.commits == []


def test_fa6_apparent_commits_inside_data_count_nothing(tmp_path):
    quoted = FIELD.rsplit(";", 1)[0] + '; echo "git commit -q -m msg"'
    heredoc = FIELD.rsplit(";", 1)[0] + "; cat <<'EOF'\ngit commit -q -m msg\nEOF"
    for n, command in enumerate((quoted, heredoc)):
        _, facts = _attempt_facts(tmp_path / str(n), _call(command))
        assert facts.commit_commands_without_id == 0


@pytest.mark.parametrize("command", ["git add -A; git commit -q -m x", "git add -A; git commit --quiet -m x"])
def test_fa6_quiet_commit_calls_count_one_each(tmp_path, command):
    _, facts = _attempt_facts(tmp_path, _call(command))
    assert facts.commit_commands_without_id == 1


def test_fa6_a_call_with_two_quiet_commit_segments_is_one_call(tmp_path):
    _, facts = _attempt_facts(tmp_path, _call("git commit -q -m a; git commit -q -m b"))
    assert facts.commit_commands_without_id == 1


def test_fa6_two_separate_calls_are_two(tmp_path):
    entries = _call("git commit -q -m a", tool_id="t1") + _call("git commit -q -m b", tool_id="t2")
    _, facts = _attempt_facts(tmp_path, entries)
    assert facts.commit_commands_without_id == 2


def test_fa6_a_commit_with_a_summary_line_counts_its_id_and_no_missing_one(tmp_path):
    _, facts = _attempt_facts(tmp_path, _call("git commit -m x", "[main abc1234] x\n 1 file changed"))
    assert facts.commits == ["abc1234"]
    assert facts.commit_commands_without_id == 0


def test_fa6_a_plain_commit_with_no_summary_in_its_output_counts(tmp_path):
    _, facts = _attempt_facts(tmp_path, _call("git commit -m x", "hook output only"))
    assert facts.commits == []
    assert facts.commit_commands_without_id == 1


def test_fa6_a_mixed_call_loses_its_id_to_the_quiet_flag_and_counts_once(tmp_path):
    entries = _call("git commit -q -m a; git commit -m b", "[main abc1234] b")
    _, facts = _attempt_facts(tmp_path, entries)
    assert facts.commits == []
    assert facts.commit_commands_without_id == 1


@pytest.mark.parametrize(
    "command",
    ["git commit --dry-run", "git commit -q -m x &", "git status", "echo git commit -q"],
)
def test_fa6_non_commit_dry_run_and_backgrounded_calls_count_nothing(tmp_path, command):
    _, facts = _attempt_facts(tmp_path, _call(command))
    assert facts.commit_commands_without_id == 0


def test_fa6_run_in_background_counts_nothing(tmp_path):
    _, facts = _attempt_facts(tmp_path, _call("git commit -q -m x", run_in_background=True))
    assert facts.commit_commands_without_id == 0


def test_fa6_a_completed_error_call_counts_nothing(tmp_path):
    entries = [use("t1", "Bash", {"command": "git commit -q -m x"}),
               result("t1", "nothing to commit", is_error=True)]
    _, facts = _attempt_facts(tmp_path, entries)
    assert facts.commit_commands_without_id == 0


def test_fa6_a_call_with_no_result_counts_nothing(tmp_path):
    _, facts = _attempt_facts(tmp_path, [use("t1", "Bash", {"command": "git commit -q -m x"})])
    assert facts.commit_commands_without_id == 0


def test_fa6_a_duplicated_tool_use_id_counts_nothing(tmp_path):
    entries = _call("git commit -q -m a") + [use("t1", "Bash", {"command": "git commit -q -m b"})]
    _, facts = _attempt_facts(tmp_path, entries)
    assert facts.commit_commands_without_id == 0


def test_fa6_unreadable_facts_say_unknown_never_a_false_zero():
    facts = reader._build_attempt_facts([], "completed", None, reader.Path("agent-x.jsonl"), True)
    assert facts.commit_commands_without_id == FACTS_UNKNOWN
    assert facts.to_dict()["commit_commands_without_id"] == FACTS_UNKNOWN


def test_fa6_to_dict_carries_the_key_and_a_hand_built_fact_defaults_to_zero(tmp_path):
    _, facts = _attempt_facts(tmp_path, _call("git add a"))
    assert facts.to_dict()["commit_commands_without_id"] == 0


# --- rendering: COMMITS, WORK AT RISK, JSON -----------------------------------------------


def _text(run, capsys, *flags):
    cli.main(["run", str(run.path), "--no-redact", *flags])
    return capsys.readouterr().out


def _commits_cell(out):
    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("AGENT"))
    column = lines[header].index("COMMITS")
    return lines[header + 1][column:].split()[0]


def test_fa6_commits_reads_zero_plus_for_a_quiet_commit(tmp_path, capsys):
    run, _ = _attempt_facts(tmp_path, _call("git add -A; git commit -q -m x"))
    assert _commits_cell(_text(run, capsys)) == "0+"


def test_fa6_commits_reads_a_plain_count_when_every_commit_printed_its_id(tmp_path, capsys):
    run, _ = _attempt_facts(tmp_path, _call("git commit -m x", "[main abc1234] x"))
    assert _commits_cell(_text(run, capsys)) == "1"


def test_fa6_commits_reads_n_plus_when_some_commits_have_ids_and_one_does_not(tmp_path, capsys):
    entries = _call("git commit -m x", "[main abc1234] x", tool_id="t1") + _call(
        "git commit -q -m y", tool_id="t2")
    run, _ = _attempt_facts(tmp_path, entries)
    assert _commits_cell(_text(run, capsys)) == "1+"


def _killed_with(entries):
    return [*entries, text(who="user", body="[Request interrupted by user]")]


def test_fa6_the_work_at_risk_count_line_names_the_commit_commands_without_an_id(
    tmp_path, capsys
):
    run, _ = _attempt_facts(tmp_path, _killed_with(_call("git commit -q -m x")), journal="open")
    out = _text(run, capsys)
    line = next(line for line in out.splitlines() if line.startswith("WORK AT RISK"))
    assert "0 commits reported (1 call with a commit command reported no id)" in line


def test_fa6_the_count_line_pluralizes_and_sums_over_the_attempts_at_risk(tmp_path, capsys):
    entries = _call("git commit -q -m a", tool_id="t1") + _call("git commit -q -m b", tool_id="t2")
    run, _ = _attempt_facts(tmp_path, _killed_with(entries), journal="open")
    line = next(line for line in _text(run, capsys).splitlines() if line.startswith("WORK AT RISK"))
    assert "(2 calls with a commit command reported no id)" in line


def test_fa6_the_count_line_is_unchanged_when_every_commit_printed_its_id(tmp_path, capsys):
    entries = _call("git commit -m x", "[main abc1234] x")
    run, _ = _attempt_facts(tmp_path, _killed_with(entries), journal="open")
    line = next(line for line in _text(run, capsys).splitlines() if line.startswith("WORK AT RISK"))
    assert "reported no id" not in line
    assert "1 commit reported" in line


@pytest.mark.parametrize("flag", ["--no-redact", "--local", None])
def test_fa6_json_carries_the_key_at_every_level(tmp_path, capsys, flag):
    run, _ = _attempt_facts(tmp_path, _call("git commit -q -m x"))
    argv = ["run", str(run.path), "--json"] + ([flag] if flag else [])
    cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    (attempt,) = payload["run"]["attempts"]
    assert attempt["facts"]["commit_commands_without_id"] == 1
    assert list(attempt["facts"]).index("commit_commands_without_id") == list(attempt["facts"]).index(
        "commits") + 1
