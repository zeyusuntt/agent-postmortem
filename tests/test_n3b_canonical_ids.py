"""N3b Part 1b item 0: no canonical id can carry a trailing newline.

N3a's agent regex ended in `$`, which matches before a trailing newline, so
is_canonical_id("agent", "<17 hex>\\n") was True. Every kind is checked.
"""

from __future__ import annotations

import uuid as uuid_module

import pytest

from agent_postmortem import disclosure


def _agent(n: int = 1) -> str:
    return format(n, "017x")


def _session(n: int = 1) -> str:
    return str(uuid_module.UUID(int=n))


@pytest.mark.parametrize(
    "kind,value",
    [("agent", _agent()), ("session", _session()), ("project", "-home-user-project")],
)
def test_a_canonical_id_is_still_canonical(kind, value):
    assert disclosure.is_canonical_id(kind, value) is True


@pytest.mark.parametrize("suffix", ["\r\n", "\n\n"])
@pytest.mark.parametrize(
    "kind,value",
    [("agent", _agent()), ("session", _session()), ("project", "-home-user-project")],
)
def test_no_canonical_id_may_carry_trailing_newlines(kind, value, suffix):
    assert disclosure.is_canonical_id(kind, value + suffix) is False


@pytest.mark.parametrize(
    "kind,value",
    [
        ("agent", _agent()),
        ("session", _session()),
        ("project", "-home-user-project"),
    ],
)
def test_no_canonical_id_may_carry_one_trailing_newline(kind, value):
    assert disclosure.is_canonical_id(kind, value + "\n") is False


@pytest.mark.parametrize("suffix", [" ", "\t"])
@pytest.mark.parametrize("kind,value", [("agent", _agent()), ("session", _session())])
def test_agent_and_session_ids_carry_no_trailing_whitespace(kind, value, suffix):
    assert disclosure.is_canonical_id(kind, value + suffix) is False


def test_require_canonical_id_rejects_a_trailing_newline():
    with pytest.raises(ValueError):
        disclosure.require_canonical_id("agent", _agent() + "\n")
