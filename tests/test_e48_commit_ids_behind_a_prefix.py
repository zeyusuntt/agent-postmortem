"""Commit ids of a commit the lexer cannot place as a whole command (field report
2026-11-03, "watch under Monitor delivers nothing until the run ends", its COMMITS part;
docs/DESIGN.md 4.13.1, the commit grammar and FA6).

The field shape: a build agent's commit ended an `&&` chain whose first word was a shell
assignment (`L=logs/x && ... > $L/a.log && SKIP=mypy mise exec -- git commit -m '...'`).
git printed its `[branch sha] subject` line, but A1 read ids only from a command the lexer
places as a whole, so the assignment and the `$L` expansions elsewhere hid the id, and the
agent read `COMMITS ?` although its commit was on the branch.

The fix judges such a command segment by segment, the way FA6 judges presence: a commit
segment whose own text is literal, behind at most a run-as-is prefix, gives its id. The
call still counts no id when a segment that spells `commit` cannot be judged (an expansion
in the commit itself, eval), when any word assigns a `GIT_` variable (git reads its
repository from those), and in every case A1 already rejects (quiet, dry run, background).

Fixtures are synthetic: paths under /tmp, the registered synthetic run id of the helpers.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands
from tests.n4b2_helpers import ledger_of, result, use, workflow_run
from tests.test_fa_commit_no_id import _commits_cell, _text

CWD = "/tmp/ws"
SUMMARY = "check one....Passed\n[feature/x 750b62f] x: a synthetic subject\n 2 files changed\n"
FIELD = (
    "L=logs/x && git add a b && git status --short | head && run-check -C /tmp/ws remote "
    "-- check > $L/a.log 2>&1 && grep -q Passed $L/a.log && "
    "SKIP=mypy mise exec -- git commit -m 'x: a synthetic subject'"
)


def _ids(command: str, output: str = SUMMARY, cwd: str = CWD) -> tuple[str, ...]:
    return commands.commit_ids(commands.commit_facts(command, cwd), output)


def _call(command, output=SUMMARY, tool_id="t1"):
    return [use(tool_id, "Bash", {"command": command}), result(tool_id, output)]


# --- the grammar: ids read --------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(FIELD, id="the field shape"),
        pytest.param("SKIP=mypy mise exec -- git commit -m x", id="a hook-skip prefix"),
        pytest.param("SKIP=mypy git commit -m x", id="an assignment prefix"),
        pytest.param("env A=1 git commit -m x", id="env with an assignment"),
        pytest.param("mise x node@20 -- git -C /tmp/ws commit -m x", id="mise x with a tool"),
        pytest.param("cd $WT && git commit -m x", id="an expansion in an earlier segment"),
        pytest.param("L=x && git commit -m x 2>&1 | tail -3", id="a pipe after the commit"),
        pytest.param("L=x && bash -c 'git commit -m x'", id="one placed wrapper"),
        pytest.param("L=x && echo commit > $L/note && git commit -m x",
                     id="a word commit in an echo"),
        # real shape: a label variable set in a segment of its own, its text naming the commit
        pytest.param("T='line 3 task 2 commit' && git add a && git commit -m x",
                     id="a literal label assignment that spells commit"),
    ],
)
def test_e48_a_literal_commit_segment_gives_its_id_in_an_unplaced_command(command):
    assert _ids(command) == ("750b62f",)


def test_e48_two_literal_commit_segments_give_the_last_two_ids():
    output = "[main 1111111] a\n hook\n[main 2222222] b\n"
    assert _ids("L=x && git commit -m a && SKIP=y git commit -m b", output) == (
        "1111111", "2222222")


# --- the grammar: the call still counts no id ------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("GIT_DIR=/tmp/other/.git git commit -m x", id="GIT_DIR on the commit"),
        pytest.param("L=x && GIT_WORK_TREE=/tmp/o git commit -m x", id="GIT_WORK_TREE"),
        pytest.param("env GIT_INDEX_FILE=/tmp/i git commit -m x", id="a git variable after env"),
        pytest.param("export GIT_DIR=/tmp/o/.git && L=$X && git commit -m x",
                     id="an exported git variable"),
        pytest.param("L=x && git commit -q -m x", id="quiet"),
        pytest.param("SKIP=a mise exec -- git commit --dry-run", id="a dry run"),
        pytest.param('L=x && git commit -m "$MSG"', id="an expansion in the commit itself"),
        pytest.param('L=x && git commit -m a && git commit -m "$MSG"',
                     id="one commit that cannot be judged poisons the call"),
        pytest.param("L=x && eval 'git commit -m x'", id="eval"),
        pytest.param('T="a $(git commit -m y)" && git commit -m x',
                     id="a substitution in an assignment value"),
        pytest.param('T="label $X commit" && git commit -m x',
                     id="an expansion in an assignment value"),
        pytest.param("L=x && time git commit -m x", id="an opaque first word"),
        pytest.param("L=x && git commit -m x &", id="backgrounded"),
        pytest.param("mise exec -C /tmp/o -- git commit -m x", id="a mise option"),
        pytest.param("L=x && bash -c \"git commit -m $M\"", id="an unplaced wrapper"),
        pytest.param("L=x && cat > $L/r.md <<'EOF'\ngit commit -m x\nEOF",
                     id="words in a heredoc"),
    ],
)
def test_e48_an_unplaced_command_that_cannot_be_judged_counts_no_id(command):
    assert _ids(command) == ()


def test_e48_a_placed_command_reads_its_ids_as_before():
    assert _ids("git add a && git commit -m x") == ("750b62f",)
    assert _ids("git commit -q -m x") == ()


# --- the ledger keeps the call's candidate repository ----------------------------------------


def test_e48_an_unplaced_commit_call_keeps_the_calls_candidate_repository(tmp_path):
    call = commands.classify_call("Bash", FIELD, cwd=CWD)
    assert call.repo == CWD
    (entry,) = ledger_of(tmp_path, _call(FIELD)).entries
    assert entry.repo == CWD


# --- the attempt fact and the COMMITS column ---------------------------------------------


def test_e48_the_field_shape_counts_its_commit_and_no_id_less_call(tmp_path):
    run = workflow_run(tmp_path, _call(FIELD))
    facts = run.attempts[0].facts
    assert facts.commits == ["750b62f"]
    assert facts.commit_commands_without_id == 0


def test_e48_the_field_shape_reads_one_commit_not_a_question_mark(tmp_path, capsys):
    run = workflow_run(tmp_path, _call(FIELD))
    assert _commits_cell(_text(run, capsys)) == "1"


def test_e48_a_quiet_commit_in_the_same_shape_still_reads_a_question_mark(tmp_path, capsys):
    quiet = FIELD.replace("git commit -m", "git commit -q -m")
    run = workflow_run(tmp_path, _call(quiet, "4ee1221 x: a synthetic subject\n"))
    assert run.attempts[0].facts.commit_commands_without_id == 1
    assert _commits_cell(_text(run, capsys)) == "?"


# --- review round 1 ----------------------------------------------------------------------


def test_e48_an_opaque_word_behind_a_placed_mise_prefix_poisons_the_call():
    # the quiet commit's hook prints a summary-shaped line; with the quiet segment hidden
    # behind `mise exec -- time`, the one visible commit would take that forged id
    output = "[main 1111111] a\n[main deadbee] hook\n"
    assert _ids("git commit -m a && mise exec -- time git commit -q -m b", output) == ()


def test_e48_a_git_subcommand_that_is_not_commit_does_not_poison():
    assert _ids("git log --grep=commit && L=x && git commit -m x") == ("750b62f",)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('export "GIT_DIR=/tmp/other/.git" && SKIP=mypy git commit -m x',
                     id="a quoted export operand"),
        pytest.param("export 'GIT_WORK_TREE=/tmp/o' && git commit -m x",
                     id="a single-quoted export operand in a placed command"),
        pytest.param("L=x && 'GIT_DIR'=/tmp/o git commit -m x", id="a quoted variable name"),
    ],
)
def test_e48_a_quoted_git_variable_assignment_counts_no_id(command):
    assert _ids(command) == ()


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("L=x && git -C /tmp/other commit -m x", id="an absolute -C"),
        pytest.param("L=x && bash -c 'cd /tmp/other && git commit -m x'", id="a cd in a wrapper"),
    ],
)
def test_e48_every_segment_of_an_unplaced_command_has_no_repository(command):
    facts = commands.commit_facts(command, CWD)
    assert facts.placed is False
    assert [seg.repo for seg in facts.segments] == [None]


def test_e48_the_differential_exemption_names_only_the_changed_positions():
    from tests.test_n4fixb_differential import e48_commit_facts_may_differ as may_differ

    assert may_differ("git commit -m promise") is False
    assert may_differ("echo mise && git commit -m x") is False
    assert may_differ("git commit -m GIT_DIR") is False
    assert may_differ("mise exec -- git commit -m x") is True
    assert may_differ("export GIT_DIR=/tmp/o && git commit -m x") is True
    assert may_differ("export 'GIT_DIR=/tmp/o' && git commit -m x") is True
    assert may_differ("bash -c 'mise exec -- git commit -m x'") is True
    assert may_differ("L=x && git commit -m x") is True


# --- review round 2: a substitution can run a commit whatever program it is an argument of ----

FORGED = "[main 1111111] a\n[main deadbee] hook\n"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('git commit -m a && echo "$(git commit -q -m b)"', id="in a double-quoted word"),
        pytest.param("git commit -m a && echo `git commit -q -m b`", id="backticks"),
        pytest.param('git commit -m a && echo x > "$(git commit -q -m b)"', id="in a redirect target"),
        pytest.param("git commit -m a && cat <<EOF\n$(git commit -q -m b)\nEOF",
                     id="in an unquoted heredoc body"),
        pytest.param("git commit -m a && diff <(git commit -q -m b) f", id="a process substitution"),
    ],
)
def test_e48_a_substitution_that_spells_commit_poisons_the_call(command):
    assert _ids(command, FORGED) == ()


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('L=x && echo "$L commit" && git commit -m x', id="an expansion, no substitution"),
        pytest.param("L=x && cat > $L/r <<'EOF'\n$(git commit -q)\nEOF\ngit commit -m x",
                     id="a substitution in a quoted heredoc is data"),
    ],
)
def test_e48_text_that_cannot_run_a_commit_does_not_poison(command):
    assert _ids(command) == ("750b62f",)


# --- review round 3 (each with the reviewer's failing test) ------------------------------------


def test_e48_heredoc_before_pipeline_poisons_call():
    # the heredoc belongs to the first cat; the lexer gives its reason to the second segment
    command = "git commit -m a && cat <<EOF | cat\n$(git commit -q -m b)\nEOF"
    assert _ids(command, FORGED) == ()


def test_e48_single_quoted_substitution_text_is_data():
    command = "L=x && echo '$(git commit -q)' \"$L\" && git commit -m x"
    assert _ids(command) == ("750b62f",)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("L=x && echo \"a\\$(git commit -q)\" \"$L\" && git commit -m x",
                     id="an escaped dollar in double quotes"),
        pytest.param("L=x && echo 'a `git commit -q`' \"$L\" && git commit -m x",
                     id="backticks in single quotes"),
    ],
)
def test_e48_quoted_or_escaped_substitution_text_is_data(command):
    assert _ids(command) == ("750b62f",)


def test_e48_single_quotes_inside_an_unquoted_heredoc_still_run_a_substitution():
    command = "git commit -m a && L=x && cat <<EOF\n'$(git commit -q -m b)'\nEOF"
    assert _ids(command, FORGED) == ()


# --- review round 4 (the reviewer's failing test; bash and zsh both run the substitution) -------


def test_e48_ansi_c_quote_cannot_hide_commit_substitution():
    command = "git commit -m a && echo $'can\\'t'\"$(git commit -q -m b)\""
    assert _ids(command, FORGED) == ()


def test_e48_an_ansi_c_quote_beside_a_commit_spelling_counts_no_id():
    # the lexer does not model $'...' (it splits this word where bash does not), so a segment
    # with one, a reason and the commit spelling cannot be judged: counting no id is the cautious side
    command = "L=x && echo $'tab\\there' \"$L commit\" && git commit -m x"
    assert _ids(command) == ()
