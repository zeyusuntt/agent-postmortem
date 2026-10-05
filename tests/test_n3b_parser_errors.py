"""N3b P13: parser errors never echo a raw argument at share or local.

Before argparse runs, main scans argv (tokens before a bare `--`) for exactly
--local, --raw, --no-redact to fix the level of an error message; two of
them, or a malformed spelling (--raw=1, --loc), fix it at share. A command
that takes no level flag (watch, demo, hook) always renders its errors at
share. Every original argv token that is not exactly a known option string,
subcommand name, or choice is classified and replaced in the message as a
span (a path by path-N, a UUID by uuid:<last4>, a canonical agent id by
agent-N, anything else by value-N); at raw the message prints as argparse
wrote it. Handler-level errors (no run found, ambiguous run, bad date) are
Part 2.
"""

from __future__ import annotations

import pytest

from agent_postmortem import argerrors
from agent_postmortem.cli import main

AGENT_ID = "ac900000000000001"
FAKE_UUID = "00000000-0000-4000-8000-000000000000"


def _err(argv, capsys) -> str:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err


def _message_line(err: str) -> str:
    lines = [line for line in err.splitlines() if ": error: " in line]
    assert len(lines) == 1, err
    return lines[0]


# --- the early level scan ------------------------------------------------------


@pytest.mark.parametrize(
    "tokens,level",
    [
        ([], "share"),
        (["scan"], "share"),
        (["scan", "--raw"], "raw"),
        (["scan", "--no-redact"], "raw"),
        (["scan", "--local"], "local"),
        (["--raw", "scan"], "raw"),
        (["scan", "--local", "--raw"], "share"),
        (["scan", "--raw", "--no-redact"], "share"),
        (["scan", "--raw", "--raw"], "share"),
        (["scan", "--raw=1"], "share"),
        (["scan", "--loc"], "share"),
        (["scan", "--no-red"], "share"),
        (["scan", "--local=yes", "--raw"], "share"),
        (["scan", "--", "--raw"], "share"),
        (["scan", "--raw", "--", "--local"], "raw"),
        (["watch", "RUN", "--raw"], "share"),
        (["demo", "--raw"], "share"),
        (["hook", "check", "--raw"], "share"),
        (["rules", "propose", "--local"], "local"),
        (["rescue", "plan", "RUN", "--raw"], "raw"),
        (["apply", "R-X", "--target", "T", "--no-redact"], "raw"),
    ],
)
def test_early_level_scan(tokens, level):
    assert argerrors.scan_level(tokens) == level


# --- share and local never echo a value ------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--local"]], ids=["share", "local"])
@pytest.mark.parametrize(
    "argv,secret,replacement,kind_words",
    [
        (["trend", "--weeks", "abc"], "abc", "value-1", "invalid int value"),
        (["trend", "--weeks", "my secret value"], "my secret value", "value-1", "invalid int value"),
        (["trend", "--weeks", AGENT_ID], AGENT_ID, "agent-1", "invalid int value"),
        (["trend", "--weeks", FAKE_UUID], FAKE_UUID, "uuid:0000", "invalid int value"),
        (["trend", "--weeks", "/private/client/repo"], "/private/client/repo", "path-1",
         "invalid int value"),
        (["trend", "--weeks", "~/client/repo"], "~/client/repo", "path-1", "invalid int value"),
        (["trend", "--weeks", "./client/repo"], "./client/repo", "path-1", "invalid int value"),
        (["trend", "--weeks=client-secret"], "client-secret", "value-1", "invalid int value"),
        (["scan", "/private/client/repo"], "/private/client/repo", "path-1",
         "unrecognized arguments"),
        (["scan", AGENT_ID], AGENT_ID, "agent-1", "unrecognized arguments"),
        (["scan", "--/private/client/repo"], "--/private/client/repo", "path-1",
         "unrecognized arguments"),
        (["scan", "--client-secret-flag"], "--client-secret-flag", "value-1",
         "unrecognized arguments"),
        (["scan", "--json=/private/client/x"], "/private/client/x", "path-1",
         "ignored explicit argument"),
        (["scan", "--kind", "client-secret"], "client-secret", "value-1", "invalid choice"),
        (["rules", "review", "--weeks", "/private/client/x"], "/private/client/x", "path-1",
         "invalid int value"),
        (["rules", "propose", "client-secret"], "client-secret", "value-1",
         "unrecognized arguments"),
        (["rescue", "plan", "RUN", "--bogus", "/private/client/y"], "/private/client/y", "path-1",
         "unrecognized arguments"),
        (["rescue", "bogus-sub", "/private/client/z"], "bogus-sub", "value-1", "invalid choice"),
        (["hook", "client-secret-hook"], "client-secret-hook", "value-1", "invalid choice"),
        (["apply", "R-X", "--target", "T", "--target-typo", "/private/client/t"],
         "/private/client/t", "path-1", "unrecognized arguments"),
    ],
)
def test_share_and_local_errors_never_echo_the_raw_value(
    argv, secret, replacement, kind_words, flags, capsys,
):
    err = _err([*argv, *flags], capsys)
    assert secret not in err
    message = _message_line(err)
    assert replacement in message
    assert kind_words in message  # the kind of argument expected survives
    assert err.startswith("usage:")


