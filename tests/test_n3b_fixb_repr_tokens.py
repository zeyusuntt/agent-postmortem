"""N3b fix B, S6: argparse prints a bad value with `%r`, which doubles a
backslash and escapes quotes and control characters, so the raw token no
longer occurs in the message. The renderer maps that spelling too; each token
below goes through an int option and an invalid choice and must never appear
in either spelling at share or local."""

from __future__ import annotations

import pytest

from agent_postmortem.cli import main

TOKENS = [
    "back\\slash ZZSECRET",
    "it's \"x\" ZZSECRET",
    "x\ty ZZSECRET",
    "C:\\Users\\zzclient\\ZZSECRET",
    "tab\tand'quote ZZSECRET",
]


def _err(argv, capsys) -> str:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err


def _assert_no_spelling(err: str, token: str) -> None:
    for spelling in (token, repr(token), repr(token)[1:-1], token.replace("\\", "\\\\")):
        assert spelling not in err, (spelling, err)
    assert "ZZSECRET" not in err
    assert "zzclient" not in err


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize("level", [[], ["--local"]])
def test_an_int_option_never_echoes_the_token_in_any_spelling(token, level, capsys):
    err = _err(["trend", *level, "--weeks", token], capsys)
    assert "invalid int value" in err
    _assert_no_spelling(err, token)


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize("level", [[], ["--local"]])
def test_an_invalid_choice_never_echoes_the_token_in_any_spelling(token, level, capsys):
    err = _err(["scan", *level, "--kind", token], capsys)
    assert "invalid choice" in err
    _assert_no_spelling(err, token)
