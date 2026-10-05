"""Fix B, S5 to S10: tests for the six mutations the sonnet review found alive
(.reviews/v0.3/N4-dispositions.md S5 to S10). Each row is proven against the
reviewer's mutation in the Fix B report; none changes behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import commands
from tests.n4b2_helpers import ledger
from tests.test_n4b2_population import at, attempt, generation, population, workflow

CWD = "/tmp/ws"


def row(command, **kwargs):
    return commands.classify_call("Bash", command, cwd=kwargs.pop("cwd", CWD), **kwargs).row


# --- S5: several sed -e scripts are all checked ---------------------------------------


@pytest.mark.parametrize("command", [
    "sed -n -e p -e 'w x' f",
    "sed -e 'w x' -e p f",
    "sed -e p -e 'w x' f",
    "sed -n -e p -e 's/a/b/e' f",
    "sed -e p -e p -e 'w x' f",
    "sed -e 'w x' -e p -e p f",
    "sed -e p -e p -e 'r y' f",
    "sed -ne p -e 'w x' f",
])
def test_one_write_capable_script_among_several_e_scripts_is_opaque(command):
    assert row(command) == 6


@pytest.mark.parametrize("command", [
    "sed -n -e p f",
    "sed -n -e 1p -e 3p f",
    "sed -e 's/a/b/' -e 's/c/d/g' f",
    "sed -n -e p -e d -e q f",
])
def test_every_e_script_in_the_grammar_is_read_only(command):
    assert row(command) == 4


# --- S6: the 1,024 boundary of a grammar target and a sed -i backup ---------------------


def long_path(length, prefix="/tmp/"):
    return prefix + "a" * (length - len(prefix))


@pytest.mark.parametrize("command_for", [
    lambda p: f"echo x > {p}",
    lambda p: f"echo x >> {p}",
    lambda p: f"cat f | tee {p}",
    lambda p: f"touch {p}",
    lambda p: f"sed -i s/a/b/ {p}",
    lambda p: f"cat > {p} <<EOF\nbody\nEOF\n",
])
def test_a_target_of_exactly_1024_characters_is_kept(command_for):
    match = commands.match_write_grammar(commands.lex(command_for(long_path(1024))), CWD)
    (target,) = match.targets
    assert target.path == long_path(1024)
    assert target.truncated is False


@pytest.mark.parametrize("command_for", [
    lambda p: f"echo x > {p}",
    lambda p: f"echo x >> {p}",
    lambda p: f"cat f | tee {p}",
    lambda p: f"touch {p}",
    lambda p: f"sed -i s/a/b/ {p}",
    lambda p: f"cat > {p} <<EOF\nbody\nEOF\n",
])
def test_a_target_of_1025_characters_is_null_and_truncated(command_for):
    match = commands.match_write_grammar(commands.lex(command_for(long_path(1025))), CWD)
    (target,) = match.targets
    assert target.path is None
    assert target.truncated is True


def test_a_relative_target_is_measured_after_joining_the_cwd():
    cwd = "/tmp/" + "d" * 100
    keep = "f" * (1024 - len(cwd) - 1)
    match = commands.match_write_grammar(commands.lex(f"echo x > {keep}"), cwd)
    assert match.targets[0].path == f"{cwd}/{keep}" and not match.targets[0].truncated
    match = commands.match_write_grammar(commands.lex(f"echo x > {keep}f"), cwd)
    assert match.targets[0].path is None and match.targets[0].truncated


def test_a_sed_backup_path_of_exactly_1024_characters_is_kept():
    base = long_path(1024 - len(".bak"))
    match = commands.match_write_grammar(commands.lex(f"sed -i.bak s/a/b/ {base}"), CWD)
    main, backup = match.targets
    assert (main.path, main.truncated) == (base, False)
    assert backup.kind == "sed_backup"
    assert backup.path == base + ".bak" and len(backup.path) == 1024
    assert backup.truncated is False


def test_a_sed_backup_path_of_1025_characters_is_null_and_truncated_but_the_file_is_kept():
    base = long_path(1025 - len(".bak"))
    match = commands.match_write_grammar(commands.lex(f"sed -i.bak s/a/b/ {base}"), CWD)
    main, backup = match.targets
    assert (main.path, main.truncated) == (base, False)
    assert (backup.kind, backup.path, backup.truncated) == ("sed_backup", None, True)


def test_a_sed_file_of_1025_characters_gives_a_null_backup_too():
    base = long_path(1025)
    match = commands.match_write_grammar(commands.lex(f"sed -i.bak s/a/b/ {base}"), CWD)
    main, backup = match.targets
    assert (main.path, main.truncated) == (None, True)
    assert (backup.path, backup.truncated) == (None, True)


# --- S7: a newline (and `;`) ends a backgrounded and-or list --------------------------


def flags(command):
    found = commands._commit_segments(commands.lex(command, commit_message_form=True), CWD, 0)
    return [background for _, background in found]


@pytest.mark.parametrize("command,expected", [
    ("git commit -m a\ngit commit -m b &", [False, True]),
    ("git commit -m a ; git commit -m b &", [False, True]),
    ("git commit -m a &\ngit commit -m b", [True, False]),
    ("git commit -m a & git commit -m b", [True, False]),
    ("git commit -m a && git commit -m b\ngit commit -m c &", [False, False, True]),
    ("git commit -m a\ngit commit -m b && git commit -m c &", [False, True, True]),
    ("git commit -m a\n\ngit commit -m b &\n", [False, True]),
])
def test_a_list_ends_at_a_newline_a_semicolon_or_an_ampersand(command, expected):
    assert flags(command) == expected


@pytest.mark.parametrize("command", [
    "git commit -m a\nsleep 1 &",
    "git commit -m a ; sleep 1 &",
    "sleep 1 &\ngit commit -m a",
    "sleep 1 & git commit -m a",
])
def test_a_backgrounded_neighbor_list_does_not_poison_a_foreground_commit(command):
    assert len(commands.commit_facts(command, CWD).segments) == 1


# --- S8: the long-command classification path normalizes run_in_background ---------------

LONG = "echo " + "a" * 3000


@pytest.mark.parametrize("value", [False, None, 1, "true", "yes", 0, "", [], {"x": 1}])
def test_a_non_true_run_in_background_is_not_background_for_a_long_command(value):
    assert len(LONG) > commands._CACHE_COMMAND_LIMIT
    assert row(LONG, run_in_background=value) == 4
    assert row(LONG) == 4


def test_only_a_literal_true_backgrounds_a_long_command():
    assert row(LONG, run_in_background=True) == 2


@pytest.mark.parametrize("value", [False, None, 1, "true", "yes", 0, "", [], {"x": 1}])
def test_the_uncached_classifier_itself_ignores_a_non_true_value(value):
    for command in ("echo hi", LONG):
        got = commands._classify_call("Bash", command, run_in_background=value, cwd=CWD)
        assert got.row == 4


@pytest.mark.parametrize("value", [1, "true", "yes", [1]])
def test_a_short_and_a_long_command_agree_on_every_value(value):
    short = commands.classify_call("Bash", "echo hi", run_in_background=value, cwd=CWD)
    plain = commands.classify_call("Bash", "echo hi", cwd=CWD)
    assert short == plain


def test_a_backgrounded_hit_backgrounds_a_long_command():
    got = commands.classify_call("Bash", LONG, backgrounded_hit=True, cwd=CWD)
    assert got.row == 2


# --- S9: timestamps that cannot be compared are not at or before ---------------------------

NAIVE = datetime(2026, 9, 1, 12, 0, 0)
AWARE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_an_aware_result_against_a_naive_first_ts_places_nobody():
    gen = generation(1, 1)
    gen = type(gen)(**{**gen.__dict__, "result_ts": AWARE, "launch_ts": AWARE})
    run = workflow([attempt("w", first=10)], [gen])
    run.attempts[0].first_ts = NAIVE + timedelta(seconds=10)
    assert ledger.generation_of(run, run.attempts[0]) is None
    assert population(run) == ["w"]


def test_a_naive_result_against_an_aware_first_ts_places_nobody():
    gen = generation(1, 1)
    gen = type(gen)(**{**gen.__dict__, "result_ts": NAIVE, "launch_ts": NAIVE})
    run = workflow([attempt("w", first=10)], [gen])
    assert ledger.generation_of(run, run.attempts[0]) is None
    assert population(run) == ["w"]


def test_comparable_timestamps_still_place_the_attempt():
    run = workflow([attempt("w", first=10)], [generation(1, 1)])
    assert ledger.generation_of(run, run.attempts[0]).tool_use_id == "toolu_1"
    assert at(1) <= at(10)


def test_a_naive_and_an_aware_generation_pair_makes_nobody_assignable():
    first = generation(1, 1)
    second = generation(2, 2)
    second = type(second)(**{**second.__dict__, "result_ts": NAIVE + timedelta(seconds=2),
                                       "launch_ts": NAIVE + timedelta(seconds=2)})
    first = type(first)(**{**first.__dict__, "result_ts": AWARE + timedelta(seconds=1),
                                     "launch_ts": AWARE + timedelta(seconds=1)})
    run = workflow([attempt("w", first=10)], [first, second])
    assert ledger.generation_of(run, run.attempts[0]) is None


# --- S10: a heredoc terminator, `<<` against `<<-` ---------------------------------------


def body_of(command):
    lexed = commands.lex(command)
    redirect = [r for seg in lexed.segments for r in seg.redirects if r.body is not None]
    return lexed, (redirect[0].body if redirect else None)


def test_a_tab_indented_terminator_does_not_close_a_plain_heredoc():
    lexed, body = body_of("cat <<EOF\n\tEOF\nEOF\n")
    assert lexed.placed and body == "\tEOF\n"


def test_a_tab_indented_terminator_closes_a_dash_heredoc():
    lexed, body = body_of("cat <<-EOF\n\tEOF\n")
    assert lexed.placed and body == ""


def test_a_dash_heredoc_strips_tabs_only_not_spaces():
    lexed, body = body_of("cat <<-EOF\n  EOF\n\t\tEOF\n")
    assert lexed.placed and body == "  EOF\n"


def test_a_plain_heredoc_with_only_an_indented_terminator_is_unclosed():
    lexed, _ = body_of("cat <<EOF\n\tEOF\n")
    assert "unclosed_heredoc" in lexed.reasons and not lexed.placed


def test_a_dash_heredoc_body_lines_are_kept_verbatim():
    lexed, body = body_of("cat <<-EOF\n\tkeep\nEOF\n")
    assert body == "\tkeep\n" and lexed.placed


def test_a_quoted_dash_delimiter_follows_the_same_rule():
    lexed, body = body_of("cat <<-'EOF'\n\tEOF\n")
    assert lexed.placed and body == ""
    lexed, body = body_of("cat <<'EOF'\n\tEOF\nEOF\n")
    assert lexed.placed and body == "\tEOF\n"


def test_the_commit_message_literal_strips_tabs_only_for_a_dash_heredoc():
    placed = 'git commit -m "$(cat <<-\'EOF\'\nmsg\n\tEOF\n)"'
    assert len(commands.commit_facts(placed, CWD).segments) == 1
    plain = 'git commit -m "$(cat <<\'EOF\'\nmsg\n\tEOF\n)"'
    assert commands.commit_facts(plain, CWD).segments == ()


def test_the_commit_message_literal_keeps_a_tab_line_as_body_under_a_plain_heredoc():
    command = 'git commit -m "$(cat <<\'EOF\'\nmsg\n\tEOF\nEOF\n)"'
    assert len(commands.commit_facts(command, CWD).segments) == 1
    lexed = commands.lex(command, commit_message_form=True)
    assert lexed.segments[0].words[-1].text == "msg\n\tEOF\n"