def test_two_values_are_numbered_in_order_of_appearance(capsys):
    err = _err(["scan", "/private/a/x", "client-one", "/private/b/y", "client-two"], capsys)
    message = _message_line(err)
    assert message.endswith("unrecognized arguments: path-1 value-1 path-2 value-2")


def test_a_repeated_value_gets_one_name(capsys):
    err = _err(["scan", "client-one", "client-one"], capsys)
    assert _message_line(err).endswith("unrecognized arguments: value-1 value-1")


def test_known_option_strings_and_subcommand_names_are_not_replaced(capsys):
    err = _err(["scan", "--weeks", "3"], capsys)
    message = _message_line(err)
    assert message.endswith("unrecognized arguments: --weeks value-1")
    assert message.startswith("postmortem: error:")


def test_a_short_value_does_not_garble_the_message(capsys):
    # A one-letter value must replace only itself, never a letter inside a word.
    err = _err(["trend", "--weeks", "a"], capsys)
    message = _message_line(err)
    assert "invalid int value: 'value-1'" in message


def test_conflicting_level_flags_fix_share_for_the_error(capsys):
    err = _err(["scan", "/private/client/repo", "--local", "--raw"], capsys)
    assert "/private/client/repo" not in err
    assert "not allowed with" in err


def test_a_malformed_level_spelling_fixes_share_for_the_error(capsys):
    err = _err(["scan", "--raw=1", "/private/client/repo"], capsys)
    assert "/private/client/repo" not in err
    assert "ignored explicit argument 'value-1'" in err


# --- raw prints argparse's message exactly ------------------------------------------


@pytest.mark.parametrize("flag", ["--raw", "--no-redact"])
def test_raw_errors_print_exactly_as_argparse_wrote_them(flag, capsys):
    err = _err(["trend", flag, "--weeks", "/private/client/repo"], capsys)
    assert (
        "postmortem trend: error: argument --weeks: invalid int value: '/private/client/repo'"
        in err
    )


def test_raw_unrecognized_arguments_are_verbatim(capsys):
    err = _err(["scan", "--raw", "/private/client/repo", "client-secret"], capsys)
    assert "unrecognized arguments: /private/client/repo client-secret" in err


# --- a command with no level flag always renders at share ---------------------------


def test_watch_errors_are_always_share_even_with_a_raw_token(capsys):
    err = _err(["watch", "RUN", "--raw", "--bogus-path", "/private/client/x"], capsys)
    assert "/private/client/x" not in err
    assert "path-1" in err
    assert "--raw" in err  # a known option string of the tool is not a value


@pytest.mark.parametrize(
    "argv",
    [
        ["watch", "/private/client/run", "--interval", "0"],
        ["watch", "/private/client/run", "--interval", "nope"],
        ["watch", "/private/client/run", "--max-hours", "999999"],
        ["watch", "/private/client/run", "--quiet-after", "1"],
        ["watch", "/private/client/run", "--done-grace", "-5"],
        ["watch", "/private/client/run", "--interval=/private/client/n"],
    ],
)
def test_malformed_watch_numbers_never_echo_a_value(argv, capsys):
    err = _err(argv, capsys)
    assert "/private/client" not in err
    assert "usage:" in err
    message = _message_line(err)
    assert "--" in message  # the flag that was malformed is still named


def test_demo_errors_are_always_share(capsys):
    err = _err(["demo", "/private/client/x", "--raw"], capsys)
    assert "/private/client/x" not in err


# --- S2: an abbreviated --flag=VALUE never echoes VALUE ------------------------


@pytest.mark.parametrize(
    "argv,secret",
    [
        (["run", "--verb=/private/zzclient/secretrepo", "x"], "/private/zzclient/secretrepo"),
        (["run", "--ev=a7e57ab1e00000001", "x"], "a7e57ab1e00000001"),
        (["scan", "--al=/private/zzclient/secretrepo"], "/private/zzclient/secretrepo"),
        (["run", "--l=" + FAKE_UUID, "x"], FAKE_UUID),
    ],
)
@pytest.mark.parametrize("level_flag", [[], ["--local"]])
def test_s2_abbreviated_flag_value_is_never_echoed(argv, secret, level_flag, capsys):
    err = _err(argv[:1] + level_flag + argv[1:], capsys)
    assert secret not in err
