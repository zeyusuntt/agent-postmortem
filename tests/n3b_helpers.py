"""Shared helpers for the N3b Part 2 per-sink tests. Not a test_*.py module.

One place for: running the CLI and capturing stdout and stderr, making the
installation secret unavailable (P5: a 0644 secret), the "no absolute path"
assertion used at local, the planted-value lists, and the fixture-case
listing the slow-path counter tests walk.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from agent_postmortem import disclosure
from agent_postmortem.cli import main

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "runs"
CASES = sorted(path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir())
LOCAL_LABEL = "disclosure: local (real names and paths; do not paste)"
RAW_LABEL = "disclosure: raw (real names, absolute paths; do not paste)"
STABLE_AGENT_FILE = re.compile(r"agent-[0-9a-f]{12,}\.jsonl")
POSITIONAL_AGENT_FILE = re.compile(r"agent-[0-9]+\.jsonl")


def cli(capsys, *argv):
    """(exit code, stdout, stderr) of one main() call."""
    capsys.readouterr()
    code = main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def alias_of(kind: str, real: str) -> str:
    """The stable alias the registry holds (or assigns) for one identity."""
    book = disclosure.register([(kind, real)])
    assert book.available
    return book.alias(kind, real)


def make_secret_unavailable(monkeypatch, tmp_path) -> Path:
    """A secret file with mode 0644: P5 says unavailable, never repaired."""
    home = tmp_path / "unavailable-xdg"
    directory = home / "agent-postmortem"
    directory.mkdir(parents=True, mode=0o700)
    secret = directory / "secret"
    secret.write_bytes(b"s" * 32)
    os.chmod(secret, 0o644)
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return secret


def planted_values(planted) -> dict[str, str]:
    return {
        "label": planted.label, "phase": planted.phase, "excerpt": planted.excerpt,
        "agent1": planted.agent1, "agent2": planted.agent2, "session": planted.session,
        "slug": planted.slug, "repo": str(planted.repo), "repo name": planted.repo.name,
        "repo file": str(planted.repo_file),
    }


def assert_nothing_planted(out: str, planted, *, except_for: tuple[str, ...] = ()) -> None:
    for name, value in planted_values(planted).items():
        if name in except_for:
            continue
        assert value not in out, f"{name} leaked: {value}"
    assert "zzplant" not in out or "slug" in except_for
    assert planted.agent1[:12] not in out or "agent1" in except_for


# What doctor tells a reader to type into Claude Code, as the exact words around it, so anything else
# that starts with a slash, a bare /reload-plugins or /plugin with other words among them, is a path.
_SLASH_COMMANDS = re.compile(
    r"(?<!\S)(?:/plugin (?:install|enable) agent-postmortem@agent-postmortem(?!\S)"
    r"|/reload-plugins or restart(?= to get them|\.))")


def assert_no_absolute_path(out: str, planted) -> None:
    """No temporary directory, repository, or repository file, and no token
    that starts with a slash, other than a command to type."""
    tmp = str(planted.repo.parent)
    for fragment in (tmp, tmp[:45], str(planted.repo), str(planted.repo_file)):
        assert fragment not in out, fragment
    for line in out.splitlines():
        for token in _SLASH_COMMANDS.sub("", line).split():
            # a bare "/" is a separator in prose (the NEVER line), not a path
            assert not (token.startswith("/") and len(token) > 1), f"absolute path in {line!r}"
