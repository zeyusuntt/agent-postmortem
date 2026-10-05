"""N3b D-a: the disclosure context and Redactor's use of it.

One frozen object per command: level ("share" | "local" | "raw"), the alias
book (None in Part 1a: no sink renders an alias yet), the home directory,
and the repository root when known. Redactor.enabled stays level != "raw".
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agent_postmortem import cli, disclosure
from agent_postmortem.redact import Redactor


def test_context_fields_and_defaults():
    ctx = disclosure.DisclosureContext(level="share")
    assert ctx.level == "share"
    assert ctx.book is None
    assert ctx.repo_root is None
    assert isinstance(ctx.home, Path)


def test_context_is_frozen():
    ctx = disclosure.DisclosureContext(level="local", home=Path("/tmp/h"), repo_root="/tmp/r")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.level = "raw"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "SHARE", "none", None, 3])
def test_context_rejects_an_unknown_level(bad):
    with pytest.raises(ValueError):
        disclosure.DisclosureContext(level=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "level,enabled", [("share", True), ("local", True), ("raw", False)]
)
def test_redactor_enabled_is_level_not_raw(level, enabled):
    redactor = Redactor(context=disclosure.DisclosureContext(level=level))
    assert redactor.enabled is enabled
    assert redactor.names.enabled is enabled
    assert redactor.context.level == level


def test_redactor_legacy_construction_builds_a_matching_context():
    assert Redactor(enabled=True).context.level == "share"
    assert Redactor(enabled=False).context.level == "raw"
    assert Redactor().context.book is None


def test_redactor_uses_the_context_home():
    ctx = disclosure.DisclosureContext(level="share", home=Path("/h/ctxhome"))
    assert Redactor(context=ctx)("see /h/ctxhome/file") == "see ~/file"


def test_redactor_raw_context_passes_text_through():
    ctx = disclosure.DisclosureContext(level="raw", home=Path("/h/ctxhome"))
    assert Redactor(context=ctx)("see /h/ctxhome/file") == "see /h/ctxhome/file"


@pytest.mark.parametrize(
    "level", ["share", "local", "raw"],
)
def test_cli_builds_the_context_from_the_level(level):
    ctx = cli._build_context(level)
    assert ctx.level == level
    assert ctx.book is None
