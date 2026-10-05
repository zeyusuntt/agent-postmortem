"""Fix B, MINOR lexer fixes (.reviews/v0.3/N4-dispositions.md): `&>` after a glued
word keeps that word, and `>> /dev/null` is treated like `> /dev/null`.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands

CWD = "/tmp/ws"


def redirects(command):
    seg = commands.lex(command).segments[0]
    return [w.text for w in seg.words], [
        (r.fd, r.op, r.target, r.glued_before) for r in seg.redirects
    ]


@pytest.mark.parametrize("command,words,redirect", [
    ("echo foo&>out", ["echo", "foo"], (None, "&>", "out", True)),
    ("echo foo&>>out", ["echo", "foo"], (None, "&>>", "out", True)),
    ("echo a b&>out", ["echo", "a", "b"], (None, "&>", "out", True)),
    ("echo 'a b'&>out", ["echo", "a b"], (None, "&>", "out", True)),
])
def test_an_ampersand_redirect_glued_to_a_word_keeps_the_word(command, words, redirect):
    got_words, got = redirects(command)
    assert got_words == words
    assert got == [redirect]


@pytest.mark.parametrize("command,words,redirect", [
    ("echo foo &>out", ["echo", "foo"], (None, "&>", "out", False)),
    ("echo foo &>>out", ["echo", "foo"], (None, "&>>", "out", False)),
    ("echo &>out", ["echo"], (None, "&>", "out", False)),
    ("echo foo &> out", ["echo", "foo"], (None, "&>", "out", False)),
])
def test_an_ampersand_redirect_after_a_space_is_unchanged(command, words, redirect):
    got_words, got = redirects(command)
    assert got_words == words
    assert got == [redirect]


def test_a_glued_ampersand_redirect_is_not_read_only_or_in_the_write_grammar():
    row = commands.classify_call("Bash", "echo foo&>/dev/null", cwd=CWD)
    assert row.row == 6
    assert commands.match_write_grammar(commands.lex("echo foo&>out"), CWD) is None


def test_an_unglued_ampersand_redirect_to_dev_null_stays_read_only():
    assert commands.classify_call("Bash", "echo foo &>/dev/null", cwd=CWD).row == 4


@pytest.mark.parametrize("command", [
    "cat f >> /dev/null",
    "cat f >>/dev/null",
    "cat f 1>> /dev/null",
    "cat f 2>>/dev/null",
    "cat f 2>> /dev/null",
    "cat f >>/dev/null 2>&1",
    "cat f 2>&1 >>/dev/null",
    "echo x >> /dev/null",
    "cd /tmp && ls >> /dev/null",
])
def test_an_append_to_dev_null_is_read_only_like_a_truncating_redirect(command):
    got = commands.classify_call("Bash", command, cwd=CWD)
    assert (got.row, got.effect, got.targets) == (4, None, ())


@pytest.mark.parametrize("command", [
    "cat f > /dev/null",
    "cat f >/dev/null",
    "echo x > /dev/null",
])
def test_a_truncating_redirect_to_dev_null_is_read_only(command):
    got = commands.classify_call("Bash", command, cwd=CWD)
    assert (got.row, got.effect, got.targets) == (4, None, ())


@pytest.mark.parametrize("command", [
    "cat f >> /dev/null2",
    "cat f >> /dev/nulll",
    "cat f >>/dev/null/x",
    "cat f 3>>/dev/null",
    "cat f &>> /dev/null",
    "cat f &>>/dev/null",
    "cat f >> out",
    "cat f>> /dev/null",
    "cat f >> /dev/null >> out",
])
def test_an_append_elsewhere_is_not_read_only(command):
    got = commands.classify_call("Bash", command, cwd=CWD)
    assert got.row != 4


def test_an_append_to_a_real_file_still_claims_its_target():
    got = commands.classify_call("Bash", "echo x >> out", cwd=CWD)
    assert (got.row, got.effect) == (5, "claimed_target")
    assert [t.path for t in got.targets] == ["/tmp/ws/out"]
