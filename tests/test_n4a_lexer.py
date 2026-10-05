"""N4a coverage group 1: the lexer (docs/DESIGN.md 4.13.1 Tokenization, C-7).

Every row is a synthetic command text. A row names the exact command, the
segments and separators the lexer must produce (or the opaque reason it must
give), so a removed rule shows up as one failing row.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands


def shape(command, **kwargs):
    """([[word texts] per segment], [separator after each segment but the last])."""
    lexed = commands.lex(command, **kwargs)
    words = [[w.text for w in seg.words] for seg in lexed.segments]
    seps = [seg.sep_after for seg in lexed.segments[:-1]]
    return words, seps


# --- splitting ---------------------------------------------------------------

SPLIT_ROWS = [
    ("a && b", [["a"], ["b"]], ["&&"]),
    ("a&&b", [["a"], ["b"]], ["&&"]),
    ("a || b", [["a"], ["b"]], ["||"]),
    ("a||b", [["a"], ["b"]], ["||"]),
    ("a ; b", [["a"], ["b"]], [";"]),
    ("a;b", [["a"], ["b"]], [";"]),
    ("a | b", [["a"], ["b"]], ["|"]),
    ("a|b", [["a"], ["b"]], ["|"]),
    ("a |& b", [["a"], ["b"]], ["|&"]),
    ("a|&b", [["a"], ["b"]], ["|&"]),
    ("a & b", [["a"], ["b"]], ["&"]),
    ("a&b", [["a"], ["b"]], ["&"]),
    ("a\nb", [["a"], ["b"]], ["\n"]),
    ("a\n\n\nb", [["a"], ["b"]], ["\n"]),
    ("\na\nb\n", [["a"], ["b"]], ["\n"]),
    ("a &&\nb", [["a"], ["b"]], ["&&"]),
    ("a ||\nb", [["a"], ["b"]], ["||"]),
    ("a |\nb", [["a"], ["b"]], ["|"]),
    ("echo a \\\n b", [["echo", "a", "b"]], []),
    ("a && b | c; d", [["a"], ["b"], ["c"], ["d"]], ["&&", "|", ";"]),
    ("echo 'a && b'", [["echo", "a && b"]], []),
    ('echo "a;b"', [["echo", "a;b"]], []),
    ("echo a\\;b", [["echo", "a;b"]], []),
    ("echo 'a\nb'", [["echo", "a\nb"]], []),
    ('echo "a | b" \'c || d\'', [["echo", "a | b", "c || d"]], []),
]


@pytest.mark.parametrize("command,words,seps", SPLIT_ROWS)
def test_split_rows(command, words, seps):
    lexed = commands.lex(command)
    assert lexed.placed, lexed.reasons
    assert shape(command) == (words, seps)


TRAILING_ROWS = [
    ("make &", "&"),
    ("make &\n", "&"),
    ("make & ", "&"),
    ("a & b &", "&"),
    ("make;", ";"),
    ("make ;\n", ";"),
    ("printf x & wait", None),
    ("make", None),
    ("make\n", None),
    ("echo 'a &'", None),
    ("echo a\\&", None),
]


@pytest.mark.parametrize("command,trailing", TRAILING_ROWS)
def test_trailing_operator(command, trailing):
    lexed = commands.lex(command)
    assert lexed.placed, lexed.reasons
    assert lexed.trailing_op == trailing


# --- words and quote state ------------------------------------------------------

WORD_ROWS = [
    ('echo "a b" \'c d\' e\\ f', [["echo", "a b", "c d", "e f"]]),
    ('echo a"b"\'c\'d', [["echo", "abcd"]]),
    ("echo ''", [["echo", ""]]),
    ('echo ""', [["echo", ""]]),
    ('echo "a\\"b"', [["echo", 'a"b']]),
    ('echo "a\\\\b"', [["echo", "a\\b"]]),
    ("echo 'a\\b'", [["echo", "a\\b"]]),
    ('echo "*" \'?\' "[x]" "~"', [["echo", "*", "?", "[x]", "~"]]),
    ("echo {a}", [["echo", "{a}"]]),
    ("echo a~", [["echo", "a~"]]),
    ("echo a#b", [["echo", "a#b"]]),
    ("echo a # b && c", [["echo", "a"]]),
    ("# $(rm -rf /)", []),
    ("echo a\n# git commit\nls", [["echo", "a"], ["ls"]]),
    ("echo x 2>&1", [["echo", "x"]]),
]


@pytest.mark.parametrize("command,words", WORD_ROWS)
def test_word_rows(command, words):
    lexed = commands.lex(command)
    assert lexed.placed, lexed.reasons
    assert [[w.text for w in seg.words] for seg in lexed.segments] == words


def test_empty_quoted_word_is_marked_quoted():
    seg = commands.lex("echo '' x").segments[0]
    assert [(w.text, w.quoted) for w in seg.words] == [("echo", False), ("", True), ("x", False)]


def test_raw_text_is_kept_for_assignment_detection():
    word = commands.lex("echo 'a'b").segments[0].words[1]
    assert (word.text, word.raw, word.quoted) == ("ab", "'a'b", True)


# --- opaque triggers ------------------------------------------------------------

OPAQUE_ROWS = [
    # expansions outside single quotes
    ("echo $HOME", "expansion"),
    ('echo "$HOME"', "expansion"),
    ("echo ${HOME}", "expansion"),
    ("echo $(ls)", "expansion"),
    ('echo "$(ls)"', "expansion"),
    ("echo $((1+1))", "expansion"),
    ("echo \\$HOME", "expansion"),
    ('echo "\\$HOME"', "expansion"),
    ("echo \\`ls\\`", "command_substitution"),
    ('echo "\\`ls\\`"', "command_substitution"),
    ("echo $'a'", "expansion"),
    ("echo `ls`", "command_substitution"),
    ('echo "`ls`"', "command_substitution"),
    # globs, braces, tilde
    ("echo *", "glob"),
    ("echo ?", "glob"),
    ("echo [a]", "glob"),
    ("echo a*b", "glob"),
    ("echo \\*", "glob"),
    ("echo {a,b}", "brace"),
    ("echo {1..3}", "brace"),
    ("echo x{a,b}y", "brace"),
    ("echo ~", "tilde"),
    ("echo ~/x", "tilde"),
    # substitutions and here-strings
    ("cat <(ls)", "process_substitution"),
    ("echo x >(cat)", "process_substitution"),
    ("cat <<< x", "here_string"),
    # descriptor redirects other than 2>&1
    ("echo x >&2", "fd_redirect"),
    ("echo x 1>&2", "fd_redirect"),
    ("echo x 3>&1", "fd_redirect"),
    ("cat <&0", "fd_redirect"),
    ("echo x >&-", "fd_redirect"),
    # parentheses and extglob openers
    ("(ls)", "paren"),
    ("echo (a)", "paren"),
    ("f() { ls; }", "paren"),
    ("uniq +(input)", "extglob"),
    ("cat @(a|b)", "extglob"),
    ("ls !(x)", "extglob"),
    ("ls ?(a)", "extglob"),
    ("ls *(a)", "extglob"),
    # structure
    ("echo 'a", "unbalanced_quote"),
    ('echo "a', "unbalanced_quote"),
    ("cat <<EOF\nabc", "unclosed_heredoc"),
    ("cat <<EOF", "unclosed_heredoc"),
    ("echo a ;; echo b", "double_semicolon"),
    ("echo x >", "missing_redirect_target"),
    ("echo > ; ls", "missing_redirect_target"),
    ("echo > > f", "missing_redirect_target"),
    ("echo x 2>", "missing_redirect_target"),
    ("echo x <<", "missing_redirect_target"),
    ("echo a &&", "dangling_operator"),
    ("echo a ||", "dangling_operator"),
    ("echo a |", "dangling_operator"),
    ("&& echo a", "empty_segment"),
    ("echo a && && echo b", "empty_segment"),
    ("echo a ; ; echo b", "empty_segment"),
    # first words
    ("eval ls", "first_word"),
    ("exec ls", "first_word"),
    ("source x", "first_word"),
    (". x", "first_word"),
    ("command ls", "first_word"),
    ("builtin ls", "first_word"),
    ("time ls", "first_word"),
    ("xargs ls", "first_word"),
    ("env ls", "first_word"),
    ("nohup ls", "first_word"),
    ("alias a=b", "first_word"),
    ("function f { :; }", "first_word"),
    ("{ ls; }", "first_word"),
    ("! ls", "first_word"),
    ("A=1 ls", "first_word"),
    ("FOO=bar", "first_word"),
    ("PATH=./bin cat f", "first_word"),
    ("cat f && eval ls", "first_word"),
    # unquoted heredoc whose body the shell would expand
    ("cat <<EOF\n$HOME\nEOF", "heredoc_expansion"),
    ("cat <<EOF\n`ls`\nEOF", "heredoc_expansion"),
]


@pytest.mark.parametrize("command,reason", OPAQUE_ROWS)
def test_opaque_rows(command, reason):
    lexed = commands.lex(command)
    assert reason in lexed.reasons, lexed.reasons
    assert not lexed.placed


PLACED_ROWS = [
    "echo '$HOME'",
    "echo '$(ls)' '`ls`' '*' '?' '[a]' '{a,b}' '~' '(' ')'",
    "echo \"a b\" 'c'",
    "echo x 2>&1",
    "echo x 2>/dev/null",
    "echo x &>/dev/null",
    "ls -la",
    "cat 'A=1'",
    "echo a=b",
    "echo eval exec nohup",
    "echo a\\(b\\)",
    "find . \\( -name x \\)",
    "echo {a",
    "echo }",
]


@pytest.mark.parametrize("command", PLACED_ROWS)
def test_placed_rows(command):
    lexed = commands.lex(command)
    assert lexed.placed, lexed.reasons


def test_every_opaque_trigger_is_independent_of_neighbours():
    # an opaque trigger in one segment makes the whole command opaque, and the
    # other segments are still lexed (the background and delegation rows need
    # the words of an opaque command).
    lexed = commands.lex("echo $X && codex exec y")
    assert not lexed.placed
    assert [[w.text for w in seg.words] for seg in lexed.segments][1] == ["codex", "exec", "y"]


# --- heredocs ---------------------------------------------------------------------

HEREDOC_ROWS = [
    ("cat <<EOF\nrm -rf /\ngit commit -m x\nEOF", "EOF", "rm -rf /\ngit commit -m x\n"),
    ("cat <<'EOF'\n$HOME `ls` $(rm x)\nEOF", "EOF", "$HOME `ls` $(rm x)\n"),
    ('cat <<"EOF"\n$HOME\nEOF', "EOF", "$HOME\n"),
    ("cat <<-EOF\n\tbody\n\tEOF", "EOF", "\tbody\n"),
    ("cat << EOF\nbody\nEOF", "EOF", "body\n"),
    ("cat <<EOF\nbody\nEOF\n", "EOF", "body\n"),
    ("cat <<EOF\n\nEOF", "EOF", "\n"),
]


@pytest.mark.parametrize("command,delim,body", HEREDOC_ROWS)
def test_heredoc_body_is_data(command, delim, body):
    lexed = commands.lex(command)
    assert lexed.placed, lexed.reasons
    assert len(lexed.segments) == 1
    seg = lexed.segments[0]
    assert [w.text for w in seg.words] == ["cat"]
    (redirect,) = seg.redirects
    assert redirect.op in ("<<", "<<-")
    assert redirect.target == delim
    assert redirect.body == body


def test_heredoc_delimiter_quoting_is_recorded():
    quoted = commands.lex("cat <<'EOF'\nx\nEOF").segments[0].redirects[0]
    plain = commands.lex("cat <<EOF\nx\nEOF").segments[0].redirects[0]
    assert quoted.target_quoted is True
    assert plain.target_quoted is False


def test_commands_after_the_delimiter_line_are_lexed():
    assert shape("cat <<EOF\nrm -rf /\nEOF\nls -a") == ([["cat"], ["ls", "-a"]], ["\n"])
    assert shape("cat <<EOF | wc -l\nx\nEOF") == ([["cat"], ["wc", "-l"]], ["|"])


def test_the_delimiter_must_be_a_whole_line():
    # 'EOF x' and ' EOF' are not the terminator for <<EOF, so this never closes
    lexed = commands.lex("cat <<EOF\nEOF x\n EOF")
    assert "unclosed_heredoc" in lexed.reasons


# --- redirects ------------------------------------------------------------------

def test_redirect_structure():
    seg = commands.lex("echo hi > f").segments[0]
    (r,) = seg.redirects
    assert (r.fd, r.op, r.target, r.glued_before, r.after_words) == (None, ">", "f", False, 2)
    seg = commands.lex("echo hi>f").segments[0]
    assert [w.text for w in seg.words] == ["echo", "hi"]
    assert seg.redirects[0].glued_before is True
    seg = commands.lex("echo hi 2>/dev/null").segments[0]
    (r,) = seg.redirects
    assert (r.fd, r.op, r.target, r.glued_before) == ("2", ">", "/dev/null", False)
    seg = commands.lex("echo hi >> f 2>&1").segments[0]
    assert [(r.fd, r.op, r.target) for r in seg.redirects] == [(None, ">>", "f"), ("2", ">&", "1")]
    seg = commands.lex("echo hi &>/dev/null").segments[0]
    assert [(r.fd, r.op, r.target) for r in seg.redirects] == [(None, "&>", "/dev/null")]
    seg = commands.lex("echo a2>f").segments[0]
    assert [w.text for w in seg.words] == ["echo", "a2"]
    assert seg.redirects[0].fd is None
    seg = commands.lex("sort < in").segments[0]
    assert [(r.fd, r.op, r.target) for r in seg.redirects] == [(None, "<", "in")]


def test_redirect_after_words_counts_words_before_the_operator():
    seg = commands.lex("> f echo x").segments[0]
    assert [w.text for w in seg.words] == ["echo", "x"]
    assert seg.redirects[0].after_words == 0
    seg = commands.lex("echo > f x").segments[0]
    assert [w.text for w in seg.words] == ["echo", "x"]
    assert seg.redirects[0].after_words == 1


# --- the literal commit-message form (L3) --------------------------------------

MESSAGE_FORM = 'git commit -m "$(cat <<\'EOF\'\nsubject line\n\nbody line\nEOF\n)"'


def test_literal_commit_message_is_one_word_in_commit_mode():
    lexed = commands.lex(MESSAGE_FORM, commit_message_form=True)
    assert lexed.placed, lexed.reasons
    words = [w.text for w in lexed.segments[0].words]
    assert words[:3] == ["git", "commit", "-m"]
    assert words[3].startswith("subject line") and "body line" in words[3]
    assert len(words) == 4


def test_the_same_text_is_opaque_everywhere_else():
    lexed = commands.lex(MESSAGE_FORM)
    assert not lexed.placed
    assert "expansion" in lexed.reasons


MESSAGE_NEGATIVES = [
    # delimiter not quoted
    'git commit -m "$(cat <<EOF\nmsg\nEOF\n)"',
    # something other than bare cat
    'git commit -m "$(cat f <<\'EOF\'\nmsg\nEOF\n)"',
    'git commit -m "$(echo <<\'EOF\'\nmsg\nEOF\n)"',
    'git commit -m "$(cat <<\'EOF\' && rm x\nmsg\nEOF\n)"',
    # no terminator
    'git commit -m "$(cat <<\'EOF\'\nmsg\n)"',
    # anything after the terminator but the closing parenthesis
    'git commit -m "$(cat <<\'EOF\'\nmsg\nEOF\n; rm x)"',
    # a plain substitution
    'git commit -m "$(echo hi)"',
    # unquoted outside double quotes
    "git commit -m $(cat <<'EOF'\nmsg\nEOF\n)",
]


@pytest.mark.parametrize("command", MESSAGE_NEGATIVES)
def test_message_form_negatives_stay_opaque_in_commit_mode(command):
    lexed = commands.lex(command, commit_message_form=True)
    assert not lexed.placed
