"""N3b P12 and P14's usage error: --local, --raw, --no-redact.

Every command that had --no-redact in v0.2 (scan, run, rules, rules propose,
rules review, apply, trend, rescue plan, rescue status) now takes --local and
--raw too; --no-redact stays as an alias of --raw; the three are one mutually
exclusive group (usage error, exit 1). watch, demo, and every hook subcommand
take no level flag. --local together with --json was a usage error (exit 1)
until JSON schema 3; N5a-1 makes it valid.
"""

from __future__ import annotations

import pytest

from agent_postmortem.cli import _build_parser, main

# argv prefix (before any level flag) for each command that takes a level.
LEVEL_COMMANDS = {
    "scan": ["scan"],
    "run": ["run", "RUN"],
    "rules": ["rules"],
    "rules propose": ["rules", "propose"],
    "rules review": ["rules", "review"],
    "apply": ["apply", "R-RETRY-SAFE", "--target", "T.md"],
    "trend": ["trend"],
    "rescue plan": ["rescue", "plan", "RUN"],
    "rescue status": ["rescue", "status", "RUN"],
}
JSON_COMMANDS = [name for name in LEVEL_COMMANDS if name != "apply"]
NO_LEVEL_COMMANDS = {
    "watch": ["watch", "RUN"],
    "demo": ["demo"],
    "hook": ["hook", "check"],
}
FLAGS = {"--local": "local", "--raw": "raw", "--no-redact": "raw"}
OTHER_MESSAGE = "--local applies to text output until JSON schema 3 (N5)"


@pytest.mark.parametrize("name", sorted(LEVEL_COMMANDS))
def test_default_level_is_share(name):
    args = _build_parser().parse_args(LEVEL_COMMANDS[name])
    assert args.level == "share"


@pytest.mark.parametrize("flag", sorted(FLAGS))
@pytest.mark.parametrize("name", sorted(LEVEL_COMMANDS))
def test_each_flag_sets_its_level(name, flag):
    args = _build_parser().parse_args([*LEVEL_COMMANDS[name], flag])
    assert args.level == FLAGS[flag]


@pytest.mark.parametrize("flag", sorted(FLAGS))
def test_a_flag_repeated_is_not_a_conflict(flag):
    args = _build_parser().parse_args(["trend", flag, flag])
    assert args.level == FLAGS[flag]


@pytest.mark.parametrize(
    "pair",
    [
        ("--local", "--raw"),
        ("--raw", "--local"),
        ("--local", "--no-redact"),
        ("--no-redact", "--local"),
        ("--raw", "--no-redact"),
        ("--no-redact", "--raw"),
    ],
)
@pytest.mark.parametrize("name", sorted(LEVEL_COMMANDS))
def test_two_level_flags_are_a_usage_error(name, pair, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([*LEVEL_COMMANDS[name], *pair])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not allowed with" in captured.err


@pytest.mark.parametrize("name", sorted(JSON_COMMANDS))
def test_local_with_json_parses_from_schema_3(name):
    # N5a-1: --local --json was a usage error until schema 3 (P14); schema-3 JSON
    # names its level in `disclosure`, so the pair is valid.
    args = _build_parser().parse_args([*LEVEL_COMMANDS[name], "--json", "--local"])
    assert args.level == "local"
    assert args.json is True


@pytest.mark.parametrize("flag", ["--raw", "--no-redact"])
@pytest.mark.parametrize("name", sorted(JSON_COMMANDS))
def test_raw_with_json_still_parses(name, flag):
    args = _build_parser().parse_args([*LEVEL_COMMANDS[name], "--json", flag])
    assert args.level == "raw"
    assert args.json is True


@pytest.mark.parametrize("flag", sorted(FLAGS))
@pytest.mark.parametrize("name", sorted(NO_LEVEL_COMMANDS))
def test_watch_demo_and_hooks_take_no_level_flag(name, flag, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([*NO_LEVEL_COMMANDS[name], flag])
    assert excinfo.value.code == 1
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("flag", sorted(FLAGS))
@pytest.mark.parametrize("sub", ["session-start", "session-end", "workflow-launched", "capabilities"])
def test_every_hook_subcommand_takes_no_level_flag(sub, flag, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["hook", sub, flag])
    assert excinfo.value.code == 1
